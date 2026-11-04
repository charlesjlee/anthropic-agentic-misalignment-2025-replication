import json
import sqlite3
import threading
import time

import httpx
import pytest

from study.expansion_costs import summarize, token_cost, totals
from study.expansion_ledger import Ledger, digest, immutable_json
from study.expansion_parallel import bounded_trials
from study.expansion_transport import (
    batch_lines,
    invoke,
    parse_batch,
    response_request,
    submit_batch,
)


def config():
    return {
        "id": "test",
        "model": "test",
        "target": 6,
        "auth": "siwc",
        "tier": "subscription",
        "rates": {"input": 2, "output": 10},
        "judge_rates": {"input": 10, "output": 50},
        "judge_auth": "siwc",
        "judge_tier": "subscription",
        "forecast": {
            "actor": {"input": 2000, "output": 1000},
            "judge": {"input": 1000, "output": 200},
        },
    }


def test_parallel_bound_and_failure_drain():
    barrier = threading.Barrier(2)
    seen = []

    def fail_one(item):
        seen.append(item)
        barrier.wait(timeout=5)
        if item == 0:
            raise RuntimeError("fixture failure")
        time.sleep(0.05)

    result = bounded_trials(range(20), fail_one, 2)
    assert sorted(seen) == [0, 1]
    assert result["submitted"] == 2 and result["completed"] == 1
    assert len(result["errors"]) == 1
    assert (
        bounded_trials(range(5), lambda x: None, 2, stopped=lambda: True)["submitted"]
        == 0
    )


def test_64_worker_wave_and_upper_bound():
    barrier = threading.Barrier(64)
    seen = []

    def work(item):
        seen.append(item)
        barrier.wait(timeout=10)

    result = bounded_trials(range(64), work, 64)
    assert result["completed"] == 64 and not result["errors"]
    assert sorted(seen) == list(range(64))
    with pytest.raises(ValueError):
        bounded_trials([], work, 65)


def test_reconciled_rejection_preserves_failed_call_and_rejects_regeneration(
    tmp_path, monkeypatch
):
    from study import expansion_setup
    from study.expansion_recovery import attempt_location

    monkeypatch.setattr(expansion_setup, "ROOT", tmp_path)
    directory = tmp_path / "actor"
    receipt = directory / "receipt.json"
    immutable_json(receipt, {"status": "http_error", "http_status": 503})
    immutable_json(
        directory / "retry-authorization.json",
        {"prior_receipt": expansion_setup.artifact(receipt)},
    )
    chosen, event_id, recovery = attempt_location(directory, "old")
    assert chosen == directory / "retry-001" and event_id == "old/retry-001"
    assert recovery["replaces"] == "old"
    (directory / "stream.jsonl").write_text("data received", encoding="utf-8")
    with pytest.raises(ValueError):
        attempt_location(directory, "old")
    events = [
        {
            "id": "old",
            "config": "test",
            "created": "one",
            "kind": "call",
            "body": {"role": "actor", "status": "http_error"},
        },
        {
            "id": "new",
            "config": "test",
            "created": "two",
            "kind": "call",
            "body": {
                "role": "actor",
                "status": "completed",
                "replaces": "old",
                "usage": {"input": 10, "output": 10},
            },
        },
    ]
    row = summarize([config()], events)[0]
    assert row["errors"] == 1 and row["unresolved_errors"] == 0
    assert row["actor_completed"] == 1 and row["state"] == "partial"


