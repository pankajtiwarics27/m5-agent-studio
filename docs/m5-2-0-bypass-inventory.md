# M5 2.0 execution-spine bypass inventory (maintained)

**Maintained inventory across `M5-P0-06` through `M5-P5-03`**
("Known bypass inventory maintained" -> "Fault-injection and security release gate").
Source of the initial entry-point enumeration:
[`m5-2-0-integration-audit.md`](m5-2-0-integration-audit.md) §3 (audit commit
`1e2e0bd`). This file records, for every consequential entry point, whether it
crosses the extracted execution-spine boundaries (ADR-0002: `Policy`,
`ActionLedger`, `IntentLedger`, `DurableActionBoundary`, `ActiveAdmissionScope`) — and what
confinement or persistence gate enforces safety when an operator-facing helper is
not a model-invoked tool. **No independent complete-coverage claim is made**:
author-side call-path hardening and adversarial tests are complete through
`M5-P5-03` ([`m5-2-0-security-release-gate.md`](m5-2-0-security-release-gate.md)),
while independent human security verification (`AS-031`,
[`security-review-signoff.md`](security-review-signoff.md)) remains `deferred`
(`release-blocked`).

Status legend:

- **through-boundary** — crosses `execution_spine.DurableActionBoundary`
  (action-class & grant-provenance check -> task-state & epoch check ->
  authorize -> durable intent -> scoped anti-bypass execution -> durable
  outcome);
- **gated** — operator/persistence helper outside model tool dispatch that is
  deny-by-default or guarded by explicit policy, quarantine, confinement, and
  unresolved-effect checks and fails closed (observed);
- **read-only** — fixed-argv host introspection or confined workspace/vault
  reads with untrusted hooks disabled; no consequential effect;
- **outside** — consequential effect not crossing either boundary or fail-closed
  confinement gate (none remaining on current `HEAD`).

## 1. Action admission (policy + task state + epoch + anti-bypass + audit)

| Entry point | Status | Notes / owning story |
|---|---|---|
| `app._execute_harness_tool` (harness, GSD, SDLC, and Direct Chat tools: `run_bash`, `ctx_execute_python`, `ctx_run_block`, `write_file`, `edit_file`, `rollback_edits`, `oc_save_doc`, `delegate_subagent`, `mcp__*`, managed reads) | **through-boundary** | Via `_run_authorized_tool` -> `DurableActionBoundary` (`M5-P0-06`, `M5-P1-02`–`M5-P1-05`, `M5-P3-02`–`M5-P4-03`): enforces `ActionClass`, grant provenance (`ScopedGrantStore`), `runtime_tasks` state & `runtime_task_epochs` lease fencing, `ExecutionProfileCoordinator` resource barriers, `SecurityIncidentStore` admission, `tool_audit_events`, and `require_active_admission` anti-bypass on `_execute_harness_tool_inner` |
| `app.execute_mcp_tool_call` (outbound MCP HTTP/stdio tool calls and `reconcile_mcp_effects_async`) | **through-boundary** | Crosses `DurableActionBoundary` + `McpExternalEffectStore` idempotency binding & uncertain-write hold (`M5-P0-06`, `M5-P1-02`, `M5-P3-05`) + `require_active_admission` on `_execute_mcp_tool_call_inner` |
| `app._execute_mcp_stdio_tool` (inbound `--mcp` stdio bridge tools `mcp_bridge__*`) | **through-boundary** | Crosses `_run_authorized_tool` -> `DurableActionBoundary`; managed-read bridge tools stay separated from consequential bridge tools (`start_council`, `send_response`, `summon_omlx_agent`, `close_council`, `create_harness_worktree`, `oc_save_doc`), which enforce `require_active_admission` inside `_dispatch_mcp_stdio_tool` (`M5-P1-02`) |
| GSD / Caveman smart blocks (`test-summary`, `first-error`, `json-peek`, `csv-peek`, `shortcut-debt`) | **through-boundary** | Normalized block selector (`M5-P1-02`): `json-peek`, `csv-peek`, and `shortcut-debt` classify as `read_only` managed reads; `test-summary` and `first-error` classify as `consequential` and enforce `require_active_admission` inside `run_reusable_block` |

## 2. Code execution (subprocess) paths

