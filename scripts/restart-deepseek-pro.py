"""User-authorized restart; preserve the two truncations and label replacements."""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock

from study.expansion_ledger import Ledger, immutable_json, now
from study.expansion_parallel import bounded_trials
from study.expansion_setup import artifact, read

spec = importlib.util.spec_from_file_location(
    "paced", ROOT / "scripts/resume-closeout.py"
)
paced = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paced)
RUNS = paced.RUNS
RESTART = RUNS / "deepseek-restart-v1"
CID = "closeout-routes/deepseek/deepseek-v4-pro-0813/v1"
REPLACEMENTS = {"none_none-003", "none_replacement-003"}
ORIGINAL_OBTAIN = paced.obtain


class ReplacementLedger:
    def __init__(self, ledger):
        self.ledger = ledger

    def reserve(self, cid, tid, role, request):
        if role != "actor":
            raise ValueError("Replacement adapter is actor-only")
        return self.ledger.reserve(cid, tid, "actor/truncation-replacement-01", request)

    def event(self, event_id, cid, tid, kind, body):
        if event_id != f"{cid}/{tid}/actor/call" or kind != "call":
            raise ValueError("Unexpected replacement event")
        return self.ledger.event(
            f"{cid}/{tid}/actor/truncation-replacement-01/call",
            cid,
            tid,
            kind,
            {
                **body,
                "replaces": event_id,
                "replacement_reason": "User requested restart after preserved length truncation",
            },
        )


def obtain(
    ledger, cid, tid, role, request, auth, m, p, directory, key, f, condition, sample
):
    if cid == CID and role == "actor" and tid in REPLACEMENTS:
        original = read(directory / "receipt.json")
        choices = (original.get("response") or {}).get("choices", [])
        if (
            original["request"] != request
            or original["status"] == "completed"
            or not choices
            or choices[0].get("finish_reason") != "length"
        ):
            raise ValueError("Replacement does not match reviewed truncation")
        return ORIGINAL_OBTAIN(
            ReplacementLedger(ledger),
            cid,
            tid,
            role,
            request,
            auth,
            m,
            p,
            directory / "truncation-replacement-01",
            key,
            f,
            condition,
            sample,
        )
    return ORIGINAL_OBTAIN(
        ledger,
        cid,
        tid,
        role,
        request,
        auth,
        m,
        p,
        directory,
        key,
        f,
        condition,
        sample,
    )


def main():
    f = read(paced.full.MANIFEST)
    # The only changed frozen module is the documented Google cost display integration.
    integration = read(ROOT / "experiments/gemini-full-v1/integration-amendment.json")
    allowed = {
        a["path"]: a
        for a in integration["after"]
        if a["path"] == "study/expansion_costs.py"
    }
    for a in f["source_files"]:
        if artifact(ROOT / a["path"]) != allowed.get(a["path"], a):
            raise ValueError("Unreviewed source drift: " + a["path"])
    if paced.full.verify_sources() != f["source_hashes"]:
        raise ValueError("Prompt or classifier drift")
    m = next(m for m in f["models"] if m["cid"] == CID)
    lane_path = RUNS / "lanes/deepseek__deepseek-v4-pro-0813.json"
    old_lane = read(lane_path)
    if old_lane["state"] != "paused":
        raise ValueError("Expected drained, paused DeepSeek lane")
    ledger = Ledger()
    _, events = ledger.snapshot()
    done = {
        (e["body"]["condition"], e["body"]["sample"])
        for e in events
        if e["config"] == CID and e["kind"] == "score"
    }
    jobs = [(c, n) for n in range(1, 101) for c in f["prompts"] if (c, n) not in done]
    amendment = {
        "created_at": now(),
        "authorization": "User: restart deepseek pro again",
        "configuration": artifact(paced.full.MANIFEST),
        "changed_inference_controls": {},
        "replacement_trials": sorted(REPLACEMENTS),
        "already_scored": len(done),
        "remaining_trials": len(jobs),
        "workers": 8,
        "maximum_new_paid_calls": len(jobs),
        "conservative_token_price_bound_usd": len(jobs)
        * (20000 * m["rates"]["input"] + 10000 * m["rates"]["output"])
        / 1e6,
        "policy": "Original prompt, 10000 output cap, provider, reasoning and scoring unchanged. Only two reviewed truncations receive a labelled replacement. A new failed actor pauses the lane; no automatic paid retry.",
        "reviewed_receipts": [
            artifact(RUNS / m["model"].replace("/", "__") / t / "actor/receipt.json")
            for t in sorted(REPLACEMENTS)
        ],
        "sources": [
            artifact(Path(__file__)),
            artifact(ROOT / "scripts/resume-closeout.py"),
        ],
    }
    with FileLock(str(RESTART) + ".lock", timeout=0):
        immutable_json(RESTART / "prior-lane.json", old_lane)
        immutable_json(RESTART / "authorization.json", amendment)
        launch = {
            "cid": CID,
            "pid": os.getpid(),
            "started_at": now(),
            "directory": RUNS.relative_to(ROOT).as_posix(),
            "authorization": artifact(RESTART / "authorization.json"),
        }
        immutable_json(RESTART / "launch.json", launch)
        immutable_json(RUNS / "model-restarts/deepseek-pro-001.json", launch)
        state = {
            "model": m["model"],
            "cid": CID,
            "state": "running",
            "started_at": launch["started_at"],
            "workers": 8,
            "runner_pid": os.getpid(),
            "restart": artifact(RESTART / "launch.json"),
        }
        paced.full.atomic(lane_path, state)
        paced.AMENDMENT = RESTART / "authorization.json"
        paced.PACER.interval = 5
        paced.obtain = obtain
        from study.credentials import read_key

        key = read_key("openrouter")
        result = bounded_trials(
            jobs,
            lambda item: paced.trial(f, m, *item, key),
            8,
            stopped=lambda: (RESTART / "STOP").exists(),
        )
        state.update(
            state="paused" if result["errors"] or result["stopped"] else "completed",
            result=result,
            finished_at=now(),
        )
        paced.full.atomic(lane_path, state)
        immutable_json(RESTART / "result.json", state)


if __name__ == "__main__":
    main()
