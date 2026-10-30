---
id: ADR-0007
title: multi-file-edit-rollback
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0007: Multi-file edit rollback by version-checked inverse patch

## Context

`M5-P2-02` made a single managed edit a durable, version-checked transaction. A
*multi-file* change is a different failure mode: the runtime writes part of a
five-file change, the run is interrupted, and the workspace is left half-edited —
new imports with no call sites, a schema bump without the migration, a test that
references a function nobody wrote. Recovery has to put the workspace back into a
consistent state.

The obvious tool, `git reset --hard`, is unusable:

1. It discards the user's uncommitted work, which `M5-P2-01` exists to preserve.
2. It is all-or-nothing across the whole tree: files outside the batch (including
   files the runtime never touched) move too.
3. It assumes the workspace is a clean Git repository at the commit the batch
   started from. Worktrees, detached states and mid-merge conflicts all break that
   assumption, and a destructive command that half-runs is worse than no recovery.

`git checkout -- PATH` is only marginally better: it forgets untracked files the
batch created and overwrites *any* newer content on those paths, including edits the
user made after the batch.

The story also has to reconcile an unknown outcome before acting: if a managed edit
action is `in_flight` or `uncertain` in the durable ledger, the runtime may still be
writing, and a rollback started then could race bytes it cannot see.

## Decision

Roll back a batch of managed edits with a **durable per-file journal plus a
version-checked inverse patch**, and gate it on **reconciliation of unresolved
actions**.

- **Durable batch journal** (`edit_rollback.py`): one atomically replaced JSON
  document per batch in the harness store (`0700` directories, `0600` files),
  holding per-path `pre`/`post` digests, the CAS blobs of both sides, the previous
  mode, the linked edit id and an explicit step state. A per-session pointer names
  the open batch.
- **Record the step before the bytes land.** The planner decides from bytes on
  disk, so a step whose write never happened is a no-op restore and a step whose
  write did happen is restored from the stored pre-image. Recording after the write
  would lose exactly the step an interrupted run needs.
- **Plan per path from current bytes**, choosing `already_reverted`,
  `delete_created`, `revert`, `revert_merged` or `conflict`. Restores are
  compare-and-swap writes against the digest the batch wrote.
- **Three-way merge instead of blind restore** when the file changed after the
  batch: `post` is the common base, the inverse hunks are applied onto the current
  text, and any hunk that collides with a user edit makes the step a conflict.
- **A refused plan writes nothing.** One blocked path blocks the whole batch and
  leaves it `unresolved` with per-path reasons, because a partial "best effort"
  rollback can leave the workspace more inconsistent than before.
- **Reconciliation gate**: an `in_flight`/`uncertain` managed-edit action for the
  same task blocks the rollback until the caller acknowledges it, and an unreadable
  ledger blocks it too. Acknowledgement is recorded in the batch notes and the
  `edit/rollback` task event.
- **Per-step durable marking** makes an interrupted rollback resumable:
  `partially_rolled_back` plus per-step state, re-planned from disk on retry.
- **No Git mutation anywhere.** The module shells out to nothing.

`M5-P2-02`'s transaction ledger stays the record of *individual* edits; the batch
journal is a second, coarser view that links to it by edit id. Duplicating the bytes
is deliberate: the merge needs both sides of each edit, and the snapshot manifest
(`M5-P2-01`) answers "what did the tree look like", not "what did this batch write".

## Alternatives

- **`git reset --hard` / `git checkout -- PATH` / `git stash`.** Rejected: all of
  them discard or overwrite user work, and the destructive ones cannot be scoped to
  a runtime batch without assuming a clean tree at a known commit.
- **Reverse-apply the agent's diffs with `git apply -R`.** Rejected: it needs
  patches as artifacts, applies to whatever the current text is (no version check),
  and mutates the index. The line-level inverse used here is the same idea with
  explicit base versions and no Git dependency.
- **Restore the `M5-P2-01` baseline snapshot wholesale.** Rejected: the baseline is
  captured before the *first* managed edit of a session, so restoring it would undo
  later, unrelated user work, and it says nothing about which paths a batch touched.
- **Best-effort partial rollback: revert the safe paths, skip the conflicts.**
  Rejected for the default path: it silently produces a third state that is neither
  before nor after, with no record of it. The explicit `unresolved` state plus a
  dry-run plan is the honest alternative.
- **Block on *any* unresolved action, including `run_bash`.** Rejected: it would keep
  a rollback blocked by an unrelated tool's ambiguity forever. Every restore is
  compare-and-swapped, so a concurrent rewrite is caught at the rename; the gate
  covers what it can actually reason about (managed edits) and warns about the rest.
- **A new SQLite table for batches.** Rejected: batch documents are small, written
  whole, and read whole; an atomic JSON replace gives the same durability without a
  schema migration, and keeps the edit history out of the exported database.

## Tradeoffs

- The CAS now holds both sides of every managed edit and has no garbage collection
  yet, so the store grows with edit volume until a retention story adds pruning.
- The merge is line-based and conservative: a user edit inside a region the batch
  wrote is always a conflict, even when a human merge would be obvious. Refusing is
  the intended direction.
- A batch is scoped to one workspace root and one session; a rollback refuses a
  batch owned by another session. Cross-session rollback is not supported.
- Paths in the secret/sensitive set are never journalled, so they are never rolled
  back — the batch view is deliberately narrower than the workspace.
- The reconciliation gate reasons about the ledger (`in_flight`/`uncertain`), not
  about an OS-level write in progress; a write from outside the managed-edit path is
  caught only by the compare-and-swap at restore time.
- Verified facts (tests, 2026-10-30): full rollback of replaced and created files,
  dry-run planning without writes, refusal on a later user edit with no partial
  writes, merged rollback around unrelated user edits, refusal when a created file
  was edited or a file was deleted afterwards, symlinked-target refusal, interrupted
  multi-file edit and interrupted rollback resume, blocked-reason abortion, journal
  privacy/marking rules, and the tool surface including the reconciliation gate and
  fail-closed ledger read — `tests/test_edit_rollback.py`. Not verified: concurrent
  processes writing the same workspace, and CAS retention behaviour over long runs.

## Review Status

Not reviewed. Record review status separately from decision status. When
reviewed, identify the reviewer or review role and date in both the metadata and
this section.
