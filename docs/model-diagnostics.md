# Hardware and model diagnostics

AS-022 defines `diagnose_models()` in
[`model_diagnostics.py`](../model_diagnostics.py). It evaluates AS-017 model
descriptors using AS-051 capability negotiation and an optional AS-015 hardware
profile, then returns a deterministic report suitable for a UI or API.

Each model entry includes feasible status, explicit rejection reasons, and
tradeoffs covering runtime, source, capabilities, support flags, context,
memory, and trust. The diagnostics layer does not select, load, download, or
execute a model; manual selection remains possible even when several models
are feasible.

## Evidence

`tests/test_model_diagnostics.py` verifies feasible and rejected candidates,
hardware-dependent reasons, deterministic ordering, tradeoff fields, JSON-safe
output, and invalid input rejection.
