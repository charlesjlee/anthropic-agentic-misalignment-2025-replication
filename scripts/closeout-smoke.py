"""Bounded closeout smoke: one reusable trial per condition; no full paid run."""

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from filelock import FileLock
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)
from inspect_evals.agentic_misalignment.prompt_generator import generate_prompts

from study.api_validation import score_response
from study.expansion_ledger import Ledger, digest, immutable_json, now, usage
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke, response_request
from study.historical_audit import CONDITIONS
from study.openrouter_smoke import checked_completion
from study.pilot import verify_sources
from study.siwc import output_text

MANIFEST = ROOT / "configs/closeout-smoke-v1.json"
RUNS = ROOT / "experiments/closeout-v1"
RESEARCH = ROOT / ".local/research/closeout-20261102"
ROUTES = [
    ("deepseek/deepseek-v4-pro-0813", "deepseek", "DeepSeek", 1.32, 3.96),
    ("deepseek/deepseek-v4.1-flash-1017", "deepseek", "DeepSeek", 0.30, 1.20),
    ("x-ai/grok-4.7", "xai", "xAI", 2.0, 6.0),
]


def prepare():
    if MANIFEST.exists():
        return read(MANIFEST)
    baseline = read(ROOT / "configs/expansion-smoke-v1.json")
    rows = read(ROOT / "ref/openai_expansion_plan_2026-11-01.json")[
        "configuration_rows"
    ]
    models = []
    for name in ["gpt-6.1-terra", "gpt-6-luna"]:
        r = next(r for r in rows if r.get("requested_direct_id") == name)
        models.append(
            {
                "model": name,
                "auth": "siwc",
                "vendor": "OpenAI",
                "release_date": r["release_date"],
                "rates": r["standard_direct_price_usd_per_m"],
                "reasoning": {
                    "effort": "medium",
                    "mode": "standard",
                    "summary": "auto",
                },
            }
        )
    for name, tag, provider, pi, po in ROUTES:
        ep = next(
            e
            for e in read(RESEARCH / (name.replace("/", "_") + "-endpoints.json"))[
                "data"
            ]["endpoints"]
            if e["tag"] == tag
        )
        models.append(
            {
                "model": name,
                "auth": "openrouter",
                "vendor": "DeepSeek" if name.startswith("deepseek/") else "xAI",
                "release_date": None,
                "rates": {"input": pi, "output": po},
                "rate_note": "Conservative endpoint price ceiling, not cheapest advertised model rate",
                "provider": provider,
                "provider_tag": tag,
                "quantization": ep.get("quantization"),
                "reasoning": {"enabled": True, "exclude": False},
                "endpoint_evidence": artifact(
                    RESEARCH / (name.replace("/", "_") + "-endpoints.json")
                ),
            }
        )
    claude_prompts = {}
    for c in CONDITIONS:
        p = generate_prompts(
            "murder",
            c["goal_type"],
            c["goal_value"],
            c["urgency_type"],
            None,
            True,
            None,
        )
        old = baseline["prompts"][c["name"]]
        assert p.email_content == old["email"]
        assert p.user_prompt + "\n\n" + p.email_content == old["messages"][1]["content"]
        from inspect_evals.agentic_misalignment.templates.system_prompt_templates import (
            SCRATCHPAD_REASONING_FORMAT,
        )

        assert old["messages"][0]["content"].count(SCRATCHPAD_REASONING_FORMAT) == 1
        assert (
            old["messages"][0]["content"].replace(SCRATCHPAD_REASONING_FORMAT, "\n\n")
            == p.system_prompt
        )
        claude_prompts[c["name"]] = {
            "messages": [
                {"role": "system", "content": p.system_prompt},
                old["messages"][1],
            ],
            "email": old["email"],
        }
    manifest = {
        "schema": 1,
        "created_at": now(),
        "campaign": "closeout-v1",
        "status": "smoke_only",
        "target_per_condition": 100,
        "smoke_per_condition": 1,
        "automatic_retries": 0,
        "paid_fallback": False,
        "include_matching_smoke": True,
        "paid_smoke_call_limit": 18,
        "paid_smoke_ceiling_usd": 1.104,
        "paid_full_run_authorized": False,
        "authorization": "2026-11-02: user selected smoke tests only; decide full paid runs after estimates",
        "models": models,
        "prompts": baseline["prompts"],
        "prod": False,
        "claude_prod_true_prompts": claude_prompts,
        "claude_status": "not launched: verify subscription-native readable reasoning control",
        "judge": baseline["judge"],
        "judge_input": "Visible assistant text only; native reasoning retained separately",
        "baseline": artifact(ROOT / "configs/expansion-smoke-v1.json"),
        "source_hashes": verify_sources(),
        "runner_sources": {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), *sorted((ROOT / "study").glob("*.py"))]
        },
    }
    immutable_json(MANIFEST, manifest)
    return manifest


