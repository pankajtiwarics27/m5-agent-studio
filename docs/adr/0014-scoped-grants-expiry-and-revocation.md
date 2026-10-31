---
id: ADR-0014
title: scoped-grants-expiry-and-revocation
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0014: Operation, resource, and task-scoped permission grants, event-driven revocation, capability containment, and subprocess secret hygiene

## Context

`M5-P3-04` requires auditable, least-privilege permissions bounded to explicit operations,
resources, and task lifecycles, paired with non-renewable expiry, immediate revocation,
capability-specific in-flight containment, and strict exclusion of credentials from
arbitrary subprocess environments:

1. **Coarse approval granularity and unbounded persistence:** Previously, interactive
   operator approvals resolved an in-memory `_PENDING_APPROVALS` future with a boolean
   `approved=True` flag without persisting a finite expiry, usage ceiling (`max_uses`),
   policy revision snapshot, or resource-scope binding. Once granted or operating under
   broad policies, permissions lacked durable operation/resource/task records or automatic
   invalidation when a session policy changed, a worker lease epoch turned over, an MCP
   server was disabled, or a task cancelled.
2. **Cross-tool revocation bypass risk:** Blocking a single tool name (for example
   `write_file` on `src/protected.py`) leaves adjacent execution surfaces open (`edit_file`,
   `rollback_edits`, `run_bash`, `ctx_execute_python`, `ctx_run_block`, or
   `skill_execute_script`). Admission and post-execution reconciliation needed a unified
   capability and canonical resource-scope model where a revocation on a capability and
   resource scope blocks every overlapping tool, path alias, dot-segment traversal, or
   internal workspace symlink.
3. **All-or-nothing containment on partial revocation:** `ProcessSupervisor` (`ADR-0013`)
   could contain an entire session or task, which terminated unrelated running subprocesses
   when only a narrow capability (for example `external_mcp` on a single stdio MCP server)
   was revoked.
4. **Subprocess secret and loader-injection exposure:** `_minimal_execution_env` accepted
   arbitrary caller-supplied `extra` keys unless matched by name, did not cross-scrub
   registered Studio keyring values (`STUDIO_API_TOKEN`, cloud provider API keys, and
   unrelated MCP server tokens/headers/env secrets), and several direct `git` worktree /
   skill-clone calls ran without passing through centralized secret and Git-config
   environment scrubbing.

## Decision

Introduce `scoped_grants.py` (stdlib-only, inlined into standalone `studio.py`) and
integrate it across `execution_spine.py`, `execution_profiles.py`,
`process_supervision.py`, and `app.py`:

1. **Durable scoped grant and revocation store (`$HARNESS_STORE_DIR/grants/state.json`):**
   - `ScopedGrantStore` persists `schema_version = 1` state atomically (`0700` directory,
     `0600` file, `O_NOFOLLOW` symlink refusal on directories and files, `fsync` on temp
     file and parent directory) containing `grants`, `revocations`,
     `session_policy_revisions`, `global_policy_revision`, `rejected_approvals`, and a
     bounded `audit_events` log (`500` entries). Corrupted JSON, schema mismatches, or
     symlinked state paths raise `GrantStoreIntegrityError` so admission fails closed.
   - Every `ScopedGrantRecord` binds `grant_id`, `session_id`, optional `task_id` and
     `epoch`, canonical `capabilities` (`workspace_read`, `workspace_write`,
     `code_execution`, `external_mcp`, `context_write`, `subagent_delegation`,
     `council_mutation`), optional `tool_names` and `args_sha256`, canonical
     `resource_scopes` (`workspace:{ws}`, `dir:{ws}:{rel}`, `path:{ws}:{rel}`,
     `mcp:{alias}`, `mcp:{alias}:{tool}`, `context:{folder}`, `global:*`),
     `session_policy_revision`, `global_policy_revision`, finite `issued_at` and
     `expires_at` (`ttl_seconds` bounded between `0` and `86400`, never `None` or
     infinite), and optional `max_uses`.
   - **No automatic renewal:** `ScopedGrantStore` exposes no extension or TTL-refresh API.
     Consuming a grant increments `uses_consumed` without touching `expires_at`; once
     `now` reaches `expires_at` or `uses_consumed` reaches `max_uses`, the grant
     transitions one-way to `expired` or `consumed` and never re-activates. Interactive
     approvals persist a one-shot (`max_uses = 1`), exact-`args_sha256`, `300s` finite
     grant; operator rejections record `rejected_approvals` and emit a `grant_rejected`
     audit event without creating any grant.
2. **Two-phase admission validation and cross-tool anti-bypass (`execution_spine.py`, `execution_profiles.py`):**
   - `"grant_id"` is registered in `MODEL_UNTRUSTED_APPROVAL_KEYS` so model-generated tool
     arguments can never self-supply or forge a grant identifier.
   - **Pre-policy gate (`DurableActionBoundary.admit_and_execute` calling `check_pre_policy_denial`):**
     Runs before `_policy.authorize` so active `RevocationRuleRecord` overlaps and explicit
     expired, consumed, revoked, policy-superseded, or scope-mismatched grants return an
     immediate hard denial (`requires_approval=False`, `error_code` in
     `{capability_revoked, grant_expired, grant_revoked, grant_consumed,
     grant_policy_superseded, grant_scope_mismatch}`) rather than falling back to an
     approval prompt.
   - **Post-barrier atomic consumption (`UnifiedExecutionCoordinator.admit_profile`):**
     Re-verifies active revocations and calls `ScopedGrantStore.validate_and_consume` only
     after unified profile checks, origin fences, containment barrier holds, and resource
     footprint barriers succeed, ensuring barrier contention never burns a `max_uses = 1`
     grant.
   - **Subsumption vs overlap (`scope_subsumes` vs `scopes_overlap`):** Grant admission
     requires `scope_subsumes(grant_scope, action_scope)` (a narrow grant cannot authorize
     a broader workspace tool), whereas revocation checks enforce
     `scopes_overlap(revoked_scope, action_scope)` plus canonical symlink/dot-segment path
     resolution (`_sg_canonical_ws_rel_path`), blocking `write_file`, `edit_file`,
     `rollback_edits`, `run_bash`, `ctx_execute_python`, `ctx_run_block`, and
     `skill_execute_script` whenever their footprint intersects a revoked capability and
     scope. Post-execution `reconcile_and_release` additionally checks
     `reconciliation.touched_paths` against `workspace_write` revocations so opaque child
     writes to a revoked file trigger containment.
