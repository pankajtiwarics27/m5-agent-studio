---
id: ADR-0020
title: bounded-child-delegation-and-parent-joins
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0020: Bounded child delegation, disjoint workspace write leases, and durable parent joins

## Context

`M5-P4-03` requires that subagents be introduced only where measured benefit justifies
delegation overhead, under four concrete implementation constraints and three acceptance
criteria:

1. **Authority and budget subsetting.** Prior to `M5-P4-03`, the `delegate_subagent`
   harness tool invoked `stream_any_model` directly inside the parent worker loop with no
   child task record, no independent budget envelope, no tool or path subset enforcement,
   and no check against mid-flight parent policy downgrades or capability revocations.
2. **Durable join, cancellation, and crash fencing.** Because delegated runs were
   ephemeral coroutines rather than durable child subtasks, cancelling a parent session
   or recovering from a worker crash could not distinguish child side effects from parent
   side effects, and a parent task could claim `completed` while child work or uncertain
   external effects remained unresolved.
3. **Conflicting workspace mutations.** On a shared local worktree, allowing a parent and
   one or more child workers to write overlapping files (or run arbitrary shell mutations
   while a child holds a file reservation) risks lost updates, torn edits, and unsafe
   rollbacks over concurrent user edits.
4. **Measured justification on 24 GB Apple Silicon.** Under the single-heavy-local-model
   slot (`ADR-0011`), local subagents execute sequentially rather than in parallel. On
   trivial single-file edits, delegation adds prompt and coordination overhead without
   improving verified outcomes; delegation is justified only when fresh-context review or
   isolated verification improves or preserves verified completion rates.

## Decision

We introduce `child_delegation.py` (stdlib-only, inlined into `studio.py`) and wire
`workflow_state.py` and `app.py` around four enforced boundaries:

1. **Strict authority and budget subsets (`ChildAuthoritySubset`, `ChildBudgetAllocation`).**
   - Every child subtask has `max_depth = 1`; `delegate_subagent` is unconditionally
     excluded from child tool allowlists and `::sub:` callers are rejected with
     `child_recursive_delegation_forbidden`.
   - Child policy rank (`plan` < `standard` < `autonomous`) cannot exceed the parent
     policy at spawn (`child_policy_exceeds_parent`) *or* at tool admission time: if the
     parent session policy is downgraded or a parent capability is revoked in
     `ScopedGrantStore` while a child is running, `_execute_harness_tool` immediately
     denies the child's next tool call (`child_parent_policy_downgraded`,
     `child_capability_revoked_on_parent`).
   - Child `readable_paths` and `writable_paths` are canonicalized inside the workspace,
     rejecting secret basenames and path traversal (`child_scope_escapes_workspace`), and
     intersecting with any delegated `parent_grant_ids` (`ScopedGrantStore` mints narrow
     task- and epoch-pinned child grants capped by remaining parent grant TTL and uses).
   - Child execution budgets (`max_steps`, `max_tool_calls`, `max_tokens`,
     `max_elapsed_s`) are carved strictly inside the parent's remaining unreserved
     budget (`child_budget_exceeds_parent`), enforced per step in SQLite, and debited
     into the parent `TaskBudgetState` idempotently once upon durable join.

2. **Disjoint workspace write reservations and version-checked rollback.**
   - Unjoined children reserve their `writable_paths` in
     `ChildDelegationStore` (`$HARNESS_STORE_DIR/delegations/state.json`, schema v1,
     `0700` directory / `0600` file / `O_EXCL` temp / temp+directory `fsync` /
     `os.replace`, covered by `tests/test_durable_state_contract.py`).
   - Spawning a sibling child whose `writable_paths` overlap an unjoined sibling's
     reservation fails closed with HTTP `409` (`child_conflicting_write_scope`).
   - While any unjoined child holds a write reservation, parent calls to mutating file
     tools on overlapping paths or to broad shell/code execution tools in the shared
     workspace are denied (`child_write_lease_conflict`).
   - Each child records managed edits into an isolated `EditBatch` keyed to its sanitized
     `child_task_id`. If the child fails, is cancelled, is explicitly discarded, or fails
     its join verification contract, `_rollback_child_delegation_mutations` executes
     `EditRollbackEngine` over the child's batches — restoring pre-delegation bytes while
     refusing (`child_rollback_conflict`) to clobber concurrent out-of-band user edits.

