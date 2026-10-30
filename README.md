# M5 Agent Studio

> Local-first AI developer platform and agent harness (repository package
> `m5-agent-studio`, product name **M5 Agentic Studio**, version `v9.0.1`).
> This README is the **ground-truth current-state document** required by the
> M5 2.0 roadmap's readme contract: every claim below was verified against the
> code or a passing test in this checkout on 2026-10-30, or is explicitly
> labeled unverified. Planned capabilities are not presented as product
> capabilities. Evidence: [`docs/m5-2-0-baseline-evidence.md`](docs/m5-2-0-baseline-evidence.md),
> [`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md).

[![CI](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml)

**Start with the guides:** [Quickstart](docs/quickstart.md) ·
[Architecture](docs/architecture.md) ·
[Supported hardware](docs/supported-hardware.md) ·
[Privacy](docs/privacy.md) ·
[Migration guide](docs/migration-guide.md) ·
[All documentation](docs/README.md)

---

## What is verified today

Verified in this checkout (commit `1e2e0bd876409a9e4eeb4b0128b403422bac7013`
plus the M5 2.0 ground-truth change-set, Linux x86_64, Python 3.11.2):

- **The application runs and its local auth path works.** `scripts/smoke_local.py`
  launches a disposable loopback instance of both `app.py` and the generated
  `studio.py` and verifies the token/security path — both exit 0.
- **The test suite passes:** **717 passed, 1 skipped** (the skip is the opt-in
  live container-runtime test, see below). `ruff`, `mypy`, `compileall`, and the
  Node-based frontend syntax check all pass.
- **Generated artifacts are reproducible:** `studio.py`,
  `docs/route-security-matrix.md`, `docs/subsystem-dependency-inventory.md`
  regenerate byte-for-byte; the roadmap dashboard matches its JSON.
- **Storage is SQLite with WAL and FTS5** (`app.py` L1039
  `PRAGMA journal_mode=WAL`; FTS5 tables at L1181/L1200; BM25 search via
  `context_store.sanitize_fts5_query`). Default DB: `studio_history.db` beside
  the entry point; override with `STUDIO_DB_PATH` (code at `app.py` L269).
- **The frontend is a single-file vanilla JS UI** (`static/index.html`,
  4,228 lines; no framework build step).
- **Tool calls go through a central authorize → audit → execute boundary**
  (`_run_authorized_tool`, `app.py` L7592) with durable `tool_audit_events`
  rows and fail-closed policy evaluation.
- **Skill-code execution is deny-by-default and boundary-routed** (since
  `M5-P0-03`): skill scripts run only with `STUDIO_ALLOW_SKILL_CODE=1` and a
  live execution mode, through the same execution boundary as tool code —
  pinned by 9 regression tests
  (`tests/test_skill_execution_containment.py`).
- **Only 5 of 42 shipped Python modules are wired into the served app**
  (`context_store`, `mcp_client`, `onboarding_preflight`, `history_privacy`,
  `workflow_orchestration`). The other 37 — including `sandbox_boundary.py`
  and `side_effect_ledger.py` — have passing unit tests but **no `app.py` call
  path**. Module presence is not integration. Full table:
  [`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md) §4.

Reported claims that this review **refuted**: `app.py` is ~2,200 lines
(actual: **9,627**), and `mcp_client` has no call sites (it is invoked at
`app.py` L4643 via `mcp_client.adaptive_call`).

## Requirements and verified setup

**Verified prerequisites** (used successfully in this review):

- Python `>=3.11` (tested on 3.11.2), `git`, and Node.js (for the frontend
  syntax check).
- Full git history for baseline verification. If the clone is shallow:

```bash
git fetch --unshallow origin
```

**Verified setup and checks** (exact commands, all run and green on 2026-10-30):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,supply-chain]'

