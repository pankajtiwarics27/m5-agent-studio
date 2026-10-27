"""AS-026: Remote origin/CSRF, rate-limit and authorization protections.

Verifies the shared security boundary in ``app.security_boundary`` (host
allowlist -> origin/CSRF check -> bearer authorization -> POST rate limit ->
body-size limit) and the startup guards that keep remote mode off by default.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

import app as studio


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, item: str, value: str) -> None:
        self.values[(service, item)] = value

    def get_password(self, service: str, item: str) -> str | None:
        return self.values.get((service, item))

    def delete_password(self, service: str, item: str) -> None:
        self.values.pop((service, item), None)


@pytest.fixture

def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    backend = MemoryKeyring()
    monkeypatch.setattr(studio.keyring, "set_password", backend.set_password)
    monkeypatch.setattr(studio.keyring, "get_password", backend.get_password)
    monkeypatch.setattr(studio.keyring, "delete_password", backend.delete_password)
    return backend


_TOKEN = "t" * 40


@pytest.fixture

def remote_protection_boundary(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring
) -> None:
    """Isolated DB plus a remote-like boundary (bearer auth on, strict host/origin allowlists)."""
    db = tmp_path / "remote-protections.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", _TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN_FILE", tmp_path / "config" / "api-token")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", set())
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()
    studio.init_db()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def test_remote_boundary_same_origin_authenticated_post_passes(remote_protection_boundary) -> None:
    with TestClient(studio.app) as client:
        response = client.post(
            "/api/combos",
            headers={**_auth_headers(), "Origin": "http://testserver"},
            json={"alias": "remote-combo", "targets": ["omlx"]},
        )
    assert response.status_code == 200
    with TestClient(studio.app) as client:
        get_ok = client.get("/api/providers", headers=_auth_headers())
    assert get_ok.status_code == 200


def test_remote_csrf_mismatched_origin_is_rejected_before_auth(remote_protection_boundary) -> None:
    """An evil Origin on a state-changing request is rejected with 403 even when the bearer token is valid."""
    with TestClient(studio.app) as client:
        response = client.post(
            "/api/combos",
            headers={**_auth_headers(), "Origin": "https://evil.example"},
            json={"alias": "x", "targets": ["omlx"]},
        )
    assert response.status_code == 403
    assert "Cross-origin" in response.json()["detail"]


def test_state_changing_request_without_origin_still_requires_auth(remote_protection_boundary) -> None:
    """Non-browser remote clients send no Origin; they must still be rejected by the authorization boundary."""
    with TestClient(studio.app) as client:
        no_token = client.post("/api/combos", json={"alias": "x", "targets": ["omlx"]})
        bad_token = client.post(
            "/api/combos",
            headers={"Authorization": "Bearer " + "w" * 40},
            json={"alias": "x", "targets": ["omlx"]},
        )
        same_origin = client.post(
            "/api/combos",
            headers={**_auth_headers(), "Origin": "http://testserver"},
            json={"alias": "x", "targets": ["omlx"]},
        )
    assert no_token.status_code == 401
    assert bad_token.status_code == 401
    assert same_origin.status_code == 200


def test_disallowed_host_header_is_rejected_even_without_auth(remote_protection_boundary) -> None:
    with TestClient(studio.app) as client:
        response = client.post(
            "/api/combos",
            headers={"Host": "rogue.example"},
            json={"alias": "x", "targets": ["omlx"]},
        )
    assert response.status_code == 400
    assert "Host is not permitted" in response.json()["detail"]


def test_expensive_post_rate_limit_fires_with_retry_after(remote_protection_boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 2)
    with TestClient(studio.app) as client:
        first = client.post("/api/chat/stream", headers=_auth_headers(), json={"messages": []})
        second = client.post("/api/chat/stream", headers=_auth_headers(), json={"messages": []})
        blocked = client.post("/api/chat/stream", headers=_auth_headers(), json={"messages": []})
    assert first.status_code != 429
    assert second.status_code != 429
    assert blocked.status_code == 429
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None
    assert 1 <= int(retry_after) <= 60


def test_get_requests_are_not_rate_limited(remote_protection_boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 1)
    with TestClient(studio.app) as client:
        responses = [client.get("/api/providers", headers=_auth_headers()) for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)


def test_rate_limit_is_keyed_per_client(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinct client addresses keep independent sliding windows."""
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 3)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()
    assert studio._rate_limit_exceeded("10.0.0.1", now=10) == (False, 0)
    assert studio._rate_limit_exceeded("10.0.0.1", now=11) == (False, 0)
    assert studio._rate_limit_exceeded("10.0.0.1", now=12) == (False, 0)
    blocked, _ = studio._rate_limit_exceeded("10.0.0.1", now=13)
    assert blocked
    # A different client is unaffected by the first client's budget.
    assert studio._rate_limit_exceeded("10.0.0.2", now=13) == (False, 0)


def test_startup_refuses_non_loopback_auth_disabled(remote_protection_boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_HOST", "203.0.113.5")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    with pytest.raises(RuntimeError, match="permitted only on loopback"):
        with TestClient(studio.app):
            pass


def test_startup_refuses_non_loopback_bind_without_token(remote_protection_boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_HOST", "203.0.113.5")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    with pytest.raises(RuntimeError, match="Refusing non-loopback bind without STUDIO_API_TOKEN"):
        with TestClient(studio.app):
            pass


def test_short_api_token_fails_closed_at_startup(remote_protection_boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_HOST", "127.0.0.1")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "too-short")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    with pytest.raises(RuntimeError, match="STUDIO_API_TOKEN must contain at least 32 characters"):
        with TestClient(studio.app):
            pass
