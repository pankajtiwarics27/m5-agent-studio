---
id: ADR-0006
title: workspace-baseline-snapshots
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0006: Pre-edit workspace baselines and selective user snapshots

## Context

The harness edits user files (`write_file`, `edit_file`) inside a per-session
workspace that is usually a Git worktree, but is not guaranteed to be one, and is
never guaranteed to be clean. Before `M5-P2-01` the only protection against a
managed edit destroying user work was the agent's own care: the tool rewrote the
target file in place, kept no copy of the previous bytes, and did not check
whether the user had changed the file since the agent last looked.

Three gaps followed from that:

1. Uncommitted work (unstaged edits, staged-but-uncommitted files, untracked
   files, deletions) has no safe copy anywhere. `git` alone does not preserve it,
   and the obvious recovery tools (`git reset --hard`, `git clean`) are exactly
   the destructive operations the program forbids.
2. The worktree cannot be trusted to be a repository at all, so "just ask Git" is
   not a general answer.
3. A full copy of the workspace would indiscriminately copy ignored files —
   `.env`, private keys, local logs — into a new location, which turns a safety
   feature into a secret-spreading feature.

The story requires: capture the Git tree/HEAD plus the user's changes, snapshot
the necessary bytes into content-addressed storage, keep ignored sensitive files
out of bulk copying while still allowing deliberate protection, and pause safely
when the disk cannot hold the snapshot.

## Decision

Add a stdlib-only module, `workspace_baseline.py`, that captures a *baseline*
before every managed edit, and fail closed in `app.py` when it cannot.

- **Store location and layout.** A per-workspace directory inside the store root,
  holding one `baseline.json` per baseline (ids look like `bl-` plus 12 hex
  characters) and a content-addressed blob store under `cas/`, sharded by the first two
  hex characters of each blob digest. `workspace_key` is the first 16 hex
  characters of the SHA-256 of the resolved workspace path, so one store can hold
  many workspaces without collisions. The default root is
  `.harnesses/baselines` (`STUDIO_BASELINE_DIR` overrides it); `.harnesses/` is
  already git-ignored, so captured user bytes can never be added to a commit. The
  path is derived from `HARNESS_STORE_DIR` at call time, not import time.
- **What is captured.** The changed-path set comes from
  `git status --porcelain -z --untracked-files=all --no-renames` plus the current
  index entries (`ls-files -s`): unstaged edits, staged changes, untracked files,
  and deletions all become candidates. A deletion is preserved from the index blob
  (`git cat-file`) or, failing that, from `HEAD`, so removing a managed file is
  reversible even though the worktree copy is gone. `HEAD` commit and reference,
  the index blob ids, and file modes are recorded in the manifest.
- **Content addressing and atomicity.** Every blob is written to a `0600` temp
  file, `fsync`ed, and `os.replace`d into the CAS; the manifest is written the same
  way after all blobs. A blob is trusted only after its digest is recomputed on
  read. The manifest carries `manifest_sha256` over the canonical payload, so a
  hand-edited manifest is detectable rather than silently believed.
- **Selective copying, never bulk.** Ignored files are never enumerated in bulk;
  if one appears in the change set it is recorded as metadata only (path, size,
  digest where cheap, `ignored_not_copied`). Files whose names match the secret
  patterns (`.env*`, `.netrc`, `id_*`, `*.pem`/`*.key`/`*.p12`, `secrets*`,
  `credentials*`, `*api-token*`/`*api-key*`) are *always* metadata only, even when
  an operator explicitly lists them. An operator can opt a specific ignored path
  into capture with `protected_ignored`, which is also how `"reason": "protected"`
  entries appear. Symlinks are never followed; they are recorded as
  `symlink_not_followed`.
- **Bounded work.** Files above `max_file_bytes` (default 2 MiB) are recorded as
  metadata (`exceeds_snapshot_size_limit`) instead of being copied, so one large
  artifact cannot fill the disk.
