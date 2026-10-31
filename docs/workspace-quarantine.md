# Workspace quarantine and trusted reconstruction (`M5-P5-01`)

Recovery after suspected contamination of a workspace. Design record:
[`docs/adr/0017-workspace-quarantine-and-trusted-reconstruction.md`](adr/0017-workspace-quarantine-and-trusted-reconstruction.md).

Module: `workspace_quarantine.py` (wired into `app.py`'s managed-edit gate, inlined into the
generated `studio.py`). Store: `$HARNESS_STORE_DIR/workspace_quarantine/`.

## What is true in this build

| Capability | Status |
| --- | --- |
| Freeze an affected workspace, preserving every byte | implemented, 18 tests |
| Make the frozen copy read-only (`0500`/`0400`) | implemented, tested |
| Refuse `write_file` / `edit_file` while a quarantine is unreleased | implemented, tested end-to-end through `_execute_harness_tool` |
| Fail closed when quarantine state cannot be read | implemented, tested |
| Reconstruct a new workspace from a trusted baseline + accepted changes | implemented, tested |
| Preserve ambiguous changes in a separate `<destination>.ambiguous` tree | implemented, tested |
| Refuse an accepted path that was never preserved | implemented, tested |
| Refuse to reconstruct over an existing directory (user work) | implemented, tested |
| Report `unresolvable_locally` on host compromise without qualified isolation | implemented, tested |
| Verify reconstruction under **qualified** isolation | **not available here** — isolation is unqualified (see `docs/m5-2-0-isolation-smoke-evidence.md`) |
| Stop a writer that bypasses M5 tools (for example a supervised shell command) | **not claimed** — the freeze covers M5 mutations only |

## Operator flow

```bash
# 1. Freeze: copies every byte, hashes it, makes the copy read-only, blocks further M5 edits.
python3 -c "
import workspace_quarantine as wq
m = wq.freeze_workspace('$WORKSPACE', store_root='$HARNESS_STORE_DIR', reason='suspected contamination')
print(m.quarantine_id, m.tree_sha256, len(m.files))"

# 2. Plan against a trusted baseline. Everything the operator does not name is 'ambiguous'.
python3 -c "
import workspace_quarantine as wq
m = wq.load_manifest('$HARNESS_STORE_DIR', 'q-...')
p = wq.plan_reconstruction(m, baseline_root='$BASELINE', accepted_paths=['src/app.py'])
print('accepted', p.accepted); print('ambiguous', p.ambiguous); print('unchanged', p.unchanged)"

# 3. Reconstruct into a NEW directory (never over existing work), withholding the rest.
python3 -c "
import workspace_quarantine as wq
m = wq.load_manifest('$HARNESS_STORE_DIR', 'q-...')
r = wq.reconstruct_workspace(m, store_root='$HARNESS_STORE_DIR', baseline_root='$BASELINE',
                             destination='$WORKSPACE-recovered',
                             accepted_paths=['src/app.py'],
                             isolation_qualified=False, host_compromise=True)
print(r.to_mapping())"

# 4. Lift the freeze only once a replacement workspace is accepted.
python3 -c "
import workspace_quarantine as wq
wq.release_quarantine('$HARNESS_STORE_DIR', 'q-...', note='reconstruction accepted')"
```

## Guarantees, precisely

- **Frozen bytes are never edited.** The tree is read-only; the manifest is `0600` in a `0700`
  record directory. Releasing a quarantine does not delete the preserved bytes.
- **Symlinks are evidence, not doors.** A symlink is recorded with its target string and its
  bytes are never copied, so a link to a host file cannot smuggle host content into the
  evidence tree.
- **Nothing is trusted by default.** A difference from the baseline is restored only if the
  operator named it *and* it is in the manifest; every other difference is written outside the
  reconstruction with an `AMBIGUOUS.md` index.
- **A modified quarantine is refused.** Accepted files are re-hashed against the manifest
  before they are copied; a mismatch aborts the reconstruction rather than installing bytes of
  unknown provenance.
- **Unresolvable means unresolvable.** With `host_compromise=True` and
  `isolation_qualified=False` the result is `recoverability: unresolvable_locally` and
  `isolated_verification: unqualified_isolation`. This is the truthful answer in this sandbox;
  nothing in the module upgrades it without a qualified isolation boundary.

## Known limits

- The freeze is enforced at the M5 managed-edit gate. A process writing to the workspace
  outside M5 tools is not stopped, and that limitation is stated in the ADR rather than
  implied away.
- Reconstruction copies the whole baseline, so a large workspace costs disk; `M5-P2-01`
  snapshot quotas are a separate control.
- The ambiguous tree keeps files, not their provenance: the module can say *that* a change was
  unattributable, not *who* made it. Attribution needs the incident record from
  `M5-P3-06` and the audit log.

## Tests

`tests/test_workspace_quarantine.py` — 18 tests: byte preservation and source immutability,
read-only frozen tree, symlink recording without following, accepted-vs-ambiguous
classification, no promotion of unaccepted changes, empty-file preservation, refusal of
unpreserved/escaping accepted paths, refusal to overwrite an existing destination, refusal of a
modified quarantine, `unresolvable_locally` reporting, active-quarantine lookup and release,
`0700`/`0600` record permissions with no temp files, duplicate quarantine IDs, unknown
`schema_version`, symlinked workspace roots, the managed-edit freeze end-to-end through the tool
surface, and fail-closed denial when the quarantine store is unreadable.
