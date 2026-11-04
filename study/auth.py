"""Independent SIWC registration; Windows DPAPI protects credentials at rest."""

import argparse
import base64
import hashlib
import json
import os
import secrets
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import win32crypt
from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / ".local" / "auth"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
PLAN_SCOPE = "chatgpt.tokens.use.direct"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = win32crypt.CryptProtectData(
        json.dumps(value).encode(), None, None, None, None, 0
    )
    temp = path.with_suffix(".tmp")
    temp.write_bytes(encrypted)
    os.replace(temp, path)


def load(path):
    return json.loads(
        win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1]
    )


def validate_identity(token, client_id, nonce=None, subject=None):
    key = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json").get_signing_key_from_jwt(
        token
    )
    claims = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        issuer=ISSUER,
        audience=client_id,
        leeway=5,
        options={"require": ["sub", "exp", "iat", "iss", "aud"]},
    )
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise ValueError("Missing account identity")
    if nonce is not None and claims.get("nonce") != nonce:
        raise ValueError("ID token nonce mismatch")
    if subject is not None and claims["sub"] != subject:
        raise ValueError("Account identity changed")
    return claims


def exchange(data):
    response = httpx.post(TOKEN_URL, data={**data, "resource": RESOURCE}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed (HTTP {response.status_code}); sign in again."
        )
    return response.json()


def merge_tokens(record, tokens):
    scopes = tokens.get("scope", " ".join(record.get("scopes", []))).split()
    if PLAN_SCOPE not in scopes:
        raise ValueError("ChatGPT plan permission was not granted")
    if tokens.get("token_type", "").lower() != "bearer" or not tokens.get(
        "access_token"
    ):
        raise ValueError("Invalid access token response")
    return {
        **record,
        **tokens,
        "scopes": scopes,
        "expires_at": time.time() + int(tokens["expires_in"]),
    }


def access_token(profile="default"):
    path = profile_path(profile)
    with FileLock(str(path) + ".lock", timeout=30):
        record = load(path)
        if record["expires_at"] < time.time() + 120:
            tokens = exchange(
                {
                    "grant_type": "refresh_token",
                    "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"],
                }
            )
            if tokens.get("id_token"):
                validate_identity(
                    tokens["id_token"], record["client_id"], subject=record["subject"]
                )
            record = merge_tokens(record, tokens)
            save(path, record)
        if PLAN_SCOPE not in record["scopes"]:
            raise ValueError("ChatGPT plan permission missing")
        return record["access_token"]


def profile_path(profile):
    if not profile or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in profile
    ):
        raise ValueError("Use letters, digits, underscore or hyphen for profile names")
    return AUTH / (profile + ".dpapi")


def callback_values(query, expected_state, client_id):
    values = parse_qs(query)
    if any(len(v) != 1 for v in values.values()):
        raise ValueError("Duplicate callback parameter")
    values = {k: v[0] for k, v in values.items()}
    if not secrets.compare_digest(values.get("state", ""), expected_state):
        raise ValueError("OAuth state mismatch")
    if "error" in values:
        raise ValueError("Authorization declined or unavailable")
    issued = values.get("client_id", client_id)
    if issued == "dynamic_agent_client" or not issued or not values.get("code"):
        raise ValueError("Registration incomplete")
    if client_id != "dynamic_agent_client" and issued != client_id:
        raise ValueError("Client registration mismatch")
    return values["code"], issued


def sign_in(profile="default"):
    path = profile_path(profile)
    AUTH.mkdir(parents=True, exist_ok=True)
    host_path = AUTH / "host.dpapi"
    if not host_path.exists():
        save(host_path, {"id": "urn:uuid:" + str(uuid.uuid4())})
    host_id = load(host_path)["id"]
    record = load(path) if path.exists() else {}
    registration_path = AUTH / (profile + ".registration.dpapi")
    registration = load(registration_path) if registration_path.exists() else {}
    client_id = record.get(
        "client_id", registration.get("client_id", "dynamic_agent_client")
    )
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            try:
                code, issued = callback_values(parsed.query, state, client_id)
                result.update(code=code, client_id=issued)
                body = b"Authorization received. You can return to Codex while identity is verified."
                self.send_response(200)
            except ValueError as exc:
                result["error"] = str(exc)
                body = b"Sign-in failed validation. Return to Codex."
                self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        params = {
            "client_id": client_id,
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": "openid profile email offline_access resource.invoke "
            + PLAN_SCOPE,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            )
            .decode()
            .rstrip("="),
        }
        if client_id == "dynamic_agent_client":
            params["agent_name_hint"] = "Data Center Study"
        url = ISSUER + "/api/accounts/authorize?" + urlencode(params)
        print(
            "Continue with ChatGPT: opening browser for Data Center Study.", flush=True
        )
        webbrowser.open(url)
        deadline = time.monotonic() + 600
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise RuntimeError("Sign-in timed out; rerun the sign-in command")
    if "error" in result:
        raise RuntimeError(result["error"])
    save(registration_path, {"client_id": result["client_id"]})
    tokens = exchange(
        {
            "grant_type": "authorization_code",
            "client_id": result["client_id"],
            "code": result["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect,
        }
    )
    claims = validate_identity(
        tokens["id_token"], result["client_id"], nonce, record.get("subject")
    )
    record = merge_tokens(
        {
            "client_id": result["client_id"],
            "subject": claims["sub"],
            "issuer": claims["iss"],
            "email": claims.get("email"),
            "ext_agent_host_id": host_id,
        },
        tokens,
    )
    with FileLock(str(path) + ".lock", timeout=30):
        save(path, record)
    print(
        "Identity verified; ChatGPT plan permission granted; encrypted credentials saved."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()
    sign_in(args.profile)
