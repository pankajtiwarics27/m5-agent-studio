# M5 2.0 Fault-Injection and Security Release Gate (`M5-P5-03`)

**Story:** `M5-P5-03` — *Fault-injection and security release gate* (`ADR-0021`).  
**Release decision on current repository state:** **`release-blocked`** (`release_ready: false`, `gate_invariants_satisfied: true`).

This document records the executed fault-injection, user-edit race, external-write
uncertainty, and sandbox/Git adversarial verification suites; the measured
reliability, memory, and UX metrics; the review status of
[`security-review-signoff.md`](security-review-signoff.md) (`AS-031`); the
residual risk register; and the mechanical release gate enforced by
`security_release_gate.py`, `scripts/security_release_gate.py`, `scripts/release.py`,
and `/api/harness/release-gate`.

## 1. Call-path security hardenings landed in `M5-P5-03`

During the `M5-P5-03` end-to-end execution-path audit against
[`m5-2-0-bypass-inventory.md`](m5-2-0-bypass-inventory.md), three concrete host
execution and workspace-confinement gaps were identified and closed:

1. **Untrusted Git `core.fsmonitor`, `diff.external`, and hook neutralization (`app.py`, `workspace_snapshots.py`).**
   - `_git_subprocess_env()` and `_wsnap_git_env()` now strip ambient
     `GIT_CONFIG_*`, `GIT_EXTERNAL_DIFF`, and `GIT_SSH*` variables and inject
     command-scope overrides (`GIT_CONFIG_COUNT=4`: `core.fsmonitor=false`,
     `core.hooksPath=/dev/null`, `protocol.file.allow=never`, `diff.external=`),
     paired with `_safe_git_argv` and `--no-ext-diff`. Opening, diffing,
     snapshotting, or creating a worktree in an untrusted repository can no
     longer execute repository-supplied binaries when `STUDIO_EXECUTION_MODE=disabled`
     or session policy is `plan`.
2. **Untracked diff confinement and redaction (`app._compute_git_diff`).**
   - Untracked diff previews now skip `.harnesses`, `.sdlc`, `.planning`,
     symlinked files, paths outside `target_dir`, and secret-named files
     (`.env`, keys, credentials), and pass preview text through `redact_text`.
3. **Worktree-apply and `.sdlc` confinement guards (`app._apply_worktree_to_main`, `app._write_sdlc_confined_text`).**
   - `_apply_worktree_to_main` refuses `plan` policy (`403`), active
     `workspace_quarantine` holds on either workspace or worktree (`409`),
     unjoined child delegations (`409`), and unresolved SQLite, MCP, or
     process-containment effects (`409`).
   - All `.sdlc` artifact reads and writes use descriptor-relative `O_NOFOLLOW`
     (`_open_workspace_entry_fd` and `workspace_snapshots.atomic_write_confined_bytes`)
     and reject `plan` policy and quarantined workspaces.

## 2. Executed fault-injection, race, uncertainty, and adversarial matrix

Command:

```bash
.venv/bin/python scripts/security_release_gate.py --json
```

All 13 automated cases execute in an isolated temporary directory and SQLite WAL
database without touching active Studio state (`13/13` passed,
`unauthorized_executions = 0`, `silent_user_data_losses = 0`):

