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

## Durable task and root-tool accounting

`DurableTaskBudgetManager` stores task counters in `runtime_tasks.budget_json`.
Steps, tokens, elapsed time, recovery usage, pause/cancel flags, and verification
contracts retain their existing durable update paths.

AP-004 centralizes task-bound tool-call admission in
`charge_hierarchical_tool_call`. The operation resolves the durable parent chain,
validates session/root/epoch identity, checks both candidate limits, and updates
the leaf and immutable root in one SQLite transaction. Root actions are charged
once; child actions charge leaf and root together. Exhaustion or cancellation
rolls the transaction back, so a denied action cannot partially debit one side.
Workflow loops no longer increment `tool_calls_used`, and child join rolls up only
steps, tokens, and elapsed time; this prevents double charging.

See [Mission authority and root-budget envelope](mission-authority-envelope.md)
for generation invalidation, actual-consumer routing, migration, and rollback.

## Evidence

`tests/test_execution_budget.py` covers the in-memory budget dimensions,
cancellation, retry exhaustion, non-negative usage validation, and clock-driven
timeout behavior. `tests/test_mission_authority_envelope.py` covers durable
leaf/root atomic charging and root exhaustion through actual application
consumers. Durable persistence/recovery behavior remains covered by
`tests/test_durable_task_state_machine.py`.
