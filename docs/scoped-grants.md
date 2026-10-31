# Scoped Grants, Expiry, Revocation, and Subprocess Secret Hygiene (`M5-P3-04`, ADR-0014)

`scoped_grants.py` (stdlib-only, inlined into standalone `studio.py`) implements auditable
operation-, resource-, and task-scoped permission grants, non-renewable finite expiry,
event-driven revocation, capability-specific in-flight containment, and strict exclusion
of secrets and loader/Git-config injection variables from arbitrary subprocess
environments. See [`ADR-0014`](adr/0014-scoped-grants-expiry-and-revocation.md),
[`execution-profiles.md`](execution-profiles.md), and
[`process-supervision.md`](process-supervision.md).

## Durable Grant & Policy-Revision Store (`$HARNESS_STORE_DIR/grants/state.json`)

`ScopedGrantStore` persists `schema_version = 1` JSON atomically (`0700` directory, `0600`
file, `O_NOFOLLOW` symlink refusal on both parent directories and state files, `fsync` on
temp file + parent directory):

- **`ScopedGrantRecord`:** Binds `grant_id` (`grant-...`), `session_id`, optional
  `task_id` and lease `epoch`, canonical `capabilities` (`workspace_read`,
  `workspace_write`, `code_execution`, `external_mcp`, `context_write`,
  `subagent_delegation`, `council_mutation`), optional `tool_names` and canonical
  `args_sha256`, canonical `resource_scopes` (`workspace:{ws}`, `dir:{ws}:{rel}`,
  `path:{ws}:{rel}`, `mcp:{alias}`, `mcp:{alias}:{tool}`, `context:{folder}`, `global:*`),
  `session_policy_revision`, `global_policy_revision`, `issued_at`, finite `expires_at`
  (`0 < ttl_seconds <= 86400`), optional `max_uses`, `uses_consumed`, and `status`
  (`active`, `expired`, `consumed`, `revoked`).
- **No automatic renewal (`AC 2`):** `ScopedGrantStore` provides no extension or TTL
  refresh API. Consuming a grant increments `uses_consumed` without altering `expires_at`;
  once `now >= expires_at` or `uses_consumed >= max_uses`, the grant transitions one-way
  to `expired` or `consumed`. Interactive operator approvals (`_persist_approval_grant_outcome`)
  issue a one-shot (`max_uses = 1`), exact-`args_sha256`, `300s` finite grant; operator
  rejections persist a `rejected_approvals` entry + `grant_rejected` audit event without
  issuing any grant.
- **Fail-closed integrity:** Corrupted JSON, unexpected `schema_version`, or symlinked
  files/directories raise `GrantStoreIntegrityError`, blocking admission rather than
  falling open. Startup calls `sweep_expired_grants()`.

## Two-Phase Admission Validation & Cross-Tool Anti-Bypass (`AC 1`, `AC 3`)

- **Untrusted model arguments:** `"grant_id"` is stripped by
  `execution_spine.MODEL_UNTRUSTED_APPROVAL_KEYS` so model-supplied tool arguments can
  never forge or select a grant ID.
- **Pre-policy gate (`DurableActionBoundary.admit_and_execute` -> `check_pre_policy_denial`):**
  Runs *before* `_policy.authorize` so active `RevocationRuleRecord` overlaps and explicit
  expired, revoked, consumed, policy-superseded, or scope-mismatched grants return an
  immediate hard denial (`requires_approval = False`, `error_code` in
  `{capability_revoked, grant_expired, grant_revoked, grant_consumed,
  grant_policy_superseded, grant_scope_mismatch}`).
- **Post-barrier atomic consumption (`UnifiedExecutionCoordinator.admit_profile`):**
  Re-checks revocations and calls `ScopedGrantStore.validate_and_consume` *after* unified
  profile checks, origin fences, containment barrier holds, and resource footprint
  barriers succeed, so barrier contention never burns a `max_uses = 1` grant.
- **Subsumption vs overlap & path canonicalization:**
  - Grant authorization requires `scope_subsumes(grant_scope, action_scope)` (a narrow
    `path:{ws}:src/a.py` grant cannot authorize `write_file` on `src/b.py` or `run_bash`
    on `workspace:{ws}`).
  - Revocation enforcement checks `scopes_overlap(revoked_scope, action_scope)` across
    resolved canonical workspace paths (`_sg_canonical_ws_rel_path`, collapsing `.` / `..`
    / `//` and internal workspace symlinks). Revoking `workspace_write` on
    `path:{ws}:src/protected.py` therefore blocks `write_file`, `edit_file`,
    `rollback_edits`, symlink/dot-segment aliases, AND `run_bash`, `ctx_execute_python`,
    `ctx_run_block`, and `skill_execute_script` whose `workspace:{ws}` footprint overlaps
    `src/protected.py`, while allowing `write_file` on disjoint files and `read_file`
    across `{ws}`.
  - Post-execution `reconcile_and_release` checks `reconciliation.touched_paths` against
    `workspace_write` revocations so any opaque child write to a revoked path triggers
    containment (`capability_revoked`).

