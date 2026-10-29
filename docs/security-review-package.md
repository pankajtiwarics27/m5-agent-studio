# Independent security review package (AS-031)

**Status of this document:** preparation for an independent security review.
It is **not** a security certification, a pentest report, or a claim that a
review has occurred. AS-031's acceptance — *"all critical/high findings
remediated or release blocked"* — requires reviewers who are independent of
the code's authors; until that happens, the story remains open and v1.0
(AS-050) stays gated on it.

## 1. Scope

| In scope | Out of scope (separate efforts) |
|---|---|
| Studio's local trust boundary: bearer authentication, host/Origin (CSRF) checks, rate limits, body limits | The security of third-party LLM providers, MCP servers, or models you connect to |
| Outbound request safety: URL validation, DNS pinning, redirect/proxy rejection | The macOS/Apple-Silicon platform itself, Docker/Podman, OrbStack |
| Tool authorization, approvals, replay protection, audit trail | Performance, quality-of-results, or benchmark claims (tracked elsewhere in the roadmap) |
| Code-execution sandbox and workspace preflight; path confinement; destructive-path hygiene | Supply-chain review of dependencies (recommend `pip-audit` as a reviewer step) |
| Secret storage (OS keyring), redaction, history export/retention |  |

## 2. System and trust model

Single-user, single-process FastAPI application bound to loopback by default;
one bearer token is the entire API authorization boundary (no user/tenant
roles — a deliberate, documented decision; see `docs/threat-model.md` for
actors, boundaries, abuse cases, and evidence requirements, validated by
`scripts/validate_threat_model.py` and `tests/test_threat_model.py`).
Remote exposure is opt-in and requires an explicit high-entropy token;
approval-gated tool execution is session-scoped and replay-protected.

## 3. Boundary inventory (code anchors for reviewers)

| # | Boundary | Where | Contract tests |
|---|---|---|---|
| B1 | Host allowlist → Origin/CSRF → bearer auth → per-client POST rate limit → body-size cap | `security_boundary` in `app.py` (single middleware; ordering is normative) | `tests/test_remote_protections.py`, `tests/test_security_and_quality.py` |
| B2 | Local API token: generation/permission/ownership | `_load_or_create_local_api_token` in `app.py` | `tests/test_security_and_quality.py`, `scripts/smoke_local.py` |
| B3 | Startup guards: auth-disabled/host/token-entropy only on loopback | settings validation in `load_settings` (`app.py`) | `tests/test_remote_protections.py`, `tests/test_configuration_contracts.py` |
| B4 | Outbound SSRF controls: syntax validation, DNS resolution, non-global IP block, request-scoped DNS pin, redirect rejection, ambient-proxy bypass | `_validate_outbound_url`, `_PinnedAsyncHTTPTransport` (`app.py`) | `tests/test_pinned_transport.py`, `tests/test_redirect_safety.py`, `tests/test_private_network_gateway.py` |
| B5 | Central tool authorization (deny-by-default, registered tools only) + audit with hashed-only records and fail-closed writes | `authorize_tool_invocation`, `_run_authorized_tool`, `_audit_tool_*` (`app.py`) | `tests/test_audit_log.py`, `tests/test_security_and_quality.py` |
| B6 | Session-scoped, replay-protected approvals (403 scope / 409 replay; bounded TTL tombstones) | approval slots + `api_approve_harness_tool` (`app.py`) | `tests/test_security_and_quality.py` (AS-027 section) |
| B7 | Execution modes: disabled (default), container sandbox (no network, read-only root, preflight), explicit host opt-in | `_execution_mode`, `_sandbox_workspace_preflight`, container runner (`app.py`) | `tests/test_sandbox_boundary.py`, `tests/test_live_sandbox.py` (opt-in, needs a container runtime) |
| B8 | Path confinement and secret-file guards | `_resolve_confined_path`, `_is_secret_filename`, `gsd-secret-read-guard` (`app.py`) | `tests/test_security_and_quality.py`, `tests/test_prompt_injection_adversarial.py` |
| B9 | Prompt-injection boundaries: escaped untrusted tool output, extraction/policy separation | `_format_untrusted_tool_response`, `extract_tool_calls` (`app.py`) | `tests/test_prompt_injection_adversarial.py` |
| B10 | Destructive-path hygiene: worktree removal only for a registered direct-child `.harnesses/wt-*` root; failures preserve data | `_remove_harness_worktree` (`app.py`) | `tests/test_security_and_quality.py` (worktree-removal and failure-preservation tests) |
| B11 | Secrets in OS keyring (SQLite stores references only); migration fails closed | keyring helpers (`app.py`) | `tests/test_security_and_quality.py`, `scripts/check_keyring.py` (target OS) |
| B12 | History privacy: export excludes credentials/audit; retention excludes them; redaction best-effort | `history_privacy.py`, `/api/history/*` | `tests/test_history_privacy.py` |

