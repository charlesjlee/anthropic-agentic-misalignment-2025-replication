import importlib.util
import json
from pathlib import Path

import pytest

from study.expansion_costs import summarize


def load():
    spec = importlib.util.spec_from_file_location(
        "resilient", Path(__file__).parents[1] / "scripts/resilient-closeout.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_failures_distinguish_results_protocol_and_service():
    m = load()
    assert (
        m.failure_kind({"status": "completed", "visible_text": "harmful refusal"})
        == "completed"
    )
    assert (
        m.failure_kind(
            {
                "status": "failed",
                "response": {"error": {"code": "server_is_overloaded"}},
            }
        )
        == "transient"
    )
    assert (
        m.failure_kind({"status": "uncertain", "error_type": "JSONDecodeError"})
        == "transient"
    )
    assert (
        m.failure_kind({"status": "failed", "error": "Unexpected native tool action"})
        == "protocol"
    )
    assert (
        m.failure_kind(
            {"status": "failed", "response": {"choices": [{"finish_reason": "length"}]}}
        )
        == "protocol"
    )
    assert (
        m.failure_kind(
            {"status": "failed", "http_status": 429, "error": "insufficient_quota"}
        )
        == "permanent"
    )
    assert m.failure_kind({"status": "model_mismatch"}) == "permanent"
    assert m.failure_kind({"status": "failed", "http_status": 401}) == "permanent"


def test_nested_retry_after_respects_elapsed_time():
    m = load()
    r = {
        "finished_at": "1970-01-01T00:01:40+00:00",
        "response": {"error": {"headers": {"retry-after": "304"}}},
    }
    assert m.retry_after_seconds(r, current=120) == 284
    assert m.retry_after_seconds(r, current=500) == 0
    assert (
        m.retry_after_seconds(
            {"rate_limit_headers": {"retry-after-ms": "6000"}}, current=100
        )
        == 6
    )
    assert (
        m.retry_after_seconds(
            {"rate_limit_headers": {"retry-after": "Thu, 01 Jan 1970 00:03:00 GMT"}},
            current=100,
        )
        == 80
    )


def harness(m, tmp_path, monkeypatch):
    events = []

    class FakeLedger:
        def reserve(self, *a):
            events.append(("reserved", a))

        def event(self, *a):
            events.append(("event", a))

    monkeypatch.setattr(m, "Ledger", FakeLedger)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(m, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(
        m,
        "artifact",
        lambda p: {"path": str(Path(p).relative_to(tmp_path)), "sha256": "fixture"},
    )
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(m.PACER, "wait", lambda: None)
    monkeypatch.setattr(m.PACER, "observe", lambda r: None)
    return events


def test_reuses_completed_actor_without_call_or_charge(tmp_path, monkeypatch):
    m = load()
    events = harness(m, tmp_path, monkeypatch)
    req = {"model": "gpt-6-sol"}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"request": req, "status": "completed"}))
    history = [
        {"id": "old", "body": {"status": "completed", "artifact": m.artifact(path)}}
    ]
    monkeypatch.setattr(m, "invoke", lambda *a: pytest.fail("Must reuse actor"))
    monkeypatch.setattr(m, "extract", lambda *a: ("retained text", None))
    result = m.get_call(
        {},
        {"cid": "c", "model": "gpt-6-sol", "auth": "siwc"},
        "none_none",
        1,
        "actor",
        req,
        {},
        history,
        None,
    )
    assert result[0][0] == "retained text" and not events
    with pytest.raises(m.PermanentFailure, match="Saved request differs"):
        m.get_call(
            {},
            {"cid": "c", "model": "gpt-6-sol", "auth": "siwc"},
            "none_none",
            1,
            "actor",
            {"model": "different"},
            {},
            history,
            None,
        )


def test_transient_retry_then_success_preserves_link(tmp_path, monkeypatch):
    m = load()
    events = harness(m, tmp_path, monkeypatch)
    receipts = iter(
        [
            {"status": "failed", "auth": "siwc", "http_status": 503},
            {"status": "completed", "response": {}},
        ]
    )
    monkeypatch.setattr(m, "invoke", lambda *a: next(receipts))
    monkeypatch.setattr(m, "extract", lambda *a: ("answer", None))
    m.get_call(
        {},
        {"cid": "c", "model": "gpt-6-sol", "auth": "siwc", "rates": None},
        "none_none",
        1,
        "actor",
        {},
        {},
        [],
        None,
    )
    calls = [args for kind, args in events if kind == "event" and args[3] == "call"]
    assert len(calls) == 2
    assert calls[1][4]["replaces_all"] == [calls[0][0]]


def test_five_failed_attempts_exhaust_only_that_trial(tmp_path, monkeypatch):
    m = load()
    events = harness(m, tmp_path, monkeypatch)
    monkeypatch.setattr(
        m, "invoke", lambda *a: {"status": "failed", "http_status": 503}
    )
    with pytest.raises(m.TrialExhausted):
        m.get_call(
            {},
            {"cid": "c", "model": "m", "auth": "siwc", "rates": None},
            "none_none",
            1,
            "actor",
            {},
            {},
            [],
            None,
        )
    assert sum(kind == "reserved" for kind, _ in events) == 5


def test_paid_retry_allowance_is_bounded(tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, "RUNS", tmp_path)
    budget = m.PaidRetryBudget()
    budget.reserved = 9.99
    with pytest.raises(m.PermanentFailure, match="allowance exhausted"):
        budget.reserve({"rates": {"input": 1, "output": 1}})
    assert budget.reserved == 9.99


def test_resolving_multiple_failures_retains_historical_counts():
    config = {
        "id": "c",
        "model": "m",
        "target": 1,
        "auth": "siwc",
        "tier": "subscription",
        "rates": None,
        "forecast": {
            "actor": {"input": 1, "output": 1},
            "judge": {"input": 1, "output": 1},
        },
        "judge_rates": None,
        "judge_auth": "siwc",
        "judge_tier": "subscription",
    }
    events = [
        {
            "id": str(n),
            "config": "c",
            "created": "now",
            "kind": "call",
            "body": {"role": "actor", "status": "failed"},
        }
        for n in range(2)
    ]
    events.append(
        {
            "id": "2",
            "config": "c",
            "created": "now",
            "kind": "call",
            "body": {
                "role": "actor",
                "status": "completed",
                "replaces_all": ["0", "1"],
            },
        }
    )
    row = summarize([config], events)[0]
    assert row["errors"] == 2 and row["unresolved_errors"] == 0
    assert row["actor_completed"] == 1
