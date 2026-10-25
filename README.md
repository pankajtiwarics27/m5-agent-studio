# M5 Agentic Studio (`v9.0`)

> **Zero-Bloat Hybrid AI Studio, Autonomous Coding Harness, Universal MCP Hub & Context-Engineering Engine** purpose-built for **Apple Silicon (M5 MacBook Air • 24GB Unified Memory • 1TB SSD)** running concurrently alongside **OrbStack Docker** and desktop apps.

---

## 1. Why We Built This (Core Design Philosophy)

Most multi-agent coding stacks assume either unlimited cloud token budgets or a 64–128 GB workstation—spawning Electron/Next.js frontends, vector databases, LSP daemons, and multiple resident LLMs that immediately trigger macOS swap thrashing or Metal OOM crashes on a **24GB Unified Memory** laptop.

**M5 Agentic Studio (`v8.0`)** solves this from first principles:

1. **~35 MB Runtime Footprint, Zero Node/Build Bloat**: Pure Python `FastAPI` + `httpx` + `SQLite WAL/FTS5` backend (`app.py`, `6,318` lines) + zero-dependency Vanilla JS/CSS OLED frontend (`static/index.html`, `3,052` lines), compiled into a single portable PEP 723 executable (`studio.py`, `491,956` bytes) runnable anywhere via `uv run studio.py`.
2. **Sequential Multi-Model Intelligence on 24GB RAM**: Pairs with `oMLX 0.7.2` (`:8000`) using explicit Metal wired-memory ceilings (`20.8 GB`), paged SSD KV caching (`30 GB`), chunked prefill, and sequential `/v1/models/{id}/unload` handoffs so **Architect**, **Fast Coder**, **Critic**, and **Judge** models collaborate without ever colliding in RAM.
3. **Zero-RAM Context Engineering (Anti-Context-Rot)**: Replaces heavy vector DBs and LSP servers with **SQLite FTS5 (`porter unicode61 + bm25`)**, **Caveman Smart Digesters (`-89%` to `-98.2%` KV reduction)**, **Pure-Python AST + Graphify v8 CodeGraph (`0 MB` daemon RAM)**, **OpenContext + LLM-Wiki persistent memory (`oc://doc/<id>`)**, and **GSD 3-Wave Fresh-Context Spec Loops (`.planning/`)**.
4. **Efficiency Without Quality Loss**: Enforces **Ponytail v5's 6-Rung Senior Dev Ladder** (`-53%` LoC, `-45%` output tokens, `98%` test rate on risky logic) + **ASD-STE100 Technical Brevity**, while routing cloud bursts (`DeepSeek-V4.1-Flash`, Claude, Gemini, Groq, OpenRouter, OmniRoute) at `0 GB` local RAM.

---

## 2. Quick Start (Humans & External Coding Agents)