## 4. Adversarial evidence already in the suite

- `tests/test_prompt_injection_adversarial.py` — untrusted content cannot grant tool privileges (forged markup, approval-in-args, MCP namespace shadowing, extraction attacks, secret-guard bypass attempts).
- `tests/test_remote_protections.py` — remote-mode boundary ordering and startup guards.
- `tests/test_failure_injection.py` — crash/failure classification for durable workflows.
- `tests/test_security_and_quality.py` — audit privacy (hashes only), approval scoping/replay, path containment, sandbox preflight, destructive-path hygiene.
- `tests/test_threat_model.py`, `tests/test_governance.py`, `tests/test_adr_process.py` — documentation-as-boundary checks (threat model, governance, ADRs must exist and validate).

## 5. Validation performed by CI on every change

Python 3.11/3.12/3.13: baseline ancestry → commit-message policy → dependency install → Ruff → mypy (typed contract modules) → compile-all → frontend syntax → full pytest → both loopback smoke tests → byte-for-byte regeneration of `studio.py`, route matrix, subsystem inventory, roadmap dashboard.

## 6. Self-review log (this preparation pass)

Method: source/test review of B1–B12 plus an adversarial pass against symlinks, retention copies, lifespan-skipped authentication, and cleanup failure paths. The additional pass was performed by an AI agent and is **not** AS-031's independent human review. Findings and remediation status:

- **W-1 (previously remediated):** cleanup formerly fell back to a raw DB-recorded path. The follow-up review found that the `shutil.rmtree` exception fallback still risked deleting a selected nested lookalike or uncommitted worktree data. The current implementation requires an exact registered worktree and leaves it intact on Git errors; tests assert data preservation.
- **F-1 (symlink reads and TOCTOU):** workspace grep/index/debt scanners previously followed symlinks outside the workspace and could read a secret through an alias. Workspace file reads and scans now use directory-descriptor-relative opens with no-follow flags, reject symlink components, apply secret-name checks to workspace-relative paths, and fail closed when the platform lacks the required primitives. A deterministic regression test swaps a regular file for an external symlink immediately before open and confirms the outside content is not read. Same-account in-place modification and hard links remain outside this symlink-specific boundary.
- **F-2 (FTS and transcript retention):** deleting context-vault chunks previously left searchable FTS5 copies; retention now removes FTS entries without a matching surviving chunk. A follow-up found that an old orphan transcript could cause its newer paired file to be removed. Orphan cleanup now checks each file's own `lstat` mtime before unlinking; a regression test verifies the old half is removed while the newer half remains.
- **F-3 (harness retention):** retention prunes expired session rows and paired JSONL/message files; documentation distinguishes these files from the SQLite export and preserves recent activity.
- **F-4 (missing-token middleware):** protected routes previously skipped authentication when the token was empty. They now fail closed with 503 unless authentication is explicitly disabled; a no-lifespan test covers this state.
- **Still examined:** middleware ordering and token-file protections; DNS-pinned transport and redirect rejection; approval replay tombstones; sandbox preflight. These tests do not replace the documented live-runtime, target-OS, browser, proxy, and supply-chain validation.

These fixes do not close AS-031: an independent external review must reproduce and verify them, and release remains blocked until the acceptance gate is satisfied.

## 7. Known limitations (accepted, documented)

1. Redaction and the sandbox preflight are best-effort defense-in-depth, not guarantees (`docs/history-privacy.md`, README security section).
2. OS keyring behavior requires validation under the target desktop account (`scripts/check_keyring.py`); CI environments correctly fail closed.
3. The live container boundary test is opt-in and requires Docker/Podman; CI does not exercise it.
4. Real-browser approval UX is simulated in Node during smoke tests, not exercised in a full browser.
5. Remote exposure behind a reverse proxy requires explicit `STUDIO_ALLOWED_HOSTS`/`STUDIO_ALLOWED_ORIGINS`; proxy/IP behavior beyond that is unvalidated.
6. The audit log is a local integrity aid, not tamper-proof.

## 8. Reviewer checklist

1. Reproduce from a clean checkout (commands in `BASELINE.md`; full history needed: `git fetch --unshallow`).
2. Review `docs/threat-model.md` against the boundary inventory in §3; confirm each threat has either a control + test or an explicit accepted limitation in §7.
3. Attempt bypasses on the running server (loopback + a remote-mode bind with `STUDIO_API_TOKEN`): auth/Origin/Host/rate/size ordering, token handling, `/api/history/export` contents, approval replay, MCP allowlists.
4. Exercise the container sandbox with an untrusted workload (network escape, root writes) — requires a local container runtime.
5. Audit dependency supply chain (e.g., `pip-audit`, review `pyproject.toml` ranges).
6. Report findings via `SECURITY.md` (private disclosure). **Release gate:** all critical/high findings remediated and re-verified, or release blocked (roadmap AS-031 → AS-046/AS-049/AS-050 chain).
