---
id: ADR-0021
title: fault-injection-and-security-release-gate
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0021: Fault-injection and security release gate with fail-closed sign-off enforcement

## Context

`M5-P5-03` is the final mandatory verification and release-gate story in the M5 2.0
roadmap (`dependencies: M5-P5-02, M5-P3-05, M5-P4-03`). Before any v1.0 release can
be approved, the project must prove three acceptance properties together:

1. **No known critical unauthorized execution paths.** Earlier phases routed tool,
   MCP, skill-script, and consequential Caveman block calls through
   `execution_spine.DurableActionBoundary` (`ADR-0002`, `ADR-0005`), process
   containment (`ADR-0013`), scoped grants (`ADR-0014`), and MCP effect tracking
   (`ADR-0015`). However, an end-to-end call-path audit during `M5-P5-03` exposed
   three remaining boundary gaps:
   - Internal Git subprocesses in `app.py` (`_compute_git_diff`,
     `_apply_worktree_to_main`, `_inspect_git_repo`, `_create_harness_worktree`) and
     `workspace_snapshots.py` (`_wsnap_run_git`) did not override repository-local
     `core.fsmonitor`, `core.hooksPath`, `protocol.file.allow`, or `diff.external`,
     allowing an untrusted `.git/config` or `.git/hooks/post-checkout` script to
     execute on the host even when `STUDIO_EXECUTION_MODE=disabled` or policy is
     read-only `plan`.
   - `_compute_git_diff` previewed untracked workspace entries without rejecting
     symlinks or secret filenames.
   - `_apply_worktree_to_main` and Agile SDLC `.sdlc` writers did not check active
     `workspace_quarantine` holds, `plan` policy, unjoined child delegations, or
     descriptor-relative `O_NOFOLLOW` confinement.
2. **No silent user-data loss under crash and race injection.** Crashes across the
   four deterministic action boundaries (`before_intent`,
   `after_intent_before_effect`, `after_effect_before_result_commit`, and
   `before_task_completion`), concurrent user file edits during managed writes or
   `EditRollbackEngine` rollbacks, dirty main checkouts during worktree apply, and
   mid-flight MCP transport failures must preserve user bytes and block blind replay.
3. **Required security sign-off recorded or release blocked.** Per
   `docs/security-review-signoff.md` (`AS-031`), independent human security review is
   explicitly `state: deferred` and `decision: pending` until a human reviewer
   independent of the codebase authors inspects `B1-B12` and `W-1/F-1..F-6`. An
   autonomous agent must never self-sign or fabricate human sign-off; instead,
   release tooling must mechanically block publication (`release-blocked`) while
   sign-off is deferred, unsigned, agent-authored, or rejected, or while mandatory
   host qualifications (`M5-P3-01`) or artifact signatures (`AS-042`) remain unmet.

## Decision

We introduce `security_release_gate.py` (stdlib-only, `_srg_` private namespace,
inlined into `studio.py`), `scripts/security_release_gate.py`, hardened Git/worktree/SDLC
confinement in `app.py` and `workspace_snapshots.py`, and release-gate enforcement in
`scripts/release.py`:

1. **Neutralize untrusted Git config and symlink escape vectors.**
   - `_git_subprocess_env()` in `app.py` and `_wsnap_git_env()` in
     `workspace_snapshots.py` inject authoritative command-scope Git configuration
     via `GIT_CONFIG_COUNT=4` (`core.fsmonitor=false`, `core.hooksPath=/dev/null`,
     `protocol.file.allow=never`, `diff.external=`) after stripping ambient
     `GIT_CONFIG_*`, `GIT_EXTERNAL_DIFF`, and `GIT_SSH*` variables, and `_safe_git_argv`
     passes explicit `-c` overrides plus `--no-ext-diff` on diffs.
   - `_compute_git_diff` skips symlinked and secret-named untracked files, confines
     paths under `target_dir`, and applies `redact_text` before returning diff text.
   - `_apply_worktree_to_main` fails closed on `plan` policy (`403`), active
     `workspace_quarantine` on either workspace or worktree (`409`), unjoined child
     delegations (`409`), and unresolved SQLite/MCP/containment effects (`409`).
   - Agile SDLC `.sdlc` reads and writes route through `_open_workspace_entry_fd` and
     `workspace_snapshots.atomic_write_confined_bytes` (`O_NOFOLLOW`), rejecting
     `plan` policy and active workspace quarantines.
