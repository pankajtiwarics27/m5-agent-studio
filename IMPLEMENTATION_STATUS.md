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
| Local authentication, origin checks, and rate limiting | **Loopback path implemented and smoke-tested; broader validation pending** | `scripts/smoke_local.py` launches both source and standalone builds on loopback and checks token generation/mode, protected API, UI token-helper injection, Origin rejection, and DB isolation; with Node it simulates prompt/retry/session reuse/cross-origin behavior. Middleware now fails closed with 503 if its token is unexpectedly unavailable, including when ASGI lifespan initialization is skipped. Real-browser approval UX, roles, proxy/IP behavior, and cross-platform checks remain open. `STUDIO_AUTH_DISABLED=1` remains loopback-only. |
| OS credential storage and legacy migration | **Implemented; macOS target-account round-trip passed once** | Provider and MCP secrets are stored as OS-keyring references, with automatic SQLite migration and masked public responses; tests confirm writes fail closed when keyring is unavailable. User-reported `scripts/check_keyring.py` passed on macOS at `b452441` using the disposable actual backend round-trip. This is evidence for that account/environment, not a platform matrix; repeat on other target accounts as needed. No encrypted fallback, consent flow, or rotation UX. |
| Outbound URL/SSRF controls | **Implemented with transport/runtime caveats** | HTTP transport revalidates immediately before I/O, pins resolved IPs to TCP while retaining original host/SNI, blocks redirects, and ignores implicit proxy variables; a real loopback socket test verifies the pinned transport. Uses httpcore transport internals and needs target-platform/provider validation; broad `STUDIO_ALLOW_PRIVATE_URLS` compatibility opt-in remains. |
| Real sandbox runner | **Implemented; one live Docker Desktop boundary test passed** | User-reported opt-in `tests/test_live_sandbox.py` passed on Docker Desktop (LinuxKit, aarch64) at `b452441`, using a pre-pulled `python:3.12-slim` image. It checks writable workspace, read-only container root, and no public network. This does not prove host-kernel escape resistance, other runtimes/platforms, or a full sandbox penetration test; the runtime shares the host kernel. MCP stdio and explicit host mode remain unsandboxed. |
| One central tool-policy broker | **Substantially implemented; some edges remain** | `_execute_harness_tool` is the final policy-enforcing executor for Direct Chat, harness, GSD, and SDLC agent tool calls; approval policy/pending-request lifecycle and an authenticated `/api/harness/audit` event log cover these paths. The audit table stores hashes/metadata, not raw args/output, and blocks execution if a start record cannot be written; separate harness JSONL/message snapshots may contain raw prompts, arguments, and outputs and are subject to configured retention. Manual MCP playground calls remain separate; browser approval UX still needs real-browser validation. |
| Path/artifact confinement | **Workspace file reads/writes and scans use descriptor-anchored no-follow operations; follow-up tests pass** | Grep, CodeGraph, shortcut-debt, listing, direct reads, context reads, delegated context, and harness writes reject symlink path components; no-follow directory-relative opens prevent a link swapped after enumeration from redirecting a read. Atomic writes replace entries through an anchored parent directory. A deterministic swap test passed on Linux/Python 3.11 and macOS/Python 3.15 at `b7d841a`. Same-account in-place file changes and hard links are outside this symlink-specific boundary; Windows/other OS behavior is not established and unsupported dir-fd/no-follow operations fail closed. |

## Phase 1 — correctness and measurable quality

| Roadmap item | Status | Evidence / remaining work |
|---|---|---|
| Unit tests for high-risk logic | **Partial** | Current suite exercises auth/token startup, keyring failure behavior, URL/path confinement, DNS pinning and redirect rejection, SQLite migration history, backup/restore, tool-audit redaction, local provider/MCP stream contracts, approval/workflow policy, and container preflight. Additional policy, FTS, concurrency/crash, cross-platform, and broader failure-boundary coverage remains. |
| Fake-provider/MCP API integration tests | **Partial** | Added fake OpenAI-compatible streaming, oMLX-compatible streaming, MCP stateless HTTP, and redirect-denial contract tests. Anthropic streams, disconnects, retries, timeouts, partial failures, fallback routing, and full API-level contracts remain untested. No live configured model/MCP endpoint was available. |
| SDLC fail-closed acceptance | **Partial** | A story now requires a successful verification command and a strict first-line `VERDICT: PASS`; the `|| true` fallback was removed. Acceptance criteria are not yet individually linked to durable evidence, and retry/exhaustion behavior lacks end-to-end tests. |
| Versioned SQLite migrations | **Partial; local recovery tooling added** | `PRAGMA user_version` is schema version 2; transactional upgrades record versions in `schema_migrations`, including the tool-audit table. `scripts/db_maintenance.py` creates validated owner-only online backups and safe restore with pre-restore copy; plaintext-credential DBs are refused. Non-additive/history-wide migrations, automatic pre-migration backup, concurrency/crash recovery, and restore across machines/keyrings remain unvalidated. |
| CI and static checks | **Substantially implemented** | GitHub Actions runs on every push and pull request (Python 3.11/3.12/3.13, Linux): baseline ancestry, Ruff lint over all sources, mypy over the typed contract modules, compile-all, inline-JavaScript syntax check, tests, dual loopback smoke tests, commit-message policy, and bit-for-bit regeneration of `studio.py`, the route matrix, the subsystem inventory, and the roadmap dashboard. Still open: a macOS runner leg, broader API-level test coverage, and extending mypy to `app.py`, `scripts/`, and `tests/` (documented ratchet). |
| Reproducible benchmarks | **Not complete** | Synthetic telemetry samples were removed from the normal empty-history result. No reproducible benchmark scripts or machine-readable hardware/model methodology has been added. |

## Phase 2 — maintainability refactor