| Suite | Case ID | Verified invariant | Result |
|---|---|---|---|
| `crash_matrix` | `crash.before_intent` | Hard crash before SQLite intent row writes zero mutations, leaves zero `runtime_actions` rows, and preserves dirty/untracked user files byte-for-byte | `pass` |
| `crash_matrix` | `crash.after_intent_before_effect` | Hard crash after `in_flight` commit sweeps to `uncertain` on lease fencing, executes `0` times, and preserves dirty/untracked user files | `pass` |
| `crash_matrix` | `crash.after_effect_before_result_commit` | Hard crash after confined file write reconciles pre-takeover target digest, deduplicates epoch-2 replay (`exec_counter == 1`), and preserves user files | `pass` |
| `crash_matrix` | `crash.before_task_completion` | Hard crash immediately before completion commit prevents premature `completed` transition while preserving committed edits and user files | `pass` |
| `user_edit_races` | `race.cas_observed_version_conflict` | Concurrent user modification between `mark_observed` and write triggers `ObservedVersionIndex` digest mismatch without overwriting user bytes | `pass` |
| `user_edit_races` | `race.post_batch_user_edit_rollback_conflict` | Concurrent user edit on top of an applied `EditBatch` causes `EditRollbackEngine` conflict refusal (`state=unresolved`), keeping user bytes intact | `pass` |
| `user_edit_races` | `race.worktree_apply_dirty_main_conflict` | `_apply_worktree_to_main` detects uncommitted main-checkout edits and aborts sync (`status=conflict`) with zero user-data loss | `pass` |
| `external_write_uncertainty` | `mcp.orphaned_in_flight_swept_to_uncertain_and_held` | Interrupted non-idempotent MCP mutation sweeps to `uncertain` and holds until explicit operator resolution (`reconciled_not_applied`) | `pass` |
| `external_write_uncertainty` | `mcp.non_idempotent_timeout_blocks_blind_replay` | `TimeoutError` on `non_idempotent` external action records `uncertain` in `DurableActionLedger` and blocks automatic replay (`replay_blocked=True`) | `pass` |
| `sandbox_adversarial` | `adv.direct_inner_executor_anti_bypass` | Direct calls to `_execute_harness_tool_inner`, `_execute_skill_script_inner`, and consequential `run_reusable_block` without `DurableActionBoundary` token are denied | `pass` |
| `sandbox_adversarial` | `adv.git_fsmonitor_hooks_external_diff_neutralized` | Repository configured with executable `core.fsmonitor`, `diff.external`, and `.git/hooks/post-checkout` never executes payload during inspect, diff, worktree, or snapshot | `pass` |
| `sandbox_adversarial` | `adv.symlink_traversal_and_secret_confinement` | Workspace symlink escape, `.sdlc` symlink escape, `../../` traversal, and `.env` diff leakage are all blocked (`O_NOFOLLOW`) | `pass` |
| `sandbox_adversarial` | `adv.unqualified_sandbox_and_shell_smuggling_refused` | Unqualified container backend + `--privileged` container argv + `env -u HOME sh -c 'cat .env'` + `git reset --hard` all fail closed | `pass` |

## 3. Measured reliability, memory, and UX metrics

Metrics below combine live measurements from `run_security_release_gate_async()`
with the target-machine Apple M5 24 GiB hardware qualification recorded in
[`m5-2-0-hardware-qualification-evidence.md`](m5-2-0-hardware-qualification-evidence.md)
(`M5-P4-01`):

| Category | Metric | Measured value | Source |
|---|---|---|---|
| Reliability | Gate suite pass rate | `13 / 13` (`100.0%`) | `scripts/security_release_gate.py --json` |
| Reliability | Critical unauthorized executions | `0` | `sandbox_adversarial` + `external_write_uncertainty` |
| Reliability | Silent user-data loss events | `0` | `crash_matrix` + `user_edit_races` |
| Reliability | Crash-to-safe-hold sweep latency (mean / max) | `17.3 ms` / `29.2 ms` | `WorkerLeaseCoordinator.fence_and_halt` + `recover_incomplete_transitions` |
| Reliability | Full 13-case isolated gate wall time | `~566 ms` | `run_security_release_gate_async` |
| Memory | Gate process peak RSS (after suite / delta) | `86.08 MiB` (`+0.80 MiB` delta) | `resource.getrusage(RUSAGE_SELF)` |
| Memory | Target hardware unified memory (`Apple M5`) | `24.0 GiB` (`25,769,803,776` bytes) | `docs/m5-2-0-hardware-qualification-evidence.md` |
| Memory | `Gemma-4-21B-REAP-4bit` load swap growth / pressure check growth | `+1.29 GiB` cold load / `0.00 GiB` steady pressure | `docs/m5-2-0-hardware-qualification-evidence.md` |
| Memory | Single-heavy-model admission & sequential switching | `pass` (`first=admitted; second=refused; after_release=admitted`) | `p4.single_heavy_model`, `p4.admission_wired` |
| UX | Cold vs warm first-token latency (`Gemma-4-21B-REAP-4bit` on M5) | `16,893.7 ms` cold / `159.4–185.0 ms` warm | `docs/m5-2-0-hardware-qualification-evidence.md` |
| UX | Durable task-state read latency (max) | `1.05 ms` | `RuntimePersistenceStore.get_task` |
| UX | CAS user-edit conflict detection latency | `0.17 ms` | `ObservedVersionIndex.expected_for` |
| UX | SSE `TaskEventLog` cursor replay latency | `0.13 ms` (`exact=true`) | `TaskEventLog.read(cursor=2)` |

