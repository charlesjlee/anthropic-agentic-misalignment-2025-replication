"""Recover completed visible output from SSE done items without regenerating a trial."""

import hashlib
import json

from study.expansion_ledger import immutable_json


def assembled_response(receipt, directory):
    terminal = receipt.get("response") or {}
    if receipt.get("status") != "completed" or terminal.get("status") != "completed":
        raise ValueError("Cannot reconstruct an incomplete response")
    if terminal.get("output"):
        return terminal
    stream = directory / "stream.jsonl"
    events = [json.loads(line) for line in stream.read_text("utf-8").splitlines()]
    completed = [e["response"] for e in events if e.get("type") == "response.completed"]
    if len(completed) != 1 or completed[0] != terminal:
        raise ValueError("Terminal receipt does not match saved stream")
    if any(
        e.get("type") in {"response.failed", "response.incomplete", "error"}
        for e in events
    ):
        raise ValueError("Stream contains failure event")
    items = {}
    for event in events:
        if event.get("type") != "response.output_item.done":
            continue
        index, item = event["output_index"], event["item"]
        if index in items:
            raise ValueError("Duplicate output item index")
        if item.get("type") == "message":
            for content_index, part in enumerate(item.get("content", [])):
                if part.get("type") != "output_text":
                    continue
                done = [
                    e
                    for e in events
                    if e.get("type") == "response.output_text.done"
                    and e.get("item_id") == item["id"]
                    and e.get("content_index") == content_index
                ]
                deltas = [
                    e["delta"]
                    for e in events
                    if e.get("type") == "response.output_text.delta"
                    and e.get("item_id") == item["id"]
                    and e.get("content_index") == content_index
                ]
                if (
                    len(done) != 1
                    or done[0]["text"] != part["text"]
                    or "".join(deltas) != part["text"]
                ):
                    raise ValueError("Completed message does not match text stream")
        items[index] = item
    if not items or sorted(items) != list(range(len(items))):
        raise ValueError("Missing completed output items")
    assembled = {**terminal, "output": [items[i] for i in sorted(items)]}
    immutable_json(
        directory / "assembled-response.json",
        {
            "method": "SSE completed items; exact text equality against done text and deltas",
            "receipt_sha256": hashlib.sha256(
                (directory / "receipt.json").read_bytes()
            ).hexdigest(),
            "stream_sha256": hashlib.sha256(stream.read_bytes()).hexdigest(),
            "response": assembled,
        },
    )
    return assembled
