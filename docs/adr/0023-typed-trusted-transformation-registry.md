---
id: ADR-0023
title: typed-trusted-transformation-registry
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0023: Typed Trusted Transformation Registry with Risk-Tiered Authority and Cascading Revocation

## Context

`M5-P5-02` (`artifact_lineage.py`, [ADR-0019](0019-artifact-lineage-provenance-and-selective-invalidation.md)) separates SHA-256 content identity from provenance sightings and enforces conservative trust propagation: effective trust is the weakest entry across sightings (`trusted`, `unverified`, `untrusted`), confidentiality is the strictest (`public`, `internal`, `secret`), and `check_operation` is deny-wins. Under that baseline, untrusted or unverified outputs from external CI runners, MCP connectors, or subagent patch proposals cannot feed restricted downstream operations such as `verification_input` or `apply_patch`.

Owner-authorized optional story `M5-P6-02` (*Typed trusted transformation registry*) introduces qualified trust relaxation strictly for demonstrated blocking workflows, subject to four non-negotiable constraints:

1. Unsupported transformation demand must be collected fail-closed before any contract is promoted, so convenience relaxations without blocking workflow evidence are refused.
2. Transformations must use allowlisted deterministic typed contracts backed by reproducible positive and adversarial qualification evidence plus measured completion benefit — never arbitrary code, shell commands, or a general proof engine.
3. Promotion must enforce risk-tiered human authority (`low`, `medium`, `high`, `critical`) and unconditionally forbid model, subagent, tool, skill, or automated-harness self-certification.
4. Revocation of a contract — or expiry/revocation of its backing operator grant or drift in `security_generation` — must invalidate every dependent lineage entry, verification `EvidenceRecord`, and anchored lesson.

## Decision

Add `trusted_transformations.py` (stdlib-only, inlined into standalone `studio.py` under the `_tt_` / `_TT_` private namespace) backed by `$HARNESS_STORE_DIR/transformations/state.json` under the shared 8-clause durable-JSON sidecar contract, and expose control-plane routes `/api/harness/transformations*` in `app.py`:

1. **Fail-closed demand collection (`TransformationDemandRecord`)**:
   - Calls to `execute_transformation(...)` when the A/B benefit gate is closed, when `transform_id` is unregistered, when no promoted contract is active, or when requested trust/operations exceed contract bounds raise `TransformationRejectedError` (leaving source provenance `untrusted`) and persist a bounded, secret-redacted `TransformationDemandRecord` (`TT_MAX_DEMAND_RECORDS = 128`).
   - `promote_contract(...)` refuses any contract lacking recorded `demand_ids` with at least one `blocking_task_id` (`missing_blocking_demand` / `demand_not_blocking`).
2. **Closed allowlist of deterministic typed transformations (`ALLOWLISTED_TRANSFORM_IDS`)**:
   - Only pure stdlib executors compiled in `trusted_transformations.py` can be qualified or promoted, each pinned by `compute_transform_implementation_digest(transform_id)` over canonical metadata and executor bytecode descriptors:
     - `diagnostic_log_sanitizer_v1` (`min_risk_tier="low"`, max relaxation `untrusted -> unverified`, max operations `diagnostic_read`, `triage_summary`, `32,768` B cap).
     - `structured_test_report_extract_v1` (`min_risk_tier="medium"`, max relaxation `untrusted -> trusted`, max operations `verification_input`, `diagnostic_read`, `16,384` B cap, numeric/identifier-only schema with zero free-form prose fields).
     - `scoped_unified_diff_normalizer_v1` (`min_risk_tier="high"`, max relaxation `untrusted -> trusted`, max operations `apply_patch`, `review_diff`, `32,768` B cap, single-file prefix-confined unified diff rejecting `..`, `.git/`, `.github/`, `.env`, `scripts/`, `docs/security-review-signoff.md`, binary patches, executable/symlink modes `100755`/`120000`, prompt injection, and secret literals).
   - `qualify_allowlisted_transform(...)` runs built-in positive and adversarial bypass suites at the current `security_generation` and requires `unexpected_bypass_count == 0`.
