"""Resume paused Claude lanes after the user confirmed subscription quota reset."""

import argparse
import importlib.util
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "resume_helpers", ROOT / "scripts/restart-sol-argon.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
r = helper.r
RUNS = ROOT / "experiments/claude-quota-restart-v1"
MANIFEST = ROOT / "configs/claude-quota-restart-v1.json"
MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


def is_quota(receipt):
    return receipt.get("status") != "completed" and any(
        item.get("api_error") == "usage_limit_reached"
        and item.get("api_error_status") == 429
        for item in receipt.get("results", [])
    )


def prepare():
    prior = r.read(r.MANIFEST)
    for a in prior["sources"]:
        if r.artifact(ROOT / a["path"]) != a:
            raise ValueError("Prior source drift")
    if r.binary_identity() != r.read(r.full.MANIFEST)["cli"]:
        raise ValueError("Claude CLI changed since pinned campaign")
    if MANIFEST.exists():
        f = r.read(MANIFEST)
        for a in f["sources"]:
            if r.artifact(ROOT / a["path"]) != a:
                raise ValueError("Restart source drift")
        return f
    models = [m for m in prior["models"] if m["model"] in MODELS]
    _, events = r.Ledger().snapshot()
    ids = {m["cid"] for m in models}
    quota_failures = []
    for e in events:
        if (
            e["config"] in ids
            and e["kind"] == "call"
            and e["body"]["status"] != "completed"
        ):
            path = ROOT / e["body"]["artifact"]["path"]
            if r.artifact(path) != e["body"]["artifact"]:
                raise ValueError("Prior failed receipt drift")
            receipt = r.read(path)
            if is_quota(receipt):
                quota_failures.append(
                    {
                        "id": e["id"],
                        "config": e["config"],
                        "trial": e["trial"],
                        "artifact": r.artifact(path),
                        "finished_at": receipt["finished_at"],
                    }
                )
    f = {
        "created": r.now(),
        "models": models,
        "prior_manifest": r.artifact(r.MANIFEST),
        "authorization": "User confirms Claude quota refreshed and requests restarting those jobs",
        "prior_quota_failures_authorized_for_retry": quota_failures,
        "settings_changed": False,
        "retry_policy": "Same bounded retry policy; only the listed expired quota failures may be retried. A new quota failure pauses the lane. Reuse completed trials; preserve every attempt.",
        "sources": [
            r.artifact(Path(__file__)),
            r.artifact(ROOT / "scripts/restart-sol-argon.py"),
        ],
    }
    r.immutable_json(MANIFEST, f)
    for a in f["sources"]:
        p = RUNS / "executed-source" / a["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((ROOT / a["path"]).read_bytes())
    return f


def main():
    f = prepare()
    for m in f["models"]:
        lane = r.read(helper.PRIOR / "lanes" / (m["model"] + ".json"))
        if lane["state"] != "paused":
            raise RuntimeError("Old selected lane is not paused")
    _, events = r.Ledger().snapshot()
    selected = {m["cid"] for m in f["models"]}
    recorded = {e["id"].rsplit("/", 1)[0] for e in events if e["kind"] == "call"}
    if any(
        e["kind"] == "reserved"
        and e["config"] in selected
        and e["id"].rsplit("/", 1)[0] not in recorded
        for e in events
    ):
        raise RuntimeError("Unreceipted reservation requires reconciliation")
    calls = defaultdict(list)
    for e in events:
        if e["kind"] == "call" and e["config"] in selected:
            calls[(e["config"], e["trial"], e["body"]["role"])].append(e)
    done = {
        (e["config"], e["body"]["condition"], e["body"]["sample"])
        for e in events
        if e["kind"] == "score" and e["config"] in selected
    }
    expired = {x["finished_at"] for x in f["prior_quota_failures_authorized_for_retry"]}
    original_kind = r.failure_kind
    r.failure_kind = lambda receipt: (
        "transient"
        if is_quota(receipt) and receipt.get("finished_at") in expired
        else original_kind(receipt)
    )
    r.RUNS, r.MANIFEST = RUNS, MANIFEST
    r.full.atomic = helper.atomic
    old_observe = r.PACER.observe

    def observe(receipt):
        try:
            old_observe(receipt)
        except PermissionError as exc:
            print(
                json.dumps({"at": r.now(), "telemetry_warning": str(exc)}), flush=True
            )

    r.PACER.observe = observe
    with r.FileLock(str(RUNS) + ".lock", timeout=0):
        launch = {
            "pid": os.getpid(),
            "started_at": r.now(),
            "manifest": r.artifact(MANIFEST),
        }
        r.immutable_json(RUNS / "launch.json", launch)
        for m in f["models"]:
            r.immutable_json(
                r.full.RUNS
                / "model-restarts"
                / ("z-claude-quota-" + m["model"] + ".json"),
                {
                    **launch,
                    "cid": m["cid"],
                    "directory": RUNS.relative_to(ROOT).as_posix(),
                },
            )

        def lane(m):
            path = RUNS / "lanes" / (m["model"] + ".json")
            state = {
                "model": m["model"],
                "cid": m["cid"],
                "state": "running",
                "workers": m["workers"],
                "started_at": r.now(),
            }
            helper.atomic(path, state)
            cohort = r.read(r.full.MANIFEST)
            jobs = [
                (c, n)
                for n in range(1, 101)
                for c in cohort["prompts"]
                if (m["cid"], c, n) not in done
            ]

            def work(item):
                return r.trial(f, m, *item, calls, None)

            # Confirm quota has reset with one pending trial before filling the lane.
            first = r.bounded_trials(jobs[:1], work, 1)
            if first["errors"]:
                result = first
            else:
                result = r.bounded_trials(
                    jobs[1:],
                    work,
                    m["workers"],
                    stopped=lambda: (RUNS / "STOP").exists(),
                )
                result["submitted"] += first["submitted"]
                result["completed"] += first["completed"]
            state.update(
                state="paused"
                if result["errors"] or result["stopped"]
                else "completed",
                finished_at=r.now(),
                result=result,
            )
            helper.atomic(path, state)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lane, f["models"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if parser.parse_args().execute:
        main()
    else:
        f = prepare()
        print(
            json.dumps(
                {
                    "models": [m["model"] for m in f["models"]],
                    "quota_retries": len(
                        f["prior_quota_failures_authorized_for_retry"]
                    ),
                }
            )
        )
