#!/usr/bin/env python3
"""Perform a disposable read/write/delete check against the active OS keyring."""
from __future__ import annotations

import secrets
import sys

try:
    import keyring
except ImportError as exc:
    print("keyring package is not installed; install the project dependencies first.", file=sys.stderr)
    raise SystemExit(2) from exc

service = "m5-agent-studio-local-check"
username = "probe-" + secrets.token_hex(12)
password = secrets.token_urlsafe(32)
backend = keyring.get_keyring()
print(f"Active keyring backend: {type(backend).__module__}.{type(backend).__name__} (priority {getattr(backend, 'priority', 'unknown')})")
try:
    keyring.set_password(service, username, password)
except Exception as exc:
    print(f"OS keyring write failed ({type(exc).__name__}); Studio will fail closed when saving credentials.", file=sys.stderr)
    raise SystemExit(2) from None

try:
    if keyring.get_password(service, username) != password:
        raise RuntimeError("Keyring round-trip returned a different value.")
except Exception as exc:
    try:
        keyring.delete_password(service, username)
    except Exception:
        pass
    print(f"OS keyring read failed ({type(exc).__name__}).", file=sys.stderr)
    raise SystemExit(2) from None

try:
    keyring.delete_password(service, username)
    if keyring.get_password(service, username) is not None:
        raise RuntimeError("Disposable keyring item still exists after deletion.")
except Exception as exc:
    print(f"Keyring check could not clean up its disposable test item ({type(exc).__name__}); remove service `{service}` manually.", file=sys.stderr)
    raise SystemExit(3) from None
print("OS keyring write/read/delete check passed; no credential value was printed.")
