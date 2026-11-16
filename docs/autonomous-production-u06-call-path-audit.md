# AP U06 call-path audit — lifecycle/sandbox qualification and refusal

**State:** source-level audit, regenerated • **Pinned to:** `dc5ec57` (`main`
after PR #306, 2026-11-16) • **Decision owned by:** repository owner (U06) •
**Implements:** nothing — this document is evidence gathering, not authorization

> **Revision history.** This page supersedes the `bc406fd` revision (2026-11-10,
> PR #304), which was accurate when written. Material changes since: AP-007's
> safe fail-closed offline subset landed through PR #303 (exact head
> `b1b2e4d747f9916a8ffd77f1126c90f8288294c8`, merged as
> `bf1fa6aefa24913ed7a88ceb12775b484dbc7cc5` on 2026-11-15) and its docs/ledger
> completion record landed through PR #306 (as `dc5ec57`). Consequences for this
> audit: the refusal-test half of the U06 safe work is now **implemented and
> merged** (gap G-6 resolved); a mission-level isolation gate now exists offline
> and fails closed with no host-execution fallback (gap G-1 addressed, not
> settled — whether it suffices is U06); a late-result fence is on the live
> action path for every task (§5). `bc406fd` line references in that revision are
> preserved only in git history; this revision re-derives every checklist item at
> `dc5ec57`. AP-007-F1 (PR #307, draft, another session) narrows the late-result
> fence to mission-owned work; when it lands, this page must be regenerated
> again rather than edited in place.

This page audits where the repository currently refuses — or fails to refuse —
unqualified lifecycle and sandbox execution, across the producer → authority →
executor → consumer paths that the blocked part of AP-007 depends on. It exists
because the AP roadmap names *"audit call paths, implement refusal tests,
coordinate with the owning program"* as the safe work while decision
[U06](autonomous-production-operating-envelope.md#u01-u06-decision-register)
("Current lifecycle/sandbox qualification and activation") is unresolved
(`docs/autonomous-production-roadmap.md` §2.3). Both halves of that safe work are
now merged for the offline subset; what remains is the decision itself.

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
- Not authorization. AP-007's offline fail-closed subset landed and is recorded
  `blocked` on U06 for its remainder (PR #306). AP-008…AP-032 remain
  unauthorized. U01–U06 remain `pending`. `authorized_downstream_stories`
  remains AP-002…AP-006 and `runtime_activation_authorized` stays `false`. No
  approval of any kind is claimed or implied.
- Not a security review or independent assessment. ADR-0040 remains
  `review_status: not-reviewed`; ADR-0041 remains `review_status: not-reviewed`.
- Not a defect report and not a promise that every path listed is safe — it is
  a map of what exists, with open status marked `UNKNOWN` where the source does
  not establish an answer.

## Question under audit

For lifecycle/sandbox paths that are **not qualified**, does an action refuse
(fail closed) instead of falling back to host/manual execution? Scope:

1. Contained code execution (the `run_bash` / `ctx_execute_python` class).
2. Mission-scoped action admission (the AP-004 authority/budget envelope and the
   AP-005 durable mission tasks).
3. Native lifecycle / dispatch (the dormant LC08 boundary and lifecycle modules).
4. Uncertain external effects.
5. The AP-007 offline fail-closed subset paths (merged 2026-11-15).

## Method

Source read at `dc5ec57` with line references. References drift; re-derive
before relying on them. No services were started, no models were loaded, no
network calls were made, no tests were run against live endpoints. Where a
behavior is established only by tests, the test module is named. A path is
marked `UNKNOWN` rather than assumed wherever reading the source does not
establish it.

## Findings

### 1. Contained code execution — refuses by construction

- `_execution_mode()` (`app.py`): the effective mode is `disabled`, `sandbox`,
  or `host`. `host` requires `STUDIO_ALLOW_HOST_EXECUTION=1`; a fresh install
  therefore cannot execute contained code at all until the operator opts in.
- `_start_code_process()` (`app.py`, `_execution_mode() == "disabled"` →
  `PermissionError`) asserts no active write transaction first, then consults
  `SecurityIncidentStore.check_admission()` (`sandbox_boundary.py`) with
  `CONTAINER_ISOLATION_GUARANTEES` plus `workspace_confinement` and
  `secret_and_vcs_protection`:
  - `SecurityIncidentIntegrityError` (corrupt/tampered incident store) →
    `PermissionError` (fail closed).
  - `sec_dec.allowed is False` → `PermissionError(sec_dec.reason)` — the
    executor is never reached and there is **no host fallback inside this
    path**. Denial codes reachable here: `security_guarantee_suspended`,
    `unsafe_host_fallback_forbidden`, `fallback_qualification_invalid` /
    `shared_failed_dependency`.
- `_sandbox_command()` (`app.py`) requires the sandbox runtime to be
  resolvable on `PATH` (else `RuntimeError`), routes through a distinct
  fallback backend only when an un-restored `mitigated_via_fallback` incident
  exists, and calls `observe_configuration()` — a hardening regression from
  the qualified generation raises `PermissionError` before any container
  starts.
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
`tests/test_security_incident_containment.py`, `tests/test_isolation_smoke.py`,
and the security release gate's adversarial case
`adv.unqualified_sandbox_and_shell_smuggling_refused` (`app.py`,
fault-injection suite) which records an unqualified container backend failing
closed without host fallback.

### 2. Mission-scoped admission — refuses before the executor

- The shared seam is `DurableActionBoundary.admit_and_execute()`
  (`execution_spine.py`): grant/action-class validation → profile admission
  (`ExecutionProfileCoordinator.admit_profile`) → mission-envelope revalidation
  and atomic budget charge (`MissionEnvelopeCoordinator.admit_mission_action`,
  implemented by `_AppMissionEnvelopeCoordinator` in `app.py` per the AP-004
  envelope) → intent ledger → only then the executor. A denial anywhere in that
  chain short-circuits before the executor runs.
- Since PR #303 the mission-budget leg has a chain-wide isolation gate
  (`execution_budget.py`): when any task from the leaf up to the root declares
  `isolation_required`, the atomic charge is refused with
  `IsolationUnavailable` unless the caller presents `IsolationEvidence`
  proving, literally, a `sandbox` execution mode, `allowed is True` admission,
  `container_qualified is True` and a backend id. `IsolationEvidence` is built
  server-side from the sandbox admission decision
  (`IsolationEvidence.from_admission`); it is never parsed from a request body
  or model output, and a missing/truthy-non-boolean/absent-backend reading
  refuses. The default is refusal, so a caller that presents nothing can never
  reach a host-execution path for such a task.
- The app's mission tool boundary answers `isolation_unavailable` on that
  refusal — `_AppMissionEnvelopeCoordinator` catches
  `execution_budget.IsolationUnavailable` and returns a denial with
  `error_code="isolation_unavailable"`; `run_bash`/`ctx_execute_python` and the
  mission tool path therefore refuse without executing and without host
  fallback (`app.py`, `_run_authorized_tool`).
- `workflow_state.DurableTaskStateMachine.halt_mission()` stops one plan
  version in one transaction (owned unfinished tasks cancelled, delegated
  descendants fenced, budgets marked cancelled, consequential actions declared
  uncertain), refuses unknown plans/identities, refuses new plans on a root
  task until a stopped plan is settled (`MissionHaltUnsettled`), and the
  recovery report (`workflow_state.py::mission_recovery_report`) claims success
  only for a checked complete outcome and lists every unresolved effect with
  one next action. Claims and resumes of an already-stopped plan raise
  `MissionIsolationRejected` when isolation evidence is absent.
- **Re-verified gap (grep, at `dc5ec57`):** `workflow_orchestration.py`
  contains no reference to sandbox admission, `execution_mode` or
  `check_admission` — durable AP-005 mission tasks still inherit refusal
  strength from the shared action seam and the budget-chain charge, not from a
  `workflow_orchestration.py`-level sandbox gate.
- Actual consumers are exercised offline with mocked transports/executors in
  `tests/test_mission_authority_envelope.py` and
  `tests/test_action_admission_gate.py`, and the AP-007 refusal paths in
  `tests/test_durable_task_state_machine.py`, `tests/test_execution_budget.py`,
  `tests/test_durable_intent_ledger.py`, `tests/test_process_supervision.py`
  and `tests/test_mission_authority_envelope.py`.
- Lifecycle/cancellation containment for mission work reuses
  `process_supervision.py` (bounded supervised spawn → cancel → force-kill →
  reap verification) and `recovery_guard.py` (stagnation detection). These are
  per-record/per-task mechanisms; none of them is a qualification check.

### 3. Native lifecycle / dispatch — dormant; refuses nothing because it enables nothing

- `_local_runtime_dispatch()` (`app.py`) composes
  `RuntimeDispatchBoundary(runtime_origins=(OMLX_BASE, OLLAMA_BASE))` without
  installing backends. `RuntimeDispatchBoundary.coordinated` defaults to
  `False` (`runtime_dispatch_boundary.py`), so `target()` returns `None` (so
  `forbid_legacy()` no-ops by design), `guard_http()` returns immediately, and
  every `RuntimeDispatchDenied` path is unreachable from app composition
  because no coordinated call is ever issued by the app itself.
- Lifecycle consumer modules (`omlx_lifecycle.py`, `ollama_lifecycle.py`,
  `lmstudio_lifecycle.py`, `model_lifecycle_coordinator.py`,
  `native_inference_scheduler.py`, `native_transition_driver.py`) are library
  surfaces. No route, activation flag or setting installs them into the
  running application; the LC08 audit series (`docs/lc08-*.md`) records the
  remaining inventory-only owners and gates.
- Qualification tooling exists and is operator-owned:
  `scripts/qualify_environment.sh` + `scripts/qualify_environment.py` (exit 2 =
  "required checks could not run" on a non-target host; qualification is an
  operator action, never automatic). Its output is qualification evidence for
  M5 stories on the target host, not AP activation.

**Implication for the U06 question:** on this codebase, "unqualified backend"
at the native-lifecycle seam does not currently fall back to anything — the
coordinated path does not exist at runtime. The refusal-relevant risk for
U06 is concentrated in (a) the contained-execution admission seam (§1),
(b) whatever a future composition installs (§3), and (c) the non-sandboxed
fallback modes that exist by explicit operator opt-in
(`STUDIO_EXECUTION_MODE=host` + `STUDIO_ALLOW_HOST_EXECUTION=1`). Whether this
host or any future host should enable any of these is exactly the owner's U06
decision.

### 4. Uncertain external effects — pause and fence, never auto-retry

- `side_effect_ledger.py`: `AmbiguousSideEffectError`,
  `UnresolvedEffectsHoldError`; non-idempotent MCP writes with blocking,
  unresolved uncertain effects are paused by the shared seam
  (`find_blocking_uncertain_writes` → `uncertain_mcp_write_paused`), and the
  session is paused rather than retried.
- Since PR #303 the late-result fence is on the finalize path of every task
  (not only missions): `DurableActionBoundary.admit_and_execute` reads the
  intent-ledger verdict from `DurableActionLedger.complete` /
  `record_exception`, and when the row was already declared `uncertain` (by a
  cancel, takeover, bounded stop or recovery), the late result no longer
  settles it — it comes back `uncertain: true` with
  `error_code: "late_result_fenced"`. Only `resolve_uncertain_action` can
  settle such a row. Note: PR #307 (draft, another session) narrows this fence
  to mission-owned work; until it lands, every task is fenced.
- AP-005 mission joins report `partial`/`failed` explicitly; AP-006
  `artifact_status` reports `partial`/`invalidated` and `require_complete`
  refuses false-completion reads. Retention/delete keep tombstoned reference
  history.
- **Test anchors:** `tests/test_side_effect_ledger.py`,
  `tests/test_mcp_idempotency_reconciliation.py`,
  `tests/test_durable_task_state_machine.py`, `tests/test_artifact_lineage.py`.

### 5. AP-007 offline fail-closed subset — landed, dormant, fail closed

Landed 2026-11-15 (PR #303, `bf1fa6a`) and recorded 2026-11-16 (PR #306,
`dc5ec57`). Everything below is offline and fail closed; no production caller
presents isolation evidence, so an isolation-required mission is always
refused in this build.

- Bounded stops: optional `max_no_progress`, `max_repair_attempts`,
  `deadline_at`, `max_memory_bytes`, `max_storage_bytes` in the existing budget
  document; counters and peaks stop strictly above the limit; the deadline is
  committed before the refusal; a declared ceiling that cannot be read refuses
  with `measurement_unavailable` instead of assuming headroom. Budgets that
  declare no new limit are byte-for-byte compatible with the pre-AP-007
  on-disk form and keep their identity digest.
- Isolation requirement: opt-in per budget (`isolation_required`), enforced
  across the whole leaf→root task chain at the budget charge, at claims and at
  resumes, with literal-proof `IsolationEvidence` or refusal — never a host
  fallback. Accepting path runs only under test fixtures.
- Late-result fence: see §4.
- Recovery report: retained partial artifacts with per-task verdicts, unresolved
  action ids and one next action per limitation; stored as hash-addressed rows
  in `runtime_mission_joins`; no UI/route surfaces it.
- `create_mission_plan` refuses a new plan on a root task until a stopped plan
  is settled (`MissionHaltUnsettled`).
- Rollback hazard (measured against the base commit's reader, not a repository
  test): a binary older than AP-007 drops declared limits on its next write to
  a task that declares them.

## Gap summary (what is not established)

| # | Gap | Owner of the decision |
|---|---|---|
| G-1 | **Addressed offline, not settled:** a mission-level isolation gate now exists and fails closed with no host fallback (§2, §5); whether AP-007's offline gate is sufficient as the mission-scoped qualification gate, and on what backend, is U06's call | U06 + AP-007 remainder authorization |
| G-2 | No installed-host qualification evidence on this host (no Docker/Podman); target-host evidence is the M5-P3-01/P4-01 records only | Owner (U02/U06) |
| G-3 | Native lifecycle path is dormant; any composition install, activation flag or setting needs separate review and evidence | Owner (U06) + LC program |
| G-4 | `STUDIO_EXECUTION_MODE=host` exists behind explicit opt-in; no audit of who may set it outside tests/docs was performed here | Owner (U04/U06) |
| G-5 | Browser worker (AP-010) unqualified; not assessed here | AP-010 authorization |
| G-6 | **Resolved 2026-11-15:** the refusal/cancellation/uncertain-effect tests were implemented and merged through PR #303 under the owner's explicit decision to land the offline subset with U06 pending (refusal tests include test_ap007_missing_isolation_blocks_dependent_execution_without_host_fallback) | — |

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
6. Does the AP-007 offline subset (§5) stand as the refusal foundation, or is
   rework required before activation questions can even be asked?

## Authority boundaries and non-goals

No code, test, workflow, ledger story state, generated artifact or activation
was changed or implied by this page. Legacy defaults (`STUDIO_EXECUTION_MODE`
unset → `disabled`; native dispatch dormant; activation `false`) remain
binding. AP-007's offline subset is landed and recorded `blocked` on U06;
the remainder of AP-007 and AP-008…AP-032 remain unauthorized and unstarted.
U01–U06 remain pending. No owner, independent-security, screenshot, media,
signing or release approval is claimed. Issue
[#301](https://github.com/pankajtiwarics27/m5-agent-studio/issues/301) records
the AP-007 blocker; this audit addresses its "Audit call paths" line.

## Maintenance

Line references are valid at `dc5ec57` only. When the answer to any question
above changes — a composition install lands, U06 resolves, a backend is
qualified, PR #307 lands and narrows the late-result fence — this page must be
regenerated or explicitly superseded rather than edited in place. A
re-derivation checklist: `_execution_mode`, `_start_code_process`,
`_sandbox_command`, `build_container_invocation`,
`SecurityIncidentStore.check_admission`, `_check_security_incident_denial`,
`DurableActionBoundary.admit_and_execute`, `_AppMissionEnvelopeCoordinator`,
`_local_runtime_dispatch`, `RuntimeDispatchBoundary.coordinated` (default
`False`), `workflow_orchestration.py` (expect no admission references until
G-1 is decided), `execution_budget.IsolationEvidence` /
`isolation_refusal` / the `charge_hierarchical_tool_call` isolation pre-pass,
`DurableActionLedger.complete` / `record_exception`,
`workflow_state.DurableTaskStateMachine.halt_mission` / `mission_recovery_report`,
`runtime_dispatch_boundary.RuntimeDispatchBoundary`.
