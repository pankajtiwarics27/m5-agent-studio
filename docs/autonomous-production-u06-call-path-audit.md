# AP U06 call-path audit — lifecycle/sandbox qualification and refusal

**State:** source-level audit, read-only • **Pinned to:** `bc406fd` (`main` after
PR #302, 2026-11-10) • **Decision owned by:** repository owner (U06) •
**Implements:** nothing — this document is evidence gathering, not authorization

This page audits where the repository currently refuses — or fails to refuse —
unqualified lifecycle and sandbox execution, across the producer → authority →
executor → consumer paths that the blocked AP-007 story depends on. It exists
because the AP roadmap names *"audit call paths, implement refusal tests,
coordinate with the owning program"* as the safe work while decision
[U06](autonomous-production-operating-envelope.md#u01-u06-decision-register)
("Current lifecycle/sandbox qualification and activation") is unresolved
(`docs/autonomous-production-roadmap.md` §2.3). It covers the audit half only.
The refusal-test half is deliberately **not** done here: writing AP-007's tests
before the owner resolves U06 and authorizes the story would pre-implement an
unauthorized story.

## What this is not

- Not a qualification claim. Nothing was installed, booted, loaded or measured.
  This host (`command -v docker podman` → neither found) is not the qualified
  Apple Silicon target, and this audit re-qualifies nothing. The M5-P3-01
  container-isolation record
  ([`m5-2-0-sandbox-isolation-evidence.md`](m5-2-0-sandbox-isolation-evidence.md),
  operator run, 2026-10-31, 12/12 checks, Apple M5, Docker 29.5.3,
  `python@sha256:aa943ce4…`) and the M5-P4-01 model-qualification record
  ([`m5-2-0-hardware-qualification-evidence.md`](m5-2-0-hardware-qualification-evidence.md))
  document what the target host did on that date. They are not AP-007/AP
  activation evidence, not this host, and not a U06 resolution.
- Not authorization. AP-007 remains `not_started` and unauthorized; AP-008…AP-032
  remain unauthorized. U01–U06 remain `pending`. `authorized_downstream_stories`
  is unchanged. No approval of any kind is claimed or implied.
- Not a security review or independent assessment. ADR-0040 remains
  `review_status: not-reviewed`.
- Not a defect report and not a promise that every path listed is safe — it is a
  map of what exists, with open status marked `UNKNOWN` where the source does
  not establish an answer.

## Question under audit

For lifecycle/sandbox paths that are **not qualified**, does an action refuse
(fail closed) instead of falling back to host/manual execution? Scope:

1. Contained code execution (the `run_bash` / `ctx_execute_python` class).
2. Mission-scoped action admission (the AP-004 authority/budget envelope and the
   AP-005 durable mission tasks).
3. Native lifecycle / dispatch (the dormant LC08 boundary and lifecycle modules).
4. Uncertain external effects.

## Method

Source read at `bc406fd` with line references. References drift; re-derive
before relying on them. No services were started, no models were loaded, no
network calls were made, no tests were run against live endpoints. Where a
behavior is established only by tests, the test module is named. A path is
marked `UNKNOWN` rather than assumed wherever reading the source does not settle
it.

## Findings

### 1. Contained code execution — refuses by construction

- `_execution_mode()` (`app.py`): the effective mode is `disabled`, `sandbox`,
  or `host`. `host` requires `STUDIO_ALLOW_HOST_EXECUTION=1`; the settings parse
  raises `ConfigurationError` otherwise, and the belt-and-braces helper again
  returns `disabled`. A fresh install therefore cannot execute contained code at
  all until the operator opts in.
- `_start_code_process()` (`app.py`, `_execution_mode() == "disabled"` →
  `PermissionError`) consults `SecurityIncidentStore.check_admission()`
  (`sandbox_boundary.py`) with `CONTAINER_ISOLATION_GUARANTEES` plus
  `workspace_confinement` and `secret_and_vcs_protection`:
  - `SecurityIncidentIntegrityError` (corrupt/tampered incident store) →
    `PermissionError` (fail closed).
  - `sec_dec.allowed is False` → `PermissionError(sec_dec.reason)` — the
    executor is never reached and there is **no host fallback inside this
    path**. Denial codes reachable here: `security_guarantee_suspended` (active
    incident), `unsafe_host_fallback_forbidden` (host/subprocess execution while
    an incident is mitigated only for sandbox fallback), and
    `fallback_qualification_invalid` / `shared_failed_dependency` (fallback
    backend absent, unqualified, drifted, or intersecting failed dependencies).
- `_sandbox_command()` (`app.py`) additionally requires the sandbox runtime to
  be resolvable on `PATH` (else `RuntimeError`), routes through a distinct
  fallback backend only when an un-restored `mitigated_via_fallback` incident
  exists, and calls `observe_configuration()` — a hardening regression from the
  qualified generation raises `PermissionError` before any container starts.
- `build_container_invocation()` (`sandbox_boundary.py`): `--pull=never`,
  `--network=none`, `--read-only`, `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, `--pids-limit=128`, `--memory=2g`,
  `--cpus=2`, `--ipc=private`, `nofile` ulimit, noexec/nosuid/nodev tmpfs,
  explicit `--user uid:gid`, workspace bind mount only. No network, no device,
  no writable rootfs, no daemon pull.
- The same admission is re-evaluated at the profile seam
  (`execution_profiles.py::_check_security_incident_denial`) with the
  profile-specific guarantee set from `required_security_guarantees()`. Store
  integrity failure → denial with `error_code="security_state_corrupt"`;
  otherwise denial carries the store's own error code
  (`security_guarantee_suspended` …). Profile denial means the application
  refuses the action; it does not downgrade to a weaker profile.

**Test anchors:** `tests/test_sandbox_boundary.py`,
`tests/test_security_incident_containment.py` (incident open → subprocess
blocked → disjoint fallback qualified and routed → host fallback refused →
requalification before restoration → crash/restart persistence → tampered state
fails closed), `tests/test_isolation_smoke.py`, and the security release gate's
adversarial case `adv.unqualified_sandbox_and_shell_smuggling_refused`
(`app.py`, fault-injection suite) which records an unqualified container backend
failing closed without host fallback.

### 2. Mission-scoped admission — refuses before the executor

- The shared seam is `DurableActionBoundary.admit_and_execute()`
  (`execution_spine.py`): grant/action-class validation → profile admission
  (`ExecutionProfileCoordinator.admit_profile`) → mission-envelope revalidation
  and atomic budget charge (`MissionEnvelopeCoordinator.admit_mission_action`,
  implemented by `_AppMissionEnvelopeCoordinator` in `app.py` per the AP-004
  envelope) → intent ledger → only then the executor. A denial anywhere in that
  chain short-circuits before the executor runs.
- Actual consumers are exercised offline with mocked transports/executors in
  `tests/test_mission_authority_envelope.py` (denials pinned
  `task_budget_exhausted`, `stale_policy_generation`, revocation/epoch fencing)
  and `tests/test_action_admission_gate.py`, consistent with the AP-004
  actual-consumer evidence.
- **Gap (verified by grep, scoped to this audit):**
  `workflow_orchestration.py` contains no reference to sandbox admission,
  `execution_mode` or `check_admission`. Durable AP-005 mission tasks therefore
  inherit refusal strength from the shared action seam, not from a
  mission-scoped sandbox gate. Whether AP-007 requires a dedicated mission-level
  refusal path is part of U06 and the AP-007 scope decision, not something this
  audit settles.
- Lifecycle/cancellation containment for mission work reuses
  `process_supervision.py` (bounded supervised spawn → cancel → force-kill →
  reap verification) and `recovery_guard.py` (stagnation detection). These are
  per-record/per-task mechanisms; none of them is a qualification check, and
  none is activated by this audit.

### 3. Native lifecycle / dispatch — dormant; refuses nothing because it enables nothing

- `_local_runtime_dispatch()` (`app.py`) composes
  `RuntimeDispatchBoundary(runtime_origins=(OMLX_BASE, OLLAMA_BASE))` with
  `coordinated=False` and **installs no backends**. The source comment is
  explicit: "LC-08 composition is deliberately not exposed as a setting/route.
  Legacy is the rollout default; installing a coordinated executor needs
  separate review."
- With `coordinated=False`: `target()` returns `None` (so `forbid_legacy()`
  no-ops by design), `guard_http()` returns immediately, and every
  `RuntimeDispatchDenied` path (`invalid_dispatch_request`,
  `coordinated_combo_unsupported`, `coordinated_backend_unavailable`,
  `coordinated_execution_unavailable`, `legacy_transport_blocked`,
  `canonical_consumer_required`) is unreachable from app composition because no
  coordinated call is ever issued by the app itself.
- Lifecycle consumer modules (`omlx_lifecycle.py`, `ollama_lifecycle.py`,
  `lmstudio_lifecycle.py`, `model_lifecycle_coordinator.py`,
  `native_inference_scheduler.py`, `native_transition_driver.py`) are library
  surfaces. No route, activation flag or setting installs them into the running
  application; the LC08 audit series (`docs/lc08-*.md`) records the remaining
  inventory-only owners and gates.
- Qualification tooling exists and is operator-owned:
  `scripts/qualify_environment.sh` + `scripts/qualify_environment.py` (exit 2 =
  "required checks could not run" on a non-target host; qualification is an
  operator action, never automatic). Its output is qualification evidence for
  M5 stories on the target host, not AP activation.

**Implication for the U06 question:** on this codebase, "unqualified backend" at
the native-lifecycle seam does not currently fall back to anything — the
coordinated path does not exist at runtime. The refusal-relevant risk for
U06 is therefore concentrated in (a) the contained-execution admission seam
(§1), (b) whatever a future composition installs (§3), and (c) the
non-sandboxed fallback modes that exist by explicit operator opt-in
(`STUDIO_EXECUTION_MODE=host` + `STUDIO_ALLOW_HOST_EXECUTION=1`). Whether this
host or any future host should enable any of these is exactly the owner's U06
decision.

### 4. Uncertain external effects — pause and fence, never auto-retry

- `side_effect_ledger.py`: `AmbiguousSideEffectError`,
  `UnresolvedEffectsHoldError`; non-idempotent MCP writes with blocking,
  unresolved uncertain effects are paused by the shared seam
  (`find_blocking_uncertain_writes` → `uncertain_mcp_write_paused`), and the
  session is paused rather than retried.
- AP-005 mission joins report `partial`/`failed` explicitly; AP-006
  `artifact_status` reports `partial`/`invalidated` and `require_complete`
  refuses false-completion reads. Retention/delete keep tombstoned reference
  history.
- **Test anchors:** `tests/test_side_effect_ledger.py`,
  `tests/test_mcp_idempotency_reconciliation.py`,
  `tests/test_durable_task_state_machine.py`, `tests/test_artifact_lineage.py`.

## Gap summary (what is not established)

| # | Gap | Owner of the decision |
|---|---|---|
| G-1 | No mission-scoped sandbox qualification gate beyond the shared action seam (§2); whether AP-007 needs one is unscoped | U06 + AP-007 authorization |
| G-2 | No installed-host qualification evidence on this host (no Docker/Podman); target-host evidence is the M5-P3-01/P4-01 records only | Owner (U02/U06) |
| G-3 | Native lifecycle path is dormant; any composition install, activation flag or setting needs separate review and evidence | Owner (U06) + LC program |
| G-4 | `STUDIO_EXECUTION_MODE=host` exists behind explicit opt-in; no audit of who may set it outside tests/docs was performed here | Owner (U04/U06) |
| G-5 | Browser worker (AP-010) unqualified; not assessed here | AP-010 authorization |
| G-6 | Refusal-test half of the U06 safe work is intentionally not implemented by this PR (would pre-implement AP-007) | Future AP-007 session, after authorization |

`UNKNOWN` markings: whether the shared seam denial covers every consequential
action class under every executor (spot-checked via the named test modules, not
re-derived exhaustively); whether any other runtime entry point bypasses
`_start_code_process` (grep shows `asyncio.create_subprocess_exec` call sites in
`app.py` beyond this audit's scope, e.g. local model runners — these rely on
their own admission seams and are not certified here).

## For the owner's U06 decision

Questions this audit surfaces, in dependency order (no answers implied):

1. Which backends are qualified on the target host today, and what does
   "qualified" require per backend (image digest, OCI runtime, hardening
   flags, evidence artifacts)?
2. Is the M5-P3-01 2026-10-31 container qualification still the operative
   evidence, or is a fresh run required for AP use?
3. May consequential mission actions ever dispatch without sandbox admission,
   or only contained-execution classes?
4. Activation order: contained execution → browser worker → native lifecycle —
   and what stays refused throughout?
5. Who records and signs the U06 resolution in the AP ledger, and where?

## Authority boundaries and non-goals

No code, test, workflow, ledger story state, generated artifact or activation
was changed or implied by this page. Legacy defaults (`STUDIO_EXECUTION_MODE`
unset → `disabled`; native dispatch dormant; activation `false`) remain
binding. AP-007 through AP-032 remain unauthorized and unstarted. U01–U06
remain pending. No owner, independent-security, screenshot, media, signing or
release approval is claimed. Issue
[#301](https://github.com/pankajtiwarics27/m5-agent-studio/issues/301) records
the AP-007 blocker; this audit addresses only its "Audit call paths" line.

## Maintenance

Line references are valid at `bc406fd` only. When the answer to any question
above changes — a composition install lands, U06 resolves, a backend is
qualified — this page must be regenerated or explicitly superseded rather than
edited in place. A re-derivation checklist: `_execution_mode`,
`_start_code_process`, `_sandbox_command`, `build_container_invocation`,
`SecurityIncidentStore.check_admission`, `_check_security_incident_denial`,
`DurableActionBoundary.admit_and_execute`, `_AppMissionEnvelopeCoordinator`,
`_local_runtime_dispatch`, `workflow_orchestration.py` (expect no admission
references until G-1 is decided).
