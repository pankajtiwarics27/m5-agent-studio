# Structured traces and diagnostics

AS-036 defines `WorkflowTrace` in
[`workflow_tracing.py`](../workflow_tracing.py). Every event carries the same
trace ID, a unique span ID, status, timestamp, detail, and normalized
attributes. Terminal statuses close the trace so later events cannot silently
appear outside the workflow lifecycle.

`failure()` produces an actionable reason, next action, and retryability flag,
then records the corresponding failed event. `export()` returns JSON-safe data
for persistence or API diagnostics and contains no provider or credential
assumptions.

## Evidence

`tests/test_workflow_tracing.py` covers event correlation, deterministic
attributes, actionable failure diagnostics, JSON-safe export, terminal closure,
and malformed input rejection.
