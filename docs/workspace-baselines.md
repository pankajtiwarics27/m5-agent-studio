# Pre-edit workspace baselines

Status: implemented in `M5-P2-01` (2026-10-30), decision recorded in
[ADR-0006](adr/0006-workspace-baseline-snapshots.md). This page documents
observed behaviour of the shipped code: what a baseline contains, what it
deliberately does **not** contain, how it is verified, and where it stops. It is
not a backup product or a durability certification.

## Why this exists

`write_file` and `edit_file` mutate files the user owns, inside a workspace that
is usually a Git worktree but is not guaranteed to be one, and is never
guaranteed to be clean. Before this story, a managed edit kept no copy of the
previous bytes and did not notice that the user had changed the file since the
agent last read it. Uncommitted work — unstaged edits, staged-but-uncommitted
files, untracked files, deletions — had no safe copy, and the usual Git recovery
answers (`git reset --hard`, `git clean`) are exactly the destructive operations
the program forbids.

`workspace_baseline.py` (stdlib only, inlined into `studio.py`) captures a
baseline before each managed edit and proves it restores. `app.py` refuses the
edit and pauses the task when it cannot.

## Storage layout

| Path (under the store root) | Content |
|---|---|
| `baselines/<workspace_key>/bl-<id>/baseline.json` | One manifest per baseline: Git `HEAD`, index blob ids, per-file entries, `captured_at` (microsecond UTC), `manifest_sha256` |
| `baselines/<workspace_key>/cas/<aa>/<sha256>` | Content-addressed blob store, sharded by digest prefix |

`workspace_key` is the first 16 hex characters of the SHA-256 of the resolved
workspace path, so one store can hold several workspaces without collisions.
Baseline ids have the shape `bl-` plus 12 hex characters and are validated
against that pattern before any path is built from them. There is no separate
index: `list_baselines` loads the workspace directory's `bl-` manifests and
orders them by `captured_at` (microsecond-precision UTC), because ids are random
and therefore say nothing about age; `latest` takes the first record matching
the session.

The store root is `.harnesses/baselines` beside the entry point by default,
derived from `HARNESS_STORE_DIR` at call time. `STUDIO_BASELINE_DIR` overrides
it. `.harnesses/` is git-ignored, so captured user bytes can never be committed
by accident. Files are created owner-only (`0600`) inside owner-only
directories.

## What a capture records

The candidate set comes from Git, then from explicit operator opt-ins:

- `git status --porcelain -z --untracked-files=all --no-renames` contributes
  unstaged modifications (`modified`), staged additions and changes
  (`staged_*`), untracked files (`untracked`), and deletions (`deleted`,
  `staged_deleted`). Ignored files are **not** enumerated: `--ignored` is never
  passed.
- `git ls-files -s` contributes the staged blob id for each path, so a staged
  change is preserved exactly as staged, not as the worktree happens to look.
- `focus_paths` (the file about to be edited) and `protected_ignored`
  (deliberate operator opt-ins) are added to the candidate set even when Git
  would not list them — an ignored file is invisible to `status`, so it can only
  be captured because someone named it.

Per entry the manifest records: workspace-relative path, change kind, SHA-256
digest, size, mode, whether the bytes were captured, a `capture_reason`, and
flags for `sensitive` / `ignored`. A deletion is preserved from the index blob
(`git cat-file`) or, when no index entry exists, from `HEAD`
(`baseline/<sha>`), so removing a file is reversible even though the worktree
copy is gone.

Capture is refused when a caller-supplied path is absolute, contains `..`, or
resolves outside the workspace (`BaselineUnsafePathError`); the path is validated
raw, never silently normalized into a different file.

## What is deliberately not captured

| Case | Recorded as | Restorable |
|---|---|---|
| Secret-named files (`.env*`, `.netrc`, `id_*`, `*.pem`/`*.key`/`*.p12`, `secrets*`, `credentials*`, `*api-token*`/`*api-key*`) | metadata only: path, size, digest | **no** — even if listed in `protected_ignored` |
| Ignored files that Git listed | metadata only (`ignored_not_copied`) | no |
| Ignored files explicitly listed in `protected_ignored` | full bytes (`change=protected`) | yes |
| Symlinks | `symlink_not_followed` | no |
| Files larger than `max_file_bytes` (2 MiB default) | `exceeds_snapshot_size_limit` | no |
| Non-regular files (sockets, FIFOs, devices) | `not_a_regular_file` | no |

The guarantee is therefore precise: **we never silently overwrite a path whose
current bytes we cannot restore.** A managed edit whose target lands in the
"not restorable" table is refused before the write, with a
`baseline/path_unrestorable` task event, instead of proceeding.

Captures are also bounded in size: the sum of the bytes a capture expects to
copy plus 8 MiB of headroom must still fit in the store's free space, otherwise
the capture raises `BaselineStorageExhausted` before writing anything.
`ENOSPC`/`EDQUOT` during a write maps to the same error.

## Restore and verification