3. **Durable `::sub:` child subtasks, orphan-effect barriers, and cancel/crash cascades.**
   - Child subtasks are persisted in SQLite `runtime_tasks` as `{parent_task_id}::sub:{key}`
     with their own `WorkerLeaseCoordinator` epoch (`runtime_task_epochs`) and
     `DurableActionLedger` entries (`runtime_actions`), kept separate from organizational
     milestones (`list_milestones` vs `list_child_subtasks`).
   - `join_child_delegation_async` refuses to join a still-running child unless fenced,
     reconciles reconcilable actions, and blocks with HTTP `409`
     (`child_unresolved_consequential_actions`) if any SQLite `runtime_actions`,
     `McpExternalEffectStore` records, or `ContainmentStore` resumption holds remain
     in-flight or uncertain on `child_task_id`.
   - `workflow_state.complete_task` refuses parent completion whenever any `::sub:` child
     is non-terminal or lacks a `joined:{delegation_id}:{disposition}` marker, or when
     any child consequential action is unresolved.
   - Parent cancellation (`cancel_task`, `api_cancel_harness_session`) and crash recovery
     (`recover_incomplete_transitions`) fence active child epochs, contain child
     subprocesses via `ProcessSupervisor`, revoke child scoped grants, sweep in-flight
     child consequential actions to `uncertain` (never dropping or orphaning them), and
     hold the parent in `blocked` when crashed child side effects require reconciliation.

4. **Reproducible single-agent vs bounded-delegation benchmark and overhead gate.**
   - `compare_delegation_runs` and `POST /api/harness/delegations/benchmark` evaluate
     matched `single_model` and `multi_agent` runs across three deterministic scenarios
     (`multi_module_verified_bugfix`, `faulty_subagent_preservation`, and
     `trivial_single_file_edit`), cross-checked for exact statistical parity with
     `workflow_evaluation.compare_workflows`.
   - `evaluate_delegation_justification` consults persisted benchmark summaries and
     rejects worker delegation when measured runs show either degraded verified outcomes
     (`delegation_degrades_verified_outcomes`) or zero verified accuracy gain with strictly
     higher latency and token cost (`delegation_overhead_unjustified`).

## Alternatives

1. **Keep `delegate_subagent` as an untracked in-memory prompt helper.** Rejected: hides
   subagent token/step consumption from `TaskBudgetState`, allows parent completion or
   cancellation to orphan child side effects, and cannot enforce path-scoped authority
   subsets or live grant revocations.
2. **Spawn full git worktrees for every child subagent.** Rejected as a mandatory default:
   duplicating worktrees on every read-only critic call or single-file worker wastes disk
   I/O and complicates CAS version tracking; disjoint path-prefix write leases plus
   per-child `EditBatch` rollback prevent conflicting mutations directly inside the
   session workspace while keeping zero-copy read-only critic audits lightweight.
3. **Unrestricted recursive subagent trees (`depth > 1`).** Rejected: on a 24 GB single
   local heavy-model slot, nested subagent trees multiply context serialization latency
   and fragment parent budgets without improving verified completion rates.

## Tradeoffs

- **Sequential execution on single-slot local hardware.** When lead and critic use
  distinct local oMLX models under one heavy-model admission slot, delegation unloads and
  reloads models sequentially rather than running concurrently. The overhead gate makes
  this cost explicit and blocks worker delegation on trivial single-file tasks where
  single-agent execution achieves identical verified outcomes at lower latency and cost.
- **Conservative shell barrier during unjoined child write leases.** Blocking parent
  `run_bash` calls while a child holds an unjoined `writable_paths` lock forces the parent
  to join or cancel the child before running workspace-wide shell commands, trading
  overlap convenience for deterministic mutation isolation.
- **Seventh durable JSON sidecar store.** Adding `delegations/state.json` increases the
  sidecar surface under `$HARNESS_STORE_DIR`, mitigated by sharing the exact atomic
  write/fsync/symlink/schema contract enforced by `tests/test_durable_state_contract.py`.

## Review Status

Accepted architecture decision (`status: accepted`); independent human review is **not reviewed** (`review_status: not-reviewed`). Code and test verification are covered by [`child_delegation.py`](../../child_delegation.py), [`workflow_state.py`](../../workflow_state.py), [`app.py`](../../app.py), [`tests/test_child_delegation.py`](../../tests/test_child_delegation.py), and [`tests/test_durable_state_contract.py`](../../tests/test_durable_state_contract.py).
