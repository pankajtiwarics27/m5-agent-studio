# Crash and failure injection suite

AS-037 defines the deterministic failure harness in
[`failure_injection.py`](../failure_injection.py). It exercises four expected
boundaries: isolated runtime crash, timeout cancellation, provider connection
loss, and client disconnect cancellation.

Each outcome records whether the failure was observed and whether the harness
classified it as recoverable/retryable. Unexpected failure kinds are not
silently converted into success; cancellation is re-raised unless it is the
declared disconnect scenario.

The harness injects failures through an async operation callback and performs
no provider, database, filesystem, or container I/O itself.

## Evidence

`tests/test_failure_injection.py` covers all four scenarios, unexpected
failure classification, cancellation propagation, and invalid suite input.
