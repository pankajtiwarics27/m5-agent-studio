# Authenticated sessions and device trust

AS-025 defines `SessionAuthority` in
[`session_trust.py`](../session_trust.py). Devices must be explicitly trusted
before a session can be issued. Session tokens are returned once and stored as
SHA-256 digests; validation requires the session ID, token, and matching device.

Sessions expire by clock, can be revoked directly, and are revoked when their
device is untrusted. Unknown devices, unknown sessions, expired sessions,
revoked sessions, and invalid tokens are rejected and produce audit callbacks
without exposing token material.

## Evidence

`tests/test_session_trust.py` covers issuance, device binding, expiry,
revocation, unknown-device rejection, invalid-token rejection, audit events,
and token non-persistence.
