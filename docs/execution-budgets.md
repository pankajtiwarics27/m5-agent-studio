# Cancellation, retries, and execution budgets

AS-035 defines `ExecutionBudget` in
[`execution_budget.py`](../execution_budget.py). A budget enforces elapsed
time, output tokens, estimated cost, tool-call count, and retry count. Usage is
checked after each increment, so callers cannot silently continue after a
limit is crossed.

Cancellation is explicit and takes precedence over other checks. Retry
attempts consume the retry budget and `allow_retry()` returns `False` instead
of retrying after exhaustion. The guard is thread-safe and uses an injected
monotonic clock for deterministic tests.

## Evidence

`tests/test_execution_budget.py` covers all budget dimensions, cancellation,
retry exhaustion, non-negative usage validation, and clock-driven timeout
behavior.