- **Fail closed on disk pressure.** Before writing anything, a capture sums the
  bytes it expects to copy and refuses with `BaselineStorageExhausted` unless
  `expected + headroom` (default 8 MiB) still fits in the store's free space;
  `ENOSPC`/`EDQUOT` during a write maps to the same error. `app.py` answers that
  with HTTP `507`, records a `baseline/storage_exhausted` task event, pauses the
  task, and refuses the edit.
- **No destructive Git.** `_Git` runs `shell=False` with a fixed `env`
  (`GIT_OPTIONAL_LOCKS=0`), a 20 s timeout, and an allowlist of plumbing
  (`rev-parse`, `ls-files`, `status`, `check-ignore`, `cat-file`, `show`,
  `ls-tree`, `diff`, `hash-object`). A denylist of mutating subcommands
  (`reset`, `clean`, `checkout`, `stash`, `restore`, `rm`, `switch`, `rebase`,
  `merge`, `apply`, `commit`, `add`, `prune`, `gc`, `reflog`, `update-ref`,
  `write-tree`, `read-tree`, `branch`, `push`, `pull`, `fetch`,
  `filter-branch`) is refused even if it appears in the allowlist. Restoring is
  never done into the workspace: verification materializes blobs into a caller
  supplied scratch directory (a `TemporaryDirectory` for the HTTP route) and
  re-hashes every file.
- **Refresh rule before an edit.** A managed edit captures a new baseline when the
  session has none, or when the target's current digest differs from the digest
  recorded in the newest baseline. Bytes left by our own successful managed write
  are remembered process-locally (`_BASELINE_MANAGED_WRITES`) so a subsequent
  edit of that untouched content does not churn a snapshot; the note is advisory,
  and losing it (restart) only costs one extra capture. A path whose current
  bytes cannot be read at all is refused (`baseline_blocked`), never edited.

## Alternatives

- **Copy the whole workspace directory before each edit.** Rejected: it copies
  ignored secrets and build trees indiscriminately, is unbounded in time and
  space, and cannot deduplicate.
- **Rely on `git stash create` / `git commit` to a scratch ref.** Rejected: it
  mutates the repository the user owns (side refs, reflog, index), fails for
  non-repositories, and cannot represent untracked or ignored files without
  further mutation.
- **`git reset --hard` + `git clean -fd` for restore.** Rejected outright: both
  are explicitly forbidden by the program's safety rules and would destroy the
  very work the baseline exists to protect.
- **SQLite table for snapshots.** Rejected for this story: blobs are large,
  append-only, and often shared across baselines, which is exactly what a
  content-addressed file store does well; SQLite would also put user file content
  inside the database that is exported and backed up.
- **Store the `.git` directory wholesale.** Rejected: it duplicates state Git
  already has durably, scales with history, and risks copying credential helpers'
  config. Recording `HEAD` plus index blob ids is enough to describe the tree.

## Tradeoffs

- Content addressing gives cheap deduplication and tamper detection, at the cost
  of a per-workspace CAS that currently has no prune/GC surface: the store only
  grows until an operator removes it. `max_file_bytes` and the 8 MiB headroom
  bound each capture, not the total.
- Metadata-only handling of secret-named and ignored files protects secrets but
  means those exact bytes are *not* restorable from a baseline. The guarantee is
  "we never silently overwrite a path we cannot restore": such a path is refused
  (`baseline/path_unrestorable`) rather than overwritten.
- Process-local memory of our own writes avoids baseline churn but is not shared
  across processes; a restart costs an extra snapshot, never a missed one.
- Capture runs `git` synchronously before each write; for large dirty trees this
  adds latency proportional to the changed-file set (not to repository size).
- Verified facts (tests, `2026-10-30`): capture of a dirty repository preserves
  staged/unstaged/untracked/deleted work, `verify()` restores byte-identical
  copies into a scratch directory, `reset --hard` is refused, a tampered manifest
  is detected, `ENOSPC` pauses instead of proceeding, and the three HTTP routes
  enforce auth. Not verified: behaviour on network filesystems, concurrent
  captures from multiple processes, and stores larger than a few thousand
  baselines.

## Review Status

Not reviewed. Record review status separately from decision status. When
reviewed, identify the reviewer or review role and date in both the metadata and
this section.
