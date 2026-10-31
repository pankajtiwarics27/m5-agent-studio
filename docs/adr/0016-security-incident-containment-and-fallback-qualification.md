---
id: ADR-0016
title: security-incident-containment-and-fallback-qualification
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0016: Security dependency graphs, configuration generations, incident suspension, and disjoint-dependency fallback qualification

## Context

`M5-P3-06` requires suspending failed security guarantees and dependent execution profiles
promptly upon regression or incident, preventing alternative backend fallbacks from
bypassing shared failed dependencies, requiring post-incident requalification before
restoration, and persisting incidents and barriers across process crashes:

1. **Unversioned runtime configuration and silent qualification drift:** Previously,
   `STUDIO_SANDBOX_RUNTIME`, `STUDIO_SANDBOX_IMAGE`, and container hardening flags in
   `sandbox_boundary.py` / `app._sandbox_command` were evaluated statelessly per call
   without a monotonic configuration generation ledger. Mutating a qualified container
   image tag, OCI runtime, or hardening flag had no durable mechanism to invalidate prior
   qualification or automatically suspend dependent profiles.
2. **Coarse session-only containment vs guarantee-level suspension:** `ProcessSupervisor`
   (`ADR-0013`) and `ScopedGrantStore` (`ADR-0014`) contained individual sessions, tasks,
   or revoked grants, whereas a backend security regression (such as an OCI `runc` escape,
   VM kernel isolation failure, compromised image digest, or missing `--network=none` flag)
   must immediately suspend every execution profile across Studio that depends on the
   failed guarantee while allowing unaffected profiles (such as confined in-process reads
   when only container isolation regressed) to remain operational.
3. **Shared-dependency fallback hazard:** Switching `STUDIO_SANDBOX_RUNTIME` from `docker`
   to `podman` appears to change backends, yet on macOS and Linux two container CLIs can
   share the same VM kernel (`orbstack_vm`), OCI runtime (`runc`), compromised image
   digest (`sha256:...`), cgroup driver, or Studio's own in-process `workspace_confinement`
   engine. Blindly failing over to a second CLI — or worse, flipping to unsandboxed `host`
   execution — re-exposes the exact broken security dependency.
4. **Replay of stale pre-incident qualification evidence on restoration:** Restoring a
   suspended primary backend without fresh post-incident requalification bound to the
   remediated configuration generation (or while lingering unkillable PIDs remain alive)
   would lift containment before remediation is proven.

## Decision

Extend [`sandbox_boundary.py`](../../sandbox_boundary.py),
[`execution_profiles.py`](../../execution_profiles.py), and [`app.py`](../../app.py) —
cross-linked with [`process_supervision.py`](../../process_supervision.py) — while keeping
SQLite at `SCHEMA_VERSION = 3` (`ADR-0003`) and preserving single-file `studio.py`
inlining:

1. **Explicit security guarantees, backend dependency graphs, and configuration generations:**
   - Define the 8 canonical guarantees (`container_filesystem_isolation`,
     `container_network_isolation`, `container_privilege_containment`,
     `container_resource_limits`, `container_process_lifetime`, `workspace_confinement`,
     `secret_and_vcs_protection`, and `mcp_transport_boundary`), partitioning container
     isolation guarantees from shared in-process engine guarantees.
   - Model each container backend via `SandboxBackendProfile` (`backend_id`, `runtime` in
     `docker | podman`, `runtime_path`, `image`, `image_digest`, `oci_runtime`,
     `kernel_boundary`, `network_driver`, `cgroup_driver`, `mount_driver`, and
     `hardening_flags`), deriving deterministic `dep:...` security dependency tokens and
     forbidding `host` / unsandboxed pseudo-backends.
   - Track monotonic `ConfigurationGeneration` records (`generation_seq`,
     `config_fingerprint`, `hardening_valid`, `hardening_regressions`) via
     `SecurityIncidentStore.observe_configuration` and `inspect_container_argv_hardening`.
     Hardening regressions or configuration drift on a qualified backend immediately
     invalidate prior qualification and open an active `SecurityIncidentRecord`.
2. **Prompt guarantee-scoped admission suspension and in-flight containment:**
   - `execution_profiles.required_security_guarantees` maps every `ExecutionProfile` to
     the guarantees it relies on, and `UnifiedExecutionCoordinator` checks
     `SecurityIncidentStore.check_admission` in both `check_pre_policy_denial` and
     `admit_profile` (with defense-in-depth enforcement in `app._start_code_process`).
   - Active incidents deny dependent actions with `error_code="security_guarantee_suspended"`,
     refuse `STUDIO_EXECUTION_MODE="host"` bypasses during container incidents
     (`unsafe_host_fallback_forbidden`), and promptly terminate matching running
     subprocesses (including `os.setsid()` detached trees) via
     `ProcessSupervisor.contain_by_capability_async`.
