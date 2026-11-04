import pytest

from study.auth import callback_values, merge_tokens, profile_path


def test_new_registration():
    assert callback_values(
        "state=s&code=c&client_id=oaiapp_abc", "s", "dynamic_agent_client"
    ) == ("c", "oaiapp_abc")


@pytest.mark.parametrize(
    "query,client",
    [
        ("state=wrong&code=c&client_id=oaiapp_abc", "dynamic_agent_client"),
        ("state=s&error=access_denied", "dynamic_agent_client"),
        ("state=s&code=c", "dynamic_agent_client"),
        ("state=s&code=c&client_id=other", "oaiapp_existing"),
        ("state=s&state=s&code=c", "oaiapp_existing"),
    ],
)
def test_invalid_callback(query, client):
    with pytest.raises(ValueError):
        callback_values(query, "s", client)


def test_returning_registration():
    assert callback_values("state=s&code=c", "s", "oaiapp_existing") == (
        "c",
        "oaiapp_existing",
    )


def test_missing_plan_scope():
    with pytest.raises(ValueError, match="permission"):
        merge_tokens(
            {}, {"scope": "openid", "access_token": "test", "expires_in": 3600}
        )


def test_profile_traversal_rejected():
    with pytest.raises(ValueError):
        profile_path("../auth")


@pytest.mark.parametrize(
    "changed",
    ["signature", "iss", "aud", "exp", "nonce", "sub", "missing_exp", "valid"],
)
def test_signed_identity_validation(monkeypatch, changed):
    import time
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from study.auth import ISSUER, validate_identity

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "iss": ISSUER,
        "aud": "client",
        "sub": "subject",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "nonce": "nonce",
    }
    if changed in {"iss", "aud", "nonce", "sub"}:
        claims[changed] = "wrong"
    elif changed == "exp":
        claims["exp"] = int(time.time()) - 100
    elif changed == "missing_exp":
        del claims["exp"]
    signing_key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if changed == "signature"
        else key
    )
    token = jwt.encode(claims, signing_key, algorithm="RS256")
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda *a: SimpleNamespace(key=key.public_key()),
    )
    if changed == "valid":
        assert (
            validate_identity(token, "client", "nonce", "subject")["sub"] == "subject"
        )
    else:
        with pytest.raises((ValueError, jwt.PyJWTError)):
            validate_identity(token, "client", "nonce", "subject")
