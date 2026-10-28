from __future__ import annotations

import base64
import hashlib
import os

from fastapi.testclient import TestClient

from api.auth import COOKIE_NAME, issue_session, read_session
from api.main import create_app


def _password_hash(password: str) -> str:
    salt = b"operator-auth-test"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    enc = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    return f"pbkdf2_sha256$200000${enc(salt)}${enc(digest)}"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("IDN_OPERATOR_AUTH_ENABLED", "true")
    monkeypatch.setenv("IDN_OPERATOR_USERNAME", "operator")
    monkeypatch.setenv("IDN_OPERATOR_PASSWORD_HASH", _password_hash("correct horse battery staple"))
    monkeypatch.setenv("IDN_SESSION_SECRET", "test-only-session-secret")
    monkeypatch.setenv("IDN_SESSION_TTL_SECONDS", "28800")
    return TestClient(create_app())


def test_login_success_sets_httponly_lax_session(monkeypatch):
    client = _client(monkeypatch)
    response = client.post("/api/v1/auth/login", json={"username": "operator", "password": "correct horse battery staple"})
    assert response.status_code == 200
    assert response.json()["role"] == "OPERATOR"
    cookie = response.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "correct horse" not in cookie


def test_login_invalid_password_and_me_requires_session(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "operator", "password": "wrong"}).status_code == 401


def test_logout_clears_session_and_protected_api_requires_session(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/v1/intents").status_code == 401
    # The session gate runs before endpoint parsing or handler execution.  An
    # anonymous request therefore cannot turn a malformed body into a planner,
    # reconciliation, capacity-calibration, or data-plane side effect.
    for path in (
        "/api/v1/intents",
        "/api/v1/intents/preview",
        "/api/v1/network/bootstrap",
        "/api/v1/resources/stable-capacity",
        "/api/v1/reconciliation/run",
    ):
        assert client.post(path, json={}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "operator", "password": "correct horse battery staple"}).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    old_cookie = client.cookies.get(COOKIE_NAME)
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", cookies={COOKIE_NAME: old_cookie}).status_code == 401


def test_password_hash_has_no_plaintext(monkeypatch):
    value = _password_hash("correct horse battery staple")
    assert "correct horse" not in value
    monkeypatch.setenv("IDN_OPERATOR_PASSWORD_HASH", value)
    assert os.environ["IDN_OPERATOR_PASSWORD_HASH"] != "correct horse battery staple"


def test_session_expiry_is_rejected(monkeypatch):
    _client(monkeypatch)
    token = issue_session(user_id="operator", role="OPERATOR", secret=b"test-only-session-secret", ttl_seconds=-1)
    assert read_session(token) is None


def test_operator_session_disabled_does_not_block_competition_api_reads(monkeypatch):
    monkeypatch.setenv("IDN_OPERATOR_AUTH_ENABLED", "false")
    client = TestClient(create_app())
    # The legacy reverse-proxy API-key boundary still governs writes.  The
    # competition UI must not additionally need an operator session for its
    # ordinary real-data reads.
    assert client.get("/api/v1/intents").status_code == 200
