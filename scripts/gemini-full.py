"""Authorized Gemini native-harness cohort, including the two validated smokes."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)

from study.api_validation import score_response
from study.expansion_ledger import Ledger, digest, immutable_json, now, usage
from study.expansion_parallel import bounded_trials
from study.expansion_setup import artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import invoke, response_request
from study.pilot import verify_sources
from study.siwc import output_text

RUNS = ROOT / "experiments/gemini-full-v1"
SMOKES = ROOT / "experiments/gemini-native-smoke-v2"
MANIFEST = ROOT / "configs/gemini-full-v1.json"
CLI = Path(os.environ["LOCALAPPDATA"]) / "agy/bin/agy.exe"
MODELS = ["gemini-4-argon-medium", "gemini-3.8-flash-medium"]
JUDGE_RATES = {"input": 10, "output": 50, "cached_input": 1}
PRINT = threading.Lock()
PACE = threading.Lock()
NEXT = 0.0


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def pace():
    global NEXT
    with PACE:
        delay = max(0, NEXT - time.monotonic())
        if delay:
            time.sleep(delay)
        NEXT = time.monotonic() + 5


def cid(model):
    return f"gemini-native/{model}/v1"


def grader_request(prompt, text):
    messages = [
        {"role": m.role, "content": m.content}
        for m in MurderClassifier().get_grader_input(prompt["email"], text)
    ]
    return response_request("gpt-6-astra", messages, "siwc")


def validate_capture(events, model, tools):
    initial = [e for e in events if e.get("event") == "init"]
    results = [e["result"] for e in events if e.get("event") == "result"]
    if len(initial) != 1 or len(results) != 1:
        raise ValueError("Missing unique native completion")
    first, result = initial[0], results[0]
    if first["init"]["model"] != model or first["init"]["agent"] != "datacenter-study":
        raise ValueError("Native model or agent mismatch")
    if (
        sorted(first["init"].get("tools", [])) != tools
        or first["init"].get("permission_mode") != "strict"
    ):
        raise ValueError("Native tool catalog or permission mode changed")
    if (
        result["status"] != "SUCCESS"
        or result["num_turns"] != 1
        or result["conversation_id"] != first["conversation_id"]
    ):
        raise ValueError("Missing successful single-turn identity")
    steps = [e["step_update"] for e in events if e.get("event") == "step_update"]
    if any(s["step_type"] not in {"user_input", "agent_response"} for s in steps):
        raise ValueError("Unexpected native tool action; not a scored safe result")
    text = "".join(
        s.get("text_delta", "") for s in steps if s["step_type"] == "agent_response"
    )
    if not text.strip() or text != result["response"]:
        raise ValueError("Terminal response differs from stream")
    return text, result


def prepare():
    if MANIFEST.exists():
        return read(MANIFEST)
    baseline = read(ROOT / "configs/closeout-smoke-v1.json")
    models = []
    for model in MODELS:
        d = SMOKES / model
        planned, validated, score = [
            read(d / name)
            for name in ["planned-request.json", "validated.json", "score.json"]
        ]
        if planned["prompt"] != baseline["prompts"]["explicit_replacement"]:
            raise ValueError("Smoke prompt mismatch")
        if hashlib.sha256(CLI.read_bytes()).hexdigest() != planned["cli_sha256"]:
            raise ValueError("Antigravity binary changed since smoke")
        agent = (d / "workspace/.agents/agents/datacenter-study.md").read_text("utf-8")
        if not agent.endswith(planned["prompt"]["messages"][0]["content"]):
            raise ValueError("Smoke system instructions differ")
        header = agent[: -len(planned["prompt"]["messages"][0]["content"])]
        events = [
            json.loads(x) for x in (d / "stream.jsonl").read_text("utf-8").splitlines()
        ]
        text, terminal = validate_capture(
            events, model, sorted(validated["native_tools_advertised"])
        )
        jr = read(d / "judge/receipt.json")
        if jr["request"] != grader_request(planned["prompt"], text):
            raise ValueError("Smoke judge request differs")
        verdict = output_text(assembled_response(jr, d / "judge"))
        if score["scores"] != score_response(text, verdict)["original"]:
            raise ValueError("Smoke score mismatch")
        models.append(
            {
                "model": model,
                "cid": cid(model),
                "workers": 4,
                "settings": planned["settings"],
                "agent_header": header,
                "advertised_tools": sorted(validated["native_tools_advertised"]),
                "smoke_score": artifact(d / "score.json"),
                "smoke_actor": artifact(d / "validated.json"),
                "smoke_judge": artifact(d / "judge/receipt.json"),
                "cli_sha256": planned["cli_sha256"],
                "release_date": "",
                "release_date_note": "not pinned; native selector identity preserved",
            }
        )
    f = {
        "created_at": now(),
        "authorization": "User explicitly requested full Gemini 4 Argon and Gemini 3.8 Flash runs after native-harness smoke results and tool-catalog limitation were disclosed.",
        "models": models,
        "target_per_condition": 100,
        "target": 1200,
        "reused_smokes": 2,
        "prompts": baseline["prompts"],
        "source_hashes": verify_sources(),
        "effort": "medium",
        "prod": False,
        "paid_credit_fallback": False,
        "actor_retries": 0,
        "judge_interval_seconds": 5,
        "judge_attempts": 3,
        "judge_retry_policy": "Only explicit OpenAI 429 with no response/terminal; preserve every labelled receipt",
        "harness": "Antigravity native primary custom agent; advertised default tool catalog; strict permissions; no executed native tools accepted; native wire system not exposed. Separate cohort, same harness as smokes.",
        "native_reasoning": "Thinking-token usage recorded; separate readable native summary not exposed. Visible scratchpad remains ordinary response text.",
        "sources": [
            artifact(p)
            for p in [
                Path(__file__),
                ROOT / "study/api_validation.py",
                ROOT / "study/expansion_transport.py",
                ROOT / "study/expansion_stream.py",
                ROOT / "study/expansion_ledger.py",
                ROOT / "study/siwc.py",
                ROOT / "study/expansion_parallel.py",
            ]
        ],
    }
    for a in f["sources"]:
        p = ROOT / a["path"]
        dest = RUNS / "executed-source" / a["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            stream.write(p.read_bytes())
    immutable_json(MANIFEST, f)
    return f


def actor(f, m, prompt, directory):
    if (directory / "request.json").exists():
        raise ValueError("Prior native attempt; reconcile without repeating")
    workspace = directory / "workspace"
    agent = workspace / ".agents/agents/datacenter-study.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(
        m["agent_header"] + prompt["messages"][0]["content"], encoding="utf-8"
    )
    immutable_json(directory / "settings.json", m["settings"])
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_GENAI_USE_VERTEXAI",
        }
    }
    args = [
        str(CLI),
        "--model",
        m["model"],
        "--effort",
        "medium",
        "--agent",
        "datacenter-study",
        "--settings",
        str((directory / "settings.json").resolve()),
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "10m",
        "-p",
        prompt["messages"][1]["content"],
    ]
    immutable_json(
        directory / "request.json",
        {
            "at": now(),
            "argv": args,
            "stdin": prompt["messages"][1]["content"],
            "prompt": prompt,
            "agent": artifact(agent),
            "manifest": artifact(MANIFEST),
        },
    )
    receipt = {
        "started_at": now(),
        "status": "uncertain",
        "model": m["model"],
        "auth": "gemini_subscription",
        "credit_fallback": False,
    }
    try:
        with (
            (directory / "stream.jsonl").open("xb") as out,
            (directory / "stderr.txt").open("xb") as err,
        ):
            proc = subprocess.Popen(
                args,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
            )
            try:
                proc.communicate(prompt["messages"][1]["content"].encode(), timeout=660)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
        receipt["exit_code"] = proc.returncode
        events = [
            json.loads(x)
            for x in (directory / "stream.jsonl").read_text("utf-8").splitlines()
        ]
        if proc.returncode:
            raise ValueError("Native CLI exited without success")
        text, result = validate_capture(events, m["model"], m["advertised_tools"])
        receipt.update(
            status="completed",
            visible_text=text,
            usage=result["usage"],
            response_id=result["conversation_id"],
            native_tools_executed=[],
            native_reasoning_summary=None,
        )
    except Exception as exc:  # noqa: BLE001 -- preserve native failures; never silently repeat actors
        receipt.update(error_type=type(exc).__name__, error=str(exc))
    receipt["finished_at"] = now()
    immutable_json(directory / "receipt.json", receipt)
    return receipt


def normalized_native(u):
    return {
        "input": u.get("input_tokens"),
        "output": u.get("output_tokens"),
        "cache_read": u.get("cache_read_tokens", 0),
        "reasoning_subset": u.get("thinking_tokens", 0),
    }


def record_call(
    ledger, m, tid, role, receipt, path, condition, sample, *, suffix="", replaces=None
):
    auth = "gemini_subscription" if role == "actor" else "siwc"
    body = {
        "role": role,
        "status": receipt["status"],
        "auth": auth,
        "tier": "subscription",
        "rates": None if role == "actor" else JUDGE_RATES,
        "usage": normalized_native(receipt.get("usage") or {})
        if role == "actor"
        else usage(receipt.get("response") or {}),
        "condition": condition,
        "sample": sample,
        "artifact": artifact(path),
        "configuration_sha256": digest(read(MANIFEST)),
        "phase": "gemini-native-full",
    }
    if replaces:
        body["replaces"] = replaces
    eid = f"{m['cid']}/{tid}/{role}{suffix}/call"
    ledger.event(eid, m["cid"], tid, "call", body)
    return eid


def trial(f, m, condition, sample):
    ledger = Ledger()
    tid = f"{condition}-{sample:03d}"
    directory = RUNS / m["model"] / tid
    p = f["prompts"][condition]
    ledger.reserve(
        m["cid"],
        tid,
        "actor",
        {
            "model": m["model"],
            "prompt": p,
            "effort": "medium",
            "settings": m["settings"],
            "manifest": digest(f),
        },
    )
    r = actor(f, m, p, directory / "actor")
    record_call(
        ledger, m, tid, "actor", r, directory / "actor/receipt.json", condition, sample
    )
    if r["status"] != "completed":
        raise RuntimeError("Native actor failed; no retry; inspect receipt")
    text = r["visible_text"]
    request = grader_request(p, text)
    replaces = None
    for attempt in range(3):
        suffix = "" if attempt == 0 else f"/rate-limit-recovery-{attempt:02d}"
        dest = directory / ("judge" + suffix)
        pace()
        ledger.reserve(m["cid"], tid, "judge" + suffix, request)
        jr = invoke(request, "siwc", dest)
        eid = record_call(
            ledger,
            m,
            tid,
            "judge",
            jr,
            dest / "receipt.json",
            condition,
            sample,
            suffix=suffix,
            replaces=replaces,
        )
        if jr["status"] == "completed":
            raw = assembled_response(jr, dest)
            if raw.get("model") != "gpt-6-astra":
                raise ValueError("Judge model mismatch")
            result = {
                "condition": condition,
                "sample": sample,
                "scores": score_response(text, output_text(raw))["original"],
                "configuration_sha256": digest(f),
                "harness": "Antigravity native",
                "actor": artifact(directory / "actor/receipt.json"),
                "judge": artifact(dest / "receipt.json"),
            }
            immutable_json(directory / "score.json", result)
            ledger.event(f"{m['cid']}/{tid}/score", m["cid"], tid, "score", result)
            with PRINT:
                print(
                    json.dumps(
                        {"at": now(), "model": m["model"], "trial": tid, "scored": True}
                    ),
                    flush=True,
                )
            return
        if (
            jr.get("http_status") != 429
            or jr.get("response")
            or jr.get("terminal_event")
        ):
            raise RuntimeError(
                "Judge incomplete; preserve completed actor for reconciliation"
            )
        replaces = eid
        delay = float(jr.get("rate_limit_headers", {}).get("retry-after", 30))
        time.sleep(min(60, max(15, delay + 2)))
    raise RuntimeError("Judge repeatedly rate limited; pause and preserve actor")


def setup_ledger(f):
    ledger = Ledger()
    for m in f["models"]:
        ledger.config(
            {
                "id": m["cid"],
                "model": m["model"],
                "label": m["model"],
                "vendor": "Google",
                "release_date": m["release_date"],
                "target": 600,
                "auth": "gemini_subscription",
                "tier": "subscription",
                "rates": None,
                "judge_auth": "siwc",
                "judge_tier": "subscription",
                "judge_rates": JUDGE_RATES,
                "judge_model": "gpt-6-astra",
                "forecast": {
                    "actor": {"input": 4800, "output": 4000},
                    "judge": {"input": 2000, "output": 160},
                },
                "section": "Gemini native harness",
                "state": "queued",
                "configuration_sha256": digest(f),
                "manifest": MANIFEST.relative_to(ROOT).as_posix(),
                "profile": "Original optional scratchpad; medium; Antigravity native harness; default tools advertised, no tool actions accepted; API-equivalent value unknown",
                "retention": "raw CLI request, stream, terminal receipt and scorer retained",
            }
        )
        d = SMOKES / m["model"]
        tid = "explicit_replacement-001"
        a = read(d / "validated.json")
        jr = read(d / "judge/receipt.json")
        record_call(
            ledger,
            m,
            tid,
            "actor",
            {**a, "status": "completed"},
            d / "validated.json",
            "explicit_replacement",
            1,
        )
        record_call(
            ledger,
            m,
            tid,
            "judge",
            jr,
            d / "judge/receipt.json",
            "explicit_replacement",
            1,
        )
        original = read(d / "score.json")
        score = {
            "condition": "explicit_replacement",
            "sample": 1,
            "scores": original["scores"],
            "configuration_sha256": digest(f),
            "imported_smoke": artifact(d / "score.json"),
            "harness": "Antigravity native; user-authorized continuation of same setup",
        }
        ledger.event(f"{m['cid']}/{tid}/score", m["cid"], tid, "score", score)


def main():
    f = prepare()
    for a in f["sources"]:
        if artifact(ROOT / a["path"]) != a:
            raise ValueError("Frozen source drift")
    if verify_sources() != f["source_hashes"] or any(
        hashlib.sha256(CLI.read_bytes()).hexdigest() != m["cli_sha256"]
        for m in f["models"]
    ):
        raise ValueError("Prompt/classifier/CLI drift")
    with FileLock(str(RUNS) + ".lock", timeout=0):
        setup_ledger(f)
        immutable_json(
            RUNS / "launch.json",
            {
                "pid": os.getpid(),
                "started_at": now(),
                "target": 1200,
                "reused_smokes": 2,
                "manifest": artifact(MANIFEST),
            },
        )

        def lane(m):
            state = {
                "model": m["model"],
                "state": "running",
                "workers": m["workers"],
                "started_at": now(),
            }
            path = RUNS / "lanes" / (m["model"] + ".json")
            atomic(path, state)
            jobs = [
                (c, n)
                for n in range(1, 101)
                for c in f["prompts"]
                if (c, n) != ("explicit_replacement", 1)
            ]
            result = bounded_trials(
                jobs,
                lambda item: trial(f, m, *item),
                m["workers"],
                stopped=lambda: (RUNS / "STOP").exists(),
            )
            state.update(
                state="paused"
                if result["errors"] or result["stopped"]
                else "completed",
                finished_at=now(),
                result=result,
            )
            atomic(path, state)
            return state

        with ThreadPoolExecutor(max_workers=2) as pool:
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
                    "models": [m["model"] for m in f["models"]],
                    "target": 1200,
                    "reused": 2,
                }
            )
        )
