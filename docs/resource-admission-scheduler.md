# Resource admission scheduler

AS-018 defines `ResourceAdmissionScheduler` in
[`resource_admission.py`](../resource_admission.py). It is a small runtime
boundary that admits work against generic memory and slot capacity without
knowing a model, provider, chip, or operating system.

## Lifecycle contract

- `try_admit()` checks memory and slots and records a lease under one lock;
  it returns `None` when the request cannot fit.
- `admit()` provides the explicit exception form for callers that cannot queue
  or defer work.
- `cancel()` and `release()` atomically return a lease's resources. Repeated
  release/cancellation is idempotent and cannot subtract resources twice.
- `reservation()` provides a context-managed release path for exceptions and
  cancellation.
- `snapshot()` reports a consistent usage view while holding the same lock as
  admission and release.

The scheduler does not block waiting for resources, execute work, probe the
host, or select a fallback. Those policies remain application-owned boundaries.

## Evidence

`tests/test_resource_admission.py` covers capacity rejection, cancellation,
idempotent release, context-managed cleanup, invalid requests, and concurrent
admission. The concurrency test holds successful leases until all contenders
have attempted admission and verifies the capacity is never oversubscribed.
