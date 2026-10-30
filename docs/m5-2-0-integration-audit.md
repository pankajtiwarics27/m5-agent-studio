# M5 2.0 integration and security-path audit (M5-P0-02)

Evidence record for roadmap story `M5-P0-02` (Repository integration and
security-path audit). Every statement below was produced by inspecting the
checkout at commit `1e2e0bd876409a9e4eeb4b0128b403422bac7013` on 2026-10-30
(Linux x86_64 sandbox, Python 3.11.2). Line numbers refer to `app.py` unless
another file is named. Method: `grep`/`sed` over the working tree plus targeted
reads of each call site; no behavior was assumed from module names.

Legend for the integration table:

- **code-exists** — the file is present in the checkout;
- **integrated** — `app.py` (the served application) imports and calls it;
- **tested** — a dedicated unit-test module exists and the suite passes
  (see [`m5-2-0-baseline-evidence.md`](m5-2-0-baseline-evidence.md));
- **qualified** — behavior demonstrated live on the target platform
  (macOS/Apple Silicon for hardware claims, a live container runtime for
  isolation claims). Nothing sandbox- or hardware-related is qualified in this
  environment.

## 1. Reported-claim verification (evidence-basis items)

The M5 2.0 roadmap recorded nine reported findings as `reported_unverified`.
Verified states against this checkout:

| # | Reported claim | Verified state | Evidence |
|---|---|---|---|
| 1 | `app.py` is approximately 2200 lines | **Refuted** | `wc -l app.py` = 9,627 lines (462 KB). `studio.py` (generated) = 15,235 lines. |
| 2 | `side_effect_ledger.py` not imported into `app.py` | **Confirmed** | Zero occurrences of `side_effect_ledger` in `app.py`. The module (`side_effect_ledger.py`, durable idempotency ledger) is unit-tested (`tests/test_side_effect_ledger.py`) but has no runtime call site. |
| 3 | `sandbox_boundary.py` not imported; inline execution functions exist | **Confirmed** | Zero occurrences of `sandbox_boundary` in `app.py`. Inline equivalents: `_execution_mode` (L360), `_sandbox_workspace_preflight` (L379), `_sandbox_command` (L410), `_start_code_process` (L458), `_execute_limited` (L492). |
| 4 | Skill execution uses direct `create_subprocess_exec` bypasses | **Confirmed, with two distinct paths** | (a) `build_skills_context` (L1806) runs `<skill>/scripts/search.py` via `asyncio.create_subprocess_exec` (L1831), gated by `STUDIO_ALLOW_SKILL_CODE` (default `0`, L294) but not routed through `_start_code_process`, so when enabled it runs unsandboxed even in `sandbox` execution mode. (b) `_fetch_online_inspiration` (L8571) runs `.agents/skills/ui-ux-pro-max/scripts/search.py` via `asyncio.create_subprocess_exec` (L8645) gated **only by file existence** — not by `STUDIO_ALLOW_SKILL_CODE`. See §3. |
| 5 | `STUDIO_ALLOW_HOST_EXECUTION=1` bypass exists | **Confirmed as a documented, deny-by-default opt-in** | Parsed at L241; legacy shorthand: setting it without `STUDIO_EXECUTION_MODE` selects `host` mode (L243); host mode refused without it (L246-247, L2281-2282). It enables unsandboxed `run_bash`/`ctx_execute_python` as the Studio OS user. Documented in `SECURITY.md` and the configuration reference. |
| 6 | `mcp_client` imported but no invocation call sites found | **Refuted** | `mcp_client` is imported (L67) and **invoked**: `mcp_client.adaptive_call` (L4643), `mcp_client.SessionStore` (L4650), `mcp_client.TransportResponse.from_httpx` (L4620) inside `_mcp_http_rpc` (L4623). |
| 7 | Frontend is `static/index.html` vanilla JS | **Confirmed** | `static/index.html` (4,228 lines), single inline `<script>` block (L1685); zero matches for React/Vue/Angular/Svelte/webpack/`import … from`/`require(`. |
| 8 | SQLite WAL and FTS5 used | **Confirmed** | `PRAGMA journal_mode=WAL` (L1039); FTS5 tables `opencontext_fts` (L1181) and `ctx_vault_fts` (L1200) created `USING fts5(...)`; FTS5 search paths L5820-5928 via `context_store.sanitize_fts5_query`. |
| 9 | 24 GB peak memory unmeasured | **Confirmed (still unmeasured)** | This environment is Linux x86_64 with ~4 GB RAM (`MemTotal: 4034452 kB`), not a 24 GB Apple Silicon Mac; no oMLX runtime present. No memory measurement was taken or invented. |

