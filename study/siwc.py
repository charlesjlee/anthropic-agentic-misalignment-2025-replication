"""Text-only Inspect adapter for documented SIWC Responses HTTP/SSE."""

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
from inspect_ai.model import ModelAPI, ModelOutput, ModelUsage, modelapi

from study.auth import RESOURCE, ROOT, access_token


def prepare_request(model, messages, config=None):
    """Preserve text and history; record the historical system-to-developer mapping."""
    if config is not None:
        requested = config.model_dump(exclude_none=True)
        allowed = {"max_connections", "max_retries", "timeout"}
        unsupported = set(requested) - allowed
        if unsupported:
            raise ValueError(
                "Unsupported SIWC generation settings: "
                + ", ".join(sorted(unsupported))
            )
    items, mapping = [], []
    for message in messages:
        role, content = message.role, message.content
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ValueError("This study adapter supports plain text messages only")
        if getattr(message, "tool_calls", None):
            raise ValueError("Tool call history is not supported")
        mapped = "developer" if role == "system" else role
        items.append({"role": mapped, "content": content})
        mapping.append(
            {
                "original_role": role,
                "request_role": mapped,
                "original_text": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
    return {"model": model, "input": items, "store": False, "stream": True}, mapping


def sse_events(lines):
    data = []
    for line in lines:
        if not line:
            if data:
                raw = "\n".join(data)
                data = []
                if raw != "[DONE]":
                    yield json.loads(raw)
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


def completed_response(events):
    terminal = None
    for event in events:
        kind = event.get("type")
        if kind in {"response.failed", "response.incomplete", "error"}:
            raise RuntimeError(f"Inference terminal status: {kind}")
        if kind == "response.completed":
            terminal = event["response"]
    if terminal is None or terminal.get("status") != "completed":
        raise RuntimeError("Stream ended without a completed response")
    return terminal


def output_text(response):
    return "".join(
        part["text"]
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def catalog(profile="default"):
    result = httpx.get(
        RESOURCE + "/models",
        headers={"Authorization": "Bearer " + access_token(profile)},
        timeout=30,
    )
    if result.status_code != 200:
        raise RuntimeError(f"Model catalog failed (HTTP {result.status_code})")
    return result.json()


def call(model, messages, config=None, profile="default", evidence_dir=None):
    request, mapping = prepare_request(model, messages, config)
    directory = Path(evidence_dir or ROOT / ".local" / "calls")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (str(uuid.uuid4()) + ".json")
    evidence = {
        "route": RESOURCE + "/responses",
        "auth_route": "siwc",
        "request": request,
        "role_mapping": mapping,
        "unsupported_settings": ["temperature", "top_p", "max_output_tokens"],
        "started_at": time.time(),
        "status": "started",
        "events": [],
    }
    try:
        token = access_token(profile)
        with httpx.stream(
            "POST",
            RESOURCE + "/responses",
            json=request,
            headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(
                (config.timeout if config and config.timeout else 180), connect=30
            ),
        ) as response:
            evidence["http_status"] = response.status_code
            if response.status_code != 200:
                raise RuntimeError(
                    f"Inference HTTP {response.status_code}; no automatic retry or paid fallback"
                )
            for event in sse_events(response.iter_lines()):
                evidence["events"].append(event)
                if event.get("type") in {
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                    "error",
                }:
                    evidence["terminal_event"] = event["type"]
        terminal = completed_response(evidence["events"])
        evidence.update(status="completed", response=terminal)
        return terminal
    except Exception as exc:
        evidence.update(
            status=evidence.get("terminal_event", "failed_before_completion"),
            error_type=type(exc).__name__,
        )
        raise
    finally:
        evidence["finished_at"] = time.time()
        path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
        )


@modelapi(name="siwc")
class SIWC(ModelAPI):
    def __init__(self, model_name, base_url=None, api_key=None, config=None, **kwargs):
        if base_url or api_key:
            raise ValueError(
                "SIWC uses its own verified account and fixed public endpoint"
            )
        super().__init__(model_name=model_name, config=config)
        self.profile = kwargs.pop("profile", "default")
        self.evidence_dir = kwargs.pop("evidence_dir", None)
        if kwargs:
            raise ValueError("Unknown SIWC provider options")

    def max_tokens(self):
        return None

    def should_retry(self, ex):
        return False

    async def generate(self, input, tools, tool_choice, config):
        if tools or tool_choice not in {"auto", "none"}:
            raise ValueError("This study adapter does not execute tools")
        response = await asyncio.to_thread(
            call, self.model_name, input, config, self.profile, self.evidence_dir
        )
        result = ModelOutput.from_content(
            model=response.get("model", self.model_name), content=output_text(response)
        )
        usage = response.get("usage")
        if usage:
            result.usage = ModelUsage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"],
                input_tokens_cache_read=usage.get("input_tokens_details", {}).get(
                    "cached_tokens"
                ),
                reasoning_tokens=usage.get("output_tokens_details", {}).get(
                    "reasoning_tokens"
                ),
            )
        return result