3. **Incident-specific shared-dependency fallback qualification (`qualify_fallback_backend`):**
   - Reject unsandboxed `host` fallback (`403 unsafe_host_fallback_forbidden`), backends
     directly named in `affected_backends`, and any incident involving shared non-container
     engine guarantees (`workspace_confinement`, `secret_and_vcs_protection`,
     `mcp_transport_boundary`).
   - Intersect `candidate.security_dependencies()` against `incident.failed_dependencies`
     and reject any overlap (`409 shared_failed_dependency`, naming every shared
     `dep:...` token).
   - Require valid hardening invariants and a post-incident `qualified` evidence report
     bound to `candidate`'s `config_fingerprint` whose passing checks cover every
     `incident.failed_guarantees` entry before transitioning to `mitigated_via_fallback`
     and routing `_sandbox_command` to the fallback backend.
4. **Fresh post-incident requalification before restoration (`requalify_and_restore`):**
   - Refuse restoration without qualification evidence (`requalification_required`), when
     hardening regressions or immutable compromised `dep:image:sha256:...` digests remain
     present (`regression_still_present`), when replaying pre-incident qualification
     records (`stale_pre_incident_qualification`), or while `ContainmentStore` holds report
     live `lingering_pids` (`lingering_processes_active`).
   - Upon valid post-incident requalification on the remediated `ConfigurationGeneration`,
     record the new `BackendQualificationRecord`, mark the incident `restored`, restore the
     primary backend, and release `hold-{incident_id}` in `ContainmentStore`.
5. **Crash-durable state and startup barrier reconciliation:**
   - Persist `SecurityIncidentStore` in `$HARNESS_STORE_DIR/security_incidents/state.json`
     (`0700` dir, `0600` file, `O_NOFOLLOW`, temp-file `os.fsync`, `os.replace`, and parent
     directory `os.fsync`), cross-persisting `hold-{incident_id}` in `ContainmentStore` and
     reconciling holds on `init_db()` startup. Corrupt or symlinked state fails closed
     (`security_state_corrupt`, `HTTP 503`).

## Alternatives

- **Allow `STUDIO_EXECUTION_MODE=host` with `STUDIO_ALLOW_HOST_EXECUTION=1` to run
  subprocesses while a container incident is active.**
  Rejected: falling back from a broken sandbox to unrestricted host execution violates the
  program's fail-closed invariant (`M5-P0-03`, `ADR-0010`) and bypasses every suspended
  container guarantee.
- **Accept alternative runtime names (`podman` vs `docker`) as sufficient proof of
  independence without dependency-set intersection and live qualification.**
  Rejected: `docker` and `podman` frequently share the same Linux/VM kernel, `runc`
  binary, or base image digest; metadata-only failover would silently re-enter the failed
  dependency (`AC 2`).
- **Store security incidents and configuration generations in new SQLite tables.**
  Rejected: `ADR-0003` freezes `SCHEMA_VERSION = 3`; owner-only fsynced state files under
  `$HARNESS_STORE_DIR` match `ContainmentStore` (`ADR-0013`) and `ScopedGrantStore`
  (`ADR-0014`) and survive crashes independently of SQLite transactions.

## Tradeoffs

- Strict configuration-generation binding invalidates a backend's `qualified` status as
  soon as its image, runtime path/version, OCI runtime, kernel boundary, or hardening
  flags change, requiring operator requalification (`qualify_environment.py --sandbox`)
  before resuming dependent profiles on that backend. This trades frictionless image tag
  updates under governed mode for deterministic regression containment.
- Dependency tokens (`oci_runtime`, `kernel_boundary`, `network_driver`, `cgroup_driver`,
  `mount_driver`) combine probed/configured attributes with operator-supplied backend
  profiles; hidden host-firmware or hardware microcode flaws below the declared kernel
  boundary still require operator incident scoping (`failed_dependencies`).

## Review Status

Not reviewed. Accepted as the architectural record for `M5-P3-06`; independent human
review has not yet been performed. See
[`docs/security-incident-containment.md`](../security-incident-containment.md) and
[`tests/test_security_incident_containment.py`](../../tests/test_security_incident_containment.py).
