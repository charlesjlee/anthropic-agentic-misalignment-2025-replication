"""Two isolated Antigravity-native smoke calls; no raw OAuth extraction or API fallback."""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from study.expansion_ledger import immutable_json, now
from study.expansion_setup import artifact, read

RUNS = ROOT / "experiments/gemini-native-smoke-v2"
CLI = Path(os.environ["LOCALAPPDATA"]) / "agy/bin/agy.exe"
MODELS = ["gemini-4-argon-medium", "gemini-3.8-flash-medium"]


def prepare():
    p = read(ROOT / "configs/closeout-smoke-v1.json")["prompts"]["explicit_replacement"]
    settings = {
        "toolPermission": "strict",
        "useG1Credits": False,
        "disableSlashCommands": True,
        "notifications": False,
    }
    agent = (
        "---\nname: datacenter-study\ndescription: Single response study task\nmainAgent: true\nsubagent: false\nmodel: inherit\ntools: []\nmcpServers: []\nskills: []\nplugins: []\ncommandExecutionPolicy: off\n---\n"
        + p["messages"][0]["content"]
    )
    for model in MODELS:
        d = RUNS / model
        workspace = d / "workspace"
        a = workspace / ".agents/agents/datacenter-study.md"
        a.parent.mkdir(parents=True, exist_ok=True)
        if a.exists() and a.read_text("utf-8") != agent:
            raise ValueError("Agent definition changed")
        a.write_text(agent, encoding="utf-8")
        immutable_json(d / "settings.json", settings)
        immutable_json(
            d / "planned-request.json",
            {
                "model": model,
                "prompt": p,
                "condition": "explicit_replacement",
                "sample": 1,
                "effort": "medium",
                "settings": settings,
                "agent": artifact(a),
                "cli_sha256": hashlib.sha256(CLI.read_bytes()).hexdigest(),
                "harness": "Antigravity native primary custom agent; raw wire prompt not exposed; tools explicitly empty; fresh conversation; summary visibility subject to CLI output",
                "status": "smoke_only_not_yet_primary_comparable",
            },
        )
    return p


def call(model, p):
    d = RUNS / model
    if (d / "request.json").exists():
        raise RuntimeError("Prior attempt exists; do not repeat")
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
        model,
        "--effort",
        "medium",
        "--agent",
        "datacenter-study",
        "--settings",
        str((d / "settings.json").resolve()),
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "10m",
        "-p",
        p["messages"][1]["content"],
    ]
    immutable_json(
        d / "request.json",
        {
            "at": now(),
            "argv": args,
            "stdin": p["messages"][1]["content"],
            "planned_request": artifact(d / "planned-request.json"),
        },
    )
    result = {
        "model": model,
        "started_at": now(),
        "status": "uncertain",
        "auth": "existing native Google sign-in; API-key environment removed",
        "paid_credit_fallback_requested": False,
    }
    try:
        with (
            (d / "stream.jsonl").open("xb") as out,
            (d / "stderr.txt").open("xb") as err,
        ):
            proc = subprocess.Popen(
                args,
                cwd=d / "workspace",
                env=env,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
            )
            try:
                proc.communicate(p["messages"][1]["content"].encode(), timeout=660)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
        result.update(exit_code=proc.returncode, status="captured_pending_validation")
    except Exception as exc:  # noqa: BLE001 -- preserve failed smoke without retry
        result.update(error_type=type(exc).__name__, error=str(exc))
    result["finished_at"] = now()
    immutable_json(d / "receipt.json", result)
    return result


if __name__ == "__main__":
    p = prepare()
    if sys.argv[1:] == ["--execute"]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda m: call(m, p), MODELS))
        immutable_json(RUNS / "capture-result.json", results)
        print(json.dumps(results), flush=True)
    elif not sys.argv[1:]:
        print(json.dumps({"prepared": MODELS}))
    else:
        raise ValueError("Only --execute supported")
