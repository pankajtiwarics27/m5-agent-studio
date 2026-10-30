# Edit rollback and action reconciliation

Status: implemented in `M5-P2-03` (2026-10-30), decision recorded in
[ADR-0007](adr/0007-multi-file-edit-rollback.md). This page documents observed
behaviour of the shipped code: what a rollback restores, what it refuses, and
where the guarantees stop.

## Why this exists

`M5-P2-02` records every applied managed edit as a durable, version-checked
transaction. That answers *what did this one edit do*. It does not answer the
question a half-finished multi-file change raises: the runtime wrote three of five
files and then stopped — how do we get back to a consistent state **without
`git reset --hard`**?

A destructive reset is not an option here: the workspace also holds the user's own
uncommitted work, which the reset would discard along with the runtime's edits.
`M5-P2-03` therefore builds a targeted inverse: the journal knows which files the
batch touched and what bytes each one held, so only those bytes move.

## The batch journal

`edit_rollback.py` (standard library only, inlined into `studio.py`) keeps one
document per batch under `.harnesses/edits/batches/` (directories `0700`, files
`0600`, atomic replace):

| Field per step | Meaning |
|---|---|
| `rel_path` | confined workspace-relative path of the edited file |
| `tool` | `write_file` or `edit_file` |
| `pre_sha256` / `pre_blob` | digest and CAS blob of the bytes the edit replaced (`null` for a create) |
| `post_sha256` / `post_blob` | digest and CAS blob of the bytes the edit wrote |
| `pre_mode` | the previous file mode, restored with the bytes |
| `state` | `applied`, `rolled_back`, `unresolved`, `skipped` |

A pointer at `.harnesses/edits/open/<session>.json` names the session's open batch;
`list_edit_batches` exposes the batch ids, their state, and the applied/total step
count. A batch is opened lazily by the first managed edit of a session and closed
when the workspace root changes.

**The step is recorded before the bytes land.** That ordering is what makes an
interrupted edit recoverable in both directions: a step whose write never happened
reads back as already being at its pre-image (a no-op restore), and a step whose
write did happen is restored from the stored pre-image bytes. Nothing depends on
an in-memory record surviving the crash.

Editing the same path twice inside one batch keeps the **first** pre-image and
advances the written side, so rolling back a batch restores what the file held
before the batch, not the intermediate state.

## Planning a rollback decides per path

`rollback_edits` plans before it writes, and the plan is decided from bytes on
disk, not from the journal's claim:

| Decision | Condition | Action |
|---|---|---|
| `already_reverted` | current bytes equal `pre_sha256` | nothing to write; step marked rolled back |
| `delete_created` | the batch created the file and it still holds `post_sha256` | remove the file |
| `revert` | current bytes equal `post_sha256` | restore `pre_sha256` bytes and mode |
| `revert_merged` | current bytes differ, but no region the batch wrote was touched | apply the inverse hunks around the user's later edits |
| `conflict` | a region the batch wrote was also changed, the file was deleted, or the path is no longer safe | refuse |

The three-way merge uses `post` as the common base: hunks that turn `post` back
into `pre` are applied onto the current text, alongside whatever the user changed
since. Disjoint hunks both apply, so a user appending to a file still gets their
line after the rollback. When a revert hunk and a user edit touch the same base
lines and disagree, the merge refuses rather than guessing.

Restores are compare-and-swap writes (`atomic_write_confined_bytes` with
`expected_sha256`): the digest is re-checked through the same directory descriptor
immediately before the rename, so a save landing between planning and applying is
caught and the write is refused rather than overwriting it.

**A refused plan writes nothing at all** — not even the steps that were individually
safe. The batch is left `unresolved` with one reason per blocked path.

## Reconciliation before rollback

An `in_flight` or `uncertain` managed-edit action means the durable ledger does not
know whether the runtime's write happened. Undoing a batch then could race bytes
nobody has seen, so:

1. `reconcile_task_actions` lists the task's unresolved actions and says whether a
   rollback would be blocked.
2. `rollback_edits` refuses while an unresolved **managed edit** (`write_file` /
   `edit_file`) exists for the task, and records the refusal in the batch notes.
3. An operator or agent that has established the outcome can pass
   `acknowledge_unresolved: true`; the acknowledged action ids are written to the
   batch notes and the `edit/rollback` task event.
4. If the ledger cannot be read at all, the rollback is blocked: fail closed.

Unresolved actions of *other* tools are reported as warnings, not blockers:
rollback only touches managed-edit targets, and every restore is compare-and-swapped,
so a concurrent rewrite surfaces as a refusal instead of a loss.

The tool excludes its own ledger row from this check — the call asking the question
is recorded `in_flight` by the boundary for the duration of the call, and that is
not evidence of an unknown outcome for anyone else.

## Interruption and resumption

Applying is per step and durable: each step is marked `rolled_back` in the batch
document before the next one starts. A rollback interrupted part-way leaves the
batch `partially_rolled_back` with the failed step `unresolved`; re-running the
rollback plans again from disk and only redoes the steps that are still applied.

## Tool surface

| Tool | Effect |
|---|---|
| `list_edit_batches` | read-only listing of this session's batches (`batch_id`, state, applied/total, paths) |
| `reconcile_task_actions` | read-only report of unresolved actions for a task and whether rollback is blocked |
| `rollback_edits` | plan (`dry_run: true`, the default) or apply (`dry_run: false`) a batch |

`rollback_edits` is denied by the read-only `plan` policy and is classified as a
managed edit by the execution boundary, so it crosses the same admission,
audit and intent-ledger path as `write_file`. It refuses a batch that belongs to
another session. Applied and refused rollbacks are published as `edit/rollback`
task events.

## Where the guarantees stop

- Rollback covers **managed edits only** (`write_file` / `edit_file`). Files
  changed by `run_bash`, external tools or the user's editor are outside the batch
  and are never reverted; they only matter as changes the plan must not overwrite.
- Secret and sensitive paths are never journalled, so they can never be rolled back;
  the batch view is narrower than the workspace by design.
- The pre- and post-edit blobs are not garbage-collected yet, so the CAS grows with
  edit volume until a retention story adds pruning.
- A conflicting plan is a refusal, not a resolution: the workspace stays in the
  partially-edited state with an explicit `unresolved` record for a human or a later
  turn to finish.
- No Git operation is performed by rollback — no `checkout`, `reset`, `clean` or
  `restore`.
