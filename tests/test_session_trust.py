from __future__ import annotations

import pytest

from session_trust import SessionAuthority, SessionError, SessionRejected


def test_trusted_device_session_validates_and_audits_without_storing_token() -> None:
    now = [100.0]
    events: list[tuple[str, str, str]] = []
    authority = SessionAuthority(ttl_seconds=10, clock=lambda: now[0], audit=lambda *event: events.append(event))
    authority.trust_device("device-a")
    grant = authority.issue("device-a")

    view = authority.validate(grant.session_id, grant.token, "device-a")
    assert view.device_id == "device-a"
    assert grant.token not in vars(authority).get("_sessions", {})
    assert events[0][0] == "session_issued"


def test_unknown_device_and_invalid_token_are_rejected_and_audited() -> None:
    events: list[tuple[str, str, str]] = []
    authority = SessionAuthority(audit=lambda *event: events.append(event))
    authority.trust_device("known")
    grant = authority.issue("known")
    with pytest.raises(SessionRejected, match="device"):
        authority.validate(grant.session_id, grant.token, "unknown")
    with pytest.raises(SessionRejected, match="invalid"):
        authority.validate(grant.session_id, "wrong", "known")
    assert "session_rejected_unknown_device" in [event[0] for event in events]
    assert "session_rejected_invalid_token" in [event[0] for event in events]


def test_expiry_direct_revoke_and_device_untrust_reject_sessions() -> None:
    now = [1.0]
    authority = SessionAuthority(ttl_seconds=5, clock=lambda: now[0])
    authority.trust_device("device")
    expired = authority.issue("device")
    now[0] = 6.0
    with pytest.raises(SessionRejected, match="expired"):
        authority.validate(expired.session_id, expired.token, "device")

    fresh = authority.issue("device")
    assert authority.revoke(fresh.session_id) is True
    assert authority.revoke(fresh.session_id) is False
    with pytest.raises(SessionRejected, match="revoked"):
        authority.validate(fresh.session_id, fresh.token, "device")

    other = authority.issue("device")
    authority.untrust_device("device")
    with pytest.raises(SessionRejected, match="revoked"):
        authority.validate(other.session_id, other.token, "device")


def test_unknown_device_cannot_issue_and_bad_configuration_fails() -> None:
    authority = SessionAuthority()
    with pytest.raises(SessionRejected, match="not trusted"):
        authority.issue("unknown")
    with pytest.raises(SessionError, match="ttl_seconds"):
        SessionAuthority(ttl_seconds=0)
    with pytest.raises(SessionError, match="device_id"):
        authority.trust_device("")
