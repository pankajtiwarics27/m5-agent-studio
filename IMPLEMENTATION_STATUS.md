# Roadmap implementation status

> **Where to find what (2026-10-28):** product-story status (AS-001…AS-063,
> with per-story evidence) now lives in
> [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json)
> and its generated dashboard
> [`docs/product-roadmap-status.md`](docs/product-roadmap-status.md). This
> document remains the record of **security-remediation depth**: what is
> implemented versus what still needs target-platform, live-runtime, or
> independent validation. The GitHub Actions CI workflow now runs on every
> push and pull request and is green on Python 3.11/3.12/3.13; the
> "workflow has not been run on GitHub" note below predates that.

This checklist tracks `/home/user/M5_AGENT_STUDIO_REVIEW_AND_ROADMAP.md`. Status reflects code and tests in this working tree, not a release approval or security certification. **The roadmap is not complete.** Do not describe the project as enterprise-ready or safe for untrusted remote users until the open P0/P1 items below are delivered and independently validated.

## Phase 0 — security boundary

| Roadmap item | Status | Evidence / remaining work |
|---|---|---|
| Route inventory and authorization manifest | **Partial** | `docs/route-security-matrix.md` is generated from registered FastAPI routes and CI checks for drift; it describes shared auth-prefix coverage, not route-level roles or authorization. No role-based authorization matrix exists. |
| Local authentication, origin checks, and rate limiting | **Local loopback path implemented and smoke-tested; broader validation pending** | `scripts/smoke_local.py` launches both source and standalone builds on loopback and checks token generation/mode, protected API, UI token-helper injection, Origin rejection, and DB isolation; with Node it simulates prompt/retry/session reuse/cross-origin behavior. Real-browser approval UX, roles, proxy/IP behavior, and cross-platform checks remain open. `STUDIO_AUTH_DISABLED=1` remains loopback-only. |
| OS credential storage and legacy migration | **Implemented with caveats; target-OS validation pending** | Provider and MCP secrets are stored as OS-keyring references, with automatic SQLite migration and masked public responses; tests confirm writes fail closed when keyring is unavailable. `scripts/check_keyring.py` performs a disposable actual OS backend round-trip. It failed here because this workspace has `keyring.backends.fail.Keyring` (no OS credential service); run it under the target desktop account. No encrypted fallback, consent flow, rotation UX, or platform matrix. |
| Outbound URL/SSRF controls | **Implemented with transport/runtime caveats** | HTTP transport revalidates immediately before I/O, pins resolved IPs to TCP while retaining original host/SNI, blocks redirects, and ignores implicit proxy variables; a real loopback socket test verifies the pinned transport. Uses httpcore transport internals and needs target-platform/provider validation; broad `STUDIO_ALLOW_PRIVATE_URLS` compatibility opt-in remains. |
| Real sandbox runner | **Implemented as an opt-in container runner; runtime validation pending** | Harness shell/Python/test-block commands can run in Docker/Podman with networking disabled, read-only container root, dropped capabilities, no-new-privileges, CPU/memory/PID limits, and only the session workspace mounted read-write. Requires a trusted pre-pulled image and has not been tested against a live runtime or across platforms; it shares the host kernel. MCP stdio and explicit host mode remain unsandboxed. |
| One central tool-policy broker | **Substantially implemented; some edges remain** | `_execute_harness_tool` is the final policy-enforcing executor for Direct Chat, harness, GSD, and SDLC agent tool calls; approval policy/pending-request lifecycle and an authenticated `/api/harness/audit` event log cover these paths. The audit stores hashes/metadata, not raw args/output, and blocks execution if a start record cannot be written. Manual MCP playground calls remain separate; browser approval UX still needs real-browser validation. |
| Path/artifact confinement | **Partial** | Resolved-path containment is used in targeted paths and a sibling-prefix regression test exists. Symlink races, all file APIs, secret-file rules, and cross-platform path cases are not comprehensively audited. |

## Phase 1 — correctness and measurable quality

| Roadmap item | Status | Evidence / remaining work |
|---|---|---|
| Unit tests for high-risk logic | **Partial** | Current suite exercises auth/token startup, keyring failure behavior, URL/path confinement, DNS pinning and redirect rejection, SQLite migration history, backup/restore, tool-audit redaction, local provider/MCP stream contracts, approval/workflow policy, and container preflight. Additional policy, FTS, concurrency/crash, cross-platform, and broader failure-boundary coverage remains. |
| Fake-provider/MCP API integration tests | **Partial** | Added fake OpenAI-compatible streaming, oMLX-compatible streaming, MCP stateless HTTP, and redirect-denial contract tests. Anthropic streams, disconnects, retries, timeouts, partial failures, fallback routing, and full API-level contracts remain untested. No live configured model/MCP endpoint was available. |
| SDLC fail-closed acceptance | **Partial** | A story now requires a successful verification command and a strict first-line `VERDICT: PASS`; the `|| true` fallback was removed. Acceptance criteria are not yet individually linked to durable evidence, and retry/exhaustion behavior lacks end-to-end tests. |
| Versioned SQLite migrations | **Partial; local recovery tooling added** | `PRAGMA user_version` is schema version 2; transactional upgrades record versions in `schema_migrations`, including the tool-audit table. `scripts/db_maintenance.py` creates validated owner-only online backups and safe restore with pre-restore copy; plaintext-credential DBs are refused. Non-additive/history-wide migrations, automatic pre-migration backup, concurrency/crash recovery, and restore across machines/keyrings remain unvalidated. |
| CI and static checks | **Partial** | GitHub Actions installs dependencies, compiles, syntax-checks inline JavaScript, runs tests, smoke-tests both loopback launch paths, and checks reproducible standalone/route-matrix artifacts. Workflow has not been run on GitHub. Lint, type checks, broad API tests, and platform matrix are not configured. |
| Reproducible benchmarks | **Not complete** | Synthetic telemetry samples were removed from the normal empty-history result. No reproducible benchmark scripts or machine-readable hardware/model methodology has been added. |

