"""One headline-condition smoke on each newly frozen Claude/hosted DeepSeek route."""

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)

from study.api_validation import score_response
from study.claude_subscription_closeout import binary_identity
from study.claude_subscription_closeout import invoke as claude_call
from study.expansion_ledger import Ledger, digest, immutable_json, now, usage
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke, response_request
from study.siwc import output_text

spec = importlib.util.spec_from_file_location(
    "closeout_base", ROOT / "scripts/closeout-smoke.py"
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
RUNS = ROOT / "experiments/closeout-routes-v1"
MANIFEST = ROOT / "configs/closeout-routes-v1.json"
CONDITION = "explicit_replacement"


def prepare():
    if MANIFEST.exists():
        return read(MANIFEST)
    prior = read(base.MANIFEST)
    configs, _ = Ledger().snapshot()
    models = []
    for name in ["claude-opus-5-5", "claude-sonnet-5-5"]:
        old = next(c for c in configs if c["model"] == name)
        models.append(
            {
                "model": name,
                "auth": "claude_subscription",
                "vendor": "Anthropic",
                "rates": old["rates"],
                "release_date": old.get("release_date", ""),
                "effort": "medium",
                "thinking_display": "summarized",
                "prod": True,
            }
        )
    for name in ["deepseek/deepseek-v4-pro-0813", "deepseek/deepseek-v4.1-flash-1017"]:
        old = next(m for m in prior["models"] if m["model"] == name)
        models.append(
            {
                **old,
                "provider": "Parasail",
                "provider_tag": "parasail/fp8",
                "quantization": "fp8",
                "prod": False,
            }
        )
    f = {
        "created_at": now(),
        "campaign": "closeout-routes-v1",
        "models": models,
        "condition": CONDITION,
        "sample": 1,
        "target_per_condition": 100,
        "smoke_counts_toward_matching_full_run": True,
        "paid_calls_limit": 2,
        "paid_ceiling_usd": 0.084,
        "full_paid_run_authorized": False,
        "reason": "First-party DeepSeek 404 account-privacy rejections preserved; new explicitly pinned hosted configuration. Claude uses authorized prod=True plus supported thinking display.",
        "prior_manifest": artifact(base.MANIFEST),
        "prompts": {
            "original": prior["prompts"][CONDITION],
            "claude_prod": prior["claude_prod_true_prompts"][CONDITION],
        },
        "judge": prior["judge"],
        "automatic_retries": 0,
        "paid_fallback": False,
        "cli": binary_identity(),
        "source_files": [
            artifact(p)
            for p in [
                Path(__file__),
                ROOT / "scripts/closeout-smoke.py",
                *sorted((ROOT / "study").glob("*.py")),
            ]
        ],
    }
    immutable_json(MANIFEST, f)
    return f


def trial(f, m, key):
    cid = f"closeout-routes/{m['model']}/v1"
    tid = CONDITION + "-001"
    d = RUNS / m["model"].replace("/", "__") / tid
    prompt = f["prompts"]["claude_prod" if m["prod"] else "original"]
    ledger = Ledger()
    ledger.config(
        {
            "id": cid,
            "model": m["model"],
            "label": m["model"] + " (closeout route)",
            "vendor": m["vendor"],
            "release_date": m.get("release_date") or "",
            "target": 600,
            "auth": m["auth"],
            "tier": "subscription"
            if m["auth"] == "claude_subscription"
            else "standard",
            "rates": m["rates"],
            "judge_auth": "siwc",
            "judge_tier": "subscription",
            "judge_rates": {"input": 10, "output": 50, "cached_input": 1},
            "judge_model": f["judge"]["model"],
            "forecast": {
                "actor": {"input": 2600, "output": 2000},
                "judge": {"input": 1036, "output": 160},
            },
            "section": "Closeout route smoke",
            "state": "smoke",
            "configuration_sha256": digest(f),
            "manifest": MANIFEST.relative_to(ROOT).as_posix(),
            "profile": "prod=True; medium; native summary"
            if m["prod"]
            else "prod=False; Parasail FP8; native reasoning",
            "retention": "pending control review",
        }
    )
    actor_text = None
    for role in ["actor", "judge"]:
        dest = d / role
        auth = m["auth"] if role == "actor" else "siwc"
        if role == "judge":
            messages = [
                {"role": x.role, "content": x.content}
                for x in MurderClassifier().get_grader_input(
                    prompt["email"], actor_text
                )
            ]
            request = response_request(f["judge"]["model"], messages, "siwc")
        elif auth == "openrouter":
            request = base.actor_request(m, prompt)
        else:
            request = {
                "model": m["model"],
                "messages": prompt["messages"],
                "effort": "medium",
                "thinking_display": "summarized",
            }
        if (dest / "receipt.json").exists():
            r = read(dest / "receipt.json")
        else:
            ledger.reserve(cid, tid, role, request)
            if auth == "claude_subscription":
                r = claude_call(m["model"], prompt["messages"], dest, effort="medium")
            elif auth == "openrouter":
                r = base.router_call(request, dest, m, key)
            else:
                r = invoke(request, "siwc", dest)
        raw = r.get("response") or {}
        if auth == "claude_subscription":
            u = r.get("usage") or {}
            normalized = {
                "input": sum(
                    u.get(k, 0)
                    for k in [
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ]
                ),
                "output": u.get("output_tokens"),
                "cache_read": u.get("cache_read_input_tokens", 0),
                "cache_write": u.get("cache_creation_input_tokens", 0),
            }
        else:
            if r["request"] != request:
                raise ValueError("Request mismatch")
            normalized = usage(raw)
        body = {
            "role": role,
            "status": r["status"],
            "auth": auth,
            "tier": "standard" if auth == "openrouter" else "subscription",
            "rates": m["rates"]
            if role == "actor"
            else {"input": 10, "output": 50, "cached_input": 1},
            "usage": normalized,
            "condition": CONDITION,
            "sample": 1,
            "phase": "smoke",
            "artifact": artifact(dest / "receipt.json"),
            "configuration_sha256": digest(f),
            "reported_usd": (raw.get("usage") or {}).get("cost"),
        }
        if auth == "claude_subscription" and r.get("api_equivalent_cost") is not None:
            body["subscription_equivalent_usd"] = r["api_equivalent_cost"]
        ledger.event(f"{cid}/{tid}/{role}/call", cid, tid, "call", body)
        if r["status"] != "completed":
            raise RuntimeError(f"{role}: {r['status']}; inspect receipt, no retry")
        if auth == "claude_subscription":
            text = r["visible_text"]
            native = [
                c.get("thinking", "")
                for a in r["assistant_messages"]
                for c in a.get("content", [])
                if c["type"] == "thinking"
            ]
        elif auth == "openrouter":
            text = base.checked_completion(raw, m["model"], m["provider"])
            native = {
                k: raw["choices"][0]["message"].get(k)
                for k in ["reasoning", "reasoning_details"]
            }
        else:
            raw = assembled_response(r, dest)
            if raw.get("model") != f["judge"]["model"]:
                raise ValueError("Judge model mismatch")
            text = output_text(raw)
            native = None
        if not text.strip():
            raise ValueError("Empty response")
        immutable_json(
            dest / "extracted.json", {"visible_text": text, "native_reasoning": native}
        )
        if role == "actor":
            actor_text = text
        else:
            result = {
                "condition": CONDITION,
                "sample": 1,
                "scores": score_response(actor_text, text)["original"],
                "configuration_sha256": digest(f),
                "include_in_matching_full_run": True,
            }
            immutable_json(d / "score.json", result)
            ledger.event(f"{cid}/{tid}/score", cid, tid, "score", result)
    return {"model": m["model"], **result["scores"]}


def main():
    f = prepare()
    for a in f["source_files"]:
        if artifact(ROOT / a["path"]) != a:
            raise ValueError("Frozen source drift")
    if binary_identity() != f["cli"]:
        raise ValueError("CLI changed")
    from study.credentials import read_key

    key = read_key("openrouter")
    paid = [m for m in f["models"] if m["auth"] == "openrouter"]
    bound = sum(
        (20000 * m["rates"]["input"] + 10000 * m["rates"]["output"]) / 1e6 for m in paid
    )
    assert len(paid) == 2 and bound <= f["paid_ceiling_usd"] + 1e-9
    for p in f["prompts"].values():
        assert sum(len(x["content"].encode()) for x in p["messages"]) + 1024 < 20000

    def safe(m):
        try:
            return trial(f, m, key)
        except Exception as e:  # noqa: BLE001 -- preserve per-route failures
            return {"model": m["model"], "error": str(e)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(safe, f["models"]))
    immutable_json(
        RUNS / "smoke-result.json",
        {"created_at": now(), "results": results, "full_paid_run_started": False},
    )
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--execute"]:
        main()
    elif not sys.argv[1:]:
        print(
            json.dumps(
                {
                    "mode": "prepared",
                    "models": [m["model"] for m in prepare()["models"]],
                }
            )
        )
    else:
        raise ValueError("Only --execute is accepted")