### A. Launch the Standalone Single-File Studio (Recommended on macOS)
Requires Python `3.11+` and [`uv`](https://docs.astral.sh/uv/) (dependencies `fastapi`, `uvicorn`, `httpx` auto-resolve via inline PEP 723 script metadata):

```bash
chmod +x studio.py
uv run studio.py
# Opens Web UI:            http://127.0.0.1:3000
# OpenAI Gateway (/v1):    http://127.0.0.1:3000/v1
```

### B. Launch in Development / Split-Source Mode
When actively editing `app.py` and `static/index.html`:

```bash
uv run app.py
# Or with plain Python if fastapi/uvicorn/httpx are already installed:
STUDIO_HOST=127.0.0.1 STUDIO_PORT=3000 python3 app.py
```

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

### D. Point Any OpenAI-Compatible CLI (`dsh`, `aider`, Cursor, Continue) at Studio's `:3000/v1` Gateway
Studio exposes a unified `/v1/models` and `/v1/chat/completions` router across local `oMLX`, configured Cloud providers (`provider::model`), and failover `combo/<alias>` routes:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:3000/v1"
export OPENAI_API_KEY="local-m5-studio"
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

## 3. System Architecture

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Browser UI (`ui-ux-pro-max` OLED `#070b14`, SVG Icons, `⌘K` Palette, `Alt+1..5` Tabs) │
│  [1. Agent Harness] [2. Council Hall] [3. Auto 4-Stage] [4. Debate] [5. Direct Chat]   │
│  + Right Telemetry Rail: 24GB Unified RAM Bar • oMLX Residency • tok/s • OrbStack      │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │ HTTP / Real-Time SSE (`Cache-Control: no-store`)
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│  FastAPI Single-Process Core (`app.py` / standalone `studio.py` • ~35 MB RAM)          │
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

## 4. Verified M5 MacBook Air (24GB RAM) Local LLM Stack & `oMLX 0.7.2` Configuration

All local model selections and memory limits were benchmarked end-to-end with OrbStack and desktop apps active:

### Verified 4-Stage Council Models & Measured M5 Throughput

| Stage / Role | Local Model (`oMLX 0.7.2`) | Quantization / Architecture | Measured Decode Speed | Why Selected for 24GB M5 |
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

## 5. How We Built It: Version Evolution & Research Provenance (`v1.0` → `v8.0`)

Every feature in `m5-agent-studio` was added only after live repository inspection and empirical verification:

| Version | Milestone | Upstream inspirations & Verified Decisions |
| :--- | :--- | :--- |
| **v1.0–v3.0** | **Local M5 AI Studio, Skills & Council Hall** | Built FastAPI + SSE UI for Direct Chat, Auto 4-Stage Council, Debate, M5 24GB RAM + `oMLX` + OrbStack telemetry, zero-Node Git skill installer, persistent Council Hall chronicles, and `--mcp` stdio bridge. |
| **v4.0** | **Hybrid Cloud Router & OmniRoute Gateway** | Added encrypted-at-rest local provider store (`chmod 0600`), 1-click presets (`OpenRouter`, `Anthropic`, `Gemini`, `Groq`, `Cerebras`, `DeepSeek`, `OmniRoute`), ordered `combo/<alias>` failover chains, and `/v1/chat/completions` OpenAI gateway. |
| **v5.0** | **Autonomous Coding Harness** | Synthesized `deepseek-harness` (`dsh`) + `openharness` (`hn`): isolated `git worktree` sandboxes (`.harnesses/wt-<id>`), `plan` / `standard` / `autonomous` approval policies, sub-agent Critic delegation, unified diff viewer, and 1-click worktree apply. |
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

### Recompiling `studio.py` After Editing `app.py` or `static/index.html`
> **Important for Humans & LLMs**: `app.py` reads `static/index.html` from disk, whereas `studio.py` embeds `static/index.html` inside `EMBEDDED_HTML = r"""..."""` so `studio.py` can be copied anywhere as a single file. Whenever you edit `app.py` or `static/index.html`, recompile `studio.py` with:

```bash
python3 - <<'PY'
import os, pathlib, py_compile

root = pathlib.Path("/home/user/m5-agent-studio")  # or pathlib.Path(".")
app_txt = (root / "app.py").read_text(encoding="utf-8")
html_txt = (root / "static" / "index.html").read_text(encoding="utf-8")

assert '"""' not in html_txt, "Triple double-quotes are not allowed inside static/index.html"
assert not html_txt.endswith("\\"), "Trailing backslash breaks raw string literal"

target_old = """@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    ext = BASE_DIR / "static" / "index.html"
    return HTMLResponse(ext.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})"""

assert target_old in app_txt, "index() route signature changed in app.py"

embedded_block = (
    'EMBEDDED_HTML = r"""' + html_txt + '"""\n\n\n'
    '@app.get("/", response_class=HTMLResponse)\n'
    'async def index() -> HTMLResponse:\n'
    '    ext = BASE_DIR / "static" / "index.html"\n'
    '    if os.environ.get("STUDIO_EXTERNAL_STATIC") == "1" and ext.exists():\n'
    '        return HTMLResponse(ext.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})\n'
    '    return HTMLResponse(EMBEDDED_HTML, headers={"Cache-Control": "no-store"})'
)

out = root / "studio.py"
out.write_text(app_txt.replace(target_old, embedded_block, 1), encoding="utf-8")
os.chmod(out, 0o755)
py_compile.compile(str(root / "app.py"), doraise=True)
py_compile.compile(str(out), doraise=True)
print(f"Rebuilt studio.py ({out.stat().st_size:,} bytes) successfully.")
PY
```

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
