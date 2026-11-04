import json

import pytest

from study.export_evidence import screen, verify


def test_secret_export_fails_closed(tmp_path):
    path = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="Secret"):
        screen(
            path, b'{"output":"synthetic-private-value"}', ["synthetic-private-value"]
        )
    with pytest.raises(ValueError, match="Credential"):
        screen(path, b'{"access_token":"secret"}', [])
    screen(path, b'{"usage":{"total_tokens":8},"request":{"temperature":1}}', [])


def test_verification_detects_tampering(tmp_path):
    import hashlib

    path = tmp_path / "receipt.json"
    path.write_bytes(b"{}")
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "receipt.json",
                        "bytes": 2,
                        "sha256": hashlib.sha256(b"{}").hexdigest(),
                    }
                ]
            }
        )
    )
    assert verify(tmp_path) == 1
    path.write_bytes(b"[]")
    with pytest.raises(ValueError, match="checksum"):
        verify(tmp_path)