## 2. Route → effect trace (FastAPI to subprocess, SQLite writes, MCP)

65 route registrations exist in `app.py`. The consequential paths traced:

### 2.1 Tool execution (harness / chat)

```
POST /api/harness/run (L8025)  POST /api/chat/stream (L3956)
  -> build_skills_context (L1806; skill script subprocess, see §3)
  -> _execute_harness_tool (L7679)            "central policy and audit boundary"
     -> _run_authorized_tool (L7592)          authorize -> durable audit -> execute
        -> authorize_tool_invocation (L6217)  inline policy (class ToolAuthorization L6206)
        -> _audit_tool_start/_finish (L7566/L7581) -> tool_audit_events table (L7571/L7586)
        -> _execute_harness_tool_inner (L7182)
           -> run_bash (L7383) / ctx_execute_python (L7489) / ctx_run_block (L7476)
              / test-summary & first-error blocks (L5717)
              -> _execute_limited (L492) -> _start_code_process (L458)
                 -> _sandbox_command (L410)  [mode=sandbox: docker/podman, network off]
                 -> host create_subprocess_exec (L467)  [mode=host: requires
                    STUDIO_ALLOW_HOST_EXECUTION=1; mode=disabled: raises (L458-461)]
           -> write_file (L7327)            workspace write
           -> oc_save_doc (L7536)           OpenContext SQLite write
```

### 2.2 MCP

```
POST /api/mcp/servers/{alias}/call (L5030)
  -> execute_mcp_tool_call (L7655) -> _run_authorized_tool (L7592)
     -> _mcp_http_rpc (L4623) -> mcp_client.adaptive_call (L4643)   [HTTP, pinned transport]
     -> _mcp_stdio_rpc (L4655) -> create_subprocess_exec (L4670)    [requires
        STUDIO_ALLOW_MCP_STDIO=1, default 0 (L293); raises PermissionError otherwise]
```

### 2.3 Skill installation

```
POST /api/skills/install (L3480)
  -> install_skills_native_git (L1910) -> git clone/fetch subprocess (L1916)
```

### 2.4 GSD / SDLC inspiration (contains the ungated skill bypass)

```
POST /api/harness/gsd-wave (L8290)   POST /api/harness/sdlc/run (L9075)
  -> _fetch_online_inspiration (L8571)
     -> enabled search MCP servers (L8586-8597)
     -> outbound HTTPS: api.github.com search (L8603), duckduckgo.com (L8621)
     -> skill script subprocess: .agents/skills/ui-ux-pro-max/scripts/search.py (L8645)
```

### 2.5 Read-only host introspection and git helpers (no user code executed)

`_sysctl_int` (L1973, `sysctl`), `_orbstack_host_rss_bytes` (L1980, `ps`),
`get_docker_telemetry` (L1997, `docker ps`/`docker stats`, L2005/L2017),
`get_mac_unified_memory` (L2050, `vm_stat`, L2058), `_inspect_git_repo` (L6355,
`git rev-parse`, L6362-6372), `_create_harness_worktree` (L6400, `git worktree
add`, L6407), `_registered_worktree_branch` (L6425), worktree removal (L6475,
`git worktree remove --force`; the surrounding function preserves the worktree
on error/timeout), `_compute_git_diff` (L7701, `git status`/`git diff`, L7706-
7745), `_apply_worktree_to_main` (L7738, `git status --porcelain`).

## 3. Direct subprocess and consequential-write inventory

### 3.1 Every direct subprocess entry point in `app.py`

| Line | Function | Command class | Gate before execution |
|---|---|---|---|
| L446 | `_cleanup_sandbox_container` | `docker`/`podman` rm | internal (sandbox mode only) |
| L467 | `_start_code_process` | tool code (shell/Python) | `_execution_mode` L360: `disabled` raises; `host` requires `STUDIO_ALLOW_HOST_EXECUTION=1`; `sandbox` uses container argv L410 |
| L478 | `_start_code_process` | tool code, host mode | same as above |
| L484 | `_start_code_process` (`create_subprocess_shell`) | tool code, host mode shell | same as above |
| L1831 | `build_skills_context` | skill `scripts/search.py` | `STUDIO_ALLOW_SKILL_CODE=1` (default 0) **and** task-design heuristic; **not** routed through `_start_code_process` |
| L1916 | `install_skills_native_git` | `git clone`/`fetch` for skill install | API call `POST /api/skills/install` (authenticated) |
| L1975/1984/2058 | telemetry helpers | `sysctl`/`ps`/`vm_stat` | fixed argv, read-only |
| L2005/2017 | `get_docker_telemetry` | `docker ps`/`stats` | fixed argv, read-only |
| L4670 | `_mcp_stdio_rpc` | configured MCP stdio command | `STUDIO_ALLOW_MCP_STDIO=1` (default 0) |
| L6362-6372 | `_inspect_git_repo` | `git rev-parse` | fixed argv, read-only |
| L6407 | `_create_harness_worktree` | `git worktree add` | git workspace precondition |
| L6425 | `_registered_worktree_branch` | `git worktree list` | fixed argv, read-only |
| L6475 | worktree removal path | `git worktree remove --force` | branch registered check `_registered_worktree_branch`; preserves data on failure |
| L6485 | `_ensure_git_exclude` | `git` local exclude write | internal |
| L7706-7745 | `_compute_git_diff` | `git status`/`git diff` | fixed argv, read-only |
| L8645 | `_fetch_online_inspiration` | skill `scripts/search.py` (`sys.executable`) | **file existence only** — no `STUDIO_ALLOW_SKILL_CODE` check |