.venv/bin/python scripts/verify_baseline.py     # pinned baseline ancestry
.venv/bin/ruff check .                          # lint
.venv/bin/mypy                                  # typed contract modules
.venv/bin/python -m compileall -q app.py scripts tests
.venv/bin/python scripts/check_frontend_syntax.py
.venv/bin/python -m pytest -q                   # 717 passed, 1 skipped
.venv/bin/python scripts/smoke_local.py         # loopback app, auth verified
.venv/bin/python scripts/smoke_local.py --standalone
```

**Documented, not verified in this environment** (macOS-only or tool-specific;
kept from the previous README because they are the project's stated macOS
quick start — they were *not* re-tested in the Linux review environment and
are not guarantees):

- Standalone run with [`uv`](https://docs.astral.sh/uv/): `chmod +x studio.py`
  then `uv run studio.py` (dependencies resolve via inline PEP 723 metadata).
- Split-source run with `uv run app.py`, or directly:
  `STUDIO_HOST=127.0.0.1 STUDIO_PORT=3000 python3 app.py` (the code defaults
  are host `127.0.0.1`, port `3000`, `app.py` L224/L227).
- First-run UX: open the **Start** tab (onboarding preflight reads local
  configuration only — code path `onboarding_preflight`, integrated).

## Verified usage

- **Run the test suite:** `.venv/bin/python -m pytest -q` → 717 passed,
  1 skipped, 1 warning (19.13 s in the review environment).
- **Start a disposable authenticated instance (verified):**
  `.venv/bin/python scripts/smoke_local.py` starts `app.py` on a free loopback
  port in a temporary directory and exercises the auth/security path.
  `--standalone` does the same for `studio.py`.
- **Authentication model (verified in code and by smoke tests):** `GET /healthz`
  is the only unauthenticated route; everything under `/api/*` and `/v1/*`
  requires the bearer token. Default token file:
  `~/.config/m5-agent-studio/api-token` (`app.py` L267); override with
  `STUDIO_API_TOKEN_FILE`. Non-loopback binding requires an explicit
  `STUDIO_API_TOKEN` of at least 32 characters, and
  `STUDIO_AUTH_DISABLED=1` is accepted only on loopback (`app.py` L237-239).
- **History and privacy endpoints (verified in code):** `GET /api/history/export`,
  `POST /api/history/retention/run` backed by the integrated
  `history_privacy` module.
- **OpenAI-compatible gateway:** `/v1/models`, `/v1/chat/completions`
  (routes at `app.py` L3275/L3363).
- The previous README's full configuration table (environment variables),
  HTTP/SSE endpoint list, UI shortcuts, tool catalog, and database schema
  reference remain accurate as code descriptions and are documented in
  [`docs/configuration-contracts.md`](docs/configuration-contracts.md),
  [`docs/architecture.md`](docs/architecture.md) and the git history of this
  file; they were spot-checked (settings parsing, route list) but not every
  row is re-verified here.

## Architecture observed in code

Observed structure (see [`docs/architecture.md`](docs/architecture.md) and the
generated [`docs/subsystem-dependency-inventory.md`](docs/subsystem-dependency-inventory.md)):

- **Backend:** one FastAPI process — `app.py` (9,627 lines) plus 42 small
  contract modules listed in `pyproject.toml`. `studio.py` (15,235 lines) is a
  **generated** standalone artifact (`scripts/build_studio.py` embeds
  `static/index.html`); never hand-edit it.
- **Frontend:** `static/index.html`, vanilla HTML/CSS/JS, no build step.
- **Storage:** SQLite (`studio_history.db`) in WAL mode with FTS5 tables
  (`opencontext_fts`, `ctx_vault_fts`) for BM25 context search.
- **Request path:** `security_boundary` middleware (rate limit, origin checks,
  body limits; `app.py` L2348) → 65 registered routes → either model streaming
  (`stream_any_model`), the harness tool loop
  (`_execute_harness_tool` L7679 → `_run_authorized_tool` L7592 →
  `_execute_harness_tool_inner` L7182), or MCP calls (`_mcp_http_rpc` L4623 →
  `mcp_client.adaptive_call`; optional stdio client `_mcp_stdio_rpc` L4655).
- **Code execution:** tool commands (`run_bash`, `ctx_execute_python`,
  `ctx_run_block`) funnel through `_execute_limited` (L492) →
  `_start_code_process` (L458) with three modes: `disabled` (default),
  `sandbox` (pre-pulled container, networking disabled), `host` (requires
  `STUDIO_ALLOW_HOST_EXECUTION=1`). Harness agent sessions edit inside a git
  worktree (`_create_harness_worktree`, L6400) and are applied back explicitly
  (`_apply_worktree_to_main`, L7738).
- **Outbound network:** URL/IP pinning and redirect rejection
  (`_PinnedNetworkBackend` L723, `_validate_outbound_url` L688); private URLs
  denied unless `STUDIO_ALLOW_PRIVATE_URLS=1`.

### Critical invariants

Operational constraints preserved from the prior README and still enforced by
the current code — any engineer or agent extending this codebase must keep
them (spot-verified where noted):

1. **Never call oMLX `POST /admin/api/server/restart`**: it orphans child
   workers (`omlx#2701`). Unload via `POST /v1/models/{id}/unload`, or restart
   oMLX cleanly via `killall oMLX && open -a oMLX`.
2. **Never enable MTP on `Qwen3.6-35B-REAP-19B`**: `mtp_enabled: true` triggers
   `HTTP 409`. Keep MTP strictly on `Qwen3.8-27B-MLX-oQ4e-mtp`.
3. **`Qwen3.8-27B` `reasoning_effort` strict enum**: only `'low'`, `'medium'`,
   `'xhigh'` validate; cap `thinking_budget` so thinking tokens cannot starve
   output on 24 GB KV budgets.
4. **Anthropic extended thinking**: with `thinking: {"type": "enabled"}`,
   Anthropic requires `temperature: 1.0` and `budget_tokens >= 1024`.
5. **Guard model unloads with `is_local_omlx_model(model)`** (code at `app.py`
   L1443): cloud models and failover combos must never hit local unload
   endpoints.
6. **MCP Streamable HTTP `Accept` header**: always send
   `"Accept": "application/json, text/event-stream"` on MCP `POST`s —
   `2026-07-28` servers reject JSON-only headers with `HTTP 406`.
7. **Harness tool-turn counting**: match
   `m.get("role") == "user" and '<tool_response name="' in content` — never
   bare `<tool_response` across system messages (the system prompt contains an
   example).
8. **Async skill context unpacking**: `build_skills_context(...)` is `async`
   and returns `(skill_block, skill_summaries)` — always await and unpack both.
9. **Node `< 22` skill-installer fallback**: `npx skills@latest` crashes on
   Node `< 22`; preserve the pure-Python `git clone --depth 1` fallback in
   `/api/skills/install`.
10. **Preserve secret-path and UI accessibility rules**: resolve candidates
    before reading/indexing, reject symlinks that escape the workspace, and
    block `.env`, `.secrets`, `*.pem`, `*.key`, and other guarded names in
    resolved read/write/grep paths (allow `.env.example`); keep structural
    emojis out of `static/index.html` (use `UI_ICONS` inline SVGs), and use
    `showToast(...)` instead of `alert(...)`.
11. **Dual tool-call dialect support**: Qwen XML
    (`<tool_call><function=NAME>...`) and JSON (`{"name": ..., "arguments": ...}`)
    dialects — plus tool aliases (`glob_files`, `grep`, `read`, `bash`) — are
    parsed by `extract_tool_calls()` (`app.py` L7101) and
    `_normalize_tool_call()` (L7045) in both the Harness and Direct Chat.

## Tests and measured results

Full log with exit codes: [`docs/m5-2-0-baseline-evidence.md`](docs/m5-2-0-baseline-evidence.md).
Summary (2026-10-30, Linux x86_64 container, 2 vCPU, ~4 GB RAM, Python 3.11.2):

| Check | Result |
|---|---|
| `pytest -q` | 717 passed, 1 skipped (opt-in live container test), 1 warning |
| `ruff check .` | clean |
| `mypy` | clean (typed contract modules) |
| `check_frontend_syntax.py` (Node) | pass |
| `smoke_local.py` / `--standalone` | pass (auth/security path verified) |
| `verify_baseline.py` | pass (`7904892d759bb66b817bb17b6f4f377874bd6e25`) after `git fetch --unshallow` |
| governance / security-signoff / docs / ADR / screenshots / demo-tour checks | pass |
| generated artifacts drift check | pass (byte-for-byte) |
| `pip-audit` | **exit 1** — 21 advisories in 2 packages: `pip` 23.0.1 and `setuptools` 66.1.1 (venv bootstrap tooling only). Declared runtime dependencies (`fastapi`, `uvicorn`, `httpx`, `httpcore`, `keyring`) have no reported advisories. |

**Not measured (explicitly blocked, no numbers claimed):** peak unified-memory
and swap on a 24 GB Apple Silicon Mac; a resident-oMLX coding-task run;
SQLite latency under FTS5 load; live container-isolation behavior (no
Docker/Podman in the review environment). No benchmark or memory figure in
this README is a guarantee; treat all throughput statements anywhere in this
repository as environment-specific.

## Security and known limitations

**Deny-by-default controls verified in code:** execution mode defaults to
`disabled`; host execution requires `STUDIO_ALLOW_HOST_EXECUTION=1`;
MCP stdio launch requires `STUDIO_ALLOW_MCP_STDIO=1`; skill-code execution
requires `STUDIO_ALLOW_SKILL_CODE=1`; private-URL access requires
`STUDIO_ALLOW_PRIVATE_URLS=1`; policy-evaluation errors deny (fail closed).

**Known limitations and open containment work** (details:
[`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md) §5):

1. **Skill-code execution is contained (closed 2026-10-30 by `M5-P0-03`).**
   The two skill-script subprocess paths (`build_skills_context`,
   `_fetch_online_inspiration`) now run only through `_run_skill_script`
   (`app.py`), which requires the `STUDIO_ALLOW_SKILL_CODE` opt-in **and** a
   live execution mode: `sandbox` routes through the container boundary,
   `host` requires `STUDIO_ALLOW_HOST_EXECUTION=1`, and `disabled` (default)
   never executes skill code — with **no silent host fallback** (the skill's
   static text still applies). Nine regression tests pin this contract
   (`tests/test_skill_execution_containment.py`). The earlier ungated
   `ui-ux-pro-max` path is no longer reachable without both opt-ins.
2. **Host mode is unrestricted by design.** `STUDIO_EXECUTION_MODE=host`
   (with `STUDIO_ALLOW_HOST_EXECUTION=1`) runs shell/Python as the Studio OS
   user with no filesystem/network/credential isolation (also stated in
   [`SECURITY.md`](SECURITY.md)). Autonomous execution must fail closed when
   isolation is unqualified; it must never silently fall back to host mode.
3. **A git worktree is not an operating-system sandbox.** Worktree isolation
   protects the main checkout from agent edits; it does not confine processes.
4. **`side_effect_ledger.py` (durable idempotency) and `sandbox_boundary.py`
   (path confinement primitives) are unit-tested but not integrated** into
   `app.py`. Exactly-once external effects are **not** claimed, and not all
   subprocess descendants can be guaranteed terminable.
5. **Live container isolation is unqualified.** No Docker/Podman was available
   for this review (`tests/test_live_sandbox.py` is opt-in and was skipped).
   Isolation guarantees remain unproven until a live runtime shows the
   denials (`M5-P0-04`, `M5-P3-01`).
6. **`pip-audit` reports 21 advisories** in venv bootstrap packages
   (`pip` 23.0.1, `setuptools` 66.1.1); runtime dependencies are clean.
7. **Independent security review (`AS-031`) is `completed`, not `verified`** —
   release remains blocked until an authorized reviewer signs the record
   ([`docs/security-review-signoff.md`](docs/security-review-signoff.md)).
   Never self-certify that gate.

## Current implementation status

The owner confirmed on 2026-10-30 that
[`product/M5_2_0_Autonomous_Roadmap.json`](product/M5_2_0_Autonomous_Roadmap.json)
is the **authoritative execution ledger**; the mapping to the AS program is
recorded in
[`docs/m5-2-0-roadmap-reconciliation.md`](docs/m5-2-0-roadmap-reconciliation.md)
and the AS roadmap is retained unmodified (no history deleted). Multiple
implementation agents may work in parallel under the claim/review/merge
protocol in [`M5_2_0_AGENT_PROMPT.md`](M5_2_0_AGENT_PROMPT.md).

- **M5 2.0 autonomous-runtime program** —
  [`product/M5_2_0_Autonomous_Roadmap.json`](product/M5_2_0_Autonomous_Roadmap.json):
  34 stories across 7 phases. **Done with evidence: `M5-P0-01`, `M5-P0-02`,
  `M5-P0-05`, `M5-P0-07`** (phase P0 at 57%). Next ready story: `M5-P0-03`
  (contain the skill-execution bypasses) — claim it via the coordination
  protocol before coding. All other stories are `not_started`; planned work is
  not implemented product behavior.
- **AS program (Adaptive Runtime)** —
  [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json)
  with dashboard [`docs/product-roadmap-status.md`](docs/product-roadmap-status.md):
  63 stories (`AS-001`…`AS-063`); status vocabulary and update rules live in
  [`AGENTS.md`](AGENTS.md). Its per-module tests are the reason "tested" is
  high while "integrated" is low — see the audit before reading status as
  capability.
- Security-remediation depth beyond story status:
  [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Development and roadmap link

- Contributor/agent workflow, commit policy, and verification sequence:
  [`AGENTS.md`](AGENTS.md), [`CONTRIBUTING.md`](CONTRIBUTING.md),
  [`BASELINE.md`](BASELINE.md) (pinned baseline contract).
- Reproducible checks: the "verified setup and checks" block above is the
  verified subset of the CI sequence; the full sequence (artifact rebuild,
  supply-chain evidence, release gates) is documented in `AGENTS.md` §5 and
  exercised by CI on Python 3.11/3.12/3.13.
- Roadmaps and evidence:
  [`product/M5_2_0_Autonomous_Roadmap.json`](product/M5_2_0_Autonomous_Roadmap.json) ·
  [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json) ·
  [`docs/m5-2-0-roadmap-reconciliation.md`](docs/m5-2-0-roadmap-reconciliation.md) ·
  [`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md) ·
  [`docs/m5-2-0-baseline-evidence.md`](docs/m5-2-0-baseline-evidence.md)
- Superseded input draft retained for provenance:
  [`README_GROUND_TRUTH_DRAFT.md`](README_GROUND_TRUTH_DRAFT.md).
- **README maintenance rule:** after every completed roadmap story, refresh
  this README using only observed and tested behavior; record regressions and
  limitations promptly; never promote an intended feature, a passing mock, or
  an untested sandbox claim into a production guarantee.

## License

MIT — see [`LICENSE`](LICENSE).