2. **Isolated, live-executed four-suite verification runner.**
   - `run_security_release_gate_async` provisions a fresh temporary directory and
     SQLite WAL store per run (zero mutation of live Studio sessions or databases)
     and executes 13 deterministic probes across `crash_matrix` (all four boundaries),
     `user_edit_races` (CAS version conflict, post-batch user edit rollback refusal,
     dirty main worktree apply refusal), `external_write_uncertainty` (orphaned
     in-flight MCP sweep to `uncertain` + operator resolution, non-idempotent timeout
     replay block), and `sandbox_adversarial` (anti-bypass on inner executors,
     malicious Git `fsmonitor`/`hooksPath`/`diff.external`, symlink/traversal/secret
     confinement, unqualified sandbox & shell wrapper smuggling refusal), while
     recording live RSS, crash-sweep latency, CAS conflict latency, SSE cursor replay
     latency, and Apple M5 24 GiB hardware qualification metrics.
3. **Fail-closed release gate (`release-blocked` vs `release-ready`).**
   - `evaluate_security_signoff_gate` parses `docs/security-review-signoff.md` and
     blocks release whenever `state != signed`, `decision != accepted`, reviewer
     fields are blank or name an AI/self-review identity, `B1-B12` are not all
     `examined` + `adequate`, any `critical`/`high` finding is `not-verified`, or
     pinned commit SHA drifts from `--expected-commit`.
   - `evaluate_release_gate` combines suite verdicts, sign-off status, roadmap
     mandatory story status (`M5-P3-01`), and supply-chain signature readiness into
     `release_decision` (`release-blocked` on the current tree), exposed via
     `GET /api/harness/release-gate`, `POST /api/harness/release-gate/evaluate`,
     `scripts/security_release_gate.py --enforce`, and `scripts/release.py gate` /
     `package --require-release-gate`.

## Alternatives

1. **Self-signing `docs/security-review-signoff.md` from automated suite output.**
   Rejected: `docs/security-review-signoff.md` §1 and `scripts/validate_security_signoff.py`
   explicitly forbid conflating author/agent checks with independent human review.
   Recording `release-blocked` satisfies `M5-P5-03` Acceptance Criterion 3 honestly.
2. **Running fault-injection probes against the live `DB_PATH` and `HARNESS_STORE_DIR`.**
   Rejected: injecting crashes or quarantines into an operator's active database risks
   interfering with live sessions; per-invocation temporary stores test the identical
   production classes with zero cross-session side effects.
3. **Relying solely on `-c` flags on selected `git` calls without `GIT_CONFIG_COUNT` in `_git_subprocess_env()`.**
   Rejected: putting highest-precedence overrides in `_git_subprocess_env()` and
   `_wsnap_git_env()` protects every internal Git subprocess uniformly, including
   callers whose positional subcommand prefix is inspected by existing tests.

## Tradeoffs

- **Release gate remains intentionally `release-blocked` in CI.** Until an independent
  human reviewer signs `docs/security-review-signoff.md`, the operator qualifies a
  container runtime for `M5-P3-01`, and a release signing key is provisioned for
  `AS-042`, `scripts/security_release_gate.py --enforce` and `scripts/release.py gate`
  exit `1`. Default invariant verification (`scripts/security_release_gate.py`) exits
  `0` so CI continuously enforces `gate_invariants_satisfied == True`.
- **Sub-second isolated suite scope.** The built-in 13-case runner executes in ~0.55s
  so operators and CI can run it on demand, complementing rather than replacing the
  full multi-thousand-test pytest suite.

## Review Status

Accepted architecture decision (`status: accepted`); independent human review is **not reviewed** (`review_status: not-reviewed`). Code and test verification are covered by [`security_release_gate.py`](../../security_release_gate.py), [`workspace_snapshots.py`](../../workspace_snapshots.py), [`app.py`](../../app.py), [`scripts/security_release_gate.py`](../../scripts/security_release_gate.py), [`scripts/release.py`](../../scripts/release.py), and [`tests/test_security_release_gate.py`](../../tests/test_security_release_gate.py).