There is no "restore into the workspace" operation, by design: rewriting the
user's tree from a snapshot is a destructive operation that this story does not
authorize. Instead, `verify()` reconstructs the snapshot into a caller-supplied
scratch directory, re-hashes every materialized file, and reports:

- `verified_paths` — blobs read and hashed successfully;
- `restored_paths` — files materialized into the scratch directory and re-hashed
  after writing;
- `missing_blobs`, `digest_mismatches` — CAS contents that are gone or were
  altered;
- `unrestorable_paths` — entries that were metadata-only by design;
- `checked_bytes`.

`ok` is true only when nothing is missing and nothing mismatched. The HTTP routes
always pass a `tempfile.TemporaryDirectory`, so a verification never writes into
the workspace. The manifest itself is checked against `manifest_sha256`, so a
hand-edited manifest is detected rather than believed.

## Fail-closed edit path

Before `write_file` / `edit_file` touches a file, `_ensure_pre_edit_baseline`
runs:

1. Load the newest baseline for this session and workspace.
2. If the target's current digest equals the digest recorded there, the existing
   snapshot already covers it — no new capture.
3. If the current bytes are exactly what our own previous managed write left
   behind (remembered process-locally), no new capture either: nothing the user
   authored is at risk and snapshotting every managed write would only grow the
   store.
4. Otherwise capture a fresh baseline focused on that path. If the target does not
   exist yet, there is no user work to lose and the edit proceeds.
5. Any failure — disk exhaustion, unreadable target, capture error, or a target
   recorded as unrestorable — refuses the edit and pauses the task.

Task events: `baseline/captured`, `baseline/storage_exhausted`, `baseline/failed`,
`baseline/path_unrestorable`, `baseline/verified`,
`baseline/verification_failed`.

## HTTP surface

All three routes are authenticated like the rest of `/api/harness`, and resolve
the workspace from the stored session (`worktree_dir`), never from the request.

| Route | Behaviour |
|---|---|
| `POST /api/harness/sessions/{session_id}/baseline` | Capture on demand. Optional JSON body: `reason`, `focus_paths`, `protected_ignored` (each capped at 64 entries). Returns the manifest plus a restore verification. `507` when the store is out of space (and the task is paused), `400` for an unsafe path, `404`/`409` for an unknown session or a workspace that is not a directory. |
| `GET /api/harness/sessions/{session_id}/baseline?limit=10` | Newest baseline for this session plus recent history (`limit` clamped to 1–50). |
| `POST /api/harness/sessions/{session_id}/baseline/{baseline_id}/verify` | Re-verify a stored baseline into a throwaway directory. `404` when the baseline does not exist, `409` when the manifest or a blob is corrupt. |

## Git safety

`_Git` runs `shell=False` with `GIT_OPTIONAL_LOCKS=0`, a 20 s timeout, and an
allowlist of read-only plumbing (`rev-parse`, `ls-files`, `status`,
`check-ignore`, `cat-file`, `show`, `ls-tree`, `diff`, `hash-object`). A
denylist of mutating subcommands (`reset`, `clean`, `checkout`, `stash`,
`restore`, `rm`, `switch`, `rebase`, `merge`, `apply`, `commit`, `add`, `prune`,
`gc`, `reflog`, `update-ref`, `write-tree`, `read-tree`, `branch`, `push`,
`pull`, `fetch`, `filter-branch`) is refused first and wins over the allowlist.
Baseline capture never writes to the repository, its index, or its refs.

## Limits and non-guarantees

- **No garbage collection yet.** The CAS only grows; there is no prune or GC
  surface and no user-facing quota. `max_file_bytes` and the free-space headroom
  bound a single capture, not the total.
- **Secret-named and ignored files are not recoverable.** Only their metadata is
  recorded. That is intentional, and it is why such paths are refused for edits.
- **The managed-write note is process-local.** After a restart the next managed
  edit captures one extra baseline. That costs bytes, never correctness.
- **Not verified:** network filesystems, several processes capturing the same
  workspace concurrently, workspaces larger than the free-space headroom allows,
  and stores with thousands of baselines.
- **Deploy and temp directory handling is out of scope** for this story
  (`M5-P2-02`).

## Evidence

`tests/test_workspace_baseline.py` — 15 tests, part of `pytest -q` (842 passed,
1 skipped, 2026-10-30). They cover: capturing staged/unstaged/untracked/deleted
work with digest equality against the worktree, `HEAD` and index recording,
metadata-only handling of secret and ignored files, `protected_ignored` capture,
byte-identical restore into a scratch directory, tamper detection, refusal of
`reset --hard`, storage-exhaustion pausing instead of proceeding, absolute and
escaping focus paths being rejected, newest-capture ordering, and the three HTTP
routes including their
`401`/`404`/`409` behaviour. The exact commands, exit codes, and per-acceptance-
criterion evidence for the story are recorded in its `tracking` block in
[`product/M5_2_0_Autonomous_Roadmap.json`](../product/M5_2_0_Autonomous_Roadmap.json).
