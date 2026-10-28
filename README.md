# M5 Agentic Studio (`v9.0.1`)

[![CI](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml)

A **zero-bloat, local-first hybrid AI studio, autonomous coding harness,
universal MCP hub, and context-engineering engine** for **Apple Silicon
(M5 MacBook Air • 24 GB unified memory • 1 TB SSD)**, designed to run
concurrently with OrbStack Docker and desktop apps.

- FastAPI backend (`app.py` + contract modules) • vanilla HTML/CSS/JS UI • generated standalone script (`studio.py`)
- Local/cloud model routing behind one OpenAI-compatible `/v1` gateway
- Agent workflows: direct chat, multi-model Council/Debate, tool-using coding harness, GSD planning, backlog-based SDLC
- SQLite/FTS5 context features, persistent OpenContext documents, output compaction, heuristic CodeGraph
- MCP (HTTP + optional stdio) and repository-provided skills
- M5-oriented telemetry (simulated when the local backend is unavailable — always check the explicit `demo_mode` / `telemetry_source` fields)

> **Status & roadmap:** development is tracked story-by-story in
> [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json)
> with a generated dashboard at
> [`docs/product-roadmap-status.md`](docs/product-roadmap-status.md).
> The working tree is **not** an enterprise-readiness or security-completion
> claim; see [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) before deploying.

---

## Contents

1. [Design philosophy](#1-design-philosophy)
2. [Quick start](#2-quick-start)
3. [Security and safe defaults](#3-security-and-safe-defaults)
4. [Local readiness checks, keyring, and database recovery](#4-local-readiness-checks-keyring-and-database-recovery)
5. [System architecture](#5-system-architecture)
6. [Author-reported M5 / oMLX configuration notes (unverified)](#6-author-reported-m5--omlx-configuration-notes-unverified)
7. [Reference: UI, tools, database, configuration, API](#7-reference-ui-tools-database-configuration-api)
8. [Critical invariants & hard-won gotchas](#8-critical-invariants--hard-won-gotchas)
9. [Project history & research notes](#9-project-history--research-notes)
10. [Development, contributing, and AI agents](#10-development-contributing-and-ai-agents)

---

## 1. Design philosophy

Most multi-agent coding stacks assume unlimited cloud token budgets or 64–128 GB
workstations — Electron frontends, vector databases, LSP daemons, multiple
resident LLMs — and promptly trigger macOS swap thrashing or Metal OOM on a
24 GB laptop. M5 Agentic Studio takes the opposite approach:

1. **Small dependency footprint** and a single FastAPI process.
2. **Local-first with explicit consent** for anything network, cloud, or billable.
3. **Hardware/model/provider independence**: swap models or providers without
   rebuilding the application, subject to runtime and capability compatibility.
4. **Reproducible engineering**: a pinned Git baseline, generated artifacts
   checked byte-for-byte in CI, and honest, evidence-backed documentation.

Performance and quality vary with hardware, model/provider, configuration, and
workload; treat benchmark statements as environment-specific unless they ship
with reproducible test data.

## 2. Quick start

### A. Standalone single-file studio (recommended on macOS)

Requires Python `3.11+` and [`uv`](https://docs.astral.sh/uv/) (dependencies
`fastapi`, `uvicorn`, `httpx`, `keyring` auto-resolve via inline PEP 723 metadata):

```bash
chmod +x studio.py
uv run studio.py
# Web UI:               http://127.0.0.1:3000  (browser prompts for the local API token on first API call)
# Token file:           ~/.config/m5-agent-studio/api-token
# OpenAI gateway (/v1): http://127.0.0.1:3000/v1
```

### B. Development / split-source mode

When editing `app.py` and `static/index.html`:

```bash
uv run app.py
# or, with dependencies already installed:
STUDIO_HOST=127.0.0.1 STUDIO_PORT=3000 python3 app.py
```

SQLite history is stored beside the entry point as `studio_history.db` by
default; set `STUDIO_DB_PATH` for an explicit location and
`STUDIO_API_TOKEN_FILE` for the token path.

### C. Expose Studio as an MCP `stdio` server

Any MCP-capable agent (Claude Code, Codex, Cursor, `dsh`, `hn`) can summon the
local Council, query CodeGraph, or read/write OpenContext memory:

```json
{
  "mcpServers": {
    "m5-studio": {
      "command": "uv",
      "args": ["/absolute/path/to/m5-agent-studio/studio.py", "--mcp", "--agent-name", "ClaudeCode"]
    }
  }
}
```

The stdio server is disabled by default. Set `STUDIO_ALLOW_MCP_STDIO=1` in that
process environment only if you trust the client: it runs with the Studio
user's OS privileges and is not sandboxed.

### D. Point any OpenAI-compatible CLI at Studio's gateway

`dsh`, `aider`, Cursor, Continue, etc. can use the unified `/v1` router across
local oMLX, configured cloud providers (`provider::model`), and failover
`combo/<alias>` routes:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:3000/v1"
# Default loopback auth persists a random token here; if configured via env, use that exact value instead.
export OPENAI_API_KEY="${STUDIO_API_TOKEN:-$(cat ~/.config/m5-agent-studio/api-token)}"
```

### E. Optional: external `OmniRoute` upstream gateway (`:20128`)

Studio ships an `omniroute` provider preset (`http://127.0.0.1:20128/v1`). To
run the upstream [OmniRoute](https://github.com/diegosouzapw/OmniRoute) server:

```bash
# OrbStack Docker (multi-arch ARM64):
docker run -d --name omniroute --restart unless-stopped --stop-timeout 40 \
  -p 127.0.0.1:20128:20128 -v omniroute-data:/app/data \
  -e INITIAL_PASSWORD="change-me-now" diegosouzapw/omniroute:latest
# or: npx -y omniroute@latest --no-open   (Node >=22.22.2 <23 || >=24.0.0)
```

## 3. Security and safe defaults

- **Loopback + token by default.** The server binds to loopback; first startup
  creates a random bearer token at `~/.config/m5-agent-studio/api-token` with
  restrictive permissions. `/api/*` and `/v1/*` require it. Non-loopback binds
  require an explicit high-entropy `STUDIO_API_TOKEN` (≥ 32 chars) and refuse
  `STUDIO_AUTH_DISABLED=1`. Configure `STUDIO_ALLOWED_HOSTS` and, behind a TLS
  proxy, `STUDIO_ALLOWED_ORIGINS` explicitly.
- **Request limits.** Expensive workflow endpoints have a per-client-IP
  sliding-window limit; bodies are capped by `STUDIO_MAX_REQUEST_BYTES`.
  Disallowed `Host` headers and cross-origin state-changing requests are
  rejected before authorization.
- **Secrets in the OS credential store.** Provider API keys and MCP tokens are
  stored as keyring references; legacy plaintext values are migrated at startup
  and startup fails closed if the keyring cannot accept them. Local history
  databases still contain sensitive prompts/code/output — protect them.
- **Private-network/HTTP URLs rejected by default.** Prefer per-host
  `STUDIO_PRIVATE_URL_HOSTS` exceptions; `STUDIO_ALLOW_PRIVATE_URLS=1` is a
  broader compatibility opt-in. Requests are re-resolved and validated
  immediately before I/O, connect to the exact validated IP (original Host/SNI
  preserved), never follow redirects, and bypass ambient proxies.
- **Skills install copy-only.** Skills come from GitHub over HTTPS;
  downloaded `scripts/search.py` is not executed unless
  `STUDIO_ALLOW_SKILL_CODE=1`. Skill text itself is untrusted prompt input.
- **Code execution disabled by default.** `STUDIO_EXECUTION_MODE=sandbox` runs
  shell/Python/test commands in Docker/Podman (`--network=none`, read-only
  root, dropped capabilities, no-new-privileges, resource limits, only the
  session workspace mounted). Pre-pull a trusted `STUDIO_SANDBOX_IMAGE`; no
  image is pulled automatically. The workspace mount is writable by design; the
  preflight (root/home refusal, secret-filename refusal, credentialed-remote
  refusal, scan budget) is filename-based defense-in-depth, not secret
  detection. `STUDIO_EXECUTION_MODE=host` additionally requires
  `STUDIO_ALLOW_HOST_EXECUTION=1` and runs unsandboxed — never for untrusted
  agents.
- **Audited tool attempts.** Harness tool attempts are recorded to a local
  SQLite audit table (hashes of arguments/output, never raw content) before
  execution and finalized afterward; a DB write failure blocks execution.
  Review at authenticated `/api/harness/audit`. Remote tool approvals are
  session-scoped and replay-protected (AS-027).
- **Worktrees are not sandboxes.** Harness Git worktrees fail closed when they
  cannot be created; they provide version-control isolation only.
- **`GET /healthz`** is a minimal unauthenticated liveness endpoint, not a
  readiness check for the LLM/provider stack.

Opt-in container boundary check (not part of default CI):

```bash
docker pull python:3.12-slim
STUDIO_EXECUTION_MODE=sandbox STUDIO_SANDBOX_IMAGE=python:3.12-slim \
  STUDIO_SANDBOX_INTEGRATION=1 pytest -q tests/test_live_sandbox.py
```

## 4. Local readiness checks, keyring, and database recovery

Disposable loopback smoke test for both launch paths (temporary DB + token;
checks health, auth, Origin rejection, DB isolation, and with Node the UI
token prompt/retry/session-reuse behavior):

```bash
python3 scripts/smoke_local.py
python3 scripts/smoke_local.py --standalone
```

Verify the real OS credential store (writes then deletes a disposable item;
run under the OS account that runs Studio):

```bash
python3 scripts/check_keyring.py
```

Back up before upgrades; stop Studio before restoring:

```bash
python3 scripts/db_maintenance.py backup  --database "${STUDIO_DB_PATH:-./studio_history.db}"
python3 scripts/db_maintenance.py restore --database "${STUDIO_DB_PATH:-./studio_history.db}" --input /path/to/backup.sqlite3 --force
```

The utility uses SQLite's backup API, validates integrity/schema, writes
owner-only backups, refuses plaintext-credential databases, and creates a
pre-restore safety copy. Cross-machine restore intentionally requires
re-entering credentials in that machine's keyring. Backups contain history —
store them accordingly.

### Exporting and pruning local history

History is exportable and prunable without leaving the machine:

```bash
python3 scripts/db_maintenance.py export     --database "${STUDIO_DB_PATH:-./studio_history.db}" --output /path/history.json
python3 scripts/db_maintenance.py retention  --database "${STUDIO_DB_PATH:-./studio_history.db}" --days 30
# Or over the authenticated API: GET /api/history/export, POST /api/history/retention/run
```

Exports are JSON bundles with best-effort secret redaction (replacement count
reported) and never include credential/configuration tables or the audit
trail. Retention deletes conversation/telemetry artifacts older than N days —
never OpenContext documents, the audit trail, or credentials. Details and
guarantees: [`docs/history-privacy.md`](docs/history-privacy.md).

## 5. System architecture

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Browser UI (`ui-ux-pro-max` OLED `#070b14`, SVG Icons, `⌘K` Palette, `Alt+1..5` Tabs) │
│  [1. Agent Harness] [2. Council Hall] [3. Auto 4-Stage] [4. Debate] [5. Direct Chat]   │
│  + Right Telemetry Rail: 24GB Unified RAM Bar • oMLX Residency • tok/s • OrbStack      │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │ HTTP / Real-Time SSE (`Cache-Control: no-store`)
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│  FastAPI Single-Process Core (`app.py` / standalone `studio.py`; runtime varies)        │
│                                                                                        │
│  ┌─────────────────────────────┐  ┌──────────────────────────┐  ┌────────────────────┐ │
│  │ Autonomous Harness Engine   │  │ Zero-RAM Context Suite   │  │ Universal Router   │ │
│  │ • Git Worktree Isolation    │  │ • Context-Mode FTS5 Vault│  │ • Local oMLX (:8000│ │
│  │ • 19 Built-In Agent Tools   │  │ • Caveman Smart Digesters│  │ • Cloud Providers  │ │
│  │ • Ponytail v5 Discipline    │  │ • AST + Graphify v8 Graph│  │ • OmniRoute Combos │ │
│  │ • GSD 3-Wave Fresh Context  │  │ • OpenContext + Wiki Lint│  │ • `:3000/v1` Proxy │ │
│  │ • `gsd-secret-read-guard`   │  │ • `<=2 KB` Compaction    │  │ • MCP Hub (HTTP +  │ │
│  └─────────────────────────────┘  └──────────────────────────┘  │   on-demand stdio) │ │
│                                                                 └────────────────────┘ │
└───────────────┬──────────────────────────────────┬─────────────────────────┬───────────┘
                │                                  │                         │
   ┌────────────▼────────────┐       ┌─────────────▼─────────────┐  ┌────────▼─────────┐
   │ `studio_history.db`     │       │ Local Apple Silicon Stack │  │ Cloud & MCP Mesh │
   │ SQLite WAL + FTS5 BM25  │       │ • `oMLX 0.7.2` (`:8000`)  │  │ • MCP 2026-07-28 │
   │ • Sessions & Chronicles │       │ • OrbStack Docker Engine  │  │   & 2025 HTTP    │
   │ • OpenContext (`oc://`) │       │ • Optional Colibri `Laya` │  │ • On-Demand Stdio│
   │ • Context Vault Chunks  │       │   System-One (`:8001`)    │  │ • DeepSeek/Claude│
   └─────────────────────────┘       └───────────────────────────┘  └──────────────────┘
```

## 6. Author-reported M5 / oMLX configuration notes (unverified)

The models, settings, and throughput figures below are **inherited project
notes, not independently reproduced benchmarks**. Results depend on exact
hardware, software, model files, and workload.

### Reported 4-Stage Council models & throughput (unverified)

| Stage / Role | Local Model (`oMLX 0.7.2`) | Quantization / Architecture | Reported Decode Speed (unverified) | Why Selected for 24GB M5 (author-reported) |
| :--- | :--- | :--- | :--- | :--- |
| **[1/4] Architect** | `Qwen3.8-27B-MLX-oQ4e-mtp` | Dense 27B (`oQ4e` + MTP speculative) | **`17.13 tok/s`** | Deep architectural planning & multi-file invariant reasoning |
| **[2/4] Fast Coder** | `Qwen3.6-35B-A3B-REAP-19B-4bit` | MoE REAP-pruned 19B (`3B` active, 4-bit) | **`42.17 tok/s`** | Fast code synthesis (~11 GB footprint; **keep `mtp_enabled: false`**) |
| **[3/4] Critic** | `Gemma-4-21B-REAP-4bit` | Non-Qwen architectural family (4-bit) | **`42.25 tok/s`** | True cross-family adversarial bug & edge-case review |
| **[4/4] Judge** | `Qwen3.8-27B-MLX-oQ4e-mtp` | Dense 27B (`oQ4e` + MTP speculative) | **`29.48 tok/s`** | Final synthesis & conflict resolution |

### Reported macOS & `~/.omlx` guardrails (prevent Metal OOM on 24 GB)

1. **Metal wired limit (`21,504 MB`)**: `sudo sysctl -w iogpu.wired_limit_mb=21504`
   (persisted via `/Library/LaunchDaemons/local.omlx.wiredlimit.plist`).
2. **`oMLX 0.7.2` global settings**: `memory_prefill_memory_guard: true`;
   `memory_guard_tier: "custom"`; `memory_guard_custom_ceiling_gb: 20.8`
   (`19.0 GB` is too low for Stage 4 long-context prefill — see `omlx#4519`);
   `chunked_prefill: true` & `prefill_priority: "context"`;
   `hot_cache_max_size: "0"` & `ssd_cache_max_size: "30GB"` (paged SSD KV cache);
   `idle_timeout_seconds: 120`.
3. **Sequential model unload** between local stage swaps: Studio calls
   `POST /v1/models/{model}/unload` + `1.0s` Metal buffer settle when
   `is_local_omlx_model(model)` is true.

## 7. Reference: UI, tools, database, configuration, API

### A. UI modes & global shortcuts

- **`⌘K` / `Ctrl+K`** — command palette (jump to any mode, Graphify shortest
  path, Ponytail debt scan, KV compaction, Git diff inspection, oMLX unload).
- **`Alt+1..5`** — Agent Harness • Council Hall • Auto 4-Stage • Debate • Direct Chat. **`Escape`** closes modals.

| Mode | How multiple LLMs collaborate | Best used for |
| :--- | :--- | :--- |
| **`Council Hall` (`Alt+2`)** | Sequential shared-transcript deliberation + human steering: Architect (thesis) → Builder/Deep Analyst → adversarial Red-Team Critic (non-Qwen family) → Lead Arbiter; interject between rounds as `Human Operative`. | Complex system design, macro/strategy analysis, adversarial stress-testing |
| **`Auto 4-Stage` (`Alt+3`)** | One-click automated 4-seat relay with auto-unload between local stages. | Hands-off spec → build → critique → synthesis |
| **`Debate` (`Alt+4`)** | All checked LLMs answer independently (zero anchoring); Judge compares blind positions side-by-side. | Eliminating anchoring bias; comparing raw model opinions |

### B. Built-in autonomous harness tools (+ dynamic MCP)

Agents in **Agent Harness** (`POST /api/harness/run`) and the **GSD 3-wave
loop** (`POST /api/harness/gsd-wave`) emit `<tool_call>` blocks:

| # | Tool | Purpose |
| :--- | :--- | :--- |
| 1 | `list_files` | List directory entries respecting ignore filters |
| 2 | `grep_search` | Regex text search guarded by `gsd-secret-read-guard` |
| 3 | `read_file` | Numbered line-range reader (`gsd-secret-read-guard` enforced) |
| 4 | `write_file` | Create/overwrite files inside the session workspace/worktree |
| 5 | `edit_file` | Exact snippet replacement (`old_text` → `new_text`) |
| 6 | `run_bash` | Shell/test commands inside `worktree_dir` with timeout |
| 7 | `delegate_subagent` | Independent Critic model in a fresh context window |
| 8 | `codegraph_architecture` | Hub files, God Nodes, symbol counts, circular imports |
| 9 | `codegraph_context` | AST symbol lookup: `file:line`, signature, callers/callees |
| 10 | `codegraph_impact` | Direct + 2-hop transitive caller blast radius |
| 11 | `codegraph_path` | BFS shortest call path with `[EXTRACTED]`/`[INFERRED]` tags |
| 12 | `codegraph_quality` | Cyclomatic complexity ranking & refactor hotspots |
| 13 | `ctx_run_block` | Zero-retyping blocks: `test-summary`, `first-error`, `json-peek`, `csv-peek`, `shortcut-debt` |
| 14 | `ctx_execute_python` | Isolated Python snippet returning compact stdout |
| 15 | `ctx_search` | SQLite FTS5 BM25 search inside compressed tool outputs (`ctx-<id>`) |
| 16 | `oc_search` | BM25 search over persistent cross-session docs |
| 17 | `oc_read` | Read a Markdown document by ID or `oc://doc/<id>` URI |
| 18 | `oc_save_doc` | Save verified architecture rules, decisions, postmortems |
| 19 | `oc_lint` | Deterministic check for broken `oc://doc/<id>` links, orphans, stale notes |
| 20+ | `mcp__<alias>__<tool>` | Any enabled tool from connected MCP servers |

### C. Tools exposed by `studio.py --mcp`

`start_council`, `get_current_session_data`, `send_response`,
`summon_omlx_agent`, `close_council`, `create_harness_worktree`,
`get_harness_diff`, `oc_search`, `oc_save_doc`, `oc_lint`, `codegraph_query`
(`mode` ∈ `architecture | path | context | impact | quality | debt`).

### D. SQLite WAL + FTS5 schema (`studio_history.db`)

- **`runs`**, **`perf_samples`** — chat/council/debate history and per-model tok/s telemetry.
- **`hall_sessions`**, **`hall_messages`** — persistent Council Hall sessions, transcripts, pinned conclusions.
- **`cloud_providers`**, **`model_combos`** — provider configs (keyring references only) and ordered failover chains.
- **`harness_sessions`** — harness runs, `worktree_dir`, event JSONL log, approval policy, compaction state.
- **`mcp_servers`** — MCP configs, cached tool schemas, per-tool enablement.
- **`opencontext_docs`** + **`opencontext_fts`** — persistent Markdown vault (`oc://doc/<id>`, folder, pinned flag).
- **`ctx_vault_chunks`** + **`ctx_vault_fts`** + **`ctx_session_metrics`** — sandboxed large tool outputs and KV bytes/tokens saved per session.

### E. Environment variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `OMLX_BASE_URL` | `http://127.0.0.1:8000` | Base URL for the local `oMLX` server |
| `STUDIO_HOST` | `127.0.0.1` | Bind host (`0.0.0.0` for container/LAN previews) |
| `STUDIO_PORT` | `3000` | HTTP + `/v1` OpenAI gateway port |
| `STUDIO_WORKSPACE` | Current working directory | Default root for Harness, CodeGraph & file attachments |
| `STUDIO_EXTERNAL_STATIC` | unset (`0`) | Set to `1` in `studio.py` to live-reload `static/index.html` instead of `EMBEDDED_HTML` |
| `STUDIO_API_TOKEN` | auto-generated on loopback; required remote | High-entropy bearer token protecting `/api/*` and `/v1/*` |
| `STUDIO_API_TOKEN_FILE` | `~/.config/m5-agent-studio/api-token` | Private file path for the generated loopback token |
| `STUDIO_AUTH_DISABLED` | `0` | Disable bearer auth on loopback only; startup refuses this with non-loopback binds |
| `STUDIO_ENABLE_API_DOCS` | `0` | Expose public FastAPI `/docs` and `/openapi.json`; keep off on exposed deployments |
| `STUDIO_MAX_REQUEST_BYTES` | `8388608` | Maximum request body size (1 KiB – 100 MiB) |
| `STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE` | `30` | Per-client-IP sliding-window limit on chat, harness, GSD/SDLC, `/v1/chat/completions` (1–600) |
| `STUDIO_HISTORY_RETENTION_DAYS` | `0` (disabled) | Delete conversation/telemetry/context-vault history older than N days (1–3650) at startup, via `POST /api/history/retention/run`, or `scripts/db_maintenance.py retention`. Never touches OpenContext docs, the audit trail, or credentials. See [docs/history-privacy.md](docs/history-privacy.md) |
| `STUDIO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated `Host` header allowlist; add LAN/proxy hostnames explicitly |
| `STUDIO_ALLOWED_ORIGINS` | empty | Comma-separated browser origins trusted behind a TLS reverse proxy |
| `STUDIO_PRIVATE_URL_HOSTS` | empty | Per-host exceptions for trusted private HTTP/HTTPS provider or MCP destinations |
| `STUDIO_ALLOW_PRIVATE_URLS` | `0` | Broad compatibility opt-in for private/HTTP destinations (less safe than per-host allowlist) |
| `STUDIO_EXECUTION_MODE` | `disabled` | `disabled`, `sandbox` (Docker/Podman; no network), or `host` (requires `STUDIO_ALLOW_HOST_EXECUTION=1`) |
| `STUDIO_SANDBOX_RUNTIME` | `docker` | Container CLI executable, typically `docker` or `podman` |
| `STUDIO_SANDBOX_IMAGE` | `python:3.12-slim` | Trusted image that must already exist locally; pin by digest for controlled deployments |
| `STUDIO_ALLOW_HOST_EXECUTION` | `0` | Required opt-in for `STUDIO_EXECUTION_MODE=host`; runs unsandboxed as the Studio user |
| `STUDIO_ALLOW_MCP_STDIO` | `0` | Opt in to configured local MCP command execution as the Studio user |
| `STUDIO_ALLOW_SKILL_CODE` | `0` | Opt in to executing a selected skill's `scripts/search.py`; skill Markdown stays untrusted input |

### F. Key HTTP & SSE endpoints

- **Telemetry & local oMLX control:** `GET /api/telemetry`, `POST /api/models/{model_id}/load`, `POST /api/models/{model_id}/unload`, `POST /api/models/unload-all`, `POST /api/cache/clear-ssd`.
- **Streaming workflows (SSE):** `POST /api/chat/stream`, `POST /api/council/stream`, `POST /api/debate/stream`.
- **Council Hall:** `GET|POST /api/hall/sessions`, `GET /api/hall/sessions/{id}`, `POST /api/hall/sessions/{id}/message`, `POST /api/hall/sessions/{id}/summon` (SSE), `POST /api/hall/sessions/{id}/close`.
- **Harness & GSD/SDLC:** `GET|POST /api/harness/sessions`, `GET|PATCH|DELETE /api/harness/sessions/{id}`, `GET .../diff`, `POST .../apply`, `POST .../compact`, `POST /api/harness/run` (SSE), `POST /api/harness/gsd-wave` (SSE), `POST /api/harness/sdlc/run`, `POST /api/harness/sdlc/feedback`, `POST /api/harness/approve` (session-scoped, replay-protected), `POST /api/harness/abort/{id}`, `GET /api/harness/audit`.
- **Context suite:** `GET|POST /api/opencontext/docs`, `PATCH|DELETE /api/opencontext/docs/{id}`, `GET /api/opencontext/search`, `GET /api/opencontext/lint`, `GET /api/codegraph/analyze`, `POST /api/codegraph/query`.
- **Router, MCP hub & skills:** `GET|POST /api/providers`, `DELETE /api/providers/{alias}`, `POST /api/combos`, `GET /v1/models`, `POST /v1/chat/completions`, `GET|POST /api/mcp/servers`, `POST /api/mcp/import-json`, `POST /api/mcp/servers/{alias}/sync`, `POST /api/mcp/servers/{alias}/call`, `GET /api/skills`, `POST /api/skills/install`.

The complete machine-generated route inventory lives at
[`docs/route-security-matrix.md`](docs/route-security-matrix.md).

## 8. Critical invariants & hard-won gotchas

Any human engineer or LLM agent extending this codebase **must preserve these
invariants**:

1. **Never call oMLX `POST /admin/api/server/restart`**: under the macOS DMG
   app supervisor it orphans child workers (`omlx#2701`). Always unload via
   `POST /v1/models/{id}/unload`, or restart oMLX cleanly via
   `killall oMLX && open -a oMLX`.
2. **Never enable MTP on `Qwen3.6-35B-REAP-19B`**: `mtp_enabled: true` triggers
   `HTTP 409`. Keep MTP strictly on `Qwen3.8-27B-MLX-oQ4e-mtp`.
3. **`Qwen3.8-27B` `reasoning_effort` strict enum**: never pass `"high"` —
   only `'low'`, `'medium'`, `'xhigh'` validate; always cap `thinking_budget`
   so thinking tokens cannot starve output on 24 GB KV budgets.
4. **Anthropic extended thinking**: with `thinking: {"type": "enabled"}`,
   Anthropic requires `temperature: 1.0` and `budget_tokens >= 1024`.
5. **Guard model unloads with `is_local_omlx_model(model)`**: cloud models and
   failover combos must never hit local unload endpoints.
6. **MCP Streamable HTTP `Accept` header**: always send
   `"Accept": "application/json, text/event-stream"` on MCP `POST`s —
   `2026-07-28` servers reject JSON-only headers with `HTTP 406`.
7. **Harness tool-turn counting**: match
   `m.get("role") == "user" and '<tool_response name="' in content` — never
   bare `<tool_response>` across system messages (the system prompt itself
   contains an example).
8. **Async skill context unpacking**: `build_skills_context(...)` is `async`
   and returns `(skill_block, skill_summaries)` — always await and unpack both.
9. **Node `< 22` skill-installer fallback**: `npx skills@latest` crashes on
   Node `< 22`; preserve the pure-Python `git clone --depth 1` fallback in
   `/api/skills/install`.
10. **Preserve `gsd-secret-read-guard` & UI accessibility rules**: block
    `.env`, `.secrets`, `*.pem`, `*.key` in `read_file`/`grep_search` (allow
    `.env.example`), keep structural emojis out of `static/index.html` (use
    `UI_ICONS` inline SVGs), and use `showToast(...)` instead of `alert(...)`.
11. **Dual tool-call dialect support**: Qwen XML
    (`<tool_call><function=NAME>...`) and JSON (`{"name": ..., "arguments": ...}`)
    dialects — plus tool aliases (`glob_files`, `grep`, `read`, `bash`) — are
    parsed by `extract_tool_calls()` + `_normalize_tool_call()` in both the
    Harness and Direct Chat.

## 9. Project history & research notes

Historical milestones and intended inspirations — **not** an audit trail.
Numeric claims are unverified unless a reproducible test is linked. Current
limitations: [`SECURITY.md`](SECURITY.md), [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

| Version | Milestone | Notes (not independently verified) |
| :--- | :--- | :--- |
| **v1.0–v3.0** | Local M5 AI Studio, skills & Council Hall | FastAPI + SSE UI for Direct Chat, Auto 4-Stage Council, Debate, 24 GB/oMLX/OrbStack telemetry, zero-Node Git skill installer, persistent Council Hall chronicles, `--mcp` stdio bridge. |
| **v4.0** | Hybrid cloud router & OmniRoute gateway | OS-keychain-backed provider credentials (SQLite stores references, not secret values), provider presets, ordered `combo/<alias>` failover, `/v1/chat/completions` gateway. |
| **v5.0** | Autonomous coding harness | Separate Git worktrees, `plan`/`standard`/`autonomous` approval policies, Critic sub-agent delegation, unified diff viewer, 1-click worktree apply. |
| **v6.0** | Universal cloud + custom MCP hub | JSON-RPC 2.0 MCP client negotiating `2026-07-28` Stateless Streamable HTTP with fallback to `2025` Stateful Session HTTP, on-demand `stdio` spawning, `mcp__<alias>__<tool>` namespacing. |
| **v7.0** | Context engineering & zero-RAM CodeGraph | Integrated `context-mode` (FTS5 tool-output vault + compaction), `OpenContext` (`oc://doc/<id>` vault), pure-Python AST CodeGraph, `gsd-core` 3-wave fresh-context loop with `gsd-secret-read-guard`. |
| **v8.0–v8.2** | `ui-ux-pro-max` overhaul & multi-LLM council | `⌘K` palette, WCAG-AAA tokens, `ponytail` v5, `graphify` v8, `caveman` digesters, `llmwiki` `oc_lint`, Colibri preset, Qwen XML tool parser, 4-seat multi-LLM Council defaults. |
| **v9.0** | Autonomous agile SDLC product factory | `Code = SOP(Team)` artifacts (`RESEARCH.md` → `PRD.md` → `ARCHITECTURE.md`), CrewAI-style flows with guardrail retries, backlog default-FAIL contract, closed-loop feedback → research/inspiration team. |
| **v9.0.1** | Hardening & open-source readiness | Pinned baseline + reproducible CI, threat model, governance docs, typed contracts, hardware/model/provider contracts, workflow state/checkpoints/idempotency, scoped remote approvals — tracked as stories AS-001…AS-063 (see [roadmap](product/Adaptive_Runtime_Product_Roadmap_v2.json)). |

## 10. Development, contributing, and AI agents

### Repository layout

```text
m5-agent-studio/
├── app.py                     # Canonical FastAPI backend source
├── workflow_*.py, mcp_client.py, context_store.py, …  # Extracted typed boundary modules
├── static/index.html          # Canonical single-file UI source
├── studio.py                  # GENERATED standalone PEP 723 executable — do not hand-edit
├── product/                   # Product roadmap JSON (source of truth for story status)
├── docs/                      # Generated security/route/inventory/status docs + ADRs
├── scripts/                   # Build, generation, validation, smoke and maintenance scripts
├── tests/                     # pytest suite (unit, contract, adversarial; opt-in live sandbox)
├── design-system/             # Persisted design tokens and a11y rules
└── BASELINE.md, BASELINE_COMMIT  # Pinned source baseline and its verification contract
```

### Rebuilding the standalone artifact

`app.py` and `static/index.html` are canonical. After changing either:

```bash
python3 scripts/build_studio.py      # embeds the UI, preserves PEP 723 metadata, compiles both entry points
```

CI regenerates `studio.py`, `docs/route-security-matrix.md`,
`docs/subsystem-dependency-inventory.md`, and
`docs/product-roadmap-status.md`, then fails on any byte-level drift.

### Verification (same sequence as CI)

```bash
python -m pip install -e '.[dev]'
python scripts/verify_baseline.py
ruff check .
mypy
git ls-files -z -- '*.py' | xargs -0 -r python -m compileall -q
python scripts/check_frontend_syntax.py
python -m pytest -q
python scripts/smoke_local.py && python scripts/smoke_local.py --standalone
python scripts/build_studio.py && python scripts/generate_route_matrix.py \
  && python scripts/generate_subsystem_inventory.py && python scripts/generate_roadmap_status.py
git diff --exit-code
```

### Working on the project

- **AI coding agents:** start with [`AGENTS.md`](AGENTS.md) — it links the
  work plan, conventions, verification steps, and guardrails needed to pick up
  a story with no other context.
- **Humans:** read [`CONTRIBUTING.md`](CONTRIBUTING.md) (setup, principles,
  commit-message policy, pull-request checklist) and the
  [Code of Conduct](CODE_OF_CONDUCT.md). Security issues: see
  [`SECURITY.md`](SECURITY.md) — do not open public issues for them.
- **Work organization:** one story per branch (`as-XXX-short-slug`); every
  commit documented (subject + explanatory body, enforced by
  `scripts/check_commit_messages.py`); pull requests rebase-merge after green
  CI; merged branches are deleted automatically.
- **Project status:** [`docs/product-roadmap-status.md`](docs/product-roadmap-status.md)
  (generated from the [roadmap JSON](product/Adaptive_Runtime_Product_Roadmap_v2.json)).

## License

[MIT](LICENSE)