Modules with subprocess use: `macos_hardware_profiler.py` L34 (fixed-argv
hardware probes, read-only), `supply_chain.py` L506/L544 (build/scan tooling,
release-time only). `tests/` and `scripts/` excluded (not runtime).

### 3.2 Consequential SQLite write entry points

Writers are in `app.py` and `context_store.py`; every one is reached only
through an authenticated route or an internal lifecycle call:

| Writer (app.py unless noted) | Line(s) | Reached from |
|---|---|---|
| `_apply_schema_migrations` / `init_db` | L986 / L1034 | startup |
| `_migrate_legacy_secrets` (keyring + DB updates) | L615, L622, L641 | startup |
| `_seed_env_providers_and_default_combo` / `_seed_default_opencontext` | L1242 / L1282 | startup seeding |
| `upsert_cloud_provider` / `delete_cloud_provider` | L1338 / L1396 | `POST /api/providers` (L2542), `DELETE /api/providers/{alias}` (L2560) |
| `upsert_model_combo` / `delete_model_combo` | L1416 / L1437 | `POST /api/combos` (L2612), `DELETE /api/combos/{alias}` (L2626) |
| `record_perf` / `save_run` | L1487 / L1517 | chat/harness runs (internal) |
| `create_hall_session` / `append_hall_message` / `close_hall_session_db` | L1533 / L1562 / L1589 | `/api/hall/*` (L3542, L3566, L3582, L3591) |
| MCP server registry upsert/sync/delete | L4578, L4760, L4770, L5014 | `/api/mcp/servers*` (L4901, L4918, L4974, L5000, L5019) |
| context metrics + vault chunk/FTS writes | L5796, L5847-5851, L5968-5972 | compaction tools (internal) |
| harness session create/update/delete | L6647, L6675, L6710-6720, L7918, L7934, L8058 | `/api/harness/sessions*` |
| tool audit events insert/finalize | L7571, L7586 | every `_run_authorized_tool` call |
| `_write_sdlc_backlog` | L8676 | `POST /api/harness/sdlc/run` (L9075) |
| `context_store.upsert_doc` / `delete_doc` (context_store.py) | L91-112 | `/api/opencontext/docs*` (L5124, L5141, L5159) |

Consequential filesystem writes: `write_file` tool (L7327, workspace),
`_apply_worktree_to_main` (L7738, syncs worktree changes into the main
workspace on session apply, `POST /api/harness/sessions/{id}/apply` L7952),
skill installation (L1910), SDLC planning files (`_ensure_sdlc_dir` L8555 and
`_write_sdlc_backlog`).

## 4. Contract-module integration status

42 top-level modules ship in `pyproject.toml`. Integrated = imported by
`app.py` (the only served application; `studio.py` is generated from it).

