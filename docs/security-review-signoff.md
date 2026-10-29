# AS-031 independent security review — sign-off record

**Current state: DEFERRED to final end-to-end verification.** The AS-031
*implementation* is complete on this branch. Per the maintainer's decision of
2026-10-29, the independent human review is not performed story-by-story: it is
performed **once, as the final end-to-end verification after all remaining
stories (AS-039…AS-050, AS-063) are complete**. Roadmap story AS-031 is therefore
`completed`, not `verified` — `verified` is reserved for a record signed by a
reviewer who is independent of this work.

This file is the single place where that reviewer records
identity/independence, scope, the exact reviewed commit, per-finding severity,
fix verification, residual risks, and the accept-or-block decision. Until then
this record makes **no security claim**: no finding is rated, no control is
declared adequate, and no release gate is satisfied.

This record is machine-checked by `scripts/validate_security_signoff.py`
(run in CI and by `tests/test_security_signoff.py`). The checker enforces, in
both directions:

- `state: unsigned` (no implementation yet) — AS-031 must be `planned`/`in_progress`;
- `state: deferred` (this state) — AS-031 must be `completed`, never `verified`,
  and the record must name the deferral; reviewer fields and judgements stay blank;
- `state: signed` — AS-031 may be `completed`, or `verified` only with
  `decision: accepted`; the record may not leave reviewer fields blank, may not
  leave any finding `unrated`/`not verified`, and may not say `accepted` while
  any critical or high severity finding is unverified.

Author-side remediation evidence lives in
[`security-review-package.md`](security-review-package.md) (boundary inventory
B1–B12, adversarial tests, environment evidence) and
[`../IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md) (remediation depth).
Those documents describe what the **authors** believe; they are not sign-off.

<!-- as031-signoff
state: deferred
reviewer: ""
reviewer_independence: ""
commit: ""
reviewed_on: ""
decision: pending
deferral: "Maintainer decision 2026-10-29: independent human review and human verification run once as the final end-to-end pass after all remaining stories are complete; recorded as completed, not verified, until then."
-->

While the record is deferred these values must stay empty and the
reviewer-judgement cells in the two tables below must stay at their unassessed
tokens; the checker rejects a partially pre-filled "review" because that is how
a self-review gets mistaken for an independent one. The deferral text is the
only free-text field, and it must name who verifies, when, and as what.

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

**Decision: pending — deferred, not waived.** No independent reviewer has
recorded a decision, and none will be recorded per story. The AS-031 acceptance
criterion — *"all critical/high findings remediated or release blocked"* — is
untested author evidence until the final end-to-end pass. Concretely:

| What | Status now |
|---|---|
| AS-031 roadmap status | `completed` — implementation and evidence are on the branch |
| AS-031 correctness/security claim | **not verified** — nothing here is signed |
| Independent review of B1–B12 | deferred to the final end-to-end verification |
| Severity of W-1, F-1…F-6 | unassigned; no severity has ever been supplied |
| M3 milestone exit gate ("all listed P0 stories verified") | formally unmet: AS-031 is `completed`, not `verified` |
| AS-046 (final security and regression audit) | still a gate; it consumes this record |
| AS-049 (release-readiness approval) | still a gate |
| AS-050 (v1.0 publication) | still a gate — must not ship on a deferred record |

The deferral is an ordering decision by the maintainer, not a downgrade of the
requirement: AS-031 cannot reach `verified`, and v1.0 cannot ship, until §1–§5
are filled in and the decision below is signed.

| Field | Value |
|---|---|
| Decision | pending |
| Deferred until | final end-to-end verification, after AS-039…AS-050 and AS-063 are complete |
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

0. Leave `deferred` in `state: deferred` while the review has not happened; a
   deferral is valid only while AS-031 is `completed` and the deferral text names
   the final end-to-end pass.
1. To perform the review, change `state:` to `signed` and clear the deferral
   text, then fill the record block, §1, §2, and both tables.
2. Replace every `unrated` with your severity and `not-verified` with `verified`
   only after you personally reproduce or inspect the fix.
3. Record residual risks and the decision.
4. Promote AS-031 from `completed` to `verified` in
   `product/Adaptive_Runtime_Product_Roadmap_v2.json` **only if** the decision is
   `accepted`, regenerate the dashboard with
   `python scripts/generate_roadmap_status.py`, and send the change as a pull
   request. The validator fails any other combination, including `verified`
   against a deferred or release-blocked record.
5. Report vulnerabilities privately per [SECURITY.md](../SECURITY.md) rather than
   in a public pull request.
