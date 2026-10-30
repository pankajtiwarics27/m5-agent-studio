---
id: ADR-0009
title: durable-recovery-memory-and-stagnation
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0009: Durable failure memory decides when to stop retrying

## Context

`M5-P1-05` bounds a task by steps, tool calls, tokens, elapsed time and recovery
attempts, all persisted in `runtime_tasks.budget_json`. `M5-P1-06` and `M5-P2-04`
require authoritative, revision-anchored evidence before a coding task may
complete. Both are enforced today. They still leave a specific loop open:

1. the steps are spent, but every step repeats the same failing strategy;
2. the evidence is authoritative at each attempt, but nothing compares it with the
   previous attempt's evidence, so "still failing the same test" is
   indistinguishable from "making progress";
3. a patch can oscillate — attempt A writes state 1, attempt B writes state 2, the
   next attempt rewrites state 1 — and each attempt looks new;
4. a resume after a crash or a failure can be triggered with the workspace exactly
   as it was when the last attempt failed, which cannot produce a different result.

The story's acceptance criteria name the outcomes: no infinite retry loop, failure
memory and budgets that survive a crash, and recovery actions that require changed
evidence or conditions.

## Decision

Failure memory is a durable, per-task record, and recovery work is bounded by
counters kept in the same document.

1. **Evidence snapshots, not prose.** Every verification attempt stores a
   `revision_digest` (from `verification_evidence.capture_source_revision`), a
   `transcript_sha256`, a `failure_fingerprint` (canonical digest of the failure
   code plus the sorted failing test ids), a `failure_code`, and an
   `environment_digest`. Two derived keys follow: `conditions_digest` (revision +
   environment) judges a *recovery* action that has not run anything yet, and
   `evidence_key` (conditions + transcript + fingerprint + code) judges a fresh
   *verification* attempt.
2. **Verdicts gate behaviour.** `first`/`progress` proceed; `stagnant` (identical
   to the most recent failure) is tolerated until `max_identical_attempts`
   (default 2) and then escalates; `oscillating` (equal to an older abandoned
   state) escalates immediately. Escalation writes `pause_requested` into both the
   recovery document and the task budget, which is the existing safe-pause
   mechanism: the execution boundary then treats the task as paused and refuses
   further consequential actions.
3. **Counters live with the memory.** `max_retries`, `max_replans` and
   `max_rollbacks` (default 3 each) are incremented in the same atomic write that
   records the attempt. A crash can therefore not spend a budget without leaving
   the attempt that spent it, nor record an attempt without spending its budget.
   Exhaustion is persisted *before* the refusal is raised.
4. **Recovery must change something.** `prepare_resume` compares the current
   conditions digest with the recorded failure's and refuses (HTTP `409`) when they
   are identical. A resumed turn spends `retry`, or `replan` when the latest
   verdict escalated — a retry of the same plan is not counted as a replan.
5. **Memory failure fails closed.** If the record cannot be read, the recovery gate
   is skipped (nothing to compare, and the recovery budget still bounds the loop);
   if it cannot be written for a failed verification, the report is annotated
   `recovery memory unavailable` with an escalation so the turn pauses rather than
   looping unbounded.

## Alternatives

* **Reuse `runtime_actions` / a new SQLite table for attempts.** Rejected: the
  history is append-mostly, bounded, and read once per attempt; a JSON document
  under the harness store keeps it out of the migration path (no schema v4 for a
  P2 story), keeps the counters and the attempts atomic together, and matches how
  edit batches and snapshots are already stored.
* **Put the counters in `runtime_tasks.budget_json`.** Rejected: `TaskBudgetState`
  is reconstructed field-by-field in eight places across `execution_budget.py` and
  `workflow_state.py`; adding fields means every one of those sites must be kept in
  sync, and a missed site silently drops a counter. Keeping budgets and failures in
  one document also removes the two-write window between "spend budget" and
  "record attempt".
* **Hash only the failing test ids.** Rejected: the same failing test with a
  different error, or with different code around it, is different evidence. The
  fingerprint includes the code and the transcript hash covers the output.
* **Treat any repeated failure as fatal.** Rejected: a single flaky failure would
  stop legitimate work. Repetition escalates only after `max_identical_attempts`,
  and oscillation escalates by definition because a revisited state means the
  strategy is not converging.
* **Pause by throwing on the next tool call.** Rejected in favour of the existing
  `pause_requested` flag, which already has a safe-boundary story (P2-01 disk-full)
  and an operator-facing resume path.

## Tradeoffs

* **More state to reason about.** A task now has three places that bound it: its
  task budget, its verification contract, and its recovery document. The document's
  read-only `recovery` summary on the task endpoint is how an operator sees it.
* **Exact-match memory.** A model that changes an irrelevant detail in the output
  produces a new `evidence_key`; repetition detection is defeated, and only the
  counters remain. This is documented rather than papered over.
* **Session-scoped memory.** Keyed by task id, so a new session in the same
  workspace starts fresh. The alternative — workspace-keyed memory — would carry
  failures across unrelated tasks, which is worse.
* **No file locking.** Per-task writes rely on the single-owner worker lease for
  serialization. Two processes bypassing the lease could interleave writes and lose
  an attempt; the counters and the lease both fail closed, so the loop stays
  bounded, but the history could be incomplete.

## Review Status

Not reviewed. Implemented and tested in this repository by the `M5-P2-05` session;
the security review gate in
[`../security-review-signoff.md`](../security-review-signoff.md) remains unsigned,
so no security qualification is claimed for this decision.
