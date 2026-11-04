"""Resume all cohorts with bounded retries, exact settings and a faster shared pacer."""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock

from study.api_validation import score_response
from study.claude_subscription_closeout import binary_identity
from study.claude_subscription_closeout import invoke as claude_call
from study.expansion_ledger import Ledger, digest, immutable_json, now, usage
from study.expansion_parallel import bounded_trials
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke
from study.pilot import verify_sources
from study.siwc import output_text


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


full = module("frozen_full", "scripts/closeout-full.py")
gemini = module("frozen_gemini", "scripts/gemini-full.py")
RUNS = ROOT / "experiments/resilient-closeout-v2"
MANIFEST = ROOT / "configs/resilient-closeout-v2.json"
OAI_INFLIGHT = threading.BoundedSemaphore(64)
OUTPUT = threading.Lock()


def failure_kind(r):
    """Classify operational failures, never inspect whether a valid answer is harmful."""
    if r.get("status") == "completed":
        return "completed"
    err = (r.get("response") or {}).get("error") or r.get("provider_error") or {}
    description = (json.dumps(err) + " " + str(r.get("error", ""))).lower()
    if any(
        x in description
        for x in [
            "insufficient_quota",
            "billing",
            "credit balance",
            "misalignment_policy_violation",
            "reasoning_extraction",
            "model mismatch",
            "model substitution",
            "catalog",
            "permission mode changed",
        ]
    ):
        return "permanent"
    if (
        r.get("http_status") in {400, 401, 402, 403, 404, 422}
        or r.get("status") == "model_mismatch"
    ):
        return "permanent"
    if any(
        c.get("finish_reason") == "length"
        for c in (r.get("response") or {}).get("choices", [])
    ):
        return "protocol"
    if "unexpected native tool action" in description:
        return "protocol"
    if r.get("http_status") in {408, 409, 425, 429, 500, 502, 503, 504}:
        return "transient"
    if any(
        x in description
        for x in ["overload", "temporarily", "rate limit", "timeout", "timed out"]
    ):
        return "transient"
    if r.get("error_type") in {
        "JSONDecodeError",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "TimeoutExpired",
        "ConnectError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "ProtocolError",
        "ConnectionError",
    }:
        return "transient"
    if r.get("status") in {"failed", "incomplete", "uncertain"} and r.get("auth") in {
        "siwc",
        "openrouter",
    }:
        return "transient"
    return "permanent"


def retry_after_seconds(r, current=None):
    current = time.time() if current is None else current
    headers = {
        **r.get("rate_limit_headers", {}),
        **(((r.get("response") or {}).get("error") or {}).get("headers") or {}),
    }
    hint = headers.get("retry-after")
    delay = None
    if hint is not None:
        try:
            delay = float(hint)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(str(hint)).timestamp() - current
                return max(0, delay)
            except (ValueError, TypeError, OverflowError):
                pass
    if delay is None and headers.get("retry-after-ms") is not None:
        try:
            delay = float(headers["retry-after-ms"]) / 1000
        except (ValueError, TypeError):
            pass
    if delay is None:
        return 0
    finished = r.get("finished_at")
    elapsed = (
        max(0, current - datetime.fromisoformat(finished).timestamp())
        if finished
        else 0
    )
    return max(0, delay - elapsed)


class Pacer:
    def __init__(self):
        self.lock = threading.Lock()
        self.interval = 0.35
        self.next = 0.0
        self.clean = 0

    def wait(self):
        with self.lock:
            delay = max(0, self.next - time.monotonic())
            if delay:
                time.sleep(delay)
            self.next = time.monotonic() + self.interval

    def observe(self, r):
        with self.lock:
            if r.get("http_status") == 429:
                self.interval = min(2.8, self.interval * 1.5)
                self.next = max(self.next, time.monotonic() + retry_after_seconds(r))
                self.clean = 0
            elif r.get("status") == "completed":
                self.clean += 1
                if self.clean >= 100:
                    self.interval = max(0.35, self.interval * 0.9)
                    self.clean = 0
            full.atomic(
                RUNS / "pacing.json",
                {
                    "at": now(),
                    "target_interval": 0.35,
                    "current_interval": self.interval,
                    "max_inflight": 64,
                },
            )


