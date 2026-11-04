"""Resume the two paused lanes; recover receipts and wait out native quota."""

import argparse
import importlib.util
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "resilient", ROOT / "scripts/resilient-closeout.py"
)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
RUNS = ROOT / "experiments/sol-argon-restart-v1"
MANIFEST = ROOT / "configs/sol-argon-restart-v1.json"
PRIOR = ROOT / "experiments/resilient-closeout-v2"
MODELS = {"gpt-6-sol", "gemini-4-argon-medium"}
QUOTAS = {}


def atomic(path, value):
    """A Windows reader can briefly prevent replacement; retry metadata writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def quota_reset(receipt, stderr):
    if "Individual quota reached" not in stderr:
        return None
    match = re.search(r"Resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", stderr)
    if not match or not any(match.groups()):
        return None
    h, m, s = [int(x or 0) for x in match.groups()]
    return datetime.fromisoformat(receipt["finished_at"]) + timedelta(
        hours=h, minutes=m, seconds=s + 60
    )


def prepare():
    old = r.read(r.MANIFEST)
    for a in old["sources"]:
        if r.artifact(ROOT / a["path"]) != a:
            raise ValueError("Prior frozen source drift")
    if MANIFEST.exists():
        f = r.read(MANIFEST)
        if r.artifact(Path(__file__)) != f["runner"]:
            raise ValueError("Restart source drift")
        return f
    f = {
        "created": r.now(),
        "models": [m for m in old["models"] if m["model"] in MODELS],
        "prior_manifest": r.artifact(r.MANIFEST),
        "runner": r.artifact(Path(__file__)),
        "authorization": "Restart Argon and Sol; hide completed runs from tracker",
        "settings_changed": False,
        "quota_policy": "Wait until reported subscription reset plus 60 seconds; no paid fallback; repeat waiting if quota recurs.",
        "recovery": "Reuse the completed Sol judge receipt lost between inference completion and ledger append due to pacing metadata PermissionError.",
        "metadata_policy": "Unique temporary path and bounded sharing-violation retries; failed pacing telemetry must not discard successful inference.",
    }
    r.immutable_json(MANIFEST, f)
    archive = RUNS / "executed-source" / "restart-sol-argon.py"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(Path(__file__).read_bytes())
    return f


def recover_sol(f):
    m = next(m for m in f["models"] if m["model"] == "gpt-6-sol")
    tid, condition, sample = "explicit_none-056", "explicit_none", 56
    base = f"{m['cid']}/{tid}/judge/resilient-v2-001"
    ledger = r.Ledger()
    _, events = ledger.snapshot()
    if any(e["id"] == base + "/call" for e in events):
        return
    reserved = next(e for e in events if e["id"] == base + "/reserved")
    actor_path = PRIOR / m["model"] / tid / "actor/attempt-001/receipt.json"
    judge_path = PRIOR / m["model"] / tid / "judge/attempt-001/receipt.json"
    actor = r.read(actor_path)
    judge = r.read(judge_path)
    primary = r.read(r.full.MANIFEST)
    prompt = r.full.prompt_for(primary, m, condition)
    expected_actor = r.full.base.actor_request(m, prompt)
    if actor["status"] != "completed" or actor["request"] != expected_actor:
        raise ValueError("Actor receipt mismatch")
    text, _ = r.extract(actor, actor_path.parent, "siwc", m["model"])
    expected = r.full.judge_request(primary, prompt, text)
    if judge["status"] != "completed" or judge["request"] != expected:
        raise ValueError("Unrecorded judge receipt not a matching completion")
    r.extract(judge, judge_path.parent, "siwc", "gpt-6-astra")
    ledger.event(
        base + "/call",
        m["cid"],
        tid,
        "call",
        {
            "role": "judge",
            "status": "completed",
            "auth": "siwc",
            "tier": "subscription",
            "rates": r.full.JUDGE_RATES,
            "usage": r.usage(judge["response"]),
            "artifact": r.artifact(judge_path),
            "condition": condition,
            "sample": sample,
            "phase": "sol-argon-recovery",
            "authorization": r.artifact(MANIFEST),
            "request_sha256": r.digest(expected),
            "recovered_reservation": reserved["id"],
            "recovery_note": "Terminal completed receipt predates ledger repair; no inference repeated.",
        },
    )


def snapshot(m):
    _, events = r.Ledger().snapshot()
    calls = defaultdict(list)
    quota_times = []
    for e in events:
        if e["config"] != m["cid"] or e["kind"] != "call":
            continue
        calls[(e["config"], e["trial"], e["body"]["role"])].append(e)
        if (
            m["cohort"] == "google"
            and e["body"]["role"] == "actor"
            and e["body"]["status"] != "completed"
        ):
            path = ROOT / e["body"]["artifact"]["path"]
            receipt = r.read(path)
            errpath = path.parent / "stderr.txt"
            reset = quota_reset(
                receipt, errpath.read_text("utf-8") if errpath.exists() else ""
            )
            if reset:
                QUOTAS[receipt["finished_at"]] = reset
                quota_times.append(reset)
    done = {
        (e["body"]["condition"], e["body"]["sample"])
        for e in events
        if e["config"] == m["cid"] and e["kind"] == "score"
    }
    return calls, done, max(quota_times) if quota_times else None


def main():
    f = prepare()
    for m in f["models"]:
        if r.read(PRIOR / "lanes" / (m["model"] + ".json"))["state"] != "paused":
            raise RuntimeError("Selected old lane is not paused")
    recover_sol(f)
    r.RUNS, r.MANIFEST = RUNS, MANIFEST
    r.full.atomic = atomic
    old_observe = r.PACER.observe

    def observe(receipt):
        try:
            old_observe(receipt)
        except PermissionError as exc:
            print(
                json.dumps({"at": r.now(), "telemetry_warning": str(exc)}), flush=True
            )

    r.PACER.observe = observe
    old_kind = r.failure_kind

    def kind(receipt):
        reset = QUOTAS.get(receipt.get("finished_at"))
        if reset and datetime.now(UTC) >= reset:
            return "transient"
        return old_kind(receipt)

    r.failure_kind = kind
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
                / ("z-sol-argon-" + m["model"] + ".json"),
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
                "workers": m["workers"],
                "started_at": r.now(),
            }
            while True:
                calls, done, reset = snapshot(m)
                if reset and reset > datetime.now(UTC):
                    state.update(state="waiting_quota", resume_at=reset.isoformat())
                    atomic(path, state)
                    while datetime.now(UTC) < reset:
                        if (RUNS / "STOP").exists():
                            state.update(state="paused", finished_at=r.now())
                            atomic(path, state)
                            return
                        time.sleep(
                            min(30, max(0, (reset - datetime.now(UTC)).total_seconds()))
                        )
                state.update(state="running")
                atomic(path, state)
                cohort = r.read(
                    r.gemini.MANIFEST if m["cohort"] == "google" else r.full.MANIFEST
                )
                jobs = [
                    (c, n)
                    for n in range(1, 101)
                    for c in cohort["prompts"]
                    if (c, n) not in done
                ]
                exhausted = []
                lock = threading.Lock()

                def work(item, calls=calls, lock=lock, exhausted=exhausted):
                    try:
                        r.trial(f, m, *item, calls, None)
                    except r.TrialExhausted as exc:
                        record = {
                            "condition": item[0],
                            "sample": item[1],
                            "error": str(exc),
                            "at": r.now(),
                        }
                        r.immutable_json(
                            RUNS
                            / m["model"]
                            / f"{item[0]}-{item[1]:03d}"
                            / "exhausted.json",
                            record,
                        )
                        with lock:
                            exhausted.append(record)

                result = r.bounded_trials(
                    jobs, work, m["workers"], stopped=lambda: (RUNS / "STOP").exists()
                )
                _, _, next_reset = snapshot(m)
                if result["errors"] and next_reset and next_reset > datetime.now(UTC):
                    continue
                state.update(
                    state="paused"
                    if result["errors"] or result["stopped"]
                    else "finished_with_gaps"
                    if exhausted
                    else "completed",
                    finished_at=r.now(),
                    result=result,
                    exhausted=exhausted,
                )
                atomic(path, state)
                return

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lane, f["models"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if parser.parse_args().execute:
        main()
    else:
        print(
            json.dumps(
                {"prepared": True, "models": [m["model"] for m in prepare()["models"]]}
            )
        )
