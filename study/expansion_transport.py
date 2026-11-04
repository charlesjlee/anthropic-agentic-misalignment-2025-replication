"""Explicit subscription, Responses and Batch transports. No automatic retries/fallback."""

import json
from pathlib import Path

import httpx

from study.expansion_ledger import immutable_json, now
from study.siwc import completed_response, sse_events

BASE = "https://api.openai.com/v1"


def response_request(model, messages, auth, reasoning=None, cap=None, batch=False):
    if auth not in {"siwc", "api"} or (batch and auth != "api"):
        raise ValueError("Batch requires explicit API route")
    if auth == "siwc" and cap is not None:
        raise ValueError("Subscription output cap unsupported")
    body = {
        "model": model,
        "input": [dict(m) for m in messages],
        "store": False,
        "stream": not batch,
    }
    if auth == "siwc":
        for message in body["input"]:
            if message["role"] == "system":
                message["role"] = "developer"
    if reasoning is not None:
        body["reasoning"] = reasoning
    if cap is not None:
        body["max_output_tokens"] = cap
    return body


def credentials(auth):
    if auth == "siwc":
        from study.auth import access_token

        return access_token()
    if auth == "api":
        from study.credentials import read_key

        return read_key("openai")
    raise ValueError("Unknown auth route")


def invoke(request, auth, directory, client=None):
    """Write reservation before network I/O; retain every SSE event and terminal response."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "request.json"
    if marker.exists():
        raise RuntimeError("Prior attempt exists; reconcile without resubmitting")
    immutable_json(
        marker,
        {
            "request": request,
            "auth": auth,
            "started_at": now(),
            "endpoint": BASE + "/responses",
        },
    )
    receipt = {
        "request": request,
        "auth": auth,
        "started_at": now(),
        "status": "uncertain",
    }
    owned = client is None
    try:
        client = client or httpx.Client(timeout=httpx.Timeout(600, connect=30))
        headers = {"Authorization": "Bearer " + credentials(auth)}
        with client.stream(
            "POST", BASE + "/responses", headers=headers, json=request
        ) as response:
            receipt["http_status"] = response.status_code
            receipt["request_id"] = response.headers.get("x-request-id")
            receipt["rate_limit_headers"] = {
                key: value
                for key, value in response.headers.items()
                if key.startswith(
                    (
                        "x-ratelimit-",
                        "x-codex-primary-",
                        "x-codex-secondary-",
                        "x-codex-rate-limit-",
                    )
                )
                or key == "retry-after"
            }
            if response.status_code != 200:
                receipt["status"] = "http_error"
                response.read()
                try:
                    error = response.json().get("error", {})
                    receipt["provider_error"] = {
                        key: str(error[key])[:2048]
                        for key in ("type", "code", "message")
                        if key in error
                    }
                except (ValueError, AttributeError):
                    receipt["provider_error"] = {"type": "unparsed_http_error"}
                raise RuntimeError("HTTP failure; no fallback")
            events = []
            with (directory / "stream.jsonl").open("x", encoding="utf-8") as log:
                for event in sse_events(response.iter_lines()):
                    log.write(json.dumps(event, ensure_ascii=False) + "\n")
                    log.flush()
                    events.append(event)
                    if event.get("type") in {
                        "response.completed",
                        "response.failed",
                        "response.incomplete",
                    }:
                        receipt["response"] = event.get("response")
                        receipt["terminal_event"] = event["type"]
                        receipt["status"] = event["type"].split(".")[-1]
            terminal = completed_response(events)
            receipt["response"] = terminal
            if terminal.get("model") != request["model"]:
                receipt["status"] = "model_mismatch"
                raise ValueError("Returned model differs from frozen model")
            receipt["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 -- persist sanitized failure for every transport/auth exception
        receipt["error_type"] = type(exc).__name__
    finally:
        if owned and client is not None:
            client.close()
        receipt["finished_at"] = now()
        immutable_json(directory / "receipt.json", receipt)
    return receipt


def batch_lines(requests):
    if not requests or len({r["model"] for r in requests.values()}) != 1:
        raise ValueError("A batch requires one model and unique custom IDs")
    rows = []
    for custom_id, request in requests.items():
        if request.get("stream") is not False:
            raise ValueError("Batch requests must be non-streaming API requests")
        rows.append(
            json.dumps(
                {
                    "custom_id": custom_id,
                    "method": "POST",
                    "url": "/v1/responses",
                    "body": request,
                }
            )
        )
    return "\n".join(rows) + "\n"


def parse_batch(text, expected):
    found = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = row["custom_id"]
        if key not in expected or key in found:
            raise ValueError("Unknown or duplicate Batch custom_id")
        response = (row.get("response") or {}).get("body") or {}
        if response and response.get("model") != expected[key]["model"]:
            raise ValueError("Batch model substitution")
        found[key] = row
    return {"results": found, "missing": sorted(set(expected) - set(found))}


def submit_batch(requests, directory, *, execute=False, client=None):
    """Explicit paid submission only. A lost create response is held for reconciliation."""
    if not execute:
        return batch_lines(requests)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "submission.json").exists():
        raise RuntimeError("Batch already reserved; do not resubmit")
    payload = batch_lines(requests)
    immutable_json(
        directory / "submission.json", {"requests": requests, "reserved_at": now()}
    )
    with httpx.Client(timeout=60) if client is None else client as session:
        headers = {"Authorization": "Bearer " + credentials("api")}
        uploaded = session.post(
            BASE + "/files",
            headers=headers,
            data={"purpose": "batch"},
            files={"file": ("input.jsonl", payload.encode(), "application/jsonl")},
        )
        uploaded.raise_for_status()
        file = uploaded.json()
        immutable_json(directory / "upload.json", file)
        batch = session.post(
            BASE + "/batches",
            headers=headers,
            json={
                "input_file_id": file["id"],
                "endpoint": "/v1/responses",
                "completion_window": "24h",
            },
        )
        batch.raise_for_status()
        body = batch.json()
        immutable_json(directory / "batch.json", body)
        return body


def poll_batch(directory):
    directory = Path(directory)
    batch = json.loads((directory / "batch.json").read_text("utf-8"))
    expected = json.loads((directory / "submission.json").read_text("utf-8"))[
        "requests"
    ]
    with httpx.Client(
        timeout=60, headers={"Authorization": "Bearer " + credentials("api")}
    ) as client:
        response = client.get(BASE + "/batches/" + batch["id"])
        response.raise_for_status()
        current = response.json()
        stamp = now().replace(":", "-")
        immutable_json(directory / "polls" / (stamp + ".json"), current)
        files = []
        for key in ["output_file_id", "error_file_id"]:
            if current.get(key):
                output = client.get(BASE + "/files/" + current[key] + "/content")
                output.raise_for_status()
                immutable_json(
                    directory / (current[key] + ".json"), {"content": output.text}
                )
                files.append(output.text)
        return current, parse_batch("\n".join(files), expected)