def test_rate_limit_receipt_retains_diagnostics_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "study.expansion_transport.credentials", lambda auth: "fixture-secret"
    )
    requests = []

    def limited(request):
        requests.append(request)
        return httpx.Response(
            429,
            headers={
                "retry-after": "30",
                "x-ratelimit-remaining-requests": "0",
                "set-cookie": "sensitive-cookie",
            },
            json={
                "error": {
                    "type": "rate_limit_error",
                    "code": "slow_down",
                    "message": "Rate limit reached",
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(limited))
    receipt = invoke(response_request("test", [], "siwc"), "siwc", tmp_path, client)
    assert receipt["status"] == "http_error" and receipt["http_status"] == 429
    assert receipt["rate_limit_headers"]["retry-after"] == "30"
    assert receipt["provider_error"]["code"] == "slow_down"
    assert len(requests) == 1
    assert "sensitive-cookie" not in (tmp_path / "receipt.json").read_text()
    assert "fixture-secret" not in (tmp_path / "receipt.json").read_text()


def test_immutable_and_duplicate_reservations(tmp_path):
    ledger = Ledger(tmp_path / "db")
    c = config()
    ledger.config(c)
    ledger.config(c)
    with pytest.raises(ValueError):
        ledger.config({**c, "target": 7})
    ledger.reserve("test", "one", "actor", {"model": "test"})
    with pytest.raises(RuntimeError):
        ledger.reserve("test", "one", "actor", {"model": "test"})
    with ledger.connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM events")
    with pytest.raises(ValueError):
        ledger.event("test/one/actor/reserved", "test", "one", "different", {})


def test_cache_and_reasoning_accounting():
    u = {
        "input": 1000,
        "output": 200,
        "cache_read": 200,
        "cache_write": 100,
        "reasoning_subset": 150,
    }
    rates = {"input": 2, "output": 10, "cached_input": 0.1, "cache_write": 2.5}
    assert token_cost(u, rates) == pytest.approx(
        (700 * 2 + 200 * 0.1 + 100 * 2.5 + 200 * 10) / 1e6
    )
    assert token_cost(u, rates, "batch") == token_cost(u, rates) / 2
    assert (
        token_cost(
            {"input": 1, "output": 1}, {"input": 1, "output": 1, "cached_input": None}
        )
        == 2e-6
    )
    with pytest.raises(ValueError):
        token_cost({**u, "cache_read": 1001}, rates)


def test_observed_forecast_and_judge_backlog():
    c = config()
    e = {
        "config": "test",
        "created": "now",
        "kind": "call",
        "body": {
            "role": "actor",
            "status": "completed",
            "usage": {"input": 100, "output": 20},
            "auth": "siwc",
        },
    }
    row = summarize([c], [e])[0]
    assert row["actor_completed"] == 1 and row["judged"] == 0
    assert row["remaining_usd"] == 0
    assert row["remaining_equivalent_usd"] == pytest.approx(5 * 0.0004 + 6 * 0.02)
    assert row["subscription_equivalent_usd"] == pytest.approx(0.0004)
    assert totals([row])["scored"] == 0


def test_paid_reported_precedes_estimate():
    c = {**config(), "auth": "api"}
    e = {
        "config": "test",
        "created": "now",
        "kind": "call",
        "body": {
            "role": "actor",
            "status": "completed",
            "usage": {"input": 100, "output": 20},
            "reported_usd": 0.1,
            "estimated_usd": 0.2,
        },
    }
    r = summarize([c], [e])[0]
    assert r["cost_so_far_usd"] == 0.1
    assert r["estimated_usd"] == 0


def test_route_serialization():
    messages = [
        {"role": "system", "content": "exact system"},
        {"role": "user", "content": "exact user"},
    ]
    request = response_request(
        "test", messages, "siwc", {"effort": "medium", "mode": "standard"}
    )
    assert request["input"][0] == {"role": "developer", "content": "exact system"}
    assert messages[0]["role"] == "system"
    assert "temperature" not in request and "max_output_tokens" not in request
    assert request["store"] is False and request["stream"] is True
    with pytest.raises(ValueError):
        response_request("test", messages, "siwc", batch=True)
    with pytest.raises(ValueError):
        response_request("test", messages, "siwc", cap=10)


@pytest.mark.parametrize(
    "terminal,successful",
    [("response.completed", True), ("response.incomplete", False), ("none", False)],
)
def test_stream_receipts_and_no_retry(tmp_path, monkeypatch, terminal, successful):
    monkeypatch.setattr(
        "study.expansion_transport.credentials", lambda auth: "test-fixture"
    )
    events = [{"type": "response.output_text.delta", "delta": "partial"}]
    if terminal != "none":
        events.append(
            {
                "type": terminal,
                "response": {
                    "id": "test",
                    "model": "test",
                    "status": terminal.split(".")[1],
                },
            }
        )
    payload = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload))
    )
    request = response_request("test", [], "siwc")
    r = invoke(request, "siwc", tmp_path, client)
    assert (r["status"] == "completed") == successful
    assert "test-fixture" not in (tmp_path / "receipt.json").read_text()
    with pytest.raises(RuntimeError):
        invoke(request, "siwc", tmp_path, client)


def test_batch_ids_partial_and_submission_gate(tmp_path):
    expected = {x: response_request("test", [], "api", batch=True) for x in ["a", "b"]}
    line = json.dumps(
        {"custom_id": "b", "response": {"body": {"model": "test"}}, "error": None}
    )
    result = parse_batch(line, expected)
    assert result["missing"] == ["a"]
    with pytest.raises(ValueError):
        parse_batch(line + "\n" + line, expected)
    assert submit_batch(expected, tmp_path) == batch_lines(expected)
    assert not list(tmp_path.iterdir())


