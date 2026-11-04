import importlib.util
from pathlib import Path


def load():
    spec = importlib.util.spec_from_file_location(
        "recovery", Path(__file__).parents[1] / "scripts/resume-closeout.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_only_explicit_empty_subscription_429_is_recoverable():
    m = load()
    r = {"auth": "siwc", "http_status": 429}
    assert m.recoverable(r)
    assert not m.recoverable({**r, "response": {"id": "maybe_generated"}})
    assert not m.recoverable({**r, "terminal_event": "response.incomplete"})
    assert not m.recoverable({**r, "auth": "openrouter"})
    assert not m.recoverable({"auth": "siwc", "status": "uncertain"})


def test_completed_receipt_reused_without_network(tmp_path, monkeypatch):
    m = load()
    request = {"model": "m"}
    m.immutable_json(
        tmp_path / "receipt.json",
        {"request": request, "status": "completed", "response": {"id": "existing"}},
    )
    monkeypatch.setattr(
        m,
        "invoke",
        lambda *a: (_ for _ in ()).throw(AssertionError("Network must not run")),
    )
    r, path = m.obtain(
        None,
        "c",
        "t",
        "actor",
        request,
        "siwc",
        {},
        {},
        tmp_path,
        None,
        {},
        "condition",
        1,
    )
    assert r["response"]["id"] == "existing"
    assert path == tmp_path
