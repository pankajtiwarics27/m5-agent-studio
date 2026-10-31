# Sandbox and filesystem boundary hardening

AS-028 defines the isolated boundary primitives in
[`sandbox_boundary.py`](../sandbox_boundary.py). `confined_path()` rejects
traversal, sibling-prefix escapes, and symlink targets outside the workspace.
`validate_workspace()` rejects symlink/non-directory roots, home/filesystem-root
mounts, known secret-bearing filenames, and over-budget trees.

`sandbox_command()` constructs Docker/Podman arguments with no network, a
read-only container root, dropped capabilities, no-new-privileges, CPU/memory/
PID limits, and only the validated workspace mounted read-write. The module
does not execute the command and does not claim kernel-level isolation.

`M5-P3-06` ([`ADR-0016`](adr/0016-security-incident-containment-and-fallback-qualification.md),
[`security-incident-containment.md`](security-incident-containment.md)) extends
`sandbox_boundary.py` with `SandboxBackendProfile` dependency graphs, monotonic
`ConfigurationGeneration` tracking, `inspect_container_argv_hardening()`,
`SecurityIncidentStore`, disjoint-dependency fallback qualification, and
post-incident requalification before restoration.

## Evidence

`tests/test_sandbox_boundary.py` covers traversal and symlink escapes, unsafe
roots and secret files, entry budgets, both supported runtimes, and required
container isolation flags. `tests/test_security_incident_containment.py` covers
configuration drift and hardening regressions, live subprocess containment,
shared-dependency fallback refusal, requalification before restoration, and
crash-durable barrier persistence.