| Entry point | Status | Notes / owning story |
|---|---|---|
| `app._run_skill_script` (`skill_execute_script`, both `build_skills_context` and `_fetch_online_inspiration` call sites) | **through-boundary** | Routed through `_run_authorized_tool` -> `DurableActionBoundary` with `skill_opt_in` provenance (`M5-P0-03`, `M5-P1-02`, `M5-P3-03`): requires `STUDIO_ALLOW_SKILL_CODE=1`, live execution mode, non-`plan` policy, supervised `ProcessSupervisor` registration, and `require_active_admission` on `_execute_skill_script_inner` |
| `app._mcp_stdio_rpc` | **gated** | `STUDIO_ALLOW_MCP_STDIO=1` (default off); tool calls reach it only via admitted `execute_mcp_tool_call` / `_execute_harness_tool` (`_execute_mcp_tool_call_inner` anti-bypass) with `scoped_grants.sanitize_subprocess_env` secret scrubbing (`M5-P3-04`); runs with host-user privileges, not the container sandbox |
| `app.install_skills_native_git` (skill install `git clone`) | **gated** | Authenticated operator API trigger; fixed git argv from validated GitHub HTTPS spec |
| Telemetry probes (`_sysctl_int`, `_orbstack_host_rss_bytes`, `get_docker_telemetry`, `get_mac_unified_memory`) | **read-only** | Fixed argv, no user or repository code |
| Internal Git helpers (`_inspect_git_repo`, `_create_harness_worktree`, `_registered_worktree_branch`, `_remove_harness_worktree`, `_compute_git_diff`, `workspace_snapshots._wsnap_run_git`, `_ensure_git_exclude`) | **gated** | Hardened across `M5-P2-01` and `M5-P5-03` (`ADR-0021`): non-destructive argv enforcement (`assert_non_destructive_git_argv`), secret-scrubbed `_git_subprocess_env()` / `_wsnap_git_env()` with `GIT_CONFIG_COUNT=4` disabling `core.fsmonitor`, `core.hooksPath`, `protocol.file.allow`, and `diff.external`, `--no-ext-diff`, symlink/secret exclusion in `_compute_git_diff`, and symlink-refusing `.git/info/exclude` updates |
| `supply_chain.py`, `macos_hardware_profiler.py`, `scripts/security_release_gate.py` subprocesses | **read-only** | Release/hardware/gate verification tooling; isolated from live session state |

## 3. Persistence writes (SQLite / sidecars / workspace files)

| Entry point | Status | Notes / owning story |
|---|---|---|
| `tool_audit_events` insert/finalize (`_audit_tool_start`/`_audit_tool_finish`) | **through-boundary** | Behind `execution_spine.ActionLedger` (`M5-P0-06`) via `runtime_persistence.run_write_transaction` (`M5-P1-01`) |
| Schema v3 spine store (`runtime_persistence.RuntimePersistenceStore`: `runtime_tasks`, `runtime_task_epochs`, `runtime_task_transitions`, `runtime_actions`) | **gated** | Full single-owner `WorkerLeaseCoordinator` + `DurableTaskStateMachine` + `TaskCheckpointStore` + `VerificationEvidence` landed (`M5-P1-01`–`M5-P1-06`, `ADR-0003`–`ADR-0006`) |
| Schema/seed writers (`upgrade_database`, `init_db`, seeding, `_migrate_legacy_secrets`, `api_history_retention_run`) | **gated** | Routed through `runtime_persistence.upgrade_database` / `run_write_transaction` with `0600` pre-upgrade snapshot, startup orphan sweeps (`DurableActionLedger`, `ProcessSupervisor`, `McpExternalEffectStore`, `ScopedGrantStore`), and zero raw `sqlite3.connect` bypasses |
| Provider/combo/Hall/MCP registry CRUD (`upsert_cloud_provider`, `upsert_model_combo`, `create_hall_session`, `upsert_mcp_server`, etc.) | **gated** | Authenticated operator API actions routed through `runtime_persistence.run_write_transaction` with keyring I/O outside DB write locks (`M5-P1-01`); `--mcp` bridge Hall mutations also cross `DurableActionBoundary` |
| Harness session SQLite + durable sidecar stores (`TaskEventLog`, `ObservedVersionIndex`, `EditBatchJournal`, `WorkspaceSnapshotStore`, `ArtifactLineageStore`, `WorkspaceQuarantineStore`, `ContainmentStore`, `ScopedGrantStore`, `McpExternalEffectStore`, `SecurityIncidentStore`, `ChildDelegationStore`) | **gated** | Turn lifecycles run on `DurableTaskStateMachine` (`M5-P1-05`), events stream/replay from durable `TaskEventLog` (`M5-P1-07`), and all `$HARNESS_STORE_DIR` sidecars enforce atomic temp+fsync+rename, `O_NOFOLLOW`, and corruption fail-closed invariants (`tests/test_durable_state_contract.py`) |
| Workspace managed edits (`write_file`, `edit_file`, `rollback_edits`), `_apply_worktree_to_main`, and Agile SDLC `.sdlc` writes (`_write_sdlc_backlog`, `_write_sdlc_confined_text`) | **gated** | Tool edits cross `DurableActionBoundary` + CAS `ObservedVersionIndex` + `EditBatchJournal` + `WorkspaceSnapshotStore` (`M5-P2-01`–`M5-P2-03`); `_apply_worktree_to_main` and `.sdlc` helpers enforce non-`plan` policy, `workspace_quarantine` freeze checks, unjoined-child and unresolved-effect holds, dirty-main conflict refusal, and descriptor-relative `O_NOFOLLOW` `atomic_write_confined_bytes` (`M5-P5-01`, `M5-P5-03`) |
| `side_effect_ledger.py` (`DurableActionLedger` over `runtime_actions`) | **through-boundary** | Integrated into `_run_authorized_tool` -> `DurableActionBoundary` (`M5-P1-03`–`M5-P1-04`): commits durable `in_flight` intent before side effects, reconciles target state before takeover, and blocks blind replay of `non_idempotent` or unresolved `uncertain` writes |

## 4. Rule

A new consequential entry point may not be added without a row in this
inventory. Moving or adding any entry point requires regression coverage in
`tests/test_security_release_gate.py` (or the owning contract test module) plus
an update here in the same change. Independent human review of this inventory
is tracked under `AS-031` ([`security-review-signoff.md`](security-review-signoff.md));
until that record is signed `accepted`, `security_release_gate` holds the
release decision at `release-blocked`.