**Not started.** Backend remains largely in `app.py`; generated `studio.py` is rebuilt from canonical source and HTML with `scripts/build_studio.py`. No module split, typed API schema layer, structured request/session logging, or modular frontend source has been introduced.

## Phase 3 — routing and runtime efficiency

**Not started beyond targeted safety fixes.** No provider capability registry, comprehensive failover/circuit-breaker contract, budget enforcement, measured token accounting, local-model scheduler, adaptive telemetry, or bounded context budget is established.

## Phase 4 — safe agent workflows

**Partial.** Plan-mode restrictions, approval checks, filtered child environments, bounded subprocess output, and stricter SDLC status checks exist. Harness tool attempts create a local metadata/hash audit record before execution; separate event/message files can contain raw transcript data and are now included in configured history retention. Model context encodes tool output as JSON-escaped untrusted data, and system prompts warn against following instructions found in files/MCP/search results. This is defense-in-depth only: no tamper-proof ledger, comprehensive workflow budgets/resume contract, or acceptance-evidence system is established; prompt-injection resistance remains non-formal.

## Phase 5 — product polish and compatibility

**Partial.** README includes loopback smoke checks for `app.py` and standalone `studio.py`, actual keyring probe instructions, `STUDIO_DB_PATH`, and validated SQLite backup/restore commands. History retention is configurable through startup, authenticated API, and CLI paths, but there is no retention UI. No browser accessibility/responsive suite, compatibility/capability matrix, or secret-safe import/export has been introduced.

## Local checks still requiring the target machine

These are environment-dependent validation steps, not code changes that can be proven in this Linux workspace:

1. Run `python3 scripts/check_keyring.py` under the desktop account that will run Studio. It must succeed before saving credentials; the current workspace has no OS keyring service and correctly fails closed.
2. For sandboxing, install Docker or Podman, pre-pull a reviewed image, then run the opt-in command in README. The current environment has no container runtime.
3. Configure the actual local model/provider and MCP endpoints, then verify a real stream and one read-only MCP call. Fake contract tests pass, but no live endpoint is configured here.
4. Use a real browser to test first-use token prompt and an approval/rejection/disconnect in Harness/GSD/SDLC. The fetch helper is simulated in Node and same-origin token handling is covered; full browser approval UX has not been run.
5. On the target OS, create a backup, stop Studio, restore into a disposable copy, and confirm required credentials are available in that OS keyring. Cross-machine restore intentionally requires re-entering credentials.

## Validation evidence for AS-031 finding remediations (2026-10-29)

**Current code under test:** `b7d841a2a94842d62662e1c57206a531dd2247f7`.

- Linux/Python 3.11 in the agent workspace: `pytest -q` — **438 passed, 1 skipped** (opt-in live container test); Ruff, mypy (35 files), compileall, frontend syntax, both loopback smoke tests, generators, roadmap `--check`, and `git diff --check` passed.
- User-reported macOS/Python 3.15 at the same commit: both follow-up regressions — **2 passed**; full suite — **438 passed, 1 skipped**. The one warning is the upstream Starlette/httpx TestClient deprecation.
- User-reported macOS keyring disposable write/read/delete check passed at `b452441`; it was not rerun at `b7d841a`.
- User-reported Docker Desktop live sandbox test passed at `b452441` with `python:3.12-slim`; it checks workspace writability, read-only container root, and no public network. It does not prove escape resistance beyond the container boundary and was not rerun at `b7d841a`.
- A user screenshot at `b452441` confirms the UI rendered in a real browser at loopback. It does not exercise first-use token prompts or approval accept/reject/disconnect flows.

**Review hand-off (added 2026-10-29):** the reviewer records their judgement in [`docs/security-review-signoff.md`](docs/security-review-signoff.md) — identity/independence, pinned commit, B1–B12 coverage, W-1/F-1…F-6 severity and fix verification, accepted residual risks, and the accept/block decision. `scripts/security_review_evidence.py --run-checks` captures the commit, environment, and verbatim results of every `BASELINE.md` command for that record. `scripts/validate_security_signoff.py` runs in CI and enforces the gate in both directions: a deferral requires AS-031 to be `completed` (never `verified`) with reviewer fields blank, and `verified` is rejected unless a signed `accepted` review exists with no unverified critical/high finding. This tooling only makes the missing review explicit; it is not a review.

**Maintainer decision (2026-10-29): human verification is deferred to the end.** Independent human review and human verification will be performed **once, at the end, as final end-to-end testing after all remaining stories are complete** (AS-039…AS-050 and AS-063), rather than story-by-story. AS-031 is therefore recorded as `completed` — implementation and evidence complete — and **not** `verified`. The sign-off record carries `state: deferred` and `decision: pending`; it names who verifies, when, and as what, and it assigns no severity to any finding.

**Still outstanding at that final end-to-end pass:** independent human source review of B1–B12 and the complete remediation diff at the then-current commit; independent reproduction/review of the reported findings; reviewer severity assessment and explicit sign-off. The follow-up F-1/F-2 messages did not provide severity ratings. Any critical/high issue must be remediated and re-verified or release must remain blocked. Until that happens this repository makes **no claim** that AS-031's criterion ("all critical/high findings remediated or release blocked") has been met, and AS-046/AS-049/AS-050 (including the v1.0 publication gate) remain open.

Live provider/MCP actions, reverse-proxy deployment, broader OS/runtime coverage, and dependency supply-chain audit are not demonstrated by the evidence above. Treat these as separate deployment/release-readiness checks where applicable; pending AS-031 sign-off does not prohibit unrelated development.

These checks do not constitute independent security sign-off, a penetration test, or readiness certification. AS-031 is `completed` (implementation done, human verification deferred to the final end-to-end pass) and will not be `verified` on author evidence.