## Phase 2 — maintainability refactor

**Not started.** Backend remains largely in `app.py`; generated `studio.py` is rebuilt from canonical source and HTML with `scripts/build_studio.py`. No module split, typed API schema layer, structured request/session logging, or modular frontend source has been introduced.

## Phase 3 — routing and runtime efficiency

**Not started beyond targeted safety fixes.** No provider capability registry, comprehensive failover/circuit-breaker contract, budget enforcement, measured token accounting, local-model scheduler, adaptive telemetry, or bounded context budget is established.

## Phase 4 — safe agent workflows

**Partial.** Plan-mode restrictions, approval checks, filtered child environments, bounded subprocess output, and stricter SDLC status checks exist. Harness tool attempts now create a local metadata/hash audit record before execution; model context encodes tool output as JSON-escaped untrusted data, and system prompts warn against following instructions found in files/MCP/search results. This is defense-in-depth only: no tamper-proof ledger, comprehensive workflow budgets/resume contract, acceptance evidence system, adversarial prompt-injection suite, or memory-retention controls.

## Phase 5 — product polish and compatibility

**Partial.** README now includes loopback smoke checks for `app.py` and standalone `studio.py`, actual keyring probe instructions, `STUDIO_DB_PATH`, and validated SQLite backup/restore commands. No browser accessibility/responsive suite, compatibility/capability matrix, secret-safe import/export, or retention controls.

## Local checks still requiring the target machine

These are environment-dependent validation steps, not code changes that can be proven in this Linux workspace:

1. Run `python3 scripts/check_keyring.py` under the desktop account that will run Studio. It must succeed before saving credentials; the current workspace has no OS keyring service and correctly fails closed.
2. For sandboxing, install Docker or Podman, pre-pull a reviewed image, then run the opt-in command in README. The current environment has no container runtime.
3. Configure the actual local model/provider and MCP endpoints, then verify a real stream and one read-only MCP call. Fake contract tests pass, but no live endpoint is configured here.
4. Use a real browser to test first-use token prompt and an approval/rejection/disconnect in Harness/GSD/SDLC. The fetch helper is simulated in Node and same-origin token handling is covered; full browser approval UX has not been run.
5. On the target OS, create a backup, stop Studio, restore into a disposable copy, and confirm required credentials are available in that OS keyring. Cross-machine restore intentionally requires re-entering credentials.

## Validation run in this workspace

- `python3 -m pip install -e '.[dev]'` — passed.
- `python3 -m py_compile app.py studio.py scripts/*.py tests/*.py` and `python3 -m compileall -q app.py scripts tests` — passed.
- `pytest -q` — **48 passed, 1 skipped** (explicit live container runtime test); one upstream Starlette/httpx deprecation warning remains.
- `python3 scripts/check_frontend_syntax.py` — passed (one inline JavaScript block).
- `python3 scripts/smoke_local.py` and `python3 scripts/smoke_local.py --standalone` — both launched a disposable loopback server and passed health, token permissions, auth, UI token prompt/retry/session reuse (Node simulation), no cross-origin token disclosure, Origin rejection, and DB-path checks.
- Fake OpenAI-compatible, oMLX-compatible, and MCP HTTP contracts passed; pinned transport passed a real loopback TCP test; redirect rejection tests passed.
- SQLite backup/restore, plaintext-secret refusal, schema-history, audit redaction, and future-schema rejection tests passed.
- `scripts/check_keyring.py` was run but could not write because the workspace provides `keyring.backends.fail.Keyring`; OS credential-store behavior must be verified under the target desktop account.
- Docker/Podman is not installed here; the opt-in live sandbox test remains skipped. No live configured provider/MCP service, real browser, or cross-platform run was available.
- `git diff --check` — passed.
- Generated artifacts were rebuilt and are reproducible. SHA-256: `studio.py` `45e412b9d16d2a77d1cae7a744c05bbf7238422a91c84c8550704f554d4dc602`; `docs/route-security-matrix.md` `c3dce18e3d3dec2c80f9472e7bc1f7fb83f4b522f15d05194a81a00ffc5b0f8b`. Combined reproducibility hash: `ccbdd013ee88ec0565f35fc55c5f780daeb479fa9c23cb44e0a0c036692e35a5`.

These checks do not constitute an independent security audit or readiness certification. The GitHub Actions workflow has not been run on GitHub; no changes were pushed.
