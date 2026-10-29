# Versioned packaging, update, and rollback (AS-040)

**Story:** AS-040 — *macOS packaging updates and rollback*.
**Acceptance criterion:** versioned install/update/rollback smoke tests pass.

The packaged artifact is the deterministic single-file `studio.py` built by
`scripts/build_studio.py` (AS-014). This document covers the lifecycle around
that artifact: packaging it with provenance, installing a version, updating,
rolling back, and verifying the result. The mechanics are platform-neutral, so
the same smoke tests run on Linux CI and on a macOS host.

## 1. What exists

| Piece | Contract |
|---|---|
| `release_manager.py` | Pure lifecycle library (mypy-gated): manifests, atomic install/activation, update with downgrade guard, toggle rollback, verification, pruning. No network, no shell, no root. |
| `scripts/release.py` | CLI: `package`, `install`, `update`, `rollback`, `verify`, `status`, `prune`. |
| `tests/test_release_manager.py` | 39 tests: install/update/rollback smoke tests, integrity failures, traversal rejection, staging hygiene, CLI end-to-end, and standalone self-containment. |

## 2. Layout and provenance

```text
~/.local/share/m5-agent-studio/          # $ROOT, or --root
├── current.json                         # {"version": "9.0.2", "artifact": "studio.py", "sha256": ...}
├── previous.json                        # rollback target, same shape
└── versions/
    ├── 9.0.1/
    │   ├── studio.py                    # immutable installed artifact
    │   └── manifest.json                # version, sha256, size, created_at, python_requires, source_commit
    └── 9.0.2/...
```

`manifest.json` is written by `package` from the real bytes on disk
(`build_manifest` never guesses a digest) and carries the source commit, so an
installed release can be tied back to a Git revision. Signing, notarization,
SBOM, and provenance attestations are AS-042 and are **not** claimed here.

## 3. Operations

```bash
# Build (or accept) the standalone artifact and record its digest + commit
python scripts/release.py package --version 9.0.1
python scripts/release.py package --version 9.0.1 --artifact /path/to/studio.py   # prebuilt

# Install and activate
python scripts/release.py install --version 9.0.1 --root ~/.local/share/m5-agent-studio

# Update (refuses to downgrade unless --allow-downgrade)
python scripts/release.py update --version 9.0.2

# Revert to the previous release (repeatable: it swaps)
python scripts/release.py rollback

# Inspect and verify digests of the active and previous releases
python scripts/release.py status
python scripts/release.py verify            # exit 1 when unhealthy

# Delete superseded releases, keeping the active release and rollback target
python scripts/release.py prune --keep 2
```

Run an installed release with `python <root>/versions/<version>/studio.py`.

## 4. Design decisions and tradeoffs

1. **Content-addressed trust.** Every activation re-hashes the artifact and
   compares size + SHA-256 against the manifest. A mismatch aborts *before* any
   pointer change, so a half-copied or tampered artifact can never become active.
2. **Atomic activation, never a partial tree.** The artifact is copied into a
   `versions/<v>` staging directory, verified, and only then moved into place with
   `os.replace`; the pointer is replaced the same way. An interrupted update
   leaves the previous release live, and staging directories are cleaned up on
   both success and failure (asserted by tests).
3. **Rollback verifies its target.** `previous.json` is checked against the
   target's own manifest and artifact digest. A corrupted older release is refused
   instead of activated — the failure mode that matters when a rollback is
   attempted precisely because something is wrong.
4. **Rollback is a toggle.** It swaps `current` and `previous`, so a rollback that
   makes things worse can be undone with the same command. There is no hidden
   "third" slot to forget.
5. **No silent downgrades.** `update` refuses an older version unless
   `--allow-downgrade` is passed deliberately; rollback is the supported path, and
   keeping them separate keeps both auditable.
6. **Non-destructive by default.** Superseded versions are retained; `prune` is a
   separate explicit command that always keeps the active release and the rollback
   target.
7. **Strict version strings, strict paths.** `MAJOR.MINOR.PATCH` only, anchored
   with `\A…\Z`, with no whitespace stripping — `"../9.0.1"`, `"9.0.1/../.."`,
   `"9.0.1\n"`, and `"v9.0.1"` are all rejected so a version can never escape the
   `versions/` directory.
8. **Isolation.** The library is pure `pathlib`/`os`/`shutil`; it does not consult
   the network, the keyring, or Studio's database, and the CLI passes no user data.

## 5. Artifact self-containment (a bug this story caught)

Packaging the artifact surfaced a regression from AS-039: `app.py` had gained
`import onboarding_preflight`, but `scripts/build_studio.py` only inlined
`history_privacy`, `mcp_client`, `context_store`, and `workflow_orchestration`.
The standalone `studio.py` therefore failed with
`ModuleNotFoundError: No module named 'onboarding_preflight'` as soon as it was
run outside the checkout. CI did not notice because `smoke_local.py --standalone`
ran the artifact from the repository root, where the sibling module was importable.

Two fixes, both kept:

1. `build_studio.py` owns an explicit `INLINED_MODULES` tuple, and
   `onboarding_preflight` is inlined and re-aliased like the other boundaries.
2. The guard is behavioural, not just a list: `smoke_local.py --standalone` now
   copies the artifact into an isolated temporary directory and runs it there with
   no checkout on `PYTHONPATH`, and two tests fail if `app.py` imports a local
   module the builder does not inline, or if the generated artifact still imports
   one. Reverting the builder fix reproduces both failures.

## 6. What is not claimed

- **No signing or notarization.** Neither the artifact nor the manifest is
  cryptographically signed; AS-042 owns SBOM/signing/provenance. Treat the digest
  as an integrity check against corruption, not against a hostile distributor.
- **No macOS-specific service integration.** No `.app` bundle, `launchd` agent, or
  installer package is produced, and no Keychain interaction happens during
  install. If a `.app`/`launchd` wrapper is added later it must call the same
  versioned layout rather than inventing a second one.
- **No live upgrade of a running process.** Studio must be restarted against the
  new `versions/<v>/studio.py`; this tooling does not hot-swap a running server,
  and the database schema (`schema_migrations`) remains the server's concern.
- **macOS execution is deferred.** The smoke tests run in CI on Linux and on the
  maintainer's final end-to-end pass on macOS (consistent with the AS-031
  deferral). A macOS run is evidence for that host, not a matrix.

## 7. Verification

```bash
python -m pytest -q tests/test_release_manager.py     # 39 passed
python scripts/smoke_local.py --standalone            # runs the artifact isolated from the checkout
```

Full suite in this workspace: `534 passed, 1 skipped` (the opt-in live container
test). The CLI was also exercised by hand against a real artifact:

```text
package 9.0.1 -> install -> update 9.0.2 (previous: 9.0.1) -> rollback (9.0.1)
  -> rollback (9.0.2) -> tamper 9.0.1 -> verify: exit 1, "previous 9.0.1: Artifact
  size mismatch ... expected 730760, found 730770"
```
