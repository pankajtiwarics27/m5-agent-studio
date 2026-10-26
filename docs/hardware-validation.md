# 16/24/48 GiB hardware validation

AS-023 defines `HardwareValidationMatrix` in
[`hardware_validation.py`](../hardware_validation.py). It runs the same
representative task descriptions against generic 16, 24, and 48 GiB profiles
through an injected executor. The matrix is independent of chip name, model,
provider, and runtime.

Before execution, each task's estimated memory is compared with available
memory. Tasks that cannot fit are reported as `skipped_capacity` and are never
sent to the executor, preventing avoidable OOM attempts. Executor `MemoryError`
and other failures are reported as failed outcomes rather than hidden.

The matrix is a validation harness, not a benchmark claim: callers supply the
representative executor and should record runtime, model, task, and measurement
methodology alongside real results.

## Evidence

`tests/test_hardware_validation.py` verifies all three default tiers, complete
representative-task execution, capacity preflight, OOM/failure reporting, and
invalid matrix definitions.