| Module | code-exists | integrated | tested | qualified |
|---|---|---|---|---|
| context_store | yes | **yes** (20 refs) | yes | no |
| mcp_client | yes | **yes** (9 refs; `adaptive_call` L4643) | yes | no |
| onboarding_preflight | yes | **yes** (5 refs) | yes | no |
| history_privacy | yes | **yes** (`apply_retention`, `export_history`; routes L3895/L3909) | yes | no |
| workflow_orchestration | yes | **yes** (`WorkflowOrchestrator` L69, L3234) | yes | no |
| side_effect_ledger | yes | **no** (0 refs in `app.py`) | yes | no |
| sandbox_boundary | yes | **no** (0 refs; inline equivalents L360-492) | yes | no |
| routing_policy | yes | **no** | yes | no |
| execution_budget | yes | **no** (used only by other modules) | yes | no |
| session_trust | yes | **no** | yes | no |
| resource_admission | yes | **no** | yes | no |
| capability_negotiation | yes | **no** (module-internal graph only) | yes | no |
| workflow_state | yes | **no** | yes | no |
| workflow_checkpoints | yes | **no** | yes | no |
| workflow_tracing | yes | **no** (used by workflow_evaluation) | yes | no |
| workflow_evaluation | yes | **no** | yes | no |
| failure_injection | yes | **no** | yes | no |
| local_runtime_lifecycle | yes | **no** | yes | no |
| local_provider_adapters | yes | **no** (used by model_discovery) | yes | no |
| provider_switching | yes | **no** | yes | no |
| model_switching | yes | **no** (used by context_reassembly) | yes | no |
| model_registry | yes | **no** (hub for the model_* graph) | yes | no |
| model_discovery / model_updates | yes | **no** | yes | no |
| model_calibration / model_comparison | yes | **no** | yes | no |
| model_diagnostics / model_recommendation | yes | **no** | yes | no |
| performance_profiles / comparison_board | yes | **no** | yes | no |
| benchmark_import / benchmark_publication | yes | **no** | yes | no |
| hardware_capabilities / hardware_validation | yes | **no** | yes | no |
| macos_hardware_profiler | yes | **no** (subprocess probes L34; not called by `app.py`) | yes | no |
| private_network_gateway | yes | **no** | yes | no |
| supply_chain / release_manager | yes | **no** (release-time scripts call them) | yes | no |
| demo_tour | yes | **no** (standalone demo tool) | yes | no |

Summary: 5 of 42 modules are wired into the served application. 37 are
standalone contract modules with passing unit tests but no `app.py` call path.
**A module with tests is not an integrated capability.** This is the single
most important gap between the repository's documentation tone and its runtime
surface, and it is what stories `M5-P0-06` and `M5-P1-*` address.

## 5. Security-relevant observations (for M5-P0-03 and beyond)

1. **Ungated skill-script subprocess** — `_fetch_online_inspiration` (L8645)
   executes repository-adjacent skill code with no `STUDIO_ALLOW_SKILL_CODE`
   check and no sandbox routing. Containment target for `M5-P0-03`.
2. **Skill-script subprocess bypasses the sandbox mode** — `build_skills_context`
   (L1831) is flag-gated (default off) but when enabled runs on the host even
   when `STUDIO_EXECUTION_MODE=sandbox`. Containment target for `M5-P0-03`.
3. **Host mode is explicit and documented** — `STUDIO_EXECUTION_MODE=host`
   requires `STUDIO_ALLOW_HOST_EXECUTION=1`; the legacy shorthand is itself
   documented in `SECURITY.md`. It remains unrestricted host execution by
   design; under M5 2.0 invariants, autonomous execution must not silently
   fall back to it, and any isolation claim remains unqualified until a live
   container runtime proves otherwise (`M5-P0-04`, `M5-P3-01`).
4. **Authorization and audit exist inline, not via `side_effect_ledger`** —
   `_run_authorized_tool` (L7592) denies on policy-evaluation failure
   (fail-closed, L7604-7607) and durably records every attempt in
   `tool_audit_events`. Durable idempotency for non-idempotent external
   effects (`side_effect_ledger.py`) is not wired in (`M5-P1-03`).
5. **No live container-runtime evidence exists in this environment** —
   `tests/test_live_sandbox.py` is skipped absent explicit opt-in (recorded in
   the baseline evidence). Isolation guarantees are therefore unqualified.

## 6. Verification commands for this audit

```bash
wc -l app.py studio.py static/index.html
grep -n "side_effect_ledger\|sandbox_boundary" app.py          # 0 matches
grep -n "mcp_client\." app.py                                  # 9 matches (L4616-4650)
grep -n "create_subprocess_exec\|create_subprocess_shell\|subprocess\.run\|subprocess\.check_output" app.py
grep -n "STUDIO_ALLOW_HOST_EXECUTION\|STUDIO_ALLOW_SKILL_CODE\|STUDIO_ALLOW_MCP_STDIO" app.py
grep -n "PRAGMA journal_mode\|USING fts5" app.py
grep -cn "react\|vue\|angular\|svelte\|webpack" static/index.html   # 0
```

All commands were run on 2026-10-30 against the audit commit; outputs are
quoted inline above. Related records:
[`m5-2-0-roadmap-reconciliation.md`](m5-2-0-roadmap-reconciliation.md),
[`m5-2-0-baseline-evidence.md`](m5-2-0-baseline-evidence.md).
