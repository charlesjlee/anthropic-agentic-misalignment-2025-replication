import getpass
import warnings

import pytest

from study import credentials


def test_noninteractive_entry_does_not_read_secret(monkeypatch):
    monkeypatch.setattr(credentials.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(getpass, "getpass", lambda _: pytest.fail("must not read"))
    with pytest.raises(RuntimeError):
        credentials.enter_key("openai")


def test_echoing_getpass_fallback_is_rejected(monkeypatch):
    monkeypatch.setattr(credentials.sys.stdin, "isatty", lambda: True)

    def unsafe_prompt(_):
        warnings.warn("no hidden input", getpass.GetPassWarning)
        pytest.fail("must not continue")

    monkeypatch.setattr(getpass, "getpass", unsafe_prompt)
    with pytest.raises(getpass.GetPassWarning):
        credentials.enter_key("openai")


def test_error_does_not_disclose_secret(monkeypatch, capsys):
    secret = "synthetic-secret-do-not-print"

    def fail(*args, **kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(credentials, "enter_key", fail)
    assert credentials.main(["set", "openai"]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_status_does_not_disclose_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(credentials, "AUTH", tmp_path)
    credentials.key_path("openai").touch()
    secret = "synthetic-secret-do-not-print"
    monkeypatch.setattr(credentials, "read_key", lambda _: secret)
    assert credentials.main(["status", "openai"]) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_rejects_mismatched_provider(monkeypatch):
    monkeypatch.setattr(
        credentials, "load", lambda _: {"provider": "openrouter", "api_key": "x" * 30}
    )
    with pytest.raises(ValueError, match="provider mismatch"):
        credentials.read_key("openai")


def test_does_not_overwrite_existing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "AUTH", tmp_path)
    path = credentials.key_path("openai")
    path.write_bytes(b"existing encrypted record")
    with pytest.raises(FileExistsError):
        credentials.store_key("openai", "synthetic-key" * 3)
    assert path.read_bytes() == b"existing encrypted record"


def test_provider_cannot_escape_project_directory():
    with pytest.raises(ValueError):
        credentials.key_path("../../elsewhere")


def test_invalid_key_never_reaches_storage(monkeypatch):
    monkeypatch.setattr(credentials, "save", lambda *_: pytest.fail("must not save"))
    with pytest.raises(ValueError):
        credentials.store_key("openai", "x" * 25 + "\n")