def test_canonical_hash_and_artifact_conflicts(tmp_path):
    assert digest({"b": 1, "a": 2}) == digest({"a": 2, "b": 1})
    p = tmp_path / "artifact.json"
    immutable_json(p, {"a": 1})
    immutable_json(p, {"a": 1})
    with pytest.raises(ValueError):
        immutable_json(p, {"a": 2})


def test_smoke_counts_toward_later_wave_and_resume(tmp_path, monkeypatch):
    from study import expansion_run, expansion_setup, pilot
    from study.api_validation import request_for
    from study.historical_audit import CONDITIONS

    monkeypatch.setattr(pilot, "verify_sources", lambda: {"fixture": "pinned"})

    monkeypatch.setattr(expansion_run, "ROOT", tmp_path)
    monkeypatch.setattr(expansion_setup, "ROOT", tmp_path)
    frozen_path = tmp_path / "frozen.json"
    monkeypatch.setattr(expansion_run, "FROZEN", frozen_path)
    prompts = {}
    for condition in CONDITIONS:
        request, email = request_for("gpt-4.1-2025-04-14", condition)
        prompts[condition["name"]] = {"messages": request["messages"], "email": email}
    immutable_json(tmp_path / "source.json", {})
    frozen = {
        "models": ["gpt-6.1-sol", "gpt-6-astra"],
        "prompts": prompts,
        "actor_reasoning": {"effort": "medium", "mode": "standard"},
        "judge": {"model": "gpt-6-astra", "reasoning": "omitted"},
        "source_hashes": pilot.verify_sources(),
        "runner_sources": {"path": "source.json"},
    }
    immutable_json(frozen_path, frozen)
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    for model in frozen["models"]:
        ledger.config(
            {
                **config(),
                "id": f"expansion/{model}/v1",
                "model": model,
                "target": 600,
                "configuration_sha256": digest(
                    {"manifest": digest(frozen), "model": model, "auth": "siwc"}
                ),
            }
        )
    calls = []

    def fake_invoke(request, auth, directory):
        calls.append(request)
        judge = directory.name == "judge"
        assert ("reasoning" in request) != judge
        text = (
            "<reasoning>Rescue allowed</reasoning><answer>no</answer>"
            if judge
            else "Allow emergency dispatch."
        )
        receipt = {
            "request": request,
            "status": "completed",
            "response": {
                "model": request["model"],
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
            },
        }
        immutable_json(directory / "receipt.json", receipt)
        return receipt

    monkeypatch.setattr(expansion_run, "invoke", fake_invoke)
    assert expansion_run.run(db, 1, False)["actor_slots_in_wave"] == 12
    assert not calls
    held = expansion_run.run(
        db, 1, True, exclude_trials=[f"{frozen['models'][0]}/{next(iter(prompts))}-001"]
    )
    assert held["completed"] == 11 and len(calls) == 22
    expansion_run.run(db, 1, True)
    assert len(calls) == 24
    expansion_run.run(db, 1, True)
    assert len(calls) == 24  # no regeneration or rejudging on resume
    limited = expansion_run.run(db, 5, True, workers=4, limit=5)
    assert limited["completed"] == 5 and not limited["errors"]
    assert len(calls) == 34
    expanded = expansion_run.run(db, 5, True, workers=4)
    assert not expanded["errors"]
    assert len(calls) == 120  # 60 total actors; initial 12 are included
    configs, events = ledger.snapshot()
    rows = summarize(configs, events)
    assert all(r["scored"] == 30 and r["actor_completed"] == 30 for r in rows)
    assert all(cell["n"] == 5 for r in rows for cell in r["conditions"].values())


def test_terminal_empty_output_reconstruction(tmp_path):
    from study.expansion_stream import assembled_response

    terminal = {"id": "r1", "status": "completed", "output": []}
    receipt = {"status": "completed", "response": terminal}
    item = {
        "id": "m1",
        "type": "message",
        "content": [{"type": "output_text", "text": "safe"}],
    }
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "m1",
            "content_index": 0,
            "delta": "safe",
        },
        {
            "type": "response.output_text.done",
            "item_id": "m1",
            "content_index": 0,
            "text": "safe",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": terminal},
    ]
    immutable_json(tmp_path / "receipt.json", receipt)
    stream = tmp_path / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in events))
    assembled = assembled_response(receipt, tmp_path)
    assert assembled["output"] == [item]
    assert (
        json.loads((tmp_path / "receipt.json").read_text())["response"]["output"] == []
    )
    events[0]["delta"] = "different"
    stream.write_text("\n".join(json.dumps(e) for e in events))
    with pytest.raises(ValueError, match="does not match"):
        assembled_response(receipt, tmp_path)
