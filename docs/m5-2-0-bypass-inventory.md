# M5 2.0 execution-spine bypass inventory (maintained)

**Maintained inventory for stories `M5-P0-06`, `M5-P1-01`, `M5-P1-02`, and `M5-P1-03`**
("Known bypass inventory maintained" -> "Durable intent/result ledger
integration"). Source of the initial entry-point enumeration:
[`m5-2-0-integration-audit.md`](m5-2-0-integration-audit.md) §3 (audit commit
`1e2e0bd`). This file records, for every consequential entry point, whether it
crosses the extracted execution-spine boundaries (ADR-0002: `Policy`,
`ActionLedger`, `IntentLedger`, `DurableActionBoundary`, `ActiveAdmissionScope`) — and what it
does instead. **No complete-coverage claim is made**: entries marked `outside`
are real gaps assigned to later stories (`M5-P1-04`, `M5-P1-05`,
`M5-P2-01`–`M5-P2-03`). Update this file in the same change that moves any
entry point across (or adds one).

Status legend:

- **through-boundary** — crosses `execution_spine.DurableActionBoundary`
  (action-class & grant-provenance check -> task-state & epoch check ->
  authorize -> durable intent -> scoped anti-bypass execution -> durable
  outcome);
- **gated** — does not cross the tool boundary, but is deny-by-default behind
  an explicit opt-in or persistence gate and fails closed (observed);
- **read-only** — fixed-argv host introspection or workspace/vault reads; no
  consequential effect;
- **outside** — consequential effect not crossing the boundary; named story.

## 1. Action admission (policy + task state + epoch + anti-bypass + audit)

| Entry point | Status | Notes / owning story |
|---|---|---|
| `app._execute_harness_tool` (harness, GSD, SDLC, and Direct Chat tools: `run_bash`, `ctx_execute_python`, `ctx_run_block`, `write_file`, `edit_file`, `oc_save_doc`, `delegate_subagent`, `mcp__*`, managed reads) | **through-boundary** | Via `_run_authorized_tool` -> `DurableActionBoundary` (`M5-P0-06` + `M5-P1-02`): enforces `ActionClass` (`read_only` vs `managed_edit` / `consequential` / `external`), grant provenance (zero model-inferred permission), authoritative `runtime_tasks` state & `runtime_task_epochs` lease-epoch fencing, `authorize_tool_invocation`, `tool_audit_events`, and `require_active_admission` anti-bypass on `_execute_harness_tool_inner` |
| `app.execute_mcp_tool_call` (outbound MCP HTTP/stdio tool calls from playground and `_fetch_online_inspiration`) | **through-boundary** | Same boundary (`denial_as_exception` preserved) + `require_active_admission` anti-bypass on `_execute_mcp_tool_call_inner` (`M5-P0-06` + `M5-P1-02`) |
| `app._execute_mcp_stdio_tool` (inbound `--mcp` stdio bridge tools `mcp_bridge__*`) | **through-boundary** | Crosses `_run_authorized_tool` -> `DurableActionBoundary`; managed-read bridge tools (`get_current_session_data`, `get_harness_diff`, `oc_search`, `oc_lint`, `codegraph_query`) stay separated from consequential bridge tools (`start_council`, `send_response`, `summon_omlx_agent`, `close_council`, `create_harness_worktree`, `oc_save_doc`), which enforce `require_active_admission` inside `_dispatch_mcp_stdio_tool` (`M5-P1-02`) |
| GSD / Caveman smart blocks (`test-summary`, `first-error`, `json-peek`, `csv-peek`, `shortcut-debt`) | **through-boundary** | Normalized case/whitespace block selector (`M5-P1-02`): `json-peek`, `csv-peek`, and `shortcut-debt` classify as `read_only` managed reads; `test-summary` and `first-error` classify as `consequential`, require admission through `DurableActionBoundary`, and enforce `require_active_admission` inside `run_reusable_block` so direct bypass fails closed |

## 2. Code execution (subprocess) paths

| Entry point | Status | Notes / owning story |
|---|---|---|
| `app._run_skill_script` (`skill_execute_script`, both `build_skills_context` and `_fetch_online_inspiration` call sites) | **through-boundary** | Routed through `_run_authorized_tool` -> `DurableActionBoundary` with server-only `skill_opt_in` provenance (`M5-P1-02`, building on `M5-P0-03`): requires `STUDIO_ALLOW_SKILL_CODE=1`, live execution mode, and non-`plan` policy; records durable `tool_audit_events`; enforces `require_active_admission` on `_execute_skill_script_inner`; container boundary in sandbox mode with no host fallback |
| `app._mcp_stdio_rpc` | **gated** | `STUDIO_ALLOW_MCP_STDIO=1` (default off); tool calls reach it only via admitted `execute_mcp_tool_call` / `_execute_harness_tool` (`_execute_mcp_tool_call_inner` anti-bypass), while explicit operator `/api/mcp/servers/{alias}/sync` uses `tools/list` discovery; runs with host-user privileges, not the code sandbox |
| `app.install_skills_native_git` (skill install `git clone`) | **gated** | Authenticated operator API trigger; fixed git argv from validated GitHub HTTPS spec |
| Telemetry probes (`_sysctl_int`, `_orbstack_host_rss_bytes`, `get_docker_telemetry`, `get_mac_unified_memory`) | **read-only** | Fixed argv, no user code |
| Git helpers (`_inspect_git_repo`, `_create_harness_worktree`, `_registered_worktree_branch`, worktree removal, `_compute_git_diff`, `_apply_worktree_to_main`, `_ensure_git_exclude`) | **outside** | Consequential local git effects (worktree add/remove, exclude writes, worktree->workspace sync) bypass the admission/ledger seam unless invoked via `mcp_bridge__create_harness_worktree`; assigned `M5-P2-01`/`M5-P2-03` (workspace safety) |
| `supply_chain.py`, `macos_hardware_profiler.py` subprocesses | **read-only** | Release/hardware tooling, not the served runtime |

## 3. Persistence writes (SQLite / files)

| Entry point | Status | Notes / owning story |
|---|---|---|
| `tool_audit_events` insert/finalize (`_audit_tool_start`/`_audit_tool_finish`) | **through-boundary** | Behind `execution_spine.ActionLedger` (`M5-P0-06`) and executed via `runtime_persistence.run_write_transaction` (`M5-P1-01`) |
| Schema v3 spine store (`runtime_persistence.RuntimePersistenceStore`: `runtime_tasks`, `runtime_task_epochs`, `runtime_task_transitions`, `runtime_actions`) | **gated** | Durable task/epoch/transition/action store landed (`M5-P1-01`, ADR-0003); `runtime_tasks` state and `runtime_task_epochs` lease-epoch fencing are enforced at admission (`M5-P1-02`), and `runtime_actions` intent/result/replay tracking is enforced across all boundary-routed actions (`M5-P1-03`); worker lease lifecycle and full task-loop migration assigned `M5-P1-04`–`M5-P1-05` |
| Schema/seed writers (`_apply_schema_migrations`, `init_db`, seeding, `_migrate_legacy_secrets`, `api_history_retention_run`) | **gated** | Routed through `runtime_persistence.upgrade_database` / `run_write_transaction` with `0600` pre-upgrade snapshot, rollback/restore protection, startup missing-result sweep (`in_flight -> uncertain`, `M5-P1-03`), keyring I/O outside write locks, and zero raw `sqlite3.connect` bypasses (`M5-P1-01`) |
| Provider/combo CRUD (`upsert_cloud_provider`, `delete_cloud_provider`, `upsert_model_combo`, `delete_model_combo`) | **gated** | Routed through `runtime_persistence.run_write_transaction` with keyring I/O outside DB write locks (`M5-P1-01`); configuration changes are operator API actions, not tool-boundary actions |
| Hall session writers (`create_hall_session`, `append_hall_message`, `close_hall_session_db`) | **gated** | Routed through `runtime_persistence.run_write_transaction` (`M5-P1-01`); inbound `--mcp` bridge Hall mutations also cross `DurableActionBoundary` (`M5-P1-02` + `M5-P1-03`) |
| MCP server registry writers (`upsert_mcp_server`, `sync_mcp_server_tools`, `delete_mcp_server`) | **gated** | Routed through `runtime_persistence.run_write_transaction` (`M5-P1-01`) |
| Harness session create/update/delete SQLite writers + `.harnesses/*.jsonl` sidecars | **outside** | SQLite writes execute via `runtime_persistence.run_write_transaction` (`M5-P1-01`) and tool admission checks live abort/task/epoch state (`M5-P1-02`), but `.harnesses/*.jsonl` / `.messages.json` sidecar files remain non-transactional and full turn lifecycle migration onto `runtime_tasks` is assigned `M5-P1-05` |
| Context vault/FTS writes (`ctx_vault_chunks`, `ctx_vault_fts`, `ctx_session_metrics`, `context_store.upsert_doc`/`delete_doc`) | **gated** | Routed through `runtime_persistence.run_write_transaction` (`M5-P1-01`); tool-triggered `oc_save_doc` (both harness and `--mcp` bridge) crosses `DurableActionBoundary` and `DurableActionLedger` (`M5-P1-02` + `M5-P1-03`) |
| SDLC backlog file writes (`_write_sdlc_backlog`), `write_file` tool, `_apply_worktree_to_main` file sync | **outside** | Filesystem effects; `write_file`'s tool call crosses admission (`M5-P1-02`) and durable intent/reconciliation (`M5-P1-03`), while `_write_sdlc_backlog`, `_apply_worktree_to_main`, and filesystem staging/rollback (`M5-P2-02`/`M5-P2-03`) remain follow-up work |
| `side_effect_ledger.py` (`DurableActionLedger` over `runtime_actions`) | **through-boundary** | Integrated into `_run_authorized_tool` -> `DurableActionBoundary` (`M5-P1-03`) and inlined into standalone `studio.py`: commits durable `in_flight` intent before side effects, tracks stable `action_id` + monotonic `attempt`, separates `never_started` / `in_flight` / `committed` / `uncertain`, reconciles missing results to `uncertain` (never `failed`), and blocks blind replay of `non_idempotent` or unverified `uncertain` writes |

## 4. Rule

A new consequential entry point may not be added without a row in this
inventory. Moving a row from `outside`/`gated` to `through-boundary` requires
contract or integration tests plus this update in the same change. Independent
verification of this inventory is required before any complete-coverage claim
(`M5-P5-03`).