def actor_request(m, prompt):
    if m["auth"] == "siwc":
        return response_request(m["model"], prompt["messages"], "siwc", m["reasoning"])
    return {
        "model": m["model"],
        "messages": prompt["messages"],
        "temperature": 1.0,
        "max_tokens": 10000,
        "reasoning": m["reasoning"],
        "transforms": [],
        "provider": {
            "only": [m["provider_tag"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {
                "prompt": m["rates"]["input"],
                "completion": m["rates"]["output"],
            },
        },
    }


def router_call(request, directory, model, key):
    immutable_json(
        directory / "request.json", {"request": request, "started_at": now()}
    )
    r = {
        "request": request,
        "started_at": now(),
        "status": "uncertain",
        "auth": "openrouter",
    }
    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=request,
            headers={"Authorization": "Bearer " + key},
            timeout=httpx.Timeout(600, connect=30),
        )
        r["http_status"] = response.status_code
        body = response.json()
        # Never retain credential-bearing error diagnostics.
        if response.status_code != 200 or body.get("error"):
            r["provider_error"] = str(body.get("error", {})).replace(key, "[redacted]")[
                :2000
            ]
            raise RuntimeError("Provider rejected request")
        r["response"] = body
        checked_completion(body, model["model"], model["provider"])
        r["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 -- preserve uncertain attempts; never retry
        r["error_type"] = type(exc).__name__
    finally:
        r["finished_at"] = now()
        immutable_json(directory / "receipt.json", r)
    return r


def trial(frozen, model, condition, key):
    ledger = Ledger()
    name = model["model"]
    cid = f"closeout/{name}/v1"
    tid = condition + "-001"
    directory = RUNS / name.replace("/", "__") / tid
    prompt = frozen["prompts"][condition]
    actor_text = None
    call_ids = {}
    for role in ["actor", "judge"]:
        auth = model["auth"] if role == "actor" else "siwc"
        messages = (
            [
                {"role": m.role, "content": m.content}
                for m in MurderClassifier().get_grader_input(
                    prompt["email"], actor_text
                )
            ]
            if role == "judge"
            else None
        )
        request = (
            actor_request(model, prompt)
            if role == "actor"
            else response_request(frozen["judge"]["model"], messages, "siwc")
        )
        d = directory / role
        eid = f"{cid}/{tid}/{role}/call"
        call_ids[role] = eid
        if (d / "receipt.json").exists():
            receipt = read(d / "receipt.json")
        else:
            if (d / "request.json").exists():
                raise RuntimeError("Uncertain reserved attempt; reconcile manually")
            ledger.reserve(cid, tid, role, request)
            receipt = (
                router_call(request, d, model, key)
                if auth == "openrouter"
                else invoke(request, "siwc", d)
            )
        if receipt["request"] != request:
            raise ValueError("Frozen request mismatch")
        raw = receipt.get("response") or {}
        ledger.event(
            eid,
            cid,
            tid,
            "call",
            {
                "role": role,
                "status": receipt["status"],
                "auth": auth,
                "tier": "standard" if auth == "openrouter" else "subscription",
                "rates": model["rates"]
                if role == "actor"
                else {"input": 10, "output": 50, "cached_input": 1},
                "usage": usage(raw),
                "reported_usd": (raw.get("usage") or {}).get("cost"),
                "artifact": artifact(d / "receipt.json"),
                "condition": condition,
                "sample": 1,
                "phase": "smoke",
                "configuration_sha256": digest(frozen),
                "request_sha256": digest(request),
            },
        )
        if receipt["status"] != "completed":
            raise RuntimeError(f"{role}: {receipt['status']}; no retry")
        if auth == "openrouter":
            text = checked_completion(raw, name, model["provider"])
            native = {
                k: raw["choices"][0]["message"].get(k)
                for k in ["reasoning", "reasoning_details"]
            }
        else:
            raw = assembled_response(receipt, d)
            if raw.get("model") != (
                name if role == "actor" else frozen["judge"]["model"]
            ):
                raise ValueError("Returned model substitution")
            text = output_text(raw)
            native = {
                "summary": [
                    s.get("text", "")
                    for item in raw.get("output", [])
                    if item.get("type") == "reasoning"
                    for s in item.get("summary", [])
                ]
            }
        if not text.strip():
            raise ValueError("Empty visible text")
        immutable_json(
            d / "extracted.json", {"visible_text": text, "native_reasoning": native}
        )
        if role == "actor":
            actor_text = text
        else:
            result = {
                "condition": condition,
                "sample": 1,
                "scores": score_response(actor_text, text)["original"],
                "calls": call_ids,
                "configuration_sha256": digest(frozen),
                "include_in_matching_full_run": True,
            }
            immutable_json(directory / "score.json", result)
            ledger.event(f"{cid}/{tid}/score", cid, tid, "score", result)
    return {"model": name, "condition": condition, **result["scores"]}


def run():
    f = prepare()
    for name, expected in f["runner_sources"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Runner source drift: " + name)
    if verify_sources() != f["source_hashes"]:
        raise ValueError("Pinned prompt/classifier drift")
    paid = [m for m in f["models"] if m["auth"] == "openrouter"]
    bound = sum(
        6 * (20000 * m["rates"]["input"] + 10000 * m["rates"]["output"]) / 1e6
        for m in paid
    )
    if (
        len(paid) * 6 != f["paid_smoke_call_limit"]
        or bound > f["paid_smoke_ceiling_usd"] + 1e-9
    ):
        raise ValueError("Smoke spending bound mismatch")
    for p in f["prompts"].values():
        if sum(len(m["content"].encode()) for m in p["messages"]) + 1024 > 20000:
            raise ValueError("Prompt exceeds input bound")
    from study.credentials import read_key

    key = read_key("openrouter")
    ledger = Ledger()
    for m in f["models"]:
        ledger.config(
            {
                "id": f"closeout/{m['model']}/v1",
                "model": m["model"],
                "label": m["model"] + " (closeout)",
                "vendor": m["vendor"],
                "release_date": m["release_date"] or "",
                "target": 600,
                "auth": m["auth"],
                "tier": "standard" if m["auth"] == "openrouter" else "subscription",
                "rates": m["rates"],
                "judge_auth": "siwc",
                "judge_tier": "subscription",
                "judge_rates": {"input": 10, "output": 50, "cached_input": 1},
                "judge_model": f["judge"]["model"],
                "forecast": {
                    "actor": {"input": 2600, "output": 2000},
                    "judge": {"input": 1036, "output": 160},
                },
                "section": "Closeout smoke; full paid runs not authorized",
                "state": "smoke",
                "configuration_sha256": digest(f),
                "manifest": MANIFEST.relative_to(ROOT).as_posix(),
                "profile": "Original optional scratchpad; native reasoning retained separately",
                "retention": "pending control review",
            }
        )
    results, errors = [], []

    # One lane per model; a failed condition stops only that model's remaining smoke.
    def lane(m):
        rows = []
        for c in f["prompts"]:
            try:
                r = trial(f, m, c, key)
                rows.append(r)
                print(json.dumps(r), flush=True)
            except Exception as exc:  # noqa: BLE001 -- isolate a failed provider lane
                return rows, {"model": m["model"], "condition": c, "error": str(exc)}
        return rows, None

    with (
        FileLock(str(RUNS) + ".lock", timeout=0),
        ThreadPoolExecutor(max_workers=5) as pool,
    ):
        for future in as_completed([pool.submit(lane, m) for m in f["models"]]):
            rows, error = future.result()
            results.extend(rows)
            if error:
                errors.append(error)
    report = {
        "created_at": now(),
        "scored": results,
        "errors": errors,
        "full_paid_run_started": False,
    }
    immutable_json(RUNS / "smoke-result.json", report)
    print(json.dumps({"scored": len(results), "errors": errors}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        run()
    else:
        f = prepare()
        print(
            json.dumps(
                {
                    "mode": "prepared_only",
                    "models": [m["model"] for m in f["models"]],
                    "paid_call_limit": f["paid_smoke_call_limit"],
                    "paid_ceiling_usd": f["paid_smoke_ceiling_usd"],
                }
            )
        )
