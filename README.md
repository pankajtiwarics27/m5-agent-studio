# M5 Agentic Studio (`v9.0.1`)

> **Zero-Bloat Hybrid AI Studio, Autonomous Coding Harness, Universal MCP Hub & Context-Engineering Engine** purpose-built for **Apple Silicon (M5 MacBook Air • 24GB Unified Memory • 1TB SSD)** running concurrently alongside **OrbStack Docker** and desktop apps.

---

## 1. Why We Built This (Core Design Philosophy)

Most multi-agent coding stacks assume either unlimited cloud token budgets or a 64–128 GB workstation—spawning Electron/Next.js frontends, vector databases, LSP daemons, and multiple resident LLMs that immediately trigger macOS swap thrashing or Metal OOM crashes on a **24GB Unified Memory** laptop.

**M5 Agentic Studio (`v9.0.1`) is a local-first hybrid AI workbench** with a FastAPI backend, vanilla HTML/CSS/JS UI, and a standalone generated script. It provides:

1. **Local/cloud model routing** via a common chat interface and OpenAI-compatible `/v1` routes, with optional sequential fallback chains.
2. **Agent workflows** including direct chat, multi-model Council/Debate, a tool-using coding harness, GSD planning, and a backlog-based SDLC flow.
3. **Context features** built on SQLite/FTS5, persistent OpenContext documents, output compaction, and heuristic CodeGraph analysis.
4. **MCP and skills integrations** with configurable HTTP/stdio servers and repository-provided skills.
5. **M5-oriented telemetry and model controls** for an oMLX/OrbStack setup. Telemetry is simulated when the local backend is unavailable; consult the explicit `demo_mode` and `telemetry_source` fields rather than treating demo values as measurements.

The project prioritizes a small dependency footprint. Performance and quality vary with hardware, model/provider, configuration, and workload; benchmark statements should be treated as environment-specific unless accompanied by reproducible test data.

> **Remediation status:** this working tree is not an enterprise-readiness or security-completion claim. The opt-in container runner still needs live-runtime/platform validation; the shared executor and workflow approvals need end-to-end review, and several roadmap items remain outstanding. See [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) before deployment.
---

## 2. Quick Start (Humans & External Coding Agents)