## 4. Review of `docs/security-review-signoff.md` (`AS-031`)

Per [`security-review-signoff.md`](security-review-signoff.md) §1 and §6:

- Current metadata state: `state: deferred`, `decision: pending`.
- Deferral policy set by maintainer (2026-10-29): independent human security
  review runs once as the final end-to-end verification pass by a human reviewer
  with zero authorship on `app.py`, `studio.py`, or the remediation commits.
- `security_release_gate.evaluate_security_signoff_gate` enforces both halves of
  `M5-P5-03` Acceptance Criterion 3 (*"Required security sign-off recorded or
  release blocked"*):
  1. It refuses any missing, malformed, `unsigned`, `deferred`, `pending`,
     `release-blocked`, AI/agent-signed, boundary-incomplete, or
     unverified-high-finding record with `passed = False` and explicit
     `blocking_reasons`.
  2. It opens (`passed = True`) only when an independent human reviewer sets
     `state: signed` and `decision: accepted`, supplies a valid 40-character
     commit SHA and ISO review date, marks all `B1-B12` rows `examined` and
     `adequate`, and verifies all `W-1` / `F-1..F-6` / `R-*` findings with zero
     unverified `critical` or `high` items.

## 5. Residual risk register and formal release decision

### Residual risks

| ID | Severity | Area | Blocks v1.0 release | Summary and mitigation |
|---|---|---|---|---|
| `RR-01` | `high` | Independent security sign-off (`AS-031`) | **Yes** | `docs/security-review-signoff.md` is `state: deferred`, `decision: pending`; release gate blocks publication until an independent human reviewer signs `decision: accepted`. |
| `RR-02` | `high` | Container execution isolation (`M5-P3-01`) | **Yes** | Live Docker/Podman isolation qualification has not yet been recorded on the target host; `STUDIO_EXECUTION_MODE` defaults to `disabled`, `sandbox` fails closed when unqualified, and the release gate blocks until `M5-P3-01` is qualified via `./scripts/qualify_environment.sh`. |
| `RR-03` | `medium` | Release artifact signing (`AS-042`) | **Yes** | SBOM, SLSA provenance, and scans verify cleanly, but no maintainer signing key is provisioned in CI; `scripts/release_supply_chain.py gate` and `--supply-chain-dir` fail closed without `signature.json`. |
| `RR-04` | `low` | macOS non-root thermal counters (`p4.thermal`) | No | macOS 26.6.1 hid thermal pressure counters from unprivileged processes during `M5-P4-01`; `model_admission` and `resource_admission` enforce conservative unified-memory budgets without depending on thermal counter availability. |
| `RR-05` | `low` | Opt-in `host` execution mode limits | No | When an operator explicitly enables `STUDIO_EXECUTION_MODE=host` + `STUDIO_ALLOW_HOST_EXECUTION=1`, static command guards cannot isolate arbitrary code running as the same OS user; UI/docs label host mode unsandboxed and default execution stays `disabled`. |

### Release decision

- **Verdict:** **`RELEASE-BLOCKED`** (`release_ready: false`).
- **Active blocking reasons on `HEAD`:**
  1. `security_signoff_not_signed`: `docs/security-review-signoff.md` is `state='deferred'`, `decision='pending'`.
  2. `mandatory_qualification_blocked`: `M5-P3-01` (container-backed code execution isolation) remains `blocked` pending operator Docker/Podman run of `./scripts/qualify_environment.sh`.
- **Enforcement commands:**
  - Verify code/security invariants in CI (exits `0` when all suites pass and blocking is active):
    `python scripts/security_release_gate.py`
  - Enforce release readiness before tagging/publishing v1.0 (exits `1` while `release-blocked`):
    `python scripts/security_release_gate.py --enforce --supply-chain-dir dist/evidence`
    or `python scripts/release.py gate --supply-chain-dir dist/evidence`
