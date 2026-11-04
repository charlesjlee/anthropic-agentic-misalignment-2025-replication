"""Resume frozen scientific settings with explicit 429-only recovery and pacing."""

import importlib.util
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock

from study.api_validation import score_response
from study.claude_subscription_closeout import invoke as claude_call
from study.expansion_ledger import Ledger, digest, immutable_json, now, usage
from study.expansion_parallel import bounded_trials
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke
from study.siwc import output_text

spec = importlib.util.spec_from_file_location("full", ROOT / "scripts/closeout-full.py")
full = importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)
RUNS = full.RUNS
AMENDMENT = RUNS / "pacing-amendment.json"
JUDGE_RATES = full.JUDGE_RATES
base = full.base
prompt_for = full.prompt_for
judge_request = full.judge_request
PRINT = threading.Lock()


class Pacer:
    def __init__(self):
        self.lock = threading.Lock()
        self.next = 0.0
        self.interval = 0.7

    def wait(self):
        while True:
            with self.lock:
                delay = self.next - time.monotonic()
                if delay <= 0:
                    self.next = time.monotonic() + self.interval
                    return
            time.sleep(min(delay, 1))

    def throttle(self, receipt):
        try:
            delay = float(receipt.get("rate_limit_headers", {}).get("retry-after", 30))
        except ValueError:
            delay = 30
        with self.lock:
            self.interval = min(5, self.interval * 1.25)
            self.next = max(self.next, time.monotonic() + max(delay, 15) + 2)


PACER = Pacer()


def recoverable(receipt):
    return (
        receipt.get("auth") == "siwc"
        and receipt.get("http_status") == 429
        and not receipt.get("response")
        and not receipt.get("terminal_event")
    )


def obtain(
    ledger, cid, tid, role, request, auth, m, p, directory, key, f, condition, sample
):
    replaces = None
    for attempt in range(3):
        dest = (
            directory
            if attempt == 0
            else directory / f"rate-limit-recovery-{attempt:02d}"
        )
        eid = (
            f"{cid}/{tid}/{role}/call"
            if attempt == 0
            else f"{cid}/{tid}/{role}/rate-limit-recovery-{attempt:02d}/call"
        )
        if (dest / "receipt.json").exists():
            r = read(dest / "receipt.json")
            if auth != "claude_subscription" and r["request"] != request:
                raise ValueError("Recovery request changed")
            if r["status"] == "completed":
                return r, dest
            if not recoverable(r):
                raise RuntimeError(
                    "Non-429 failure needs review; no automatic replacement"
                )
            replaces = eid
            continue
        if (dest / "request.json").exists():
            raise RuntimeError("Unreceipted attempt; do not resubmit")
        if auth == "siwc":
            PACER.wait()
        role_id = role if attempt == 0 else f"{role}/rate-limit-recovery-{attempt:02d}"
        ledger.reserve(cid, tid, role_id, request)
        if auth == "siwc":
            with full.SIWC:
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
            "phase": "full-paced",
            "authorization_sha256": digest(f),
            "request_sha256": digest(request),
            "truncation": any(
                x.get("finish_reason") == "length" for x in raw.get("choices", [])
            ),
            "known_unbilled": recoverable(r),
            "amendment": artifact(AMENDMENT),
        }
        if replaces:
            body["replaces"] = replaces
        if auth == "claude_subscription" and r.get("api_equivalent_cost") is not None:
            body["subscription_equivalent_usd"] = r["api_equivalent_cost"]
        ledger.event(eid, cid, tid, "call", body)
        if r["status"] == "completed":
            return r, dest
        if not recoverable(r):
            raise RuntimeError(
                f"{role}: {r['status']}; no automatic non-429 replacement"
            )
        PACER.throttle(r)
        replaces = eid
    raise RuntimeError("Three 429 rejections; lane paused")


def main():
    f = read(full.MANIFEST)
    # All actor/judge and prompt files stay exactly as frozen for wave one.
    for a in f["source_files"]:
        if artifact(ROOT / a["path"]) != a:
            raise ValueError("Source drift: " + a["path"])
    if (
        full.binary_identity() != f["cli"]
        or full.verify_sources() != f["source_hashes"]
    ):
        raise ValueError("Scientific configuration drift")
    amendment = {
        "created": now(),
        "reason": "Shared OpenAI 429 rejects, Retry-After 11 seconds. Reuse complete actors; preserve every original request/receipt; new labelled attempts only for explicit 429 without response or terminal event. No paid call retries. Scientific settings unchanged.",
        "initial_openai_interval_seconds": 0.7,
        "max_attempts_per_openai_call": 3,
        "source": artifact(Path(__file__)),
        "original_manifest": artifact(full.MANIFEST),
    }
    immutable_json(AMENDMENT, amendment)
    from study.credentials import read_key

    key = read_key("openrouter")
    ledger = Ledger()
    with FileLock(str(RUNS) + ".lock", timeout=0):
        immutable_json(
            RUNS / "resume-launch.json",
            {
                "pid": os.getpid(),
                "started_at": now(),
                "manifest": artifact(full.MANIFEST),
                "amendment": artifact(AMENDMENT),
            },
        )

        def lane(m):
            path = RUNS / "lanes" / (m["model"].replace("/", "__") + ".json")
            state = {
                "model": m["model"],
                "cid": m["cid"],
                "started_at": now(),
                "state": "running",
                "workers": m["workers"],
                "pacing": "shared OpenAI .7s minimum start interval, adaptive backoff",
            }
            full.atomic(path, state)
            try:
                _, events = ledger.snapshot()
                done = {
                    (e["body"]["condition"], e["body"]["sample"])
                    for e in events
                    if e["config"] == m["cid"] and e["kind"] == "score"
                }
                jobs = [
                    (c, n)
                    for n in range(1, 101)
                    for c in f["prompts"]
                    if (c, n) not in done
                ]
                result = bounded_trials(
                    jobs,
                    lambda item: trial(f, m, *item, key),
                    m["workers"],
                    stopped=lambda: (RUNS / "STOP-PACED").exists(),
                )
                state.update(
                    state="paused"
                    if result["errors"] or result["stopped"]
                    else "completed",
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001 -- preserve independent model failures
                state.update(
                    state="paused", error_type=type(exc).__name__, error=str(exc)
                )
            state["finished_at"] = now()
            full.atomic(path, state)
            return state

        with ThreadPoolExecutor(max_workers=11) as pool:
            results = list(pool.map(lane, f["models"]))
    immutable_json(RUNS / "paced-result.json", {"at": now(), "models": results})


# The trial function below is derived from the archived first-wave implementation;
# only call acquisition changes. Extraction and original primary scoring are identical.
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
        r, dest = obtain(
            ledger, cid, tid, role, request, auth, m, p, dest, key, f, condition, sample
        )
        raw = r.get("response") or {}
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


if __name__ == "__main__":
    main()