3. **Risk-tiered human authority & zero model self-certification (`PromotionAttestation`)**:
   - Any promotion or reviewer attestation naming an actor in `TT_FORBIDDEN_ACTORS` (`model`, `assistant`, `llm`, `subagent`, `worker`, `mcp_tool`, `external_agent`, `skill`, `automated_harness`, `system_auto`, `unknown`, `anonymous`), matching worker/model ID prefixes, lacking `origin_provenance_trust == "trusted_operator"`, or referencing a non-operator `ScopedGrantStore` grant raises `ModelSelfCertificationForbiddenError` (`HTTP 403`).
   - `low` tier requires authenticated `operator` authority (`max_relaxed_trust == "unverified"`).
   - `medium` tier requires `operator` authority plus a live, active `ScopedGrantStore` grant (`context_write` or `workspace_write`) covering the transformation scope.
   - `high` and `critical` tiers require `operator` authority, a live `workspace_write` `ScopedGrantStore` grant, and independent `security_reviewer` dual sign-off (`reviewer_id != promoted_by`, `reviewer_origin_trust == "trusted_reviewer"`, non-empty `review_ticket_id`).
   - Transformations write canonical envelope bytes with a distinct SHA-256 into `ArtifactRegistry` (`derived_from=(input_sha256,)`) so source untrusted bytes are never laundered and source confidentiality (`secret`, `internal`, `public`) is preserved without downgrade.
4. **Cascading revocation (`revoke_contract`, `revalidate_active_contracts`, `verify_execution_guarantee`)**:
   - Explicit revocation, `ScopedGrantStore` grant revocation/expiry, implementation digest drift, or `security_generation` advancement marks matching contracts `revoked`, marks `TransformationExecutionRecord` entries `invalidated`, calls `ArtifactRegistry.invalidate(output_sha256)` (cascading across `derived_from` descendants and marking dependent `EvidenceRecord` entries `stale`), and calls `EvidenceLessonStore.invalidate_lessons_by_lineage_digests(...)`.
5. **Measured A/B gate & optional feature disablement**:
   - Fresh stores initialize `ab_gate_open: False` until `run_builtin_transformation_ab_suite` records verified completion lift with zero unauthorized relaxations (`overall` verified completion `0.333333 -> 1.0`, `+0.666667`; median tokens `620.0 -> 340.0`; median overhead `3.6643 ms`; `unauthorized_relaxations = 0`). Operators can disable the feature at any time via `STUDIO_ENABLE_TRUSTED_TRANSFORMATIONS=0` or `POST /api/harness/transformations/config`.

## Alternatives

- **Allow model-authored or configuration-supplied regex/Python transforms.** Rejected: letting model output or untrusted config author a trust-relaxing sanitizer creates a direct self-certification and code-execution bypass.
- **Overwrite source `ProvenanceEntry` trust in `ArtifactRegistry` in place.** Rejected: violates ADR-0019's append-only sighting invariant and would launder raw untrusted bytes for unrelated consumers. Recording a distinct canonical output envelope linked via `derived_from` scopes relaxation strictly to the validated output schema and `allowed_operations`.
- **Single flat operator approval across all transformation risk tiers.** Rejected: relaxing diagnostic logs to `unverified` has materially lower blast radius than relaxing a patch bundle to `trusted` for `apply_patch`; binding `medium` to live revocable `ScopedGrantStore` grants and `high`/`critical` to independent dual review matches authority to risk.
- **General taint-Tracking theorem prover across arbitrary tools.** Rejected by the M5 2.0 roadmap non-goals and ADR-0019: undecidable for general code and unnecessary for bounded, schema-validated transformations.

## Tradeoffs

- Adding a new transformation type requires a reviewed code change to `_TT_ALLOWLISTED_SPECS` and its adversarial corpus rather than runtime plugin loading. Accepted because trust relaxation is a security boundary, and recorded `TransformationDemandRecord` entries give operators concrete evidence of which transformations are worth qualifying.
- Binding active contracts to `security_generation` and live `ScopedGrantStore` grants means a security-config rotation or grant expiry automatically revokes promoted `medium`/`high` contracts and invalidates derived evidence until re-promoted. Accepted: fail-closed staleness matches `M5-P3-06` and `M5-P5-02`.
- Verified facts: `trusted_transformations.py` passes `mypy` (`57` typed source files), `ruff`, all `8` clauses of `tests/test_durable_state_contract.py`, and `tests/test_trusted_transformations.py` covering demand redaction, anti-self-certification, risk-tiered promotion, confidentiality preservation, cascading revocation, and standalone `studio.py` parity.

## Review Status

Not reviewed. Produced during autonomous implementation of owner-authorized optional story `M5-P6-02` on branch `arena/eb4484b8-m5-agent-studio`; independent human security review remains required alongside AS-031 (`docs/security-review-signoff.md`).
