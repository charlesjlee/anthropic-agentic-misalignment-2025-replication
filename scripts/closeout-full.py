"""Authorized 11-model closeout; immutable attempts, matching smoke reuse, no retries."""

import argparse
import importlib.util
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)

from study.api_validation import score_response
from study.claude_subscription_closeout import binary_identity
from study.claude_subscription_closeout import invoke as claude_call
from study.expansion_ledger import Ledger, digest, immutable_json, now, usage
from study.expansion_parallel import bounded_trials
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke, response_request
from study.pilot import verify_sources
from study.siwc import output_text

spec = importlib.util.spec_from_file_location(
    "closeout_base", ROOT / "scripts/closeout-smoke.py"
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
MANIFEST = ROOT / "configs/closeout-full-v1.json"
RUNS = ROOT / "experiments/closeout-full-v1"
OPENAI = [
    "gpt-6.1-terra",
    "gpt-6-luna",
    "gpt-6-sol",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
]
SIWC = threading.BoundedSemaphore(64)
PRINT = threading.Lock()
JUDGE_RATES = {"input": 10, "output": 50, "cached_input": 1}


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temp, path)


def prompt_for(f, m, condition):
    return f["claude_prompts" if m.get("prod") else "prompts"][condition]


def judge_request(f, prompt, text):
    messages = [
        {"role": x.role, "content": x.content}
        for x in MurderClassifier().get_grader_input(prompt["email"], text)
    ]
    return response_request(f["judge"]["model"], messages, "siwc")


def validate_smoke(f, m, directory, condition):
    p = prompt_for(f, m, condition)
    score = read(directory / "score.json")
    a = read(directory / "actor/receipt.json")
    if (directory / "actor/reconciled.json").exists():
        a = read(directory / "actor/reconciled.json")
    original = read(directory / "actor/receipt.json")
    if m["auth"] == "claude_subscription":
        req = original["request"]
        if (
            req["model"] != m["model"]
            or req["messages"] != p["messages"]
            or req["effort"] != "medium"
        ):
            raise ValueError("Claude smoke settings differ")
        if "display=summarized" not in req["native_reasoning"]:
            raise ValueError("Missing native summary request")
    elif original["request"] != base.actor_request(m, p):
        raise ValueError("Actor smoke request differs")
    actor = (
        a["visible_text"]
        if m["auth"] == "claude_subscription"
        else read(directory / "actor/extracted.json")["visible_text"]
    )
    judge_receipt = read(directory / "judge/receipt.json")
    judge = output_text(assembled_response(judge_receipt, directory / "judge"))
    if judge_receipt["request"] != judge_request(f, p, actor):
        raise ValueError("Judge smoke request differs")
    if score["scores"] != score_response(actor, judge)["original"]:
        raise ValueError("Smoke score replay mismatch")
    # Reconciled Claude artifacts preserve the failed parser receipt separately.
    if a.get("status") != "completed":
        raise ValueError("No completed smoke receipt")
    return {
        "condition": condition,
        "sample": 1,
        "score": artifact(directory / "score.json"),
        "actor_request": artifact(directory / "actor/request.json"),
        "directory": directory.relative_to(ROOT).as_posix(),
    }


