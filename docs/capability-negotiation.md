# Capability negotiation per model and task

AS-051 defines `CapabilityNegotiator` in
[`capability_negotiation.py`](../capability_negotiation.py). It evaluates a
registered AS-017 `ModelDescriptor` against explicit task requirements and the
optional AS-015 `HardwareProfile`.

The decision checks model capabilities, context limits, tool/schema/streaming
support, trust status, hardware/runtime capabilities, and available memory. A
rejected model returns stable human-readable reasons and compatible alternatives
when candidates are supplied. Negotiation never downloads, loads, dispatches,
falls back, or grants permissions; the caller must explicitly select an
alternative after reviewing the decision.

## Evidence

`tests/test_capability_negotiation.py` covers successful negotiation, missing
model and hardware capabilities, limit/support/trust rejection, memory fit,
and deterministic compatible alternatives. Tests use only immutable fixture
descriptors and profiles.
