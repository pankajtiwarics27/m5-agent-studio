# M5 2.0 execution-spine bypass inventory (maintained)

**Maintained inventory for story `M5-P0-06`** ("Known bypass inventory
maintained"). Source of the entry-point enumeration:
[`m5-2-0-integration-audit.md`](m5-2-0-integration-audit.md) §3 (audit commit
`1e2e0bd`). This file records, for every consequential entry point, whether it
crosses the extracted execution-spine boundaries (ADR-0002: `Policy`,
`ActionLedger`, `DurableActionBoundary`) — and what it does instead. **No
complete-coverage claim is made**: entries marked `outside` are real gaps
assigned to later stories. Update this file in the same change that moves any
entry point across (or adds one).

Status legend:

- **through-boundary** — crosses `execution_spine.DurableActionBoundary`
  (authorize -> durable intent -> execute -> durable outcome);
- **gated** — does not cross the boundary, but is deny-by-default behind an
  explicit opt-in and fails closed (observed);
- **read-only** — fixed-argv host introspection or git reads; no consequential
  effect;
- **outside** — consequential effect not crossing the boundary; named story.

## 1. Action admission (policy + audit)

| Entry point | Status | Notes / owning story |
|---|---|---|
| `app._execute_harness_tool` (harness + Direct Chat tools: `run_bash`, `ctx_execute_python`, `ctx_run_block`, `write_file`, `oc_save_doc`, …) | **through-boundary** | Via `_run_authorized_tool` -> `DurableActionBoundary`; policy `authorize_tool_invocation`, ledger `tool_audit_events` (migrated in M5-P0-06) |
| `app.execute_mcp_tool_call` (MCP tool calls) | **through-boundary** | Same boundary (`denial_as_exception` semantics preserved) |
| GSD smart blocks (`test-summary`, `first-error`, `json-peek`, `csv-peek`) | partial | Shell-producing blocks run through `_execute_limited` (execution-mode gated) but their dispatch is invoked inside the tool loop; admission applies to the outer tool call. Verified in audit §2.1 |

## 2. Code execution (subprocess) paths

| Entry point | Status | Notes / owning story |
|---|---|---|
| `app._run_skill_script` (skill `scripts/search.py`, both call sites) | **gated** | `STUDIO_ALLOW_SKILL_CODE=1` + live execution mode; container boundary in sandbox mode; no host fallback (M5-P0-03; 9 tests) |
| `app._mcp_stdio_rpc` | **gated** | `STUDIO_ALLOW_MCP_STDIO=1` (default off); runs with host-user privileges, not the code sandbox |
| `app.install_skills_native_git` (skill install `git clone`) | **gated** | Authenticated API trigger; fixed git argv from validated HTTPS spec |
| Telemetry probes (`_sysctl_int`, `_orbstack_host_rss_bytes`, `get_docker_telemetry`, `get_mac_unified_memory`) | **read-only** | Fixed argv, no user code |
| Git helpers (`_inspect_git_repo`, `_create_harness_worktree`, `_registered_worktree_branch`, worktree removal, `_compute_git_diff`, `_apply_worktree_to_main`, `_ensure_git_exclude`) | **outside** | Consequential local git effects (worktree add/remove, exclude writes, worktree->workspace sync) bypass the admission/ledger seam; assigned `M5-P2-01`/`M5-P2-03` (workspace safety) |
| `supply_chain.py`, `macos_hardware_profiler.py` subprocesses | **read-only** | Release/hardware tooling, not the served runtime |

## 3. Persistence writes (SQLite / files)

| Entry point | Status | Notes / owning story |
|---|---|---|
| `tool_audit_events` insert/finalize (`_audit_tool_start`/`_audit_tool_finish`) | **through-boundary** | Now behind `execution_spine.ActionLedger` (M5-P0-06) |
| Provider/combo CRUD (`upsert_cloud_provider`, `delete_cloud_provider`, `upsert_model_combo`, `delete_model_combo`) | **outside** | Direct `conn.execute` writers; assigned `M5-P1-01` (persistence discipline) |
| Hall session writers (`create_hall_session`, `append_hall_message`, `close_hall_session_db`) | **outside** | Same |
| MCP server registry writers (~L4630-4830 region) | **outside** | Same |
| Harness session create/update/delete writers | **outside** | Same; task-state story `M5-P1-05` |
| Context vault/FTS writes (`ctx_vault_chunks`, `ctx_vault_fts`, `ctx_session_metrics`, `context_store.upsert_doc`/`delete_doc`) | **outside** | Same |
| Schema/seed writers (`_apply_schema_migrations`, `init_db`, seeding, `_migrate_legacy_secrets`) | **outside** | Startup lifecycle; migration discipline assigned `M5-P1-01` |
| SDLC backlog file writes (`_write_sdlc_backlog`), `write_file` tool, `_apply_worktree_to_main` file sync | **outside** | Filesystem effects; `write_file`'s tool call crosses admission but the write itself is unledgered — `M5-P2-02`/`M5-P2-03` |
| `side_effect_ledger.py` (durable idempotency) | **outside** | Module exists, unit-tested, **not integrated**; owning story `M5-P1-03` |

## 4. Rule

A new consequential entry point may not be added without a row in this
inventory. Moving a row from `outside`/`gated` to `through-boundary` requires
contract or integration tests plus this update in the same change. Independent
verification of this inventory is required before any complete-coverage claim
(`M5-P5-03`).