def prepare():
    if MANIFEST.exists():
        return read(MANIFEST)
    old = read(base.MANIFEST)
    routes = read(ROOT / "configs/closeout-routes-v1.json")
    inventory = read(ROOT / "ref/openai_expansion_plan_2026-11-01.json")[
        "configuration_rows"
    ]
    models = []
    for name in OPENAI:
        previous = next((m for m in old["models"] if m["model"] == name), None)
        row = next(r for r in inventory if r.get("requested_direct_id") == name)
        m = (
            dict(previous)
            if previous
            else {
                "model": name,
                "auth": "siwc",
                "vendor": "OpenAI",
                "rates": row["standard_direct_price_usd_per_m"],
                "release_date": row["release_date"],
                "reasoning": {
                    "effort": "medium",
                    "mode": "standard",
                    "summary": "auto",
                },
            }
        )
        m.update(
            cid=f"closeout/{name}/v1" if previous else f"closeout-full/{name}/v1",
            workers=8,
            prior_root="experiments/closeout-v1" if previous else None,
        )
        models.append(m)
    for name in ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]:
        previous = next((m for m in routes["models"] if m["model"] == name), None)
        m = (
            dict(previous)
            if previous
            else {
                "model": name,
                "auth": "claude_subscription",
                "vendor": "Anthropic",
                "rates": {
                    "input": 0.1,
                    "output": 0.5,
                    "cached_input": 0.01,
                    "cache_write": 0.125,
                },
                "release_date": "2026-10-07",
                "effort": "medium",
                "thinking_display": "summarized",
                "prod": True,
            }
        )
        m.update(
            cid=f"closeout-routes/{name}/v1"
            if previous
            else f"closeout-full/{name}/v1",
            workers=4,
            prior_root="experiments/closeout-routes-v1" if previous else None,
        )
        models.append(m)
    for name, source, prior_root, prefix in [
        ("x-ai/grok-4.7", old, "experiments/closeout-v1", "closeout"),
        (
            "deepseek/deepseek-v4-pro-0813",
            routes,
            "experiments/closeout-routes-v1",
            "closeout-routes",
        ),
    ]:
        m = dict(next(m for m in source["models"] if m["model"] == name))
        m.update(cid=f"{prefix}/{name}/v1", workers=8, prior_root=prior_root)
        models.append(m)
    f = {
        "created_at": now(),
        "campaign": "closeout-full-v1",
        "authorization": "User explicitly requested full runs of these eleven models; Grok and DeepSeek Pro paid full runs authorized; Flash dropped; Gemini smoke only.",
        "models": models,
        "prompts": old["prompts"],
        "claude_prompts": old["claude_prod_true_prompts"],
        "judge": old["judge"],
        "source_hashes": verify_sources(),
        "cli": binary_identity(),
        "target_per_condition": 100,
        "automatic_retries": 0,
        "paid_fallback": False,
        "siwc_max_inflight": 64,
        "failure_policy": "Stop new work for affected model; drain other in-flight attempts; preserve every failure; no outcome-based stopping",
        "prior_manifests": [
            artifact(base.MANIFEST),
            artifact(ROOT / "configs/closeout-routes-v1.json"),
        ],
        "reused_smokes": {},
    }
    for m in models:
        inherited = []
        if m["prior_root"]:
            for c in f["prompts"]:
                d = (
                    ROOT
                    / m["prior_root"]
                    / m["model"].replace("/", "__")
                    / (c + "-001")
                )
                if (d / "score.json").exists():
                    inherited.append(validate_smoke(f, m, d, c))
        f["reused_smokes"][m["cid"]] = inherited
    if sum(map(len, f["reused_smokes"].values())) != 21:
        raise ValueError("Expected exactly 21 matching smoke trials")
    f["paid_new_actor_limit"] = sum(
        600 - len(f["reused_smokes"][m["cid"]])
        for m in models
        if m["auth"] == "openrouter"
    )
    f["paid_token_price_upper_bound_usd"] = sum(
        (600 - len(f["reused_smokes"][m["cid"]]))
        * (20000 * m["rates"]["input"] + 10000 * m["rates"]["output"])
        / 1e6
        for m in models
        if m["auth"] == "openrouter"
    )
    for p in f["prompts"].values():
        if sum(len(x["content"].encode()) for x in p["messages"]) + 1024 > 20000:
            raise ValueError("Paid prompt input bound exceeded")
    files = [
        Path(__file__),
        ROOT / "scripts/closeout-smoke.py",
        *sorted((ROOT / "study").glob("*.py")),
    ]
    f["source_files"] = [artifact(p) for p in files]
    for p in files:
        destination = RUNS / "executed-source" / p.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as out:
            out.write(p.read_bytes())
    immutable_json(MANIFEST, f)
    return f


