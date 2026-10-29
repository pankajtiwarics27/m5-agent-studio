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

### Roadmap prerequisites

AS-031 depends on AS-024 through AS-030. In the roadmap snapshot, AS-024–027
and AS-029–030 are `verified`; AS-028 is `completed`. A user-reported live
Docker Desktop test for AS-028 passed at `b452441` for writable workspace,
read-only container root, and no public network. It does not prove kernel or
host escape resistance, so AS-028 remains `completed`, not `verified`. No
prerequisite is recorded as failed. This dependency snapshot does not satisfy
the independent AS-031 review requirement.

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
| B8 | Path confinement and secret-file guards, including descriptor-relative no-follow reads and atomic writes | `_workspace_relative_parts`, `_open_workspace_entry_fd`, `_iter_workspace_entries`, `_atomic_write_workspace_text`, `_is_secret_filename`, `gsd-secret-read-guard` (`app.py`) | `tests/test_security_and_quality.py` (including `test_grep_search_rejects_symlink_swapped_after_enumeration`), `tests/test_prompt_injection_adversarial.py` |
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
- **Still examined by automated/source checks:** middleware ordering and token-file protections; DNS-pinned transport and redirect rejection; approval replay tombstones; sandbox preflight. This evidence does not replace independent human review.

### Additional environment evidence (user-reported)

- At current commit `b7d841a2a94842d62662e1c57206a531dd2247f7`, macOS/Python 3.15: the symlink-swap and per-file transcript-age regressions both passed; full pytest reported 438 passed, 1 skipped.
- At the same commit, Linux/Python 3.11 in the agent workspace: 438 passed, 1 opt-in sandbox test skipped; Ruff, mypy, compileall, frontend syntax, both smoke checks, artifact generation/checks, and `git diff --check` passed.
- At `b452441`, the user reported a successful macOS Keychain disposable round-trip and one passing Docker Desktop live sandbox test with `python:3.12-slim`. The container test checked workspace write access, read-only container root, and public-network denial; it does not prove host-kernel escape resistance and was not rerun on `b7d841a`.
- A real-browser screenshot at loopback showed the Studio page rendered at `b452441`; no real-browser approval acceptance/rejection/disconnect exercise is evidenced.

These results are test and environment evidence, **not independent human security sign-off**. No independent reviewer report tied to `b7d841a` has been supplied. The follow-up F-1/F-2 reports did not include severity ratings. AS-031 is not closed until an independent reviewer records scope, exact commit, findings/severity, verification of fixes, residual risks, and an explicit accept/block decision. Any critical/high issue must be remediated and re-verified or release must remain blocked. This release gate does not block unrelated development.

## 7. Known limitations and separate validation

1. Redaction and the sandbox preflight are best-effort defense-in-depth, not guarantees (`docs/history-privacy.md`, README security section).
2. The user-reported Keychain check passed on one macOS desktop account at `b452441`; this is not a platform/account matrix or independent review evidence.
3. The opt-in live Docker Desktop boundary test passed at `b452441`, but was not rerun at `b7d841a`; it does not prove kernel escape resistance. Other runtimes/platforms remain unvalidated.
4. The real-browser evidence confirms page rendering only. Full token-prompt and approval accept/reject/disconnect UX remains untested in a browser (the smoke test injects the auth helper in Node).
5. Remote exposure behind a reverse proxy requires explicit `STUDIO_ALLOWED_HOSTS`/`STUDIO_ALLOWED_ORIGINS`; proxy/IP behavior has not been validated.
6. No live provider/MCP actions or dependency supply-chain audit are evidenced. Treat these as separate deployment/release-readiness tasks where applicable.
7. The audit log is a local integrity aid, not tamper-proof. Same-account in-place file modification and hard links remain outside the symlink-specific boundary.

## 8. Reviewer checklist

1. Review exact commit `b7d841a2a94842d62662e1c57206a531dd2247f7` from a clean checkout; commands are in `BASELINE.md` (fetch full history for baseline verification).
2. Confirm independence from the authors and review `docs/threat-model.md` against B1–B12; check every threat has a control/test or an explicit residual limitation.
3. Verify the original remediation set and follow-ups, including the symlink-swap test and independent age check for each orphan transcript file. Assign severity to every new finding and re-verify any fix.
4. Attempt relevant bypasses on a local server: auth/Origin/Host/rate/size ordering, token handling, `/api/history/export`, approval replay, MCP allowlists, and safe workspace path behavior.
5. Consider the live-container result and its limits; do not infer host-kernel escape resistance from the current test.
6. Record reviewer identity/independence, scope, exact commit, commands and manual probes, findings with severity/reproduction, residual risks, and explicit accept/block decision. Report vulnerabilities privately via `SECURITY.md`.
7. **Release gate:** all critical/high findings remediated and re-verified, or release blocked (roadmap AS-031 → AS-046/AS-049/AS-050 chain). Unrelated development may continue while this release gate is pending.
8. Record the outcome in [`security-review-signoff.md`](security-review-signoff.md) and run `python scripts/validate_security_signoff.py`; CI fails on an unsigned record that coexists with a closed AS-031, or a signed `accepted` record with an unverified critical/high finding.

## 9. Sign-off record and automated gate

The reviewer decision lives in [`security-review-signoff.md`](security-review-signoff.md),
not in this package. That record carries a machine-readable block
(`state` / `reviewer` / `reviewer_independence` / `commit` / `reviewed_on` /
`decision`), a B1–B12 coverage table, a W-1/F-1…F-6 finding register with
reviewer severity and fix-verification columns, an accepted-residual-risk table,
and the signature block.

`scripts/validate_security_signoff.py` (run in CI, and by
`tests/test_security_signoff.py`) enforces:

| Rule | Why |
|---|---|
| AS-031 may be `completed`/`verified` only when the record is `signed` with `decision: accepted` | Prevents an author from closing the story on author evidence |
| An unsigned record must keep reviewer fields empty, every finding `unrated`/`not-verified`, and every boundary `not-examined` | A pre-filled "review" is indistinguishable from a self-review |
| A signed record must fill identity, independence, a full 40-character commit SHA, and an ISO review date | Ties the judgement to a person and an exact tree |
| `accepted` is rejected while any critical/high finding is `not-verified`, any boundary control is `inadequate`, or unverified findings have no accepted residual risk | Implements *"all critical/high findings remediated or release blocked"* |
| A signed record may not retain `_unset_`/`_unassigned_`-style placeholders | Catches a half-completed record |

`scripts/security_review_evidence.py` produces the bundle a reviewer pins in
§2 of the record: commit, branch, dirty/shallow state, interpreter/platform,
resolved dependency versions, and — with `--run-checks` — the exit status of
every `BASELINE.md` command. It reports failures verbatim and states explicitly
that the result is author-run evidence, not a review.

Current state: the record is **unsigned** and `decision: pending`, so AS-031 is
`in_progress`, this package's self-review stands as the only review performed,
and the release gates downstream of AS-031 remain closed.
