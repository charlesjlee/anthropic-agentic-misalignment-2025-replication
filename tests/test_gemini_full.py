import copy
import importlib.util
from pathlib import Path

import pytest


def runner():
    spec = importlib.util.spec_from_file_location(
        "gemini_full", Path(__file__).parents[1] / "scripts/gemini-full.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def events():
    return [
        {
            "event": "init",
            "conversation_id": "same",
            "init": {
                "model": "gemini-test",
                "agent": "datacenter-study",
                "tools": ["finish"],
                "permission_mode": "strict",
            },
        },
        {
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "text_delta": "response"},
        },
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "num_turns": 1,
                "conversation_id": "same",
                "response": "response",
            },
        },
    ]


def test_native_terminal_and_identity_verified():
    m = runner()
    assert m.validate_capture(events(), "gemini-test", ["finish"])[0] == "response"
    for change in [
        {"status": "ERROR"},
        {"conversation_id": "different"},
        {"num_turns": 2},
        {"response": "different"},
    ]:
        changed = copy.deepcopy(events())
        changed[-1]["result"].update(change)
        with pytest.raises(ValueError):
            m.validate_capture(changed, "gemini-test", ["finish"])


def test_native_action_and_catalog_change_rejected():
    m = runner()
    changed = events()
    changed.insert(
        1, {"event": "step_update", "step_update": {"step_type": "run_command"}}
    )
    with pytest.raises(ValueError, match="native tool action"):
        m.validate_capture(changed, "gemini-test", ["finish"])
    with pytest.raises(ValueError, match="catalog"):
        m.validate_capture(events(), "gemini-test", [])


def test_google_unknown_equivalent_is_not_cash_charge():
    from study.expansion_costs import summarize

    config = {
        "id": "g",
        "auth": "gemini_subscription",
        "tier": "subscription",
        "target": 600,
        "rates": None,
        "judge_auth": "siwc",
        "judge_tier": "subscription",
        "judge_rates": {"input": 10, "output": 50},
        "forecast": {
            "actor": {"input": 100, "output": 100},
            "judge": {"input": 100, "output": 100},
        },
    }
    event = {
        "id": "call",
        "config": "g",
        "kind": "call",
        "created": "now",
        "body": {
            "role": "actor",
            "auth": "gemini_subscription",
            "status": "completed",
            "usage": {"input": 100, "output": 100},
        },
    }
    row = summarize([config], [event])[0]
    assert (
        row["cost_so_far_usd"] == row["remaining_usd"] == row["unknown_cost_calls"] == 0
    )
    assert row["remaining_unknown"] is False
    assert row["remaining_equivalent_unknown"] is True