def ledger_config(f, m):
    return {
        "id": m["cid"],
        "model": m["model"],
        "label": m["model"] + " (closeout full)",
        "vendor": m["vendor"],
        "release_date": m.get("release_date") or "",
        "target": 600,
        "auth": m["auth"],
        "tier": "standard" if m["auth"] == "openrouter" else "subscription",
        "rates": m["rates"],
        "judge_auth": "siwc",
        "judge_tier": "subscription",
        "judge_rates": JUDGE_RATES,
        "judge_model": f["judge"]["model"],
        "forecast": {
            "actor": {"input": 2600, "output": 2000},
            "judge": {"input": 1036, "output": 160},
        },
        "section": "Authorized closeout full runs",
        "state": "queued",
        "configuration_sha256": digest(f),
        "manifest": MANIFEST.relative_to(ROOT).as_posix(),
        "profile": "prod=True; native summarized thinking; medium"
        if m.get("prod")
        else "Original optional scratchpad; native reasoning requested",
        "retention": "pending final human review",
    }


def trial(f, m, condition, sample, key):
    ledger = Ledger()
    cid, tid = m["cid"], f"{condition}-{sample:03d}"
    directory = RUNS / m["model"].replace("/", "__") / tid
    p = prompt_for(f, m, condition)
    actor = None
    for role in ["actor", "judge"]:
        auth = m["auth"] if role == "actor" else "siwc"
        dest = directory / role
        request = (
            judge_request(f, p, actor)
            if role == "judge"
            else (
                {
                    "model": m["model"],
                    "messages": p["messages"],
                    "effort": "medium",
                    "thinking_display": "summarized",
                }
                if auth == "claude_subscription"
                else base.actor_request(m, p)
            )
        )
        ledger.reserve(cid, tid, role, request)
        if auth == "siwc":
            with SIWC:
                r = invoke(request, auth, dest)
        elif auth == "claude_subscription":
            r = claude_call(m["model"], p["messages"], dest, effort="medium")
        else:
            r = base.router_call(request, dest, m, key)
        raw = r.get("response") or {}
        u = r.get("usage") or {}
        normalized = (
            {
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
            if auth == "claude_subscription"
            else usage(raw)
        )
        body = {
            "role": role,
            "status": r["status"],
            "auth": auth,
            "tier": "standard" if auth == "openrouter" else "subscription",
            "rates": m["rates"] if role == "actor" else JUDGE_RATES,
            "usage": normalized,
            "reported_usd": (raw.get("usage") or {}).get("cost"),
            "artifact": artifact(dest / "receipt.json"),
            "condition": condition,
            "sample": sample,
            "phase": "full",
            "authorization_sha256": digest(f),
            "request_sha256": digest(request),
            "truncation": any(
                x.get("finish_reason") == "length" for x in raw.get("choices", [])
            ),
        }
        if auth == "claude_subscription" and r.get("api_equivalent_cost") is not None:
            body["subscription_equivalent_usd"] = r["api_equivalent_cost"]
        ledger.event(f"{cid}/{tid}/{role}/call", cid, tid, "call", body)
        if r["status"] != "completed":
            raise RuntimeError(
                f"{role}: {r['status']}; {dest.relative_to(ROOT)}; no retry"
            )
        if auth == "claude_subscription":
            text = r["visible_text"]
            native = [
                c.get("thinking", "")
                for a in r["assistant_messages"]
                for c in a.get("content", [])
                if c.get("type") == "thinking"
            ]
        elif auth == "openrouter":
            text = base.checked_completion(raw, m["model"], m["provider"])
            native = {
                k: raw["choices"][0]["message"].get(k)
                for k in ["reasoning", "reasoning_details"]
            }
        else:
            raw = assembled_response(r, dest)
            if raw.get("model") != request["model"]:
                raise ValueError("Model substitution")
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
            raise ValueError("Empty final text")
        immutable_json(
            dest / "extracted.json", {"visible_text": text, "native_reasoning": native}
        )
        if role == "actor":
            actor = text
        else:
            result = {
                "condition": condition,
                "sample": sample,
                "scores": score_response(actor, text)["original"],
                "authorization_sha256": digest(f),
                "include_in_matching_full_run": True,
            }
            immutable_json(directory / "score.json", result)
            ledger.event(f"{cid}/{tid}/score", cid, tid, "score", result)
    with PRINT:
        print(
            json.dumps(
                {"at": now(), "model": m["model"], "trial": tid, "scored": True}
            ),
            flush=True,
        )


def pending_jobs(f, m, events):
    done = {
        (e["body"]["condition"], e["body"]["sample"])
        for e in events
        if e["config"] == m["cid"] and e["kind"] == "score"
    }
    reserved = {
        e["trial"]
        for e in events
        if e["config"] == m["cid"] and e["kind"] == "reserved"
    }
    jobs = [(c, n) for n in range(1, 101) for c in f["prompts"] if (c, n) not in done]
    if any(f"{c}-{n:03d}" in reserved for c, n in jobs):
        raise RuntimeError("Incomplete reserved trial: reconcile before resume")
    return jobs


def main():
    f = prepare()
    for a in f["source_files"]:
        if artifact(ROOT / a["path"]) != a:
            raise ValueError("Source drift: " + a["path"])
    if verify_sources() != f["source_hashes"] or binary_identity() != f["cli"]:
        raise ValueError("Prompt/classifier/CLI drift")
    from study.credentials import read_key

    key = read_key("openrouter")
    ledger = Ledger()
    configs, _ = ledger.snapshot()
    existing = {c["id"] for c in configs}
    for m in f["models"]:
        if m["cid"] not in existing:
            ledger.config(ledger_config(f, m))
    immutable_json(
        RUNS / "launch.json",
        {
            "pid": os.getpid(),
            "started_at": now(),
            "manifest": artifact(MANIFEST),
            "reused_trials": 21,
        },
    )

    def lane(m):
        status_path = RUNS / "lanes" / (m["model"].replace("/", "__") + ".json")
        state = {
            "model": m["model"],
            "cid": m["cid"],
            "started_at": now(),
            "state": "running",
            "workers": m["workers"],
        }
        atomic(status_path, state)
        try:
            _, events = ledger.snapshot()
            jobs = pending_jobs(f, m, events)
            # First new trial validates every route before increasing concurrency.
            if jobs:
                trial(f, m, *jobs.pop(0), key)
            result = bounded_trials(
                jobs,
                lambda item: trial(f, m, *item, key),
                m["workers"],
                stopped=lambda: (RUNS / "STOP").exists(),
            )
            state.update(
                state="paused"
                if result["errors"] or result["stopped"]
                else "completed",
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 -- stop this model and retain other lanes
            state.update(state="paused", error_type=type(exc).__name__, error=str(exc))
        state["finished_at"] = now()
        atomic(status_path, state)
        return state

    with (
        FileLock(str(RUNS) + ".lock", timeout=0),
        ThreadPoolExecutor(max_workers=11) as pool,
    ):
        results = list(pool.map(lane, f["models"]))
    immutable_json(RUNS / "run-result.json", {"at": now(), "models": results})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        main()
    else:
        f = prepare()
        print(
            json.dumps(
                {
                    "prepared": True,
                    "models": len(f["models"]),
                    "reused": sum(map(len, f["reused_smokes"].values())),
                    "paid_new_limit": f["paid_new_actor_limit"],
                    "paid_token_price_upper_bound_usd": f[
                        "paid_token_price_upper_bound_usd"
                    ],
                }
            )
        )
