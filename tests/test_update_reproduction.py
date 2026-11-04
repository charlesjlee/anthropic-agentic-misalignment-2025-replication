"""Evidence verification must fail closed on corruption and incomplete calls."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "experiments/update/run.py"
SPEC = importlib.util.spec_from_file_location("update_reproduction", SOURCE)
repro = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repro)


def artifact(tmp_path, receipt):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_index_cannot_escape_root(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        repro.checked_path(tmp_path, "../credentials.json")


def test_modified_receipt_is_rejected(tmp_path):
    item = artifact(tmp_path, {"status": "completed"})
    (tmp_path / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        repro.checked_artifact(tmp_path, item)


@pytest.mark.parametrize("status", ["uncertain", "http_error", "incomplete"])
def test_incomplete_receipt_cannot_count_as_safe(tmp_path, status):
    item = artifact(tmp_path, {"status": status})
    event = {"id": "trial/actor", "body": {"artifact": item, "status": "completed"}}
    with pytest.raises(ValueError, match="Incomplete"):
        repro.extract(tmp_path, event)


def test_truncated_router_output_is_rejected(tmp_path):
    item = artifact(
        tmp_path,
        {
            "status": "completed",
            "response": {
                "choices": [
                    {"finish_reason": "length", "message": {"content": "I refuse"}}
                ]
            },
        },
    )
    event = {"body": {"artifact": item, "status": "completed", "auth": "openrouter"}}
    with pytest.raises(ValueError, match="Incomplete OpenRouter"):
        repro.extract(tmp_path, event)


def test_native_reasoning_not_scored_as_visible_text(tmp_path):
    item = artifact(
        tmp_path,
        {
            "status": "completed",
            "visible_text": "Visible refusal",
            "native_reasoning_summary": "Private summary",
        },
    )
    event = {
        "body": {"artifact": item, "status": "completed", "auth": "gemini_subscription"}
    }
    assert repro.extract(tmp_path, event) == "Visible refusal"
