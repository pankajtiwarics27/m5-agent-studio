"""Single-operator authenticated sessions and device-trust policy."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


class SessionError(ValueError):
    """Raised when session issuance input is invalid."""


class SessionRejected(RuntimeError):
    """Raised when a session, token, or device cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SessionGrant:
    session_id: str
    token: str
    device_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: str
    device_id: str
    issued_at: float
    expires_at: float
    revoked: bool


@dataclass(slots=True)
class _SessionState:
    session_id: str
    token_digest: bytes
    device_id: str
    issued_at: float
    expires_at: float
    revoked: bool = False


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SessionError(f"{field} must be a non-empty string without NUL characters.")
    return value.strip()


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class SessionAuthority:
    """Issue and validate bearer sessions bound to explicitly trusted devices."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
        audit: Callable[[str, str, str], None] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise SessionError("ttl_seconds must be positive.")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._audit = audit or (lambda _event, _session_id, _device_id: None)
        self._lock = threading.RLock()
        self._trusted_devices: set[str] = set()
        self._sessions: dict[str, _SessionState] = {}

    def trust_device(self, device_id: str) -> None:
        normalized = _identifier(device_id, "device_id")
        with self._lock:
            self._trusted_devices.add(normalized)

    def untrust_device(self, device_id: str) -> None:
        normalized = _identifier(device_id, "device_id")
        with self._lock:
            self._trusted_devices.discard(normalized)
            for state in self._sessions.values():
                if state.device_id == normalized:
                    state.revoked = True
                    self._audit("session_revoked_device_untrusted", state.session_id, normalized)

    def issue(self, device_id: str) -> SessionGrant:
        normalized = _identifier(device_id, "device_id")
        now = self._clock()
        with self._lock:
            if normalized not in self._trusted_devices:
                self._audit("session_rejected_unknown_device", "", normalized)
                raise SessionRejected("device is not trusted.")
            session_id = secrets.token_urlsafe(18)
            token = secrets.token_urlsafe(36)
            state = _SessionState(session_id, _digest(token), normalized, now, now + self._ttl_seconds)
            self._sessions[session_id] = state
            self._audit("session_issued", session_id, normalized)
            return SessionGrant(session_id, token, normalized, state.expires_at)

    def validate(self, session_id: str, token: str, device_id: str) -> SessionView:
        normalized_session = _identifier(session_id, "session_id")
        normalized_device = _identifier(device_id, "device_id")
        with self._lock:
            state = self._sessions.get(normalized_session)
            if state is None:
                self._audit("session_rejected_unknown_session", normalized_session, normalized_device)
                raise SessionRejected("session is not recognized.")
            if state.revoked:
                self._audit("session_rejected_revoked", state.session_id, normalized_device)
                raise SessionRejected("session is revoked.")
            if self._clock() >= state.expires_at:
                state.revoked = True
                self._audit("session_rejected_expired", state.session_id, normalized_device)
                raise SessionRejected("session is expired.")
            if state.device_id != normalized_device:
                self._audit("session_rejected_unknown_device", state.session_id, normalized_device)
                raise SessionRejected("device is not trusted for this session.")
            if not isinstance(token, str) or not hmac.compare_digest(state.token_digest, _digest(token)):
                self._audit("session_rejected_invalid_token", state.session_id, normalized_device)
                raise SessionRejected("session token is invalid.")
            return SessionView(state.session_id, state.device_id, state.issued_at, state.expires_at, state.revoked)

    def revoke(self, session_id: str) -> bool:
        normalized = _identifier(session_id, "session_id")
        with self._lock:
            state = self._sessions.get(normalized)
            if state is None or state.revoked:
                return False
            state.revoked = True
            self._audit("session_revoked", state.session_id, state.device_id)
            return True
