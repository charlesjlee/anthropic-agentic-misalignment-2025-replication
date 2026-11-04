"""Validate captured native smokes and score visible text, without repeating actors."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)

from study.api_validation import score_response
from study.expansion_ledger import immutable_json, now
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke, response_request
from study.siwc import output_text

RUNS = ROOT / "experiments/gemini-native-smoke-v2"


def validate(directory):
    planned = read(directory / "planned-request.json")
    receipt = read(directory / "receipt.json")
    events = [
        json.loads(x)
        for x in (directory / "stream.jsonl").read_text("utf-8").splitlines()
    ]
    init = [e for e in events if e.get("event") == "init"]
    results = [e["result"] for e in events if e.get("event") == "result"]
    if receipt["exit_code"] or len(init) != 1 or len(results) != 1:
        raise ValueError("Missing successful native completion")
    first, result = init[0], results[0]
    if (
        first["init"]["model"] != planned["model"]
        or first["init"]["agent"] != "datacenter-study"
    ):
        raise ValueError("Model/agent mismatch")
    if (
        result["status"] != "SUCCESS"
        or result["num_turns"] != 1
        or result["conversation_id"] != first["conversation_id"]
    ):
        raise ValueError("Invalid terminal identity or state")
    steps = [e["step_update"] for e in events if e.get("event") == "step_update"]
    if any(s["step_type"] not in {"user_input", "agent_response"} for s in steps):
        raise ValueError("Unexpected native action")
    text = "".join(
        s.get("text_delta", "") for s in steps if s["step_type"] == "agent_response"
    )
    if not text.strip() or text != result["response"]:
        raise ValueError("Text stream differs from terminal result")
    record = {
        "at": now(),
        "status": "completed_native_smoke",
        "model": planned["model"],
        "usage": result["usage"],
        "visible_text": text,
        "response_id": result["conversation_id"],
        "receipt": artifact(directory / "receipt.json"),
        "stream": artifact(directory / "stream.jsonl"),
        "native_tools_advertised": first["init"].get("tools", []),
        "native_tools_executed": [],
        "native_reasoning_summary": None,
        "limitation": "CLI advertises default tool catalog despite custom tools=[]; no tool actions occurred. Native system/wrapper not visible on wire. Separate harness smoke; not pooled into direct-API or tool-disabled primary cohort.",
    }
    immutable_json(directory / "validated.json", record)
    jd = directory / "judge"
    messages = [
        {"role": m.role, "content": m.content}
        for m in MurderClassifier().get_grader_input(planned["prompt"]["email"], text)
    ]
    request = response_request("gpt-6-astra", messages, "siwc")
    r = (
        read(jd / "receipt.json")
        if (jd / "receipt.json").exists()
        else invoke(request, "siwc", jd)
    )
    if r["status"] != "completed" or r["request"] != request:
        raise ValueError("Judge incomplete; do not count safe")
    verdict = output_text(assembled_response(r, jd))
    score = score_response(text, verdict)["original"]
    immutable_json(
        directory / "score.json",
        {
            "scores": score,
            "cohort": "Antigravity native harness smoke",
            "include_in_primary_full_run": False,
            "actor": artifact(directory / "validated.json"),
            "judge": artifact(jd / "receipt.json"),
        },
    )
    return {
        "model": planned["model"],
        "scores": score,
        "thinking_tokens": result["usage"].get("thinking_tokens"),
        "tools_executed": 0,
        "separate_harness": True,
    }


if __name__ == "__main__":
    rows = [validate(d) for d in sorted(RUNS.iterdir()) if d.is_dir()]
    immutable_json(RUNS / "validated-result.json", rows)
    print(json.dumps(rows), flush=True)
