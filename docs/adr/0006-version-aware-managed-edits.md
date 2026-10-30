---
id: ADR-0006
title: version-aware-managed-edits
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0006: Version-aware managed edits and durable edit transactions

## Context

`M5-P2-01` made user work *recoverable*: before a managed `write_file` /
`edit_file`, the runtime snapshots the target's bytes into a content-addressed
store and can restore them. Recoverable is not the same as *never lost*. The edit
path itself still had three gaps:

1. **No version.** The agent reads a file, reasons about it, and later calls
   `edit_file` with `old_text`. If the user saved the file in between — or the
   runtime itself wrote it in another turn — the tool applied a transform computed
   against bytes that no longer existed. For `write_file` the effect is worse:
   the whole file is replaced with content derived from the stale read, so the
   user's newer work is silently reverted even though a snapshot exists somewhere.
2. **No compare-and-swap.** Reads and writes happen at different moments. Even
   when the caller knows the version it expects, the check and the replacement
   were separate operations, so a save landing in between was still overwritten.
3. **No record.** After an edit, nothing durable said which bytes were replaced
   by which, under which tool, exchange or snapshot. Recovery and reconciliation
   had to trust an in-memory claim or diff the tree.

`M5-P2-02` requires that a concurrent user edit produces a safe conflict rather
than an overwrite, that an expected hash is enforced, and that unauthorized paths
are rejected.

## Decision

Treat every managed edit as a **compare-and-swap on content**, recorded as a
durable transaction.

- **Three levels of version expectation**, in order of authority: the caller's
  explicit `expected_sha256` argument, then the digest `read_file` last handed
  this session for that path (kept durably in an observed-version index), then
  the digest this runtime last wrote there (from the transaction ledger). The
  first expectation that exists decides; a mismatch is a `stale_version`
  conflict and nothing is written. Only a target that was never read and never
  written has no expectation, which is why creating a new file still works.
- **The expectation is enforced at the rename**, not only up front. Both atomic
  writers re-hash the current bytes through the same directory descriptor
  immediately before `os.rename` and refuse on mismatch, so a save that lands
  during the edit is caught (the temporary file is unlinked and the workspace is
  left untouched).
- **`read_file` reports and records the version it delivered** (`sha256:` in the
  output plus a `sha256` field), hashed from the bytes on disk rather than from
  the decoded text, so the digest the agent can quote back is the digest the
  writer compares.
- **Durable edit transactions** (`edit_transactions.py`, stdlib only):
  append-only per-session JSONL in the `edits/` directory of the harness
  store, `O_APPEND` +
  `fsync`, owner-only (`0600` files in a `0700` directory), bounded by rotation
  to the newest records. Each row carries `edit_id`, monotonic `seq`,
  session/task ids, tool, path, pre-edit digest (absent for a create), post-edit
  digest, the expected and observed digests the decision was based on, mode, size
  and timestamp. A torn line is counted, never repaired into a plausible record.
- **Durable observed versions**: a per-session JSON index (atomic replace) with a
  bounded number of entries, so the expectation survives a restart — the agent's
  transcript does too, and a restart must not silently downgrade "I read version
  X" to "no expectation".
- **Structured refusals**: `error_code` is `stale_version` (with expected,
  current and observed digests, and the source of the expectation),
  `invalid_expected_sha256`, or `unauthorized_path` for absolute paths, `..`
  traversal, symlinked leaves or directories, and secret paths. `write_file` /
  `edit_file` results also carry `edit_transaction`, `sha256` and
  `previous_sha256` so the caller never has to guess what changed.
- **Task events** `edit/conflict`, `edit/applied` (with `edit_id`, digests, size)
  and `edit/ledger_failed` make the decision visible on the durable stream the UI
  already renders.

`M5-P2-01`'s snapshot capture stays exactly where it was: this ADR adds a *version*
check in front of it, not a second copy of the bytes.

## Alternatives

- **Refuse any edit to a file the agent has not read in this turn.** Rejected: it
  would break legitimate create-then-edit flows and turn a safety feature into a
  workflow tax; the observed-version + last-written layers already cover the
  cases where a real user change can be lost.
- **Rely on file mtimes or sizes instead of digests.** Rejected: `mtime` has
  coarse resolution, moves backwards under `rsync`/`cp -p`, and says nothing
  about *content*; a same-second save would be invisible.
- **Check the version before writing without re-checking at the rename.**
  Rejected: that is exactly the read-then-write race this story exists to close.
- **Store transactions in SQLite (schema v4).** Rejected for this story: the
  ledger is append-only, per-session, and bounded, which a JSONL file with
  `O_APPEND` + `fsync` already provides without a migration; SQLite would also
  put the edit history inside the exported database.
- **Have the writer restore the file on conflict.** Rejected: a conflicting edit
  never wrote anything, so there is nothing to undo, and restoring would replace
  the user's newest bytes with an older snapshot.

## Tradeoffs

- The observed-version index can be conservative: `read_file` on a partial range
  still records the whole-file digest, so a user edit *outside* the read range
  produces a conflict. Fail-closed is the intended direction — the agent re-reads
  and retries.
- The locks and sequence cache assume one application process per session, the
  same assumption the durable event stream (ADR-0005) and the worker-lease model
  already make; concurrent writers from separate processes are not supported and
  are not claimed to be.
- A ledger append failure after the bytes are on disk cannot un-write them. The
  edit is reported as applied with `edit_transaction: null` plus an
  `edit/ledger_failed` event, so the gap is visible rather than hidden.
- Verified facts (tests, 2026-10-30): expected-hash enforcement (matching,
  mismatching, malformed), concurrent user edits detected through observed and
  last-written expectations, a save landing between check and rename caught by
  the compare-and-swap, symlink/absolute/traversal/secret refusals, durable
  history across a restart, and the standalone bundle inlining the ledger — 19
  tests in `tests/test_edit_transactions.py`. Not verified: multi-process
  concurrent edits of the same workspace, network filesystems with weak rename
  atomicity, and Windows (the confined-write path requires directory descriptors).

## Review Status

Not reviewed. Record review status separately from decision status. When
reviewed, identify the reviewer or review role and date in both the metadata and
this section.