PACER = Pacer()


class PaidRetryBudget:
    def __init__(self):
        self.lock = threading.Lock()
        self.reserved = 0.0

    def reserve(self, m):
        bound = (20000 * m["rates"]["input"] + 10000 * m["rates"]["output"]) / 1e6
        with self.lock:
            if self.reserved + bound > 10:
                raise PermanentFailure(
                    "Paid retry allowance exhausted ($10 conservative reservation)"
                )
            self.reserved += bound
            full.atomic(
                RUNS / "paid-retry-budget.json",
                {"at": now(), "limit": 10, "reserved_upper_bound_usd": self.reserved},
            )


PAID = PaidRetryBudget()


class PermanentFailure(RuntimeError):
    pass


class TrialExhausted(RuntimeError):
    pass


def prepare():
    if MANIFEST.exists():
        return read(MANIFEST)
    primary, google = read(full.MANIFEST), read(gemini.MANIFEST)
    if (
        verify_sources() != primary["source_hashes"]
        or verify_sources() != google["source_hashes"]
    ):
        raise ValueError("Prompt/classifier drift")
    if binary_identity() != primary["cli"]:
        raise ValueError("Claude binary drift")
    if any(
        hashlib.sha256(gemini.CLI.read_bytes()).hexdigest() != m["cli_sha256"]
        for m in google["models"]
    ):
        raise ValueError("Gemini binary drift")
    models = [
        {
            **m,
            "cohort": "primary",
            "workers": 16 if m["auth"] == "siwc" else m["workers"],
        }
        for m in primary["models"]
    ]
    models += [
        {**m, "cohort": "google", "auth": "gemini_subscription", "rates": None}
        for m in google["models"]
    ]
    f = {
        "created": now(),
        "authorization": "Restart all paused runs, double OAI speed, add a retry policy.",
        "models": models,
        "primary_manifest": artifact(full.MANIFEST),
        "google_manifest": artifact(gemini.MANIFEST),
        "scientific_settings_changed": False,
        "oai_start_interval_seconds": 0.35,
        "prior_interval_seconds": 0.7,
        "openai_workers_per_model": 16,
        "oai_max_inflight": 64,
        "transient_attempts_max": 5,
        "protocol_attempts_max": 3,
        "call_retry_wall_limit_seconds": 1800,
        "paid_retry_upper_bound_allowance_usd": 10,
        "retryable_protocols": ["truncated output", "unexpected native tool action"],
        "retry_policy": "Reuse every completed actor/judge. Retry transient network, decoding, overload and rate-limit failures with exponential backoff and jitter; honor Retry-After including terminal-event headers. Invalid protocol completions get labelled replacements under identical controls, with attrition retained. Never retry valid harmful/safe/refusal scores. Exhausted trial leaves a gap and remaining trials continue; auth/billing/model/setting errors pause that model.",
        "sources": [
            artifact(p)
            for p in [
                Path(__file__),
                ROOT / "scripts/closeout-smoke.py",
                ROOT / "scripts/closeout-full.py",
                ROOT / "scripts/gemini-full.py",
                ROOT / "study/claude_subscription_closeout.py",
                ROOT / "study/expansion_transport.py",
                ROOT / "study/expansion_stream.py",
                ROOT / "study/api_validation.py",
                ROOT / "study/expansion_ledger.py",
                ROOT / "study/expansion_parallel.py",
            ]
        ],
    }
    for a in f["sources"]:
        dest = RUNS / "executed-source" / a["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            stream.write((ROOT / a["path"]).read_bytes())
    immutable_json(MANIFEST, f)
    return f


def extract(r, directory, auth, model):
    if auth in {"claude_subscription", "gemini_subscription"}:
        if r.get("returned_model", r.get("model", model)) != model:
            raise PermanentFailure("Returned native model mismatch")
        text = r["visible_text"]
        native = r.get("native_reasoning_summary")
        if auth == "claude_subscription":
            native = [
                c.get("thinking", "")
                for a in r.get("assistant_messages", [])
                for c in a.get("content", [])
                if c.get("type") == "thinking"
            ]
    elif auth == "openrouter":
        raw = r["response"]
        text = full.base.checked_completion(
            raw,
            model,
            r["request"]["provider"]["only"][0].split("/")[0].title()
            if model.startswith("deepseek/")
            else "xAI",
        )
        native = {
            k: raw["choices"][0]["message"].get(k)
            for k in ["reasoning", "reasoning_details"]
        }
    else:
        raw = assembled_response(r, directory)
        if raw.get("model") != model:
            raise PermanentFailure("Returned model mismatch")
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
        raise PermanentFailure("Completed response contained no visible text")
    return text, native


def get_call(f, m, condition, sample, role, request, prompt, history, key):
    ledger = Ledger()
    cid = m["cid"]
    tid = f"{condition}-{sample:03d}"
    auth = m["auth"] if role == "actor" else "siwc"
    model = m["model"] if role == "actor" else "gpt-6-astra"
    for e in reversed(history):
        if e["body"].get("status") == "completed":
            path = ROOT / e["body"]["artifact"]["path"]
            if artifact(path) != e["body"]["artifact"]:
                raise PermanentFailure("Saved receipt hash differs; cannot reuse")
            r = read(path)
            if auth in {"siwc", "openrouter"} and r["request"] != request:
                raise PermanentFailure("Saved request differs; cannot reuse")
            if auth == "claude_subscription" and any(
                r["request"].get(k) != request[k]
                for k in ["model", "messages", "effort"]
            ):
                raise PermanentFailure("Saved Claude settings differ; cannot reuse")
            if (
                auth == "gemini_subscription"
                and read(path.parent / "request.json")["prompt"] != prompt
            ):
                raise PermanentFailure("Saved Gemini prompt differs; cannot reuse")
            return extract(r, path.parent, auth, model), path, e["id"]
    failed = [e["id"] for e in history]
    previous = read(ROOT / history[-1]["body"]["artifact"]["path"]) if history else None
    began = time.monotonic()
    new_attempts = 0
    while True:
        category = failure_kind(previous) if previous else None
        if category == "permanent":
            raise PermanentFailure(
                "Non-retryable provider/configuration error; see retained receipt"
            )
        cap = 3 if category == "protocol" else 5
        if len(history) + new_attempts >= cap or time.monotonic() - began > 1800:
            raise TrialExhausted(f"{role}: retry limit reached ({category})")
        number = len(history) + new_attempts + 1
        dest = (
            RUNS / m["model"].replace("/", "__") / tid / role / f"attempt-{number:03d}"
        )
        if previous:
            delay = max(
                retry_after_seconds(previous),
                min(60, 2 ** min(number, 6)) + random.uniform(0, 2),
            )
            if time.monotonic() - began + delay > 1800:
                raise TrialExhausted(
                    "Retry-After exceeds this call's 30-minute retry window"
                )
            ledger.event(
                f"{cid}/{tid}/{role}/resilient-v2-{number:03d}/retry",
                cid,
                tid,
                "retry",
                {
                    "role": role,
                    "reason": category,
                    "delay_seconds": delay,
                    "replaces": failed[-1] if failed else None,
                    "phase": "resilient-v2",
                },
            )
            time.sleep(delay)
        if auth == "openrouter" and (history or new_attempts):
            PAID.reserve(m)
        role_id = f"{role}/resilient-v2-{number:03d}"
        ledger.reserve(cid, tid, role_id, request)
        if auth == "siwc":
            with OAI_INFLIGHT:
                PACER.wait()
                r = invoke(request, auth, dest)
                PACER.observe(r)
        elif auth == "openrouter":
            r = full.base.router_call(request, dest, m, key)
        elif auth == "claude_subscription":
            r = claude_call(m["model"], prompt["messages"], dest, effort="medium")
        else:
            r = gemini.actor(read(gemini.MANIFEST), m, prompt, dest)
        raw = r.get("response") or {}
        u = r.get("usage") or {}
        if auth == "claude_subscription":
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
        elif auth == "gemini_subscription":
            normalized = gemini.normalized_native(u)
        else:
            normalized = usage(raw)
        body = {
            "role": role,
            "status": r["status"],
            "auth": auth,
            "tier": "standard" if auth == "openrouter" else "subscription",
            "rates": m["rates"] if role == "actor" else full.JUDGE_RATES,
            "usage": normalized,
            "reported_usd": (raw.get("usage") or {}).get("cost"),
            "artifact": artifact(dest / "receipt.json"),
            "condition": condition,
            "sample": sample,
            "phase": "resilient-v2",
            "authorization": artifact(MANIFEST),
            "request_sha256": digest(request),
            "failure_category": failure_kind(r),
            "replaces_all": failed.copy(),
            "truncation": any(
                c.get("finish_reason") == "length" for c in raw.get("choices", [])
            ),
        }
        if failed:
            body["replaces"] = failed[-1]
        if auth == "claude_subscription" and r.get("api_equivalent_cost") is not None:
            body["subscription_equivalent_usd"] = r["api_equivalent_cost"]
        eid = f"{cid}/{tid}/{role_id}/call"
        ledger.event(eid, cid, tid, "call", body)
        new_attempts += 1
        if r["status"] == "completed":
            value = extract(r, dest, auth, model)
            immutable_json(
                dest / "extracted.json",
                {"visible_text": value[0], "native_reasoning": value[1]},
            )
            return value, dest / "receipt.json", eid
        failed.append(eid)
        previous = r


def trial(f, m, condition, sample, calls, key):
    primary = read(full.MANIFEST)
    google = read(gemini.MANIFEST)
    prompt = (
        google["prompts"][condition]
        if m["cohort"] == "google"
        else full.prompt_for(primary, m, condition)
    )
    req = (
        {
            "model": m["model"],
            "prompt": prompt,
            "effort": "medium",
            "settings": m["settings"],
        }
        if m["cohort"] == "google"
        else {
            "model": m["model"],
            "messages": prompt["messages"],
            "effort": "medium",
            "thinking_display": "summarized",
        }
        if m["auth"] == "claude_subscription"
        else full.base.actor_request(m, prompt)
    )
    tid = f"{condition}-{sample:03d}"
    actor, apath, aid = get_call(
        f,
        m,
        condition,
        sample,
        "actor",
        req,
        prompt,
        calls[(m["cid"], tid, "actor")],
        key,
    )
    jreq = full.judge_request(primary, prompt, actor[0])
    judge, jpath, jid = get_call(
        f,
        m,
        condition,
        sample,
        "judge",
        jreq,
        prompt,
        calls[(m["cid"], tid, "judge")],
        key,
    )
    result = {
        "condition": condition,
        "sample": sample,
        "scores": score_response(actor[0], judge[0])["original"],
        "authorization": artifact(MANIFEST),
        "actor": artifact(apath),
        "judge": artifact(jpath),
        "calls": {"actor": aid, "judge": jid},
        "harness": m["cohort"],
    }
    dest = RUNS / m["model"].replace("/", "__") / tid
    immutable_json(dest / "score.json", result)
    Ledger().event(f"{m['cid']}/{tid}/score", m["cid"], tid, "score", result)
    with OUTPUT:
        print(
            json.dumps(
                {"at": now(), "model": m["model"], "trial": tid, "scored": True}
            ),
            flush=True,
        )


def main():
    f = prepare()
    for a in f["sources"]:
        if artifact(ROOT / a["path"]) != a:
            raise ValueError("Frozen source drift")
    # Never overlap old workers: their logical trials and receipts must be settled.
    for directory in [full.RUNS, gemini.RUNS]:
        if any(
            read(p).get("state") == "running"
            for p in (directory / "lanes").glob("*.json")
        ):
            raise RuntimeError("Prior workers still draining")
    ledger = Ledger()
    _, events = ledger.snapshot()
    calls = defaultdict(list)
    for e in events:
        if e["kind"] == "call":
            calls[(e["config"], e["trial"], e["body"]["role"])].append(e)
    selected = {m["cid"] for m in f["models"]}
    done = {
        (e["config"], e["body"]["condition"], e["body"]["sample"])
        for e in events
        if e["kind"] == "score" and e["config"] in selected
    }
    # A reserved call with no receipt is an ambiguous interruption, never an implicit clean retry.
    recorded = {e["id"].rsplit("/", 1)[0] for e in events if e["kind"] == "call"}
    gaps = [
        e["id"]
        for e in events
        if e["config"] in selected
        and e["kind"] == "reserved"
        and e["id"].rsplit("/", 1)[0] not in recorded
    ]
    if gaps:
        raise RuntimeError(
            "Unreceipted reservations need reconciliation: " + str(gaps[:3])
        )
    from study.credentials import read_key

    key = read_key("openrouter")
    with FileLock(str(RUNS) + ".lock", timeout=0):
        launch = {
            "pid": os.getpid(),
            "started_at": now(),
            "manifest": artifact(MANIFEST),
        }
        immutable_json(RUNS / "launch.json", launch)
        for m in f["models"]:
            immutable_json(
                full.RUNS
                / "model-restarts"
                / ("resilient-v2-" + m["model"].replace("/", "__") + ".json"),
                {
                    **launch,
                    "cid": m["cid"],
                    "directory": RUNS.relative_to(ROOT).as_posix(),
                },
            )

        def lane(m):
            path = RUNS / "lanes" / (m["model"].replace("/", "__") + ".json")
            state = {
                "model": m["model"],
                "cid": m["cid"],
                "state": "running",
                "workers": m["workers"],
                "started_at": now(),
            }
            full.atomic(path, state)
            cohort = (
                read(gemini.MANIFEST)
                if m["cohort"] == "google"
                else read(full.MANIFEST)
            )
            jobs = [
                (c, n)
                for n in range(1, 101)
                for c in cohort["prompts"]
                if (m["cid"], c, n) not in done
            ]
            exhausted = []
            lock = threading.Lock()

            def work(item):
                try:
                    trial(f, m, *item, calls, key)
                except TrialExhausted as exc:
                    record = {
                        "condition": item[0],
                        "sample": item[1],
                        "error": str(exc),
                        "at": now(),
                    }
                    immutable_json(
                        RUNS
                        / m["model"].replace("/", "__")
                        / f"{item[0]}-{item[1]:03d}"
                        / "exhausted.json",
                        record,
                    )
                    with lock:
                        exhausted.append(record)

            result = bounded_trials(
                jobs, work, m["workers"], stopped=lambda: (RUNS / "STOP").exists()
            )
            state.update(
                state="paused"
                if result["errors"] or result["stopped"]
                else "finished_with_gaps"
                if exhausted
                else "completed",
                finished_at=now(),
                result=result,
                exhausted=exhausted,
            )
            full.atomic(path, state)
            return state

        with ThreadPoolExecutor(max_workers=13) as pool:
            results = list(pool.map(lane, f["models"]))
        immutable_json(RUNS / "result.json", {"at": now(), "models": results})


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
                    "interval": f["oai_start_interval_seconds"],
                }
            )
        )
