# Version-aware managed edits

Status: implemented in `M5-P2-02` (2026-10-30), decision recorded in
[ADR-0006](adr/0006-version-aware-managed-edits.md). This page documents observed
behaviour of the shipped code: what an edit is allowed to assume, what it records,
how a conflict is reported, and where the guarantees stop.

## Why this exists

`M5-P2-01` made user work recoverable: before `write_file` / `edit_file` mutates a
target, the runtime snapshots its bytes. Recoverable is not the same as never
lost. An edit is computed against a *version* of a file the agent read earlier; if
the user saved the file in between, applying the edit silently reverts their work
(from a stale `write_file`) or merges into bytes the agent never saw (from a stale
`edit_file`), and nothing durable said which bytes were replaced.

`M5-P2-02` treats a managed edit as a compare-and-swap on content and records it
as a transaction.

## The version contract

Every managed edit resolves exactly one **expected version**, in this order:

| Priority | Source | When it exists |
|---|---|---|
| 1 | `expected_sha256` argument | The caller pins the version explicitly (accepted as 64 hex characters, optionally `sha256:`-prefixed, case-insensitive) |
| 2 | Observed version | `read_file` delivered this path to this session at some point (survives restarts) |
| 3 | Last written version | This runtime applied a managed edit to this path (from the transaction ledger) |
| — | none | The target was never read and never written: a first write |

The first expectation that exists decides. If it does not equal the current
on-disk digest, the edit is refused with `error_code: "stale_version"` and nothing
is written. A malformed `expected_sha256` is refused with
`error_code: "invalid_expected_sha256"` — never ignored.

The expectation is enforced twice:

1. **Up front**, before any snapshot or read, so a stale edit is cheap to refuse;
2. **At the rename**, by re-hashing the file through the same directory descriptor
   immediately before `os.replace`. A save that lands between the check and the
   replace is still caught (`conflict_source: "edit_race"`), the temporary file is
   unlinked, and the workspace is unchanged.

## `read_file` reports the version

`read_file` returns a `sha256` field and appends `sha256: <digest>` to its output.
The digest is computed from the bytes on disk (not from the decoded text), and it
is recorded as the session's observed version for that path. The agent can quote
it back as `expected_sha256` to pin the version explicitly.

## Applying an edit

`write_file` and `edit_file` return, on success:

| Field | Meaning |
|---|---|
| `ok` | the bytes were replaced |
| `sha256` | digest of the bytes now on disk |
| `previous_sha256` | digest that was replaced (`null` for a create) |
| `edit_transaction` | the durable transaction record (see below) or `null` when the ledger append failed |
| `diff` | unified diff snippet of the change |

A managed edit also:

1. lazily snapshots the pre-edit target into the `M5-P2-01` content-addressed
   store (unchanged behaviour);
2. applies the compare-and-swap replacement;
3. records the transaction durably;
4. marks the written digest as the session's observed version;
5. appends an `edit/applied` task event.

## Durable records

Layout, under the harness store directory (`HARNESS_STORE_DIR`, `.harnesses/` by
default, git-ignored):

| Path | Content |
|---|---|
| `edits/<session>.jsonl` | One transaction per line: `edit_id`, `seq`, `session_id`, `task_id`, `tool`, `rel_path`, `pre_sha256`, `post_sha256`, `pre_existed`, `expected_sha256`, `observed_sha256`, `mode`, `bytes_written`, `recorded_at` |
| `edits/<session>.observed.json` | Path → `sha256` for the versions `read_file` handed the agent, atomically replaced, bounded to 2048 entries (oldest evicted) |

Both are owner-only (`0700` directory, `0600` files). Ledger appends use
`O_APPEND` + `fsync`; the observed index is written to a temp file and
`os.replace`d. A ledger larger than 4 MiB is rotated to the newest 2000 records.
A torn line (crash mid-append) is counted by
`EditTransactionLedger.count_unreadable_lines` and skipped — it is never repaired
into a plausible record, and the next `seq` continues after the newest valid row.

Reading the history:

```python
import edit_transactions

ledger = edit_transactions.EditTransactionLedger(Path(".harnesses"))
ledger.list_transactions("hn-abc123")                 # newest first
ledger.latest_for_path("hn-abc123", "app.py")         # newest edit of one path
```

## Conflicts and refusals

| `error_code` | Cause | What the caller should do |
|---|---|---|
| `stale_version` | the expected version no longer matches (`conflict_source` says `explicit`, `observed_read`, `last_write`, `write_race`, or `edit_race`) | call `read_file` again, re-apply to the current content, optionally pass `expected_sha256` |
| `invalid_expected_sha256` | the argument is not a 64-character hex digest | pass a real digest, or omit the argument |
| `unauthorized_path` | absolute path, `..` traversal, symlinked file or directory, or a secret path | use a workspace-relative path to a regular file |
| `disk_full` | `ENOSPC` / quota exhaustion during snapshot or write | free space; the task is paused at a safe boundary |

Every refusal writes nothing: the workspace, the ledger and the observed index are
unchanged, and the tool result carries the expected/current digests plus a
recovery hint. `edit/conflict` is appended to the task-event stream so the refusal
is visible in the transcript.

## Task events

| Event | Payload |
|---|---|
| `edit/applied` | `edit_id`, `seq`, `path`, `tool`, `pre_sha256`, `post_sha256`, `bytes` (idempotent via `edit_id`) |
| `edit/conflict` | `path`, `tool`, `expected_sha256`, `current_sha256`, `source` |
| `edit/ledger_failed` | `path`, `tool`, `reason` — the bytes were written but the transaction could not be recorded |

## Limits and non-guarantees

- **One application process per session.** The sequence cache and store locks are
  process-local, matching the durable event stream and the worker-lease model.
  Concurrent writers from separate processes are not supported.
- **A ledger failure cannot un-write an edit.** The tool result then carries
  `edit_transaction: null` and an `edit/ledger_failed` event; the gap is visible,
  not silent.
- **Partial reads are conservative.** `read_file` on a line range still records
  the whole-file digest, so a user edit outside the range causes a conflict.
- **First writes are not version-checked.** A target that was never read and never
  written is created or replaced; `M5-P2-01`'s CAS snapshot is what makes the
  previous bytes recoverable in that case.
- **Not verified:** multi-process concurrent edits, network filesystems with weak
  rename atomicity, and Windows (the confined-write path requires directory
  descriptors). Multi-file inverse-patch rollback is `M5-P2-03`.

## Evidence

`tests/test_edit_transactions.py` — 19 tests, part of `pytest -q` (854 passed,
1 skipped, 2026-10-30):

- expected-hash parsing and enforcement (matching applies, mismatching and
  malformed refuse without writing);
- a concurrent user edit detected through the observed version and, after
  clearing it, through the last written version;
- a save landing between the check and the rename caught by the compare-and-swap;
- `read_file` reporting the digest it delivered, and the documented re-read
  recovery loop;
- absolute, traversal, symlink (inside and outside the workspace) and secret-path
  refusals, with nothing written outside the workspace and no leftover temp files;
- ledger durability across reload, torn-line reporting, ordering, rotation bounds,
  private permissions, and unsafe session/path rejection;
- observed-version durability, bounding and session scoping;
- `edit/applied` / `edit/conflict` on the task stream;
- the standalone bundle inlining the module.

Exact commands, exit codes and per-acceptance-criterion evidence for the story are
recorded in its `tracking` block in
[`product/M5_2_0_Autonomous_Roadmap.json`](../product/M5_2_0_Autonomous_Roadmap.json).
