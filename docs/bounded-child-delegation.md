# Bounded child delegation and durable parent joins

`child_delegation.py`, `workflow_state.py`, and `app.py` (`M5-P4-03`,
[ADR-0020](adr/0020-bounded-child-delegation-and-parent-joins.md)) govern how a parent
harness session spawns, bounds, cancels, verifies, and joins delegated subagents without
permission escalation, conflicting workspace edits, or orphaned consequential actions.

## Authority and budget subsets

Every delegated child (`critic`, `worker`, or `verifier`) runs as a first-level subtask
(`max_depth = 1`) under a strict subset of its parent's live authority and remaining
budget:

- **Policy monotonicity (`CD_POLICY_RANK`).** `derive_child_authority` enforces
  `rank(child.policy) <= rank(parent.policy)` (`plan` < `standard` < `autonomous`) at
  spawn (`child_policy_exceeds_parent`), and `enforce_child_tool_authority` re-checks the
  parent session's *live* policy and *live* `ScopedGrantStore` revocations before every
  child tool admission (`child_parent_policy_downgraded`,
  `child_capability_revoked_on_parent`).
- **No recursive delegation.** `delegate_subagent` is unconditionally forbidden inside
  child tool subsets and `spawn_child_delegation_async` rejects `::sub:` callers with
  `child_recursive_delegation_forbidden`.
- **Workspace-confined path scopes and grant narrowing.** `readable_paths` and
  `writable_paths` must resolve inside the session workspace (`..` escapes and secret
  basenames such as `.env` or private keys fail closed). When `parent_grant_ids` are
  supplied, each grant must be active under the current policy revision, and
  `ScopedGrantStore` mints narrow child grants pinned to `(child_task_id, child_epoch)`
  with TTL and max-uses capped by both the parent grant and child budget.
- **Carved execution budgets.** `derive_child_budget_allocation` carves `max_steps`,
  `max_tool_calls`, `max_tokens`, and `max_elapsed_s` strictly inside the parent's
  remaining budget minus active sibling reservations (`child_budget_exceeds_parent`).
  Each child step records usage against the child's `TaskBudgetState` (`enforce=True`),
  halting immediately on overrun; consumed usage is debited into the parent's budget once
  upon durable join.

## Disjoint workspace write locks and rollback

- **Sibling and parent write isolation.** Unjoined children hold exclusive reservations
  over their `writable_paths` in `ChildDelegationStore`
  (`$HARNESS_STORE_DIR/delegations/state.json`). Overlapping sibling `writable_paths`
  are rejected with HTTP `409` (`child_conflicting_write_scope`), while disjoint sibling
  scopes can coexist. While an unjoined child holds any write reservation, parent calls
  to mutating file tools on overlapping paths or broad shell/code execution tools fail
  closed (`child_write_lease_conflict`).
- **Isolated `EditBatch` rollback on rejected or discarded joins.** Managed edits
  performed by a child record into a child-scoped `EditBatch`. If the child fails, is
  cancelled, is joined with `disposition="discard"`, or fails its join verification
  contract, `join_child_delegation_async` automatically rolls back the child's batches
  via `EditRollbackEngine`. If an out-of-band user edit modified the same lines after the
  child ran, rollback refuses to clobber user work (`child_rollback_conflict`).

## Durable join, cancel cascade, and orphan-action barriers

- **SQLite `::sub:` subtask rows.** Child subtasks are stored in `runtime_tasks` as
  `{parent_task_id}::sub:{sub_key}` (`list_child_subtasks`), distinct from organizational
  milestones (`list_milestones`), and claim their own `WorkerLeaseCoordinator` epoch.
- **Zero-orphan join gate.** `join_child_delegation_async` reconciles reconcilable child
  actions and refuses to join (`409 child_unresolved_consequential_actions`) while any
  consequential `runtime_actions` row, `McpExternalEffectStore` entry, or
  `ContainmentStore` resumption hold for `child_task_id` remains `in_flight` or
  `uncertain`.
- **Parent completion barrier.** `workflow_state.complete_task` refuses parent completion
  while any `::sub:` child is non-terminal or lacks a `joined:{delegation_id}:{disposition}`
  checkpoint marker, or while any child consequential action is unresolved. Accepted
  child `managed_edit` actions count toward parent `verify_task_completion`, whereas
  discarded/cancelled children's rolled-back actions do not.
- **Cancel and crash propagation.** Parent cancellation (`cancel_task`,
  `POST /api/harness/sessions/{session_id}/cancel`) and startup/crash sweeps
  (`recover_incomplete_transitions`) fence child epochs, terminate supervised child
  subprocesses (`ProcessSupervisor.contain_by_scope_async`), revoke child scoped grants,
  sweep in-flight child consequential actions to `uncertain`, and hold the parent in
  `blocked` when crashed child side effects require operator reconciliation.

## Benchmark comparison and overhead gating

`compare_delegation_runs` and `POST /api/harness/delegations/benchmark` execute matched
`single_model` vs `multi_agent` runs across three reproducible scenarios:

1. `multi_module_verified_bugfix` — bounded worker + verification catches an edge-case
   defect missed by a single-pass edit, improving verified success rate (`0.0 -> 1.0`,
   `delegation_overhead_justified = true`).
2. `faulty_subagent_preservation` — a faulty subagent regression fails join verification
   and is rolled back automatically via `EditRollbackEngine`, preserving `1.0` verified
   parent success rate (`preserves_or_improves_verified_outcomes = true`).
3. `trivial_single_file_edit` — both strategies achieve `1.0` verified success, while
   `single_model` finishes with lower latency and token cost
   (`delegation_overhead_justified = false`). Passing `benchmark_category` into
   `delegate_subagent` / `spawn_child_delegation_async` consults persisted summaries and
   rejects unjustified worker delegation (`delegation_overhead_unjustified`).

## HTTP and tool surface

- Tool: `delegate_subagent` (wired through `run_bounded_child_delegation_async`).
- `GET /api/harness/sessions/{session_id}/delegations` — list child subtasks, delegation
  records, unjoined count, and reserved budget.
- `POST /api/harness/sessions/{session_id}/delegations` — spawn (and optionally execute
  and join) a bounded child delegation.
- `POST /api/harness/sessions/{session_id}/delegations/{delegation_id}/join` — verify,
  accept or roll back/discard, debit parent budget, and stamp durable join.
- `POST /api/harness/sessions/{session_id}/delegations/{delegation_id}/cancel` — fence
  child epoch, contain child subprocesses, revoke child grants, and sweep in-flight
  effects.
- `POST /api/harness/delegations/benchmark` — run reproducible `single_model` vs
  `multi_agent` benchmark scenarios and optionally persist category summaries.

## Evidence

- `tests/test_child_delegation.py` verifies authority subsetting, live parent policy
  downgrade and capability revocation, budget carving and idempotent debit, sibling and
  parent write-lease conflict prevention, rollback and user-edit conflict preservation,
  orphan-action join barriers (SQLite + MCP), parent cancel subprocess containment, crash
  recovery sweeps, workspace quarantine freezes, corrupt store `503` handling, and
  benchmark parity with `workflow_evaluation.compare_workflows`.
- `tests/test_durable_state_contract.py` verifies all eight crash-durability and
  symlink/permission invariants on `$HARNESS_STORE_DIR/delegations/state.json`.
