"""Recover split Claude content events offline; make only the two missing judge calls."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)

from study.api_validation import score_response
from study.claude_subscription_closeout import assemble_assistant
from study.expansion_ledger import Ledger, digest, immutable_json, now, usage
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke, response_request
from study.siwc import output_text


def main():
    root = ROOT / "experiments/closeout-routes-v1"
    report = root / "claude-reconciliation.json"
    if report.exists():
        print(json.dumps(read(report)))
        return
    f = read(ROOT / "configs/closeout-routes-v1.json")
    ledger = Ledger()
    rows = []
    amendment = {
        "created_at": now(),
        "reason": "One native message emitted as separate thinking/text events; no actor regeneration",
        "original_manifest": artifact(ROOT / "configs/closeout-routes-v1.json"),
        "parser": artifact(ROOT / "study/claude_subscription_closeout.py"),
        "reconciliation_script": artifact(Path(__file__)),
        "source_snapshot": "experiments/closeout-routes-v1/executed-source",
        "tests": "6 passed: split completion, mixed identity/model, duplicate, truncation, real tool call",
    }
    immutable_json(root / "parser-amendment.json", amendment)
    for m in [m for m in f["models"] if m["auth"] == "claude_subscription"]:
        model = m["model"]
        cid = f"closeout-routes/{model}/v1"
        tid = f["condition"] + "-001"
        d = root / model / tid
        old = read(d / "actor/receipt.json")
        assert old["exit_code"] == 0 and len(old["results"]) == 1
        assert old["request"]["messages"] == f["prompts"]["claude_prod"]["messages"]
        assert (
            old["init"][0]["model"] == model
            and not old["init"][0]["tools"]
            and not old["init"][0]["mcp_servers"]
        )
        assert "--thinking-display" in old["request"]["argv"]
        assembled, text = assemble_assistant(
            old["assistant_messages"], old["results"][0], model
        )
        terminal = old["results"][0]
        u = terminal["usage"]
        normalized = {
            "input": sum(
                u.get(k, 0)
                for k in [
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ]
            ),
            "output": u["output_tokens"],
            "cache_read": u.get("cache_read_input_tokens", 0),
            "cache_write": u.get("cache_creation_input_tokens", 0),
        }
        native = [
            p["thinking"] for p in assembled["content"] if p["type"] == "thinking"
        ]
        recovery = {
            "status": "completed",
            "original_receipt": artifact(d / "actor/receipt.json"),
            "visible_text": text,
            "native_summary": native,
            "usage": normalized,
            "terminal": terminal,
            "returned_model": model,
            "response_id": assembled["id"],
            "actor_regenerated": False,
        }
        immutable_json(d / "actor/reconciled.json", recovery)
        ledger.event(
            f"{cid}/{tid}/actor/reconciled",
            cid,
            tid,
            "call",
            {
                "role": "actor",
                "status": "completed",
                "auth": "claude_subscription",
                "tier": "subscription",
                "rates": m["rates"],
                "usage": normalized,
                "subscription_equivalent_usd": terminal["total_cost_usd"],
                "replaces": f"{cid}/{tid}/actor/call",
                "artifact": artifact(d / "actor/reconciled.json"),
                "condition": f["condition"],
                "sample": 1,
                "phase": "smoke",
                "configuration_sha256": digest(f),
            },
        )
        messages = [
            {"role": x.role, "content": x.content}
            for x in MurderClassifier().get_grader_input(
                f["prompts"]["claude_prod"]["email"], text
            )
        ]
        request = response_request(f["judge"]["model"], messages, "siwc")
        jd = d / "judge"
        if (jd / "receipt.json").exists():
            r = read(jd / "receipt.json")
        else:
            ledger.reserve(cid, tid, "judge", request)
            r = invoke(request, "siwc", jd)
        assert r["request"] == request
        ledger.event(
            f"{cid}/{tid}/judge/call",
            cid,
            tid,
            "call",
            {
                "role": "judge",
                "status": r["status"],
                "auth": "siwc",
                "tier": "subscription",
                "usage": usage(r.get("response") or {}),
                "rates": {"input": 10, "output": 50, "cached_input": 1},
                "artifact": artifact(jd / "receipt.json"),
                "condition": f["condition"],
                "sample": 1,
                "phase": "smoke",
                "configuration_sha256": digest(f),
            },
        )
        if r["status"] != "completed":
            raise RuntimeError("Judge not completed; no retry")
        raw = assembled_response(r, jd)
        assert raw["model"] == f["judge"]["model"]
        result = {
            "condition": f["condition"],
            "sample": 1,
            "scores": score_response(text, output_text(raw))["original"],
            "configuration_sha256": digest(f),
            "include_in_matching_full_run": True,
        }
        immutable_json(d / "score.json", result)
        ledger.event(f"{cid}/{tid}/score", cid, tid, "score", result)
        rows.append(
            {
                "model": model,
                "summary_characters": sum(map(len, native)),
                **result["scores"],
            }
        )
    immutable_json(report, {"rows": rows, "actor_calls": 0, "missing_judge_calls": 2})
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
