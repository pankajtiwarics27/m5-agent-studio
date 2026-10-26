# Local runtime lifecycle adapter

AS-019 defines `OmlxLifecycleAdapter` in
[`local_runtime_lifecycle.py`](../local_runtime_lifecycle.py). It isolates
load, unload, failure, and telemetry semantics behind an injected transport;
the adapter does not import an SDK, select a model, or assume a chip or memory
tier.

## Contract

- `load(model_id)` calls the runtime administration endpoint with a bounded
  load timeout and returns a normalized result.
- `unload(model_id)` calls the runtime unload endpoint and returns a normalized
  result.
- `telemetry()` validates and sorts model state, active requests, memory
  counters, and pressure into immutable records.
- Transport failures, non-success statuses, and malformed payloads become
  `LifecycleError` messages containing only a safe operation/status category;
  cancellation propagates so callers can stop work promptly.

## Evidence

`tests/test_local_runtime_lifecycle.py` uses an in-memory async transport to
verify endpoint paths, encoded model IDs, load/unload behavior, telemetry
normalization, unavailable-runtime failures, malformed responses, and
cancellation propagation.
