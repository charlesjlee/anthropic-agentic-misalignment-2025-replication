"""Unmodified official Claude Code subscription transport, isolated single text turn."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from study.expansion_ledger import immutable_json, now

CLI = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"


def environment():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))}
    env.update(CLAUDE_CODE_MAX_RETRIES="0", CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK="1")
    return env


def identity():
    result = subprocess.run([str(CLI), "auth", "status"], env=environment(),
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    status = json.loads(result.stdout)
    selected = {k: status.get(k) for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
    if result.returncode or selected != {"loggedIn": True, "authMethod": "claude.ai",
                                       "apiProvider": "firstParty", "subscriptionType": "max"}:
        raise ValueError("Expected native Claude Max sign-in; no API-key fallback")
    return selected


def binary_identity():
    version = subprocess.run([str(CLI), "--version"], capture_output=True, text=True,
                             encoding="utf-8", timeout=30, env=environment()).stdout.strip()
    return {"path": str(CLI), "version": version,
            "sha256": hashlib.sha256(CLI.read_bytes()).hexdigest()}


def invoke(model, messages, directory, *, effort="medium"):
    directory = Path(directory).resolve()
    if (directory / "request.json").exists():
        raise RuntimeError("Prior Claude attempt exists; reconcile without resubmitting")
    if [m["role"] for m in messages] != ["system", "user"]:
        raise ValueError("This frozen adapter requires one system and one user message")
    directory.mkdir(parents=True, exist_ok=True)
    auth = identity()
    (directory / "system.txt").write_text(messages[0]["content"], encoding="utf-8")
    settings = {"switchModelsOnFlag": False, "availableModels": [model], "effortLevel": effort}
    immutable_json(directory / "settings.json", settings)
    immutable_json(directory / "mcp.json", {"mcpServers": {}})
    args = [str(CLI), "-p", "--model", model, "--effort", effort,
            "--safe-mode", "--setting-sources", "", "--settings", str(directory / "settings.json"),
            "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config",
            "--mcp-config", str(directory / "mcp.json"), "--disable-slash-commands",
            "--no-chrome", "--permission-mode", "dontAsk", "--max-turns", "1",
            "--no-session-persistence", "--system-prompt-file", str(directory / "system.txt"),
            "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    request = {"model": model, "messages": messages, "effort": effort,
               "native_reasoning": "adaptive via Claude Code", "temperature": "not exposed; omitted",
               "output_cap": "not exposed by selected CLI flags; omitted",
               "auth": "claude_subscription", "argv": args, "settings": settings,
               "wrapper": "Custom system prompt replaces default; safe mode; no tools, MCP, skills or memory",
               "wire_request_visibility": "CLI invocation and events recorded; raw HTTP payload not exposed",
               "retry_limit": 0}
    immutable_json(directory / "request.json", {"request": request, "started_at": now()})
    receipt = {"request": request, "auth": "claude_subscription", "auth_metadata": auth,
               "started_at": now(), "status": "uncertain"}
    try:
        with (directory / "stream.jsonl").open("xb") as output, (directory / "stderr.txt").open("xb") as errors:
            process = subprocess.Popen(args, cwd=directory, env=environment(),
                                       stdin=subprocess.PIPE, stdout=output, stderr=errors)
            try:
                process.communicate(messages[1]["content"].encode("utf-8"), timeout=600)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
        receipt["exit_code"] = process.returncode
        events = [json.loads(line) for line in (directory / "stream.jsonl").read_text("utf-8").splitlines() if line.strip()]
        init = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
        results = [e for e in events if e.get("type") == "result"]
        assistants = [e["message"] for e in events if e.get("type") == "assistant" and e.get("message")]
        receipt["init"] = init
        receipt["results"] = results
        receipt["assistant_messages"] = assistants
        if len(init) != 1 or init[0].get("model") != model:
            raise ValueError("Missing init or substituted Claude model")
        if init[0].get("tools") or init[0].get("mcp_servers"):
            raise ValueError("Unexpected native tools or MCP servers")
        if process.returncode or len(results) != 1 or results[0].get("is_error") or results[0].get("subtype") != "success":
            raise ValueError("Claude CLI did not complete successfully")
        if len(assistants) != 1 or assistants[0].get("model") != model:
            raise ValueError("Expected one assistant completion on exact model")
        if any(p.get("type") == "tool_use" for p in assistants[0].get("content", [])):
            raise ValueError("Unexpected real tool call")
        text = "".join(p.get("text", "") for p in assistants[0].get("content", []) if p.get("type") == "text")
        stop_reason = results[0].get("stop_reason") or assistants[0].get("stop_reason")
        if not text.strip() or stop_reason not in {"end_turn", "stop_sequence", "refusal"}:
            raise ValueError("Missing text or incomplete Claude stop reason")
        receipt.update(status="completed", visible_text=text, usage=results[0].get("usage"),
                       api_equivalent_cost=results[0].get("total_cost_usd"),
                       returned_model=assistants[0]["model"], response_id=assistants[0].get("id"))
    except Exception as exc:  # noqa: BLE001 -- retain every failed attempt, no hidden resubmission
        receipt.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        receipt["finished_at"] = now()
        immutable_json(directory / "receipt.json", receipt)
    return receipt
