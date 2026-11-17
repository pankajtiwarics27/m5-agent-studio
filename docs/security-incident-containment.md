# Security Incident Containment and Fallback Qualification

`M5-P3-06` ([`ADR-0016`](adr/0016-security-incident-containment-and-fallback-qualification.md))
tracks security dependencies and monotonic configuration generations in
[`sandbox_boundary.py`](../sandbox_boundary.py), suspends affected admissions and running
subprocesses promptly when a guarantee regresses, qualifies alternative container backends
against incident-specific shared dependencies, and requires fresh post-incident
requalification before restoring suspended guarantees.

## Security guarantees, dependency graphs, and configuration generations

Studio maps execution profiles (`execution_profiles.required_security_guarantees`) onto 8
canonical guarantees:

- **Container isolation guarantees (`CONTAINER_ISOLATION_GUARANTEES`):**
  `container_filesystem_isolation`, `container_network_isolation`,
  `container_privilege_containment`, `container_resource_limits`, and
  `container_process_lifetime`.
- **Shared engine guarantees (`SHARED_ENGINE_GUARANTEES`):**
  `workspace_confinement`, `secret_and_vcs_protection`, and `mcp_transport_boundary`.

Each sandbox backend (`SandboxBackendProfile`, restricted to `docker` or `podman` — never
unsandboxed `host`) binds concrete `dep:...` dependencies (`dep:runtime:{rt}`,
`dep:oci_runtime:{oci}`, `dep:kernel:{vm_or_kernel}`, `dep:netns:{drv}`,
`dep:cgroup:{drv}`, `dep:mount:{drv}`, `dep:image:{digest_or_ref}`,
`dep:image_ref:{image}`, `dep:workspace_confinement`, and `dep:secret_hygiene`) and records
monotonic `ConfigurationGeneration` entries (`generation_seq`, `config_fingerprint`,
`hardening_valid`, `hardening_regressions`).

`inspect_container_argv_hardening` inspects constructed container invocations for dropped
mandatory flags (`--pull=never`, `--network=none`, `--read-only`, `--cap-drop=ALL`,
`--security-opt=no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`, `--ipc=private`,
and `noexec,nosuid,nodev` `/tmp`) or forbidden escalations (`--privileged`,
`--network=host`, shared host namespaces, `--cap-add`, or runtime socket mounts). Any
hardening regression or configuration generation drift on a qualified backend immediately
opens an active `SecurityIncidentRecord`.

## Prompt suspension of affected admissions and subprocesses

- `UnifiedExecutionCoordinator` checks `SecurityIncidentStore.check_admission` in both
  `check_pre_policy_denial` and `admit_profile`, while `app._start_code_process` enforces
  the same check at subprocess spawn.
- Profiles whose required guarantees intersect an active incident fail closed with
  `error_code="security_guarantee_suspended"`, leaving unrelated profiles (for example
  confined in-process `read_file` during a container-only regression) operational.
- Switching `STUDIO_EXECUTION_MODE` to `host` cannot bypass an active or fallback-mitigated
  container incident (`unsafe_host_fallback_forbidden`).
- Triggering an incident via `POST /api/security/incidents` or
  `trigger_security_incident_async` immediately terminates matching running subprocesses
  (including `os.setsid()` detached descendants) via
  `ProcessSupervisor.contain_by_capability_async`.

## Disjoint-dependency fallback qualification

`POST /api/security/incidents/{incident_id}/fallback` (`SecurityIncidentStore.qualify_fallback_backend`)
enforces five fail-closed checks before routing sandbox execution to an alternative backend:

1. Unsandboxed `host` or non-container backends are rejected (`403 unsafe_host_fallback_forbidden`).
2. Failures in `SHARED_ENGINE_GUARANTEES` (`workspace_confinement`,
   `secret_and_vcs_protection`, `mcp_transport_boundary`) or backends in
   `affected_backends` cannot be bypassed via runtime failover.
3. Any overlap between `candidate.security_dependencies()` and
   `incident.failed_dependencies` (shared VM kernel, shared OCI runtime, shared compromised
   image digest) is rejected with `409 shared_failed_dependency`.
4. The candidate backend must pass `inspect_container_argv_hardening`.
5. A post-incident `qualified` evidence report bound to the candidate's
   `config_fingerprint` must pass checks covering every `incident.failed_guarantees` entry.

## Post-incident requalification and crash durability

- `POST /api/security/incidents/{incident_id}/restore` (`requalify_and_restore`) refuses
  missing qualification (`requalification_required`), un-remediated hardening or immutable
  compromised `dep:image:sha256:...` digests (`regression_still_present`), stale
  pre-incident qualification artifacts (`stale_pre_incident_qualification`), and live
  `lingering_pids` (`lingering_processes_active`).
- State is persisted in `$HARNESS_STORE_DIR/security_incidents/state.json` (`0700` dir,
  `0600` file, `O_NOFOLLOW`, temp-file `os.fsync` + `os.replace` + directory `os.fsync`),
  cross-linked to `hold-{incident_id}` in `ContainmentStore` and reconciled on `init_db()`
  startup. Corrupt or symlinked state fails closed (`security_state_corrupt`, `HTTP 503`).

## What a qualification record says about its report

A `BackendQualificationRecord` carries two fields about the report it was built
from ([ADR-0044](adr/0044-qualification-provenance-and-reported-refusal-for-mission-isolation.md)).
Ingestion, fallback qualification and requalification before restoration all
write them.

| Field | Meaning |
|---|---|
| `report_time_stated` | `true` only when the report stated a usable generation time of its own (`generated_at_epoch`, `qualified_at` or `generated_at_utc`). When it is `false`, `report_generated_at` is the time of ingestion. |
| `host_fingerprint` | `sha256:` and 64 hex digits, computed by `qualification_host_fingerprint` from the machine description in the report (`fingerprint`: operating system, architecture, processor model, processor count, memory size). Empty when the report carries no complete description. |

Nothing in this document's admission, fallback or restoration rules reads these
fields. A report without a generation time, or without a machine description, is
accepted as before, and `check_admission` answers as before. The fields exist
for the mission isolation gate, which accepts a qualification as proof only when
its report stated its time and was produced on the recorded target host. See
[Bounded mission execution](bounded-mission-execution.md#what-a-qualification-rests-on).

Both fields appear in the `qualification` objects that
`GET /api/security/incidents` and the three `POST` routes return, and in
`state.json`. A record written before the fields existed reads as `false` and
empty. The fingerprint identifies a kind of machine, not one unit, and a report
is not authenticated.

## Evidence

[`tests/test_security_incident_containment.py`](../tests/test_security_incident_containment.py)
verifies configuration generation tracking and drift/hardening regression suspension,
qualification failure ingestion, host-mode bypass refusal, live in-flight subprocess
termination, shared-dependency fallback rejection, disjoint fallback routing,
post-incident requalification before restoration, file/directory `fsync` crash recovery,
corruption/symlink fail-closed behavior, and standalone `studio.py` parity. One case
sends reports with and without a generation time and a machine description through
the three routes that record a qualification: each is accepted as before, and each
record says what its report stated.
