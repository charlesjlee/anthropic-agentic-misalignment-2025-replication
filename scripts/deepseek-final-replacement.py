"""One explicitly authorized paid replacement; never retry the actor automatically."""

import argparse
import importlib.util
import json
import os
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "helpers", ROOT / "scripts/restart-sol-argon.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
r = helper.r
RUNS = ROOT / "experiments/deepseek-final-replacement-v1"
MANIFEST = ROOT / "configs/deepseek-final-replacement-v1.json"
CID = "closeout-routes/deepseek/deepseek-v4-pro-0813/v1"
CONDITION, SAMPLE = "explicit_replacement", 40
TID = "explicit_replacement-040"


def prepare():
    old = r.read(r.MANIFEST)
    for a in old["sources"]:
        if r.artifact(ROOT / a["path"]) != a:
            raise ValueError("Frozen source drift")
    if MANIFEST.exists():
        f = r.read(MANIFEST)
        for a in f["sources"]:
            if r.artifact(ROOT / a["path"]) != a:
                raise ValueError("Replacement source drift")
        return f
    m = next(m for m in old["models"] if m["cid"] == CID)
    primary = r.read(r.full.MANIFEST)
    if r.verify_sources() != primary["source_hashes"]:
        raise ValueError("Prompt/classifier drift")
    prompt = r.full.prompt_for(primary, m, CONDITION)
    request = r.full.base.actor_request(m, prompt)
    _, events = r.Ledger().snapshot()
    prior = [
        e
        for e in events
        if e["config"] == CID
        and e["trial"] == TID
        and e["kind"] == "call"
        and e["body"]["role"] == "actor"
    ]
    if len(prior) != 5:
        raise ValueError("Unexpected prior attempt count")
    for e in prior:
        path = ROOT / e["body"]["artifact"]["path"]
        receipt = r.read(path)
        if (
            r.artifact(path) != e["body"]["artifact"]
            or receipt["request"] != request
            or receipt["status"] == "completed"
        ):
            raise ValueError("Prior failed request/receipt differs")
    f = {
        "created": r.now(),
        "model": m,
        "prior_manifest": r.artifact(r.MANIFEST),
        "authorization": "User approved one additional labelled replacement with the same settings to reach 600/600",
        "maximum_new_paid_calls": 1,
        "maximum_paid_token_estimate_usd": (
            20000 * m["rates"]["input"] + 10000 * m["rates"]["output"]
        )
        / 1e6,
        "request": request,
        "settings_changed": False,
        "condition": CONDITION,
        "sample": SAMPLE,
        "replaces_all": [e["id"] for e in prior],
        "prior_receipts": [e["body"]["artifact"] for e in prior],
        "sources": [
            r.artifact(Path(__file__)),
            r.artifact(ROOT / "scripts/restart-sol-argon.py"),
        ],
        "policy": "One paid actor attempt only; no actor retry. Existing subscription judge retry policy. Retain all five failed attempts and link successful replacement.",
    }
    r.immutable_json(MANIFEST, f)
    for a in f["sources"]:
        p = RUNS / "executed-source" / a["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((ROOT / a["path"]).read_bytes())
    return f


def main():
    f = prepare()
    m = f["model"]
    ledger = r.Ledger()
    _, events = ledger.snapshot()
    done = {
        (e["body"]["condition"], e["body"]["sample"])
        for e in events
        if e["config"] == CID and e["kind"] == "score"
    }
    primary = r.read(r.full.MANIFEST)
    missing = {(c, n) for c in primary["prompts"] for n in range(1, 101)} - done
    if missing != {(CONDITION, SAMPLE)}:
        raise ValueError("Expected exactly this one missing trial")
    old_lane = r.read(helper.PRIOR / "lanes/deepseek__deepseek-v4-pro-0813.json")
    if old_lane["state"] != "finished_with_gaps":
        raise ValueError("Old worker is not finished")
    from study.credentials import read_key

    key = read_key("openrouter")
    original_call = r.get_call
    r.RUNS, r.MANIFEST = RUNS, MANIFEST
    r.full.atomic = helper.atomic

    def call(config, model, condition, sample, role, request, prompt, history, token):
        if role != "actor":
            return original_call(
                config, model, condition, sample, role, request, prompt, history, token
            )
        if request != f["request"] or condition != CONDITION or sample != SAMPLE:
            raise ValueError("Replacement request drift")
        dest = RUNS / m["model"].replace("/", "__") / TID / "actor/user-replacement-006"
        role_id = "actor/user-replacement-006"
        ledger.reserve(CID, TID, role_id, request)
        receipt = r.full.base.router_call(request, dest, m, key)
        raw = receipt.get("response") or {}
        eid = f"{CID}/{TID}/{role_id}/call"
        ledger.event(
            eid,
            CID,
            TID,
            "call",
            {
                "role": "actor",
                "status": receipt["status"],
                "auth": "openrouter",
                "tier": "standard",
                "rates": m["rates"],
                "usage": r.usage(raw),
                "reported_usd": (raw.get("usage") or {}).get("cost"),
                "artifact": r.artifact(dest / "receipt.json"),
                "condition": CONDITION,
                "sample": SAMPLE,
                "phase": "deepseek-final-replacement-v1",
                "authorization": r.artifact(MANIFEST),
                "request_sha256": r.digest(request),
                "replaces_all": f["replaces_all"],
                "replaces": f["replaces_all"][-1],
                "truncation": any(
                    c.get("finish_reason") == "length" for c in raw.get("choices", [])
                ),
            },
        )
        if receipt["status"] != "completed":
            raise RuntimeError(
                "Single authorized actor replacement failed; no further paid attempt"
            )
        value = r.extract(receipt, dest, "openrouter", m["model"])
        r.immutable_json(
            dest / "extracted.json",
            {"visible_text": value[0], "native_reasoning": value[1]},
        )
        return value, dest / "receipt.json", eid

    r.get_call = call
    with r.FileLock(str(RUNS) + ".lock", timeout=0):
        launch = {
            "pid": os.getpid(),
            "started_at": r.now(),
            "manifest": r.artifact(MANIFEST),
        }
        r.immutable_json(RUNS / "launch.json", launch)
        r.immutable_json(
            r.full.RUNS / "model-restarts/z-deepseek-final-replacement.json",
            {**launch, "cid": CID, "directory": RUNS.relative_to(ROOT).as_posix()},
        )
        state = {
            "model": m["model"],
            "cid": CID,
            "state": "running",
            "workers": 1,
            "started_at": r.now(),
        }
        lane = RUNS / "lanes/deepseek__deepseek-v4-pro-0813.json"
        helper.atomic(lane, state)
        try:
            r.trial(f, m, CONDITION, SAMPLE, defaultdict(list), key)
            state.update(state="completed", finished_at=r.now())
        except Exception as exc:
            state.update(
                state="finished_with_gaps",
                finished_at=r.now(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        finally:
            helper.atomic(lane, state)


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
                    "prepared": True,
                    "max_paid_calls": f["maximum_new_paid_calls"],
                    "max_output_tokens": f["request"]["max_tokens"],
                }
            )
        )