### A. Launch the Standalone Single-File Studio (Recommended on macOS)
Requires Python `3.11+` and [`uv`](https://docs.astral.sh/uv/) (dependencies `fastapi`, `uvicorn`, `httpx`, and `keyring` auto-resolve via inline PEP 723 script metadata):

```bash
chmod +x studio.py
uv run studio.py
# Opens Web UI:            http://127.0.0.1:3000 (browser prompts for local API token on first API call)
# Token file:              ~/.config/m5-agent-studio/api-token
# OpenAI Gateway (/v1):    http://127.0.0.1:3000/v1
```

### B. Launch in Development / Split-Source Mode
When actively editing `app.py` and `static/index.html`:

```bash
uv run app.py
# Or with plain Python if fastapi/uvicorn/httpx are already installed:
STUDIO_HOST=127.0.0.1 STUDIO_PORT=3000 python3 app.py
```

By default, SQLite history is stored beside the source/standalone script at `studio_history.db`; set `STUDIO_DB_PATH` to an explicit user-writable location if the project directory is read-only. The local token path is independently configurable with `STUDIO_API_TOKEN_FILE`.

### C. Expose Studio as an MCP `stdio` Server (Claude Code, Codex, Cursor, `dsh`, `hn`)
Any external MCP-capable agent can summon your local M5 Council, query CodeGraph shortest paths, or read/write OpenContext memory:

```json
{
  "mcpServers": {
    "m5-studio": {
      "command": "uv",
      "args": ["run", "/absolute/path/to/m5-agent-studio/studio.py", "--mcp", "--agent-name", "ClaudeCode"]
    }
  }
}
```

This configuration asks the external client to launch Studio's stdio server, which is disabled by default. Set `STUDIO_ALLOW_MCP_STDIO=1` in that process environment only if you trust the client and accept that it runs with the Studio user's OS privileges; it is not sandboxed.

### D. Point Any OpenAI-Compatible CLI (`dsh`, `aider`, Cursor, Continue) at Studio's `:3000/v1` Gateway
Studio exposes a unified `/v1/models` and `/v1/chat/completions` router across local `oMLX`, configured Cloud providers (`provider::model`), and failover `combo/<alias>` routes:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:3000/v1"
# Default loopback auth persists a random token here; if configured via env, use that exact value instead.
export OPENAI_API_KEY="${STUDIO_API_TOKEN:-$(cat ~/.config/m5-agent-studio/api-token)}"
```

### E. (Optional) Launch External `OmniRoute` Upstream Gateway (`:20128`) & Pair with Studio
Studio already includes a native `:3000/v1` router, plus a 1-click **`omniroute`** provider preset (`http://127.0.0.1:20128/v1`). If you want to run the upstream [`diegosouzapw/OmniRoute`](https://github.com/diegosouzapw/OmniRoute) server (375+ providers, quota-aware OAuth/free-tier fallback):

```bash
# Option 1: OrbStack Docker on M5 Air (recommended — multi-arch ARM64, isolated from host Node version)
docker run -d \
  --name omniroute \
  --restart unless-stopped \
  --stop-timeout 40 \
  -p 127.0.0.1:20128:20128 \
  -v omniroute-data:/app/data \
  -e INITIAL_PASSWORD="change-me-now" \
  diegosouzapw/omniroute:latest

# Option 2: Via npm / npx (requires Node >=22.22.2 <23 || >=24.0.0)
npx -y omniroute@latest --no-open
# Dashboard: http://127.0.0.1:20128  |  OpenAI API: http://127.0.0.1:20128/v1  |  MCP: omniroute --mcp
```

---

## Security and Safe Defaults (v9.0.1)

- The server binds to loopback by default. Unless explicitly disabled on loopback, the first startup creates a random local bearer token at `~/.config/m5-agent-studio/api-token` with restrictive file/directory permissions. `/api/*` and `/v1/*` require it; the browser prompts for it and stores it in session storage. Set `STUDIO_API_TOKEN` explicitly for managed deployments, or configure `STUDIO_API_TOKEN_FILE` for the generated local token path. Non-loopback binds require an explicit high-entropy `STUDIO_API_TOKEN` (at least 32 characters). Set `STUDIO_ALLOWED_HOSTS` and, behind a TLS reverse proxy, `STUDIO_ALLOWED_ORIGINS` explicitly. Expensive workflow endpoints have a per-client-IP sliding-window limit, and request bodies are capped by `STUDIO_MAX_REQUEST_BYTES`.
- Provider API keys and MCP tokens/credential-like headers and environment variables are stored in the OS credential store. Legacy plaintext values are migrated during startup; startup fails rather than silently keeping credentials in SQLite if the credential store cannot accept the migration. Conversation/history databases are still local files and may contain sensitive prompts, code, and tool output; protect and retain them accordingly.
- Private-network or HTTP provider/MCP URLs are rejected by default. Prefer `STUDIO_PRIVATE_URL_HOSTS` for explicit per-host exceptions (comma-separated hostnames); `STUDIO_ALLOW_PRIVATE_URLS=1` remains a broader compatibility opt-in and should not be used on public/shared deployments. Requests are resolved/validated immediately before I/O, the transport connects to the exact validated IP while preserving the original HTTP Host/TLS SNI, and redirects are not followed. The transport bypasses ambient proxy variables so requests cannot evade pinning via an implicit proxy; proxy-required environments need an explicit reviewed proxy integration.
- Skills install from GitHub over HTTPS using a copy-only path; downloaded `scripts/search.py` is not executed unless `STUDIO_ALLOW_SKILL_CODE=1`. Skill text itself is untrusted prompt input.
- Code execution is disabled by default. Set `STUDIO_EXECUTION_MODE=sandbox` to run shell/Python/test commands in a Docker/Podman container (`--network=none`, read-only root, dropped capabilities, no-new-privileges, resource limits, and only the session workspace mounted). Configure and pre-pull a trusted `STUDIO_SANDBOX_IMAGE`; no image is pulled automatically. The host workspace mount is writable by design. A preflight refuses the filesystem root/home directory, known secret-bearing filenames, credentialed Git remote URLs, unreadable trees, and workspaces over its 100,000-entry scan budget; this is filename-based defense-in-depth, not a guarantee that arbitrary secrets embedded in source/config files are detected. `STUDIO_EXECUTION_MODE=host` is allowed only with `STUDIO_ALLOW_HOST_EXECUTION=1` and runs with the Studio user's privileges; do not use it for untrusted agents. MCP stdio remains off by default; `STUDIO_ALLOW_MCP_STDIO=1` runs configured commands with host privileges and is not sandboxed.
- Harness tool attempts are recorded in a local SQLite audit table before execution and finalized afterward. The log records tool/session/policy, explicit-approval flag, success, timing, and SHA-256 digests of arguments/output—not raw arguments or output. Review it at authenticated `/api/harness/audit`; a DB write failure blocks tool execution. This is a local integrity aid, not tamper-proof or a substitute for backups.
- Harness worktree creation now fails closed if a requested Git worktree cannot be created. A worktree is version-control isolation, not an OS sandbox.
- `GET /healthz` is a minimal unauthenticated liveness endpoint. Do not treat it as a readiness check for the LLM/provider stack.

To enable the optional no-network code runner, install Docker or Podman, pre-pull a trusted image, then launch Studio with `STUDIO_EXECUTION_MODE=sandbox` (for example, `docker pull python:3.12-slim` followed by `STUDIO_EXECUTION_MODE=sandbox uv run studio.py`). This base image has Python and a shell; add project-specific tools/dependencies in your own trusted image. The container image is never downloaded by Studio.

To exercise the real container boundary locally (not run in default CI):

```bash
docker pull python:3.12-slim
STUDIO_EXECUTION_MODE=sandbox STUDIO_SANDBOX_IMAGE=python:3.12-slim STUDIO_SANDBOX_INTEGRATION=1 pytest -q tests/test_live_sandbox.py
```

This opt-in test verifies that the mounted workspace is writable, the container root is read-only, and a public network connection fails. It is not a container escape test or security certification.

### Local readiness checks, keyring, and database recovery

After installing dependencies, run the disposable local server smoke test for both launch paths:

```bash
python3 scripts/smoke_local.py
python3 scripts/smoke_local.py --standalone
```

These launch a loopback-only instance with a temporary database and token, then check health, auth, Origin rejection, DB isolation, and cleanup. When Node is installed, they also simulate the UI token prompt/retry/session reuse and verify the bearer token is never sent cross-origin; this is not a full browser test. To verify the actual OS credential store (this writes and then deletes a random disposable item): `python3 scripts/check_keyring.py`. Run it under the same OS account that will run Studio; keyring behavior is platform/session dependent.

Back up local history before upgrades, and stop Studio before restoring:

```bash
python3 scripts/db_maintenance.py backup --database "${STUDIO_DB_PATH:-./studio_history.db}"
python3 scripts/db_maintenance.py restore --database "${STUDIO_DB_PATH:-./studio_history.db}" --input /path/to/backup.sqlite3 --force
```

The utility uses SQLite's backup API, validates integrity/schema, writes owner-only backup files, refuses plaintext credentials, and creates a pre-restore safety backup. Restoring on a different machine does not migrate OS-keyring entries: re-enter credentials there. Backups still contain conversation, code, and tool-output history; store them accordingly.

## 3. System Architecture

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

---

## 4. Author-Reported M5 MacBook Air (24GB RAM) Local LLM Stack & `oMLX 0.7.2` Configuration

The following model names, settings, and throughput figures are inherited project notes and have **not** been independently reproduced or validated in this remediation. Results depend on exact hardware, software, model files, and workload; do not treat them as verified benchmarks or operational guidance without testing on your system.

### Reported 4-Stage Council Models & Throughput (Unverified)

| Stage / Role | Local Model (`oMLX 0.7.2`) | Quantization / Architecture | Reported Decode Speed (unverified) | Why Selected for 24GB M5 (author-reported) |
| :--- | :--- | :--- | :--- | :--- |
| **[1/4] Architect** | `Qwen3.8-27B-MLX-oQ4e-mtp` | Dense 27B (`oQ4e` + MTP speculative) | **`17.13 tok/s`** | Deep architectural planning & multi-file invariant reasoning |
| **[2/4] Fast Coder** | `Qwen3.6-35B-A3B-REAP-19B-4bit` | MoE REAP-pruned 19B (`3B` active, 4-bit) | **`42.17 tok/s`** | Fast code synthesis (~11 GB footprint; **keep `mtp_enabled: false`**) |
| **[3/4] Critic** | `Gemma-4-21B-REAP-4bit` | Non-Qwen architectural family (4-bit) | **`42.25 tok/s`** | True cross-family adversarial bug & edge-case review |
| **[4/4] Judge** | `Qwen3.8-27B-MLX-oQ4e-mtp` | Dense 27B (`oQ4e` + MTP speculative) | **`29.48 tok/s`** | Final synthesis & conflict resolution |

### Required macOS & `~/.omlx` Guardrails (Prevents Metal OOM on 24GB Unified RAM)
1. **macOS Metal Wired Limit (`21,504 MB` / `21 GB`)**:
   ```bash
   sudo sysctl -w iogpu.wired_limit_mb=21504
   ```
   *(Persisted via `/Library/LaunchDaemons/local.omlx.wiredlimit.plist`.)*
2. **`oMLX 0.7.2` Global Settings**:
   - `memory_prefill_memory_guard: true`
   - `memory_guard_tier: "custom"`
   - `memory_guard_custom_ceiling_gb: 20.8` *(Note: `19.0 GB` is too low for Step 4 long-context prefill—see `omlx#4519`)*
   - `chunked_prefill: true` & `prefill_priority: "context"`
   - `hot_cache_max_size: "0"` & `ssd_cache_max_size: "30GB"` (paged SSD KV cache on the M5's fast NVMe)
   - `idle_timeout_seconds: 120`
3. **Sequential Model Unload**: Between every local stage swap (Council, Hall Full Round, GSD Wave), Studio calls `POST /v1/models/{model}/unload` + `1.0s` Metal buffer settle when `is_local_omlx_model(model)` is true.

---

## 5. Project History & Research Notes (`v1.0` → `v8.0`)

This historical table records project claims and intended inspirations; it is not an audit trail or independent verification. Numeric performance, compatibility, and quality claims below are unverified in this remediation unless a reproducible test is linked. The current implementation limitations are listed in [`SECURITY.md`](SECURITY.md) and [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

| Version | Milestone | Project notes (not independently verified) |
| :--- | :--- | :--- |
| **v1.0–v3.0** | **Local M5 AI Studio, Skills & Council Hall** | Built FastAPI + SSE UI for Direct Chat, Auto 4-Stage Council, Debate, M5 24GB RAM + `oMLX` + OrbStack telemetry, zero-Node Git skill installer, persistent Council Hall chronicles, and `--mcp` stdio bridge. |
| **v4.0** | **Hybrid Cloud Router & OmniRoute Gateway** | Added OS-keychain-backed provider credentials (SQLite stores references, not secret values), 1-click presets (`OpenRouter`, `Anthropic`, `Gemini`, `Groq`, `Cerebras`, `DeepSeek`, `OmniRoute`), ordered `combo/<alias>` failover chains, and `/v1/chat/completions` OpenAI gateway. |
| **v5.0** | **Autonomous Coding Harness** | Synthesized `deepseek-harness` (`dsh`) + `openharness` (`hn`): separate Git worktrees (`.harnesses/wt-<id>`; not OS sandboxes), `plan` / `standard` / `autonomous` approval policies, sub-agent Critic delegation, unified diff viewer, and 1-click worktree apply. |
| **v6.0** | **Universal Cloud + Custom MCP Hub** | Full JSON-RPC 2.0 MCP client auto-negotiating **`2026-07-28` Stateless Streamable HTTP** with fallback to **`2025` Stateful Session HTTP (`Mcp-Session-Id`)**, plus **on-demand `stdio` spawning (`0 MB` idle RAM)** and `mcp__<alias>__<tool>` namespacing. |
| **v7.0** | **Context Engineering & Zero-RAM CodeGraph** | Integrated 4 repos at `0 MB` extra daemon RAM:<br>• **`mksglu/context-mode`**: Auto-sandboxes tool outputs `> 3,200 chars` into SQLite `FTS5` + `ctx_search` + `ctx_execute_python` + `<= 2 KB` `<session_guide>` compaction.<br>• **`0xranx/OpenContext`**: Persistent `oc://doc/<id>` Markdown vault (`architecture`/`decisions`/`preferences`/`postmortems`) with pinned prompt injection.<br>• **`Jakedismo/codegraph-rust`**: Pure-Python AST + multi-lang graph (`architecture`, `context`, `impact`, `quality`) replacing heavy SurrealDB/LSP daemons.<br>• **`open-gsd/gsd-core`**: 3-Wave Fresh-Context Spec Loop (`PLAN.md` → `0`-history Execute → `VERIFICATION.md`) + `gsd-secret-read-guard`. |
| **v8.0–v8.2** | **`ui-ux-pro-max` Overhaul, 6-Repo Efficiency Audit & Qwen XML / Multi-LLM Council** | Applied `ui-ux-pro-max` (`MASTER.md`, zero emoji structural icons, `⌘K` Command Palette, WCAG AAA `#a3b5d1`, non-blocking Toasts); integrated `ponytail` v5 (`-53%` LoC, `98%` risky-logic test rate), `graphify` v8 (`codegraph_path` + God Nodes), `caveman` Read-Less Smart Digesters (`-98.2%` JSON / `-89.2%` pytest) + `Blocks` + ASD-STE100, `llmwiki` `oc_lint`, `colibri` `:8001` preset, `deepseek-v4.1-flash` preset, `Qwen3.8` `<function=...>` XML tool parser in Direct Chat, and 4-Seat Multi-LLM Council Hall defaults. |
| **v9.0** | **Autonomous Agile SDLC Product Factory (`MetaGPT` + `CrewAI` + `Anthropic` Backlog + Feedback Loop)** | Synthesized `MetaGPT`'s `Code = SOP(Team)` artifacts (`RESEARCH.md` → `PRD.md` → `ARCHITECTURE.md`) + `CrewAI` `Flows` (`@router` + `Guardrail` retries) + Anthropic's Long-Running Harness `backlog.json` Default-FAIL contract + Closed-Loop User Feedback → Research & Live Online Inspiration Team (`POST /api/harness/sdlc/run`, `POST /api/harness/sdlc/feedback`) at `0 MB` extra daemon RAM! |

---

## 6. Repository Layout & Dual-File Build Pipeline

```text
m5-agent-studio/
├── README.md                                      # This human + LLM architecture & maintainer guide
├── app.py                                         # Primary modular FastAPI backend source (v8.0, ~6.3k lines)
├── static/
│   └── index.html                                 # Primary single-file OLED UI source (v8.0, ~3.0k lines)
├── studio.py                                      # Compiled standalone PEP 723 executable (app.py + EMBEDDED_HTML)
├── studio_history.db                              # SQLite WAL + FTS5 persistent store (chmod 0600, auto-created)
├── design-system/
│   └── m5-agentic-studio/
│       └── MASTER.md                              # Persisted `ui-ux-pro-max` design tokens, contrast & a11y rules
└── .agents/
    └── skills/
        └── ui-ux-pro-max/                         # Installed local agent skill (SKILL.md + BM25 search engine)
```

### Rebuilding the standalone artifact

`app.py` and `static/index.html` are canonical. Regenerate the portable standalone file after changing either:

```bash
python3 scripts/build_studio.py
python3 -m py_compile app.py studio.py
```

The build script embeds the current UI, preserves the PEP 723 dependency metadata, and verifies that both Python entry points compile. CI checks that a fresh build produces no diff in `studio.py`.
---

## 7. Complete Tool, Database & API Reference (Machine-Readable Context for LLMs)

### A. UI Modes & Global Keyboard Shortcuts
- **`⌘K` / `Ctrl+K`**: Open Global Command Palette (jump to any mode, run Graphify Shortest Path, scan Ponytail Debt, compact Harness KV context, inspect Git diff, or unload `oMLX` RAM).
- **`Alt+1`** → **Agent Harness** | **`Alt+2`** → **Council Hall** | **`Alt+3`** → **Auto 4-Stage** | **`Alt+4`** → **Debate** | **`Alt+5`** → **Direct Chat**.
- **`Escape`**: Close any open modal dialog.

#### How the Multi-LLM Modes Differ (`Council Hall` vs. `Auto 4-Stage` vs. `Debate`)
| Mode | How Multiple LLMs Collaborate | Best Used For |
| :--- | :--- | :--- |
| **`Council Hall` (`Alt+2`)** | **Sequential Shared-Transcript Deliberation + Human Steering**: Seat 1 (`Architect / Thesis`) proposes the framework → Seat 2 (`Builder / Deep Analyst`, distinct LLM) expands with code or empirical data → Seat 3 (`Adversarial Red-Team Critic`, non-Qwen LLM family e.g. `Gemma-4`) attacks blind spots, bugs, or stale assumptions → Seat 4 (`Lead Arbiter`) weighs all 3 voices and pins the verdict. You can interject between rounds as `Human Operative`. | Complex system design, macro/strategy analysis, adversarial stress-testing with human follow-ups |
| **`Auto 4-Stage` (`Alt+3`)** | **One-Click Automated 4-LLM Pipeline**: Runs the exact 4-seat sequential relay (`Architect -> Builder/Analyst -> Red-Team Critic -> Arbiter`) end-to-end from a single prompt, auto-unloading each local M5 model between stages. | Hands-off end-to-end spec → build/analysis → critique → final synthesis |
| **`Debate` (`Alt+4`)** | **Blind Independent Answers + Arbiter Comparison**: All checked LLMs (`Qwen3.8`, `Qwen3.6`, `Gemma-4`, Cloud APIs) answer the prompt **independently without seeing each other's responses** (zero anchoring / groupthink), then the Judge compares all blind positions side-by-side. | Eliminating anchoring bias, comparing raw model opinions & capabilities |

### B. All 19 Built-In Autonomous Harness Tools (+ Dynamic `mcp__<alias>__<tool>`)
Agents inside **Agent Harness** (`POST /api/harness/run`) and **GSD 3-Wave Spec Loop** (`POST /api/harness/gsd-wave`) emit `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`:

| # | Tool Name | Subsystem | Purpose |
| :--- | :--- | :--- | :--- |
| 1 | `list_files` | Workspace | List directory entries respecting ignore filters |
| 2 | `grep_search` | Workspace | Regex text search guarded by `gsd-secret-read-guard` |
| 3 | `read_file` | Workspace | Numbered line-range file reader (`gsd-secret-read-guard` enforced) |
| 4 | `write_file` | Workspace | Create/overwrite file inside session workspace / `git worktree` |
| 5 | `edit_file` | Workspace | Exact snippet replacement (`old_text` → `new_text`) |
| 6 | `run_bash` | Execution | Execute shell/test commands inside `worktree_dir` with timeout |
| 7 | `delegate_subagent` | Multi-Agent | Spawn independent Critic model in a fresh context window |
| 8 | `codegraph_architecture` | CodeGraph + Graphify | Rank hub files, **God Nodes**, symbol counts & circular imports |
| 9 | `codegraph_context` | CodeGraph + Graphify | AST symbol lookup returning `file:line`, signature, callers & callees |
| 10 | `codegraph_impact` | CodeGraph + Graphify | Direct + 2-hop transitive caller blast-radius before refactors |
| 11 | `codegraph_path` | Graphify v8 | BFS shortest call path `SymA -> SymB` with `[EXTRACTED]`/`[INFERRED]` tags |
| 12 | `codegraph_quality` | CodeGraph | Cyclomatic complexity ranking & refactoring hotspots |
| 13 | `ctx_run_block` | Caveman Blocks | Zero-retyping blocks: `test-summary`, `first-error`, `json-peek`, `csv-peek`, `shortcut-debt` |
| 14 | `ctx_execute_python` | Context-Mode | "Think in Code" isolated Python snippet returning compact stdout |
| 15 | `ctx_search` | Context-Mode | SQLite FTS5 BM25 search inside compressed tool outputs (`ctx-<id>`) |
| 16 | `oc_search` | OpenContext | BM25 search over persistent cross-session architecture/decision docs |
| 17 | `oc_read` | OpenContext | Read full Markdown document by ID or `oc://doc/<id>` URI |
| 18 | `oc_save_doc` | OpenContext | Save verified architecture rules, decisions, or postmortems |
| 19 | `oc_lint` | LLM-Wiki Hygiene | Deterministic check for broken `oc://doc/<id>` links, orphans & stale notes |
| `20+` | `mcp__<alias>__<tool>` | MCP Hub | Any enabled tool from connected Cloud HTTP or local `stdio` MCP servers |

### C. All 11 Tools Exposed by `uv run studio.py --mcp`
`start_council`, `get_current_session_data`, `send_response`, `summon_omlx_agent`, `close_council`, `create_harness_worktree`, `get_harness_diff`, `oc_search`, `oc_save_doc`, `oc_lint`, and `codegraph_query` (`mode` ∈ `architecture | path | context | impact | quality | debt`).

### D. SQLite WAL + FTS5 Schema (`studio_history.db`)
- **`runs`** & **`perf_samples`**: Chat/Council/Debate history & per-model `tok/s` telemetry leaderboard.
- **`hall_sessions`** & **`hall_messages`**: Persistent multi-turn Council Hall sessions, participant transcripts & pinned conclusions.
- **`cloud_providers`** & **`model_combos`**: Cloud API configurations (`chmod 0600` DB) and ordered OmniRoute fallback chains (`combo/<alias>`).
- **`harness_sessions`**: Agentic Harness runs, `worktree_dir`, event JSONL log, approval policy & compaction state.
- **`mcp_servers`**: Registered Cloud Streamable HTTP / SSE and local `stdio` MCP server configs, cached tool schemas & per-tool enablement.
- **`opencontext_docs`** + **`opencontext_fts` (FTS5)**: Persistent Markdown knowledge vault (`oc://doc/<id>`, folder, pinned flag).
- **`ctx_vault_chunks`** + **`ctx_vault_fts` (FTS5)** + **`ctx_session_metrics`**: Sandboxed large tool outputs (`ctx-<id>`) and cumulative KV bytes/tokens saved per Harness session.

### E. Environment Variables
| Variable | Default | Description |
| :--- | :--- | :--- |
| `OMLX_BASE_URL` | `http://127.0.0.1:8000` | Base URL for local `oMLX` server |
| `STUDIO_HOST` | `127.0.0.1` | Bind host (`0.0.0.0` for container/LAN previews) |
| `STUDIO_PORT` | `3000` | HTTP + `/v1` OpenAI Gateway port |
| `STUDIO_WORKSPACE` | Current working directory | Default root directory for Harness, CodeGraph & file attachments |
| `STUDIO_EXTERNAL_STATIC` | unset (`0`) | Set to `1` in `studio.py` to live-reload `static/index.html` instead of `EMBEDDED_HTML` |
| `STUDIO_API_TOKEN` | auto-generated on loopback; required remote | High-entropy bearer token; protects `/api/*` and `/v1/*` |
| `STUDIO_API_TOKEN_FILE` | `~/.config/m5-agent-studio/api-token` | Private file path for the generated loopback token |
| `STUDIO_AUTH_DISABLED` | `0` | Explicitly disable bearer auth on loopback only; startup refuses this with non-loopback binds |
| `STUDIO_ENABLE_API_DOCS` | `0` | Expose public FastAPI `/docs` and `/openapi.json` routes; keep off on exposed deployments |
| `STUDIO_MAX_REQUEST_BYTES` | `8388608` | Maximum request body size; valid range is 1 KiB through 100 MiB |
| `STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE` | `30` | Per-client-IP sliding-window limit on chat, harness, GSD/SDLC, and `/v1/chat/completions`; range 1–600 |
| `STUDIO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated Host header allowlist; add LAN/proxy hostnames explicitly |
| `STUDIO_ALLOWED_ORIGINS` | empty | Comma-separated browser origins trusted behind a TLS reverse proxy |
| `STUDIO_PRIVATE_URL_HOSTS` | empty | Comma-separated per-host exceptions for trusted private HTTP/HTTPS provider or MCP destinations |
| `STUDIO_ALLOW_PRIVATE_URLS` | `0` | Broad compatibility opt-in for private/HTTP destinations; less safe than the per-host allowlist |
| `STUDIO_EXECUTION_MODE` | `disabled` | `disabled`, `sandbox` (Docker/Podman; no network), or `host` (requires `STUDIO_ALLOW_HOST_EXECUTION=1`) |
| `STUDIO_SANDBOX_RUNTIME` | `docker` | Container CLI executable, typically `docker` or `podman` |
| `STUDIO_SANDBOX_IMAGE` | `python:3.12-slim` | Trusted image that must already exist locally; configure required tools/dependencies in a custom image and pin by digest for controlled deployments |
| `STUDIO_ALLOW_HOST_EXECUTION` | `0` | Required opt-in for `STUDIO_EXECUTION_MODE=host`; runs unsandboxed as Studio user |
| `STUDIO_ALLOW_MCP_STDIO` | `0` | Opt in to configured local MCP command execution as the Studio user |
| `STUDIO_ALLOW_SKILL_CODE` | `0` | Opt in to executing a selected skill's `scripts/search.py`; skill Markdown is still treated as untrusted prompt input |

### F. Key HTTP & SSE API Endpoints
- **Telemetry & Local `oMLX` Control**: `GET /api/telemetry`, `POST /api/models/{model_id}/load`, `POST /api/models/{model_id}/unload`, `POST /api/models/unload-all`, `POST /api/cache/clear-ssd`.
- **Streaming Workflows (SSE)**: `POST /api/chat/stream`, `POST /api/council/stream`, `POST /api/debate/stream`.
- **Council Hall**: `GET|POST /api/hall/sessions`, `GET /api/hall/sessions/{id}`, `POST /api/hall/sessions/{id}/message`, `POST /api/hall/sessions/{id}/summon` (SSE), `POST /api/hall/sessions/{id}/close`.
- **Agentic Harness & GSD Spec Loop**: `GET|POST /api/harness/sessions`, `GET|PATCH|DELETE /api/harness/sessions/{id}`, `GET /api/harness/sessions/{id}/diff`, `POST /api/harness/sessions/{id}/apply`, `POST /api/harness/sessions/{id}/compact`, `POST /api/harness/run` (SSE), `POST /api/harness/gsd-wave` (SSE), `POST /api/harness/approve`, `POST /api/harness/abort/{id}`.
- **Context Suite (OpenContext + CodeGraph)**: `GET|POST /api/opencontext/docs`, `PATCH|DELETE /api/opencontext/docs/{id}`, `GET /api/opencontext/search`, `GET /api/opencontext/lint`, `GET /api/codegraph/analyze`, `POST /api/codegraph/query`.
- **Cloud Router, MCP Hub & Skills**: `GET|POST /api/providers`, `DELETE /api/providers/{alias}`, `POST /api/combos`, `GET /v1/models`, `POST /v1/chat/completions`, `GET|POST /api/mcp/servers`, `POST /api/mcp/import-json`, `POST /api/mcp/servers/{alias}/sync`, `POST /api/mcp/servers/{alias}/call`, `GET /api/skills`, `POST /api/skills/install`.

---

## 8. Critical Invariants & Hard-Won Gotchas (Read Before Modifying!)

Any human engineer or LLM agent extending this codebase **must preserve these verified invariants**:

1. **Never Call `oMLX` `POST /admin/api/server/restart`**: Under the macOS DMG app supervisor, that endpoint orphans child workers (`omlx#2701`). Always unload models via `POST /v1/models/{id}/unload` or restart `oMLX` cleanly via `killall oMLX && open -a oMLX`.
2. **Never Enable MTP on `Qwen3.6-35B-REAP-19B`**: Setting `mtp_enabled: true` on the REAP-pruned MoE model triggers `HTTP 409`. Keep MTP strictly on `Qwen3.8-27B-MLX-oQ4e-mtp`.
3. **`Qwen3.8-27B` `reasoning_effort` Strict Enum**: Never pass `"reasoning_effort": "high"`—`Qwen3.8` strictly validates `'low'`, `'medium'`, or `'xhigh'` (and always cap `thinking_budget` so thinking tokens cannot starve output on 24GB KV budgets).
4. **Anthropic Extended Thinking Constraint**: When routing to `anthropic::*` (`/v1/messages`) with `thinking: {"type": "enabled"}`, Anthropic's API requires `temperature: 1.0` and `budget_tokens >= 1024`.
5. **Guard Model Unloads with `is_local_omlx_model(model)`**: Cloud models (`provider::model`) and failover combos (`combo/alias`) must never hit local `/v1/models/{id}/unload`.
6. **MCP Streamable HTTP `Accept` Header**: Always send `"Accept": "application/json, text/event-stream"` on MCP `POST` requests—`2026-07-28` Streamable HTTP servers reject `application/json`-only headers with `HTTP 406`.
7. **Harness Tool-Turn Counting Pattern**: Because `HARNESS_SYSTEM_PROMPT_TEMPLATE` itself mentions `<tool_response>...</tool_response>` as an example, turn counters and compactors must match `m.get("role") == "user" and '<tool_response name="' in content` (never match bare `<tool_response>` across system messages).
8. **Async Skill Context Unpacking**: `build_skills_context(...)` is `async` and returns a 2-tuple `(skill_block, skill_summaries)`—always `await` and unpack both values across Chat, Council, Debate, Hall, and Harness routes.
9. **Node `< 22` Skill Installer Fallback**: `npx skills@latest` crashes on Node `< 22` (`fs/promises` `glob` import); preserve the pure-Python `git clone --depth 1` fallback in `/api/skills/install`.
10. **Preserve `gsd-secret-read-guard` & UI Accessibility Rules**: Keep `.env`, `.secrets`, `*.pem`, and `*.key` blocked in `read_file`/`grep_search` (while allowing `.env.example`), keep structural emojis out of `static/index.html` (use `UI_ICONS` inline SVGs), and use `showToast(...)` instead of blocking `alert(...)`.
11. **Dual Tool-Call Dialect Support (`v8.1`)**: `Qwen3.8` / `Qwen3.6` emit native XML `<tool_call><function=NAME><parameter=KEY>VALUE</parameter></function></tool_call>` (and tool aliases like `glob_files`, `grep`, `read`, `bash`), whereas other models emit JSON `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`. `extract_tool_calls()` + `_normalize_tool_call()` parse **both** formats in **Agent Harness** AND in **Direct Chat** (`POST /api/chat/stream` with read-only `Workspace + MCP Tools`).
