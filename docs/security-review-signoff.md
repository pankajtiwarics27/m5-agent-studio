# AS-031 independent security review — sign-off record

**Current state: UNSIGNED.** AS-031 ("Independent security review") remains
`in_progress` in the roadmap. This file is the single place where an
independent reviewer records identity/independence, scope, the exact reviewed
commit, per-finding severity, fix verification, residual risks, and the
accept-or-block decision.

This record is machine-checked by `scripts/validate_security_signoff.py`
(run in CI and by `tests/test_security_signoff.py`). The checker enforces, in
both directions:

- while the record below says `state: unsigned`, roadmap story AS-031 may not be
  marked `completed` or `verified`;
- AS-031 may only be `completed`/`verified` when the record says
  `state: signed` **and** `decision: accepted`;
- a `signed` record may not leave reviewer fields blank, may not leave any
  finding `unrated`/`not verified`, and may not say `accepted` while any
  critical or high severity finding is unverified.

Author-side remediation evidence lives in
[`security-review-package.md`](security-review-package.md) (boundary inventory
B1–B12, adversarial tests, environment evidence) and
[`../IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md) (remediation depth).
Those documents describe what the **authors** believe; they are not sign-off.

<!-- as031-signoff
state: unsigned
reviewer: ""
reviewer_independence: ""
commit: ""
reviewed_on: ""
decision: pending
-->

A record is only valid once every quoted value above is filled. While the
record is unsigned these values must stay empty and the reviewer-judgement cells
in the two tables below must stay at their unassessed tokens; the checker
rejects a partially pre-filled "review" because that is how a self-review gets
mistaken for an independent one.

## 1. Reviewer identity and independence

| Field | Value |
|---|---|
| Reviewer name / handle | _unassigned_ |
| Contact (private channel per [SECURITY.md](../SECURITY.md)) | _unassigned_ |
| Independence statement (no authorship or co-authorship of `app.py`, `studio.py`, the remediation commits, or this record) | _unsigned_ |
| Conflicts of interest declared | _unassigned_ |
| Engagement type (internal second-party / external third-party / community) | _unassigned_ |

Independence is not inferred from the tooling that prepared this package. The
AI-assisted adversarial pass recorded in `security-review-package.md` §6 was
performed by the authors' agent and does **not** satisfy this field.

## 2. Review scope and pinned commit

| Field | Value |
|---|---|
| Reviewed commit (full 40-character SHA) | _unset_ |
| Working tree clean at review time | _unset_ |
| Review date (UTC, `YYYY-MM-DD`) | _unset_ |
| Documents reviewed | `docs/threat-model.md`, `docs/security-review-package.md`, `IMPLEMENTATION_STATUS.md`, `docs/history-privacy.md`, `docs/sandbox-boundary.md`, `docs/route-security-matrix.md` |
| Commands and manual probes executed | _to be recorded by reviewer_ |
| Evidence bundle reference (output of `scripts/security_review_evidence.py`) | _unset_ |

Explicitly **in scope** for AS-031: the local trust boundary of the Studio
process itself, as enumerated by B1–B12 in §3.

Explicitly **out of scope** (tracked as separate validation tasks, not blanket
release blockers): third-party providers/MCP servers and their operators,
dependency supply-chain audit (`pip-audit` is a recommended reviewer step, not
an author claim), reverse-proxy deployment topology, non-Linux/macOS platform
matrices, and host-kernel escape resistance beyond what the opt-in live
container test demonstrates.

## 3. Boundary review coverage (B1–B12)

Rows and code anchors are defined in
[`security-review-package.md`](security-review-package.md) §3. The reviewer
marks each row after examining the current implementation and its tests. Do not
change the ID column.

| Boundary | Reviewer examined | Control adequate | Tests adequate | Residual risk noted |
|---|---|---|---|---|
| B1 auth/Origin/Host/rate/body-limit middleware order | not-examined | not-assessed | not-assessed | no |
| B2 local API token generation, permissions, ownership | not-examined | not-assessed | not-assessed | no |
| B3 startup guards (auth-disabled, host, token entropy) | not-examined | not-assessed | not-assessed | no |
| B4 outbound SSRF controls and DNS pinning | not-examined | not-assessed | not-assessed | no |
| B5 tool authorization, audit hashing, fail-closed writes | not-examined | not-assessed | not-assessed | no |
| B6 approval scoping and replay protection | not-examined | not-assessed | not-assessed | no |
| B7 execution modes and sandbox preflight | not-examined | not-assessed | not-assessed | no |
| B8 path confinement, descriptor-relative no-follow I/O | not-examined | not-assessed | not-assessed | no |
| B9 prompt-injection boundaries | not-examined | not-assessed | not-assessed | no |
| B10 destructive-path hygiene / worktree removal | not-examined | not-assessed | not-assessed | no |
| B11 keyring secret storage and migration | not-examined | not-assessed | not-assessed | no |
| B12 history privacy export/retention/redaction | not-examined | not-assessed | not-assessed | no |

## 4. Finding register and severity

Severity scale: `critical` > `high` > `medium` > `low` > `none`. The
author-impact column is the authors' **non-binding** description of blast radius
and is deliberately not a severity; per the handover, no severity was supplied
for the follow-up F-1/F-2 reports, and the reviewer assigns every rating.

| ID | Finding (author description) | Author remediation | Author impact sketch (non-binding) | Reviewer severity | Fix verified |
|---|---|---|---|---|---|
| W-1 | Harness worktree cleanup could fall back to a raw database-recorded path and delete a selected nested lookalike or uncommitted worktree data. | Cleanup now requires an exact registered direct-child `.harnesses/wt-*` root and preserves data on Git failure; tests assert preservation. | Data loss confined to paths already reachable by the same OS account. | unrated | not-verified |
| F-1 | Workspace grep/index/debt scanners and direct reads followed symlinks out of the workspace, allowing reads (including secret-named targets) through an alias; a second report showed an enumeration-to-open race (TOCTOU). | Descriptor-relative `O_NOFOLLOW` opens, symlink-component rejection, secret-name checks on workspace-relative paths, fail-closed when primitives are unavailable; deterministic symlink-swap regression test. | Disclosure of files readable by the same OS account; secret guards bypassed via alias. Same-account in-place modification and hard links remain outside this boundary. | unrated | not-verified |
| F-2 | Deleting context-vault chunks left searchable FTS5 copies, and retention could delete a newer paired transcript when its sibling was older. | Retention removes FTS rows without a surviving chunk; orphan transcript cleanup checks each file's own `lstat` mtime; regression covers old-half removal with newer half preserved. | Stale content remains searchable; unintended deletion of a recent transcript. | unrated | not-verified |
| F-3 | Harness retention did not prune expired sessions with their paired JSONL/message files. | Retention prunes expired session rows and paired files while preserving recent activity; docs distinguish these files from the SQLite export. | Local plaintext prompt/tool output persisted longer than configured. | unrated | not-verified |
| F-4 | Protected routes skipped authentication when the token was empty, e.g. when ASGI lifespan initialization was skipped. | Middleware fails closed with 503 unless authentication is explicitly disabled; no-lifespan test covers the state. | Unauthenticated access on a misconfigured or lifespan-skipped process. | unrated | not-verified |
| F-5 | Documented cleanup and retention behaviour did not match implementation (overbroad claims). | Docs corrected in `SECURITY.md`, `README.md`, `docs/history-privacy.md`, `IMPLEMENTATION_STATUS.md`. | Operators trusting overstated guarantees. | unrated | not-verified |
| F-6 | Self-review framing risked presenting author-run checks as independent assurance. | Package states explicitly that no independent review, pentest, or certification has occurred; this record enforces the missing step. | Release gate closed on author evidence. | unrated | not-verified |

Findings discovered **by the reviewer** during this engagement are appended to
this table with the next free `R-n` identifier, a reproduction, a severity, and
a verification state. Any `critical`/`high` row with `not-verified` forces
`decision: release-blocked`.

## 5. Residual risks accepted at sign-off

The reviewer lists what remains after remediation and who carries it. The
authors' current list (see `security-review-package.md` §7) is a starting point,
not an accepted-risk register:

1. Redaction and sandbox preflight are best-effort defense-in-depth.
2. The OS-keyring check passed once on one macOS account; no platform matrix.
3. The live container boundary test is single-runtime and does not prove
   host-kernel escape resistance.
4. Real-browser token-prompt and approval accept/reject/disconnect flows are not
   exercised end-to-end.
5. Reverse-proxy Host/Origin behaviour is unvalidated.
6. No live provider/MCP or dependency supply-chain audit is evidenced.
7. The audit log is an integrity aid, not tamper-evident; same-account in-place
   modification and hard links remain outside the symlink-specific boundary.

| Accepted residual risk | Rationale | Owner | Revisit trigger |
|---|---|---|---|
| _none recorded_ | _unsigned_ | _unsigned_ | _unsigned_ |

## 6. Decision and signature

Decision values: `pending` (no review yet), `accepted` (no critical/high
findings outstanding), `release-blocked` (at least one critical/high finding
unremediated or unverified, or the reviewer cannot reach the required
assurance).

**Decision: pending.** No independent reviewer has recorded a decision, so the
AS-031 acceptance criterion — *"all critical/high findings remediated or release
blocked"* — is not satisfied, AS-046/AS-049/AS-050 stay gated, and AS-031 stays
`in_progress`. Unrelated development may continue.

| Field | Value |
|---|---|
| Decision | pending |
| Decision date (UTC) | _unset_ |
| Signature (name + method, e.g. signed commit, PGP, or maintainer on record) | _unset_ |
| Follow-up owner and date for remediation of blocked findings | _unset_ |

## 7. Completing this record (reviewer instructions)

```bash
git clone https://github.com/pankajtiwarics27/m5-agent-studio.git
cd m5-agent-studio
git checkout --detach <commit-under-review>
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[dev]'

# Optional: capture environment, commit, and check results verbatim.
python scripts/security_review_evidence.py --run-checks --output review-evidence.md

python scripts/validate_security_signoff.py   # must pass after you edit this file
```

1. Fill the record block, §1, §2, and both tables.
2. Replace every `unrated` with your severity and `not-verified` with `verified`
   only after you personally reproduce or inspect the fix.
3. Record residual risks and the decision.
4. Update AS-031 to `completed`/`verified` in
   `product/Adaptive_Runtime_Product_Roadmap_v2.json` **only if** the decision is
   `accepted`, regenerate the dashboard with
   `python scripts/generate_roadmap_status.py`, and send the change as a pull
   request. The validator fails any other combination.
5. Report vulnerabilities privately per [SECURITY.md](../SECURITY.md) rather than
   in a public pull request.