3. **Event-driven revocation and capability-specific in-flight containment (`process_supervision.py`, `app.py`):**
   - Session policy patches (`PATCH /api/harness/sessions/{session_id}`), worker lease
     epoch fencing (`fence_and_halt`), task cancellation/quarantine/deletion, and MCP
     server disable/delete/policy updates immediately bump the relevant policy revision
     and revoke affected grants, emitting `security/grant_revoked` task events when a
     session event stream is present.
   - `SupervisedProcessRecord` records `(grant_id, capabilities, resource_scopes)` for
     every spawned child process, and `ProcessSupervisor.contain_by_capability_sync` /
     `contain_by_capability_async` selectively runs bounded 3-stage `SIGTERM`, `SIGKILL`,
     and post-kill verification (`reason = capability_revoked`) only on in-flight
     subprocesses whose `grant_id` matches or whose capabilities and resource scopes
     overlap the revoked rule, leaving unrelated running capabilities untouched.
   - Studio exposes `GET /api/harness/sessions/{session_id}/grants`,
     `POST /api/harness/sessions/{session_id}/grants`, and
     `POST /api/harness/sessions/{session_id}/grants/revoke`, and sweeps due grants at
     application startup.
4. **Subprocess secret and injection-variable exclusion (`sanitize_subprocess_env`):**
   - Arbitrary subprocesses (`_arbitrary_subprocess_env` for `_start_code_process`,
     `_execute_skill_script_inner`, `_docker_telemetry_env`, and `_git_subprocess_env` for
     worktree and skill `git` commands) pass `allow_scoped_mcp_env=False`, stripping all
     secret-named environment variables (`*_API_KEY`, `*_SECRET*`, `*_TOKEN*`,
     `*_PASSWORD*`, provider prefixes), loader/startup/Git-config injection variables
     (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`, `PYTHONPATH`, `PYTHONSTARTUP`,
     `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`,
     `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`,
     `GIT_CONFIG_VALUE_*`, `STUDIO_API_TOKEN`), and any value containing registered Studio
     keyring secrets (at least `6` chars).
   - Scoped stdio MCP servers (`_mcp_stdio_env`) pass `allow_scoped_mcp_env=True` with
     `known_secret_values` excluding only that target server's own configured `env`, so an
     MCP server receives its own explicitly configured credentials while never inheriting
     `STUDIO_API_TOKEN`, cloud provider keys, other MCP servers' secrets, or loader/Git
     injection variables.

## Alternatives

1. **Keep boolean `_PENDING_APPROVALS` flags with in-memory TTL dicts:** Rejected because
   in-memory dicts vanish across restarts, leave no auditable ledger of policy revisions
   or consumption counts, and cannot prevent cross-tool revocation bypasses.
2. **Revoke by tool name only (`tool_names=("write_file",)`):** Rejected because `AC 3`
   explicitly requires that revocation cannot be bypassed through another tool; capability
   plus canonical resource-scope overlap blocks every mutating or executing tool whose
   footprint intersects the revoked target.
3. **Terminate all session subprocesses on every single-grant revocation:** Rejected
   because `M5-P3-04` Task 3 requires capability-specific in-flight containment; tagging
   `SupervisedProcessRecord` with `(grant_id, capabilities, resource_scopes)` lets Studio
   terminate an affected `external_mcp` or `code_execution` tree without disrupting
   unrelated active capabilities.

## Tradeoffs

- **Workspace-wide footprint overlap for arbitrary code execution:** Because `run_bash`,
  `ctx_execute_python`, and `skill_execute_script` claim `workspace:{ws}` footprints unless
  confined to a narrower static scope, revoking `workspace_write` on a single file inside
  `{ws}` also denies arbitrary code execution inside `{ws}` while still allowing
  `write_file`/`edit_file` on disjoint files and `read_file` across `{ws}`. This
  conservatism is intentional: an arbitrary shell or Python process in `{ws}` could
  otherwise overwrite the revoked file directly (`AC 3`).
- **Single-process file-locked store:** Matching `ContainmentStore` (`ADR-0013`),
  `TaskEventStore` (`ADR-0005`), and worker leases (`ADR-0003`), `ScopedGrantStore`
  synchronizes concurrent callers within a single Studio process via `RLock` and atomic
  `O_NOFOLLOW` temp-file replacement rather than an external distributed lock service.
- **Length threshold on value-based secret scrubbing:** Value scanning scrubs registered
  secret strings of length `6` characters or longer so accidental short substrings (such as
  `"1"` or `"dev"`) cannot corrupt `PATH` or `LANG`, while key-name scrubbing
  (`is_secret_env_key`) unconditionally strips secret-named keys regardless of value
  length.

## Review Status

Not reviewed. Authored and verified by automated implementation tests
(`tests/test_scoped_grants.py`) in `M5-P3-04`; no independent review is claimed yet.