## Event-Driven Revocation & Capability-Specific In-Flight Containment

- **Lifecycle hooks (`app.py`):**
  - `PATCH /api/harness/sessions/{session_id}` policy changes bump
    `session_policy_revision`, revoke active session grants (`policy_revision_changed`),
    and — when switching to `plan` — reject pending approvals and contain running
    `workspace_write` / `code_execution` / `external_mcp` subprocesses while preserving
    read-only work.
  - Worker lease epoch fencing (`fence_and_halt`), session cancellation (`POST .../cancel`),
    containment quarantine (`POST .../contain`), and session deletion immediately revoke
    bound grants.
  - Disabling, deleting, or changing `approval_policy` on an MCP server bumps
    `global_policy_revision`, revokes `mcp:{alias}` grants, and contains only in-flight
    `external_mcp` subprocesses targeting `mcp:{alias}`.
- **Selective containment (`ProcessSupervisor.contain_by_capability_sync` / `contain_by_capability_async`):**
  `SupervisedProcessRecord` persists `(grant_id, capabilities, resource_scopes)` for every
  spawned child process (`_register_active_subprocess`). Revoking a grant or capability
  scope runs bounded 3-stage `SIGTERM -> SIGKILL -> verify` termination *only* on matching
  or overlapping in-flight subprocesses (`reason = "capability_revoked"`), leaving
  unrelated active capabilities untouched.
- **HTTP API & Session Snapshot:**
  - `GET /api/harness/sessions/{session_id}/grants` — active/historical grants,
    revocations, policy revisions, and recent audit events (also surfaced under
    `scoped_grants` in session state snapshots).
  - `POST /api/harness/sessions/{session_id}/grants` — issue an auditable finite scoped
    grant (`capabilities`, `resource_scopes`, `ttl_seconds`, optional `max_uses`,
    `tool_names`, `task_bound`, `epoch_bound`).
  - `POST /api/harness/sessions/{session_id}/grants/revoke` — revoke a `grant_id` or
    `(capabilities, resource_scopes)` rule and trigger capability-specific in-flight
    containment (`contain_in_flight = true`).

## Subprocess Environment Secret & Injection Scrubbing (`sanitize_subprocess_env`)

- **Arbitrary subprocesses (`_arbitrary_subprocess_env` / `_git_subprocess_env`):**
  Used by `_start_code_process` (`run_bash`, `ctx_execute_python`, `ctx_run_block`),
  `_execute_skill_script_inner` (`skill_execute_script`), `_docker_telemetry_env`, and
  direct worktree / skill `git` invocations (`allow_scoped_mcp_env = False`). Strips:
  1. All secret-named keys (`*_API_KEY`, `*_SECRET*`, `*_TOKEN*`, `*_PASSWORD*`,
     `*_CREDENTIAL*`, `*_AUTH*`, `OPENAI_*`, `ANTHROPIC_*`, `GEMINI_*`, `AWS_*`, `GH_*`,
     `GITHUB_*`, `HF_*`, `NPM_*`, `PYPI_*`, etc.);
  2. All loader, startup, SSH, and Git-config injection keys (`LD_PRELOAD`,
     `LD_LIBRARY_PATH`, `DYLD_*`, `PYTHONPATH`, `PYTHONSTARTUP`, `NODE_OPTIONS`,
     `BASH_ENV`, `ENV`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `SSH_AUTH_SOCK`,
     `GIT_EXEC_PATH`, `GIT_TEMPLATE_DIR`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`,
     `GIT_CONFIG_KEY_*`, `GIT_CONFIG_VALUE_*`, `STUDIO_API_TOKEN`);
  3. Any key whose value (`len >= 6`) contains a registered secret from
     `STUDIO_API_TOKEN`, `cloud_providers.api_key`, or `mcp_servers` (`auth_token`,
     sensitive `headers_json`, sensitive `env_json`).
- **Scoped stdio MCP servers (`_mcp_stdio_env`):**
  Passes `allow_scoped_mcp_env = True` with `known_secret_values` excluding only that
  target server's own configured `env`, so an MCP server receives its own configured
  credentials while never inheriting `STUDIO_API_TOKEN`, cloud provider keys, other MCP
  servers' secrets, or loader/Git-config injection variables.
