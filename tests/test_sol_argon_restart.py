import importlib.util
from datetime import UTC, datetime
from pathlib import Path


def load():
    spec = importlib.util.spec_from_file_location(
        "restart", Path(__file__).parents[1] / "scripts/restart-sol-argon.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_quota_reset_uses_receipt_time_and_buffer():
    m = load()
    receipt = {"finished_at": "2026-11-02T19:47:22.757918+00:00"}
    reset = m.quota_reset(receipt, "Individual quota reached. Resets in 23h39m56s.")
    assert reset == datetime(2026, 11, 3, 19, 28, 18, 757918, tzinfo=UTC)
    assert m.quota_reset(receipt, "Temporary server error") is None
    assert m.quota_reset(receipt, "Individual quota reached. No reset provided") is None


def test_atomic_retries_temporary_sharing_violation(tmp_path, monkeypatch):
    m = load()
    original = m.os.replace
    calls = []

    def flaky(src, dest):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError("reader holds file")
        original(src, dest)

    monkeypatch.setattr(m.os, "replace", flaky)
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    dest = tmp_path / "state.json"
    m.atomic(dest, {"value": 1})
    assert len(calls) == 3
    assert m.json.loads(dest.read_text()) == {"value": 1}
    assert list(tmp_path.glob("*.tmp")) == []
