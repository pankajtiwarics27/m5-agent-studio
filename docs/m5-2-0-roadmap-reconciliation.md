# M5 2.0 roadmap reconciliation (M5-P0-01)

Evidence record for roadmap story `M5-P0-01` (Reconcile authoritative roadmap).
Performed 2026-10-30 against commit `1e2e0bd876409a9e4eeb4b0128b403422bac7013`.

## 1. Competing sources

| File | Role after this reconciliation |
|---|---|
| [`product/M5_2_0_Autonomous_Roadmap.json`](../product/M5_2_0_Autonomous_Roadmap.json) | **Authoritative execution ledger (owner-confirmed 2026-10-30)** for the M5 2.0 autonomous-runtime program (stories `M5-P0-01`…`M5-P6-03`). Installed by this story at its declared `source_of_truth.intended_path`. |
| [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](../product/Adaptive_Runtime_Product_Roadmap_v2.json) | **Retained, unmodified** — historical source and status ledger for the AS story program (`AS-001`…`AS-063`), its generated dashboard ([`product-roadmap-status.md`](product-roadmap-status.md)), and the contributor process in [`AGENTS.md`](../AGENTS.md). No history deleted. |
| [`README_GROUND_TRUTH_DRAFT.md`](../README_GROUND_TRUTH_DRAFT.md) | Retained input draft (readme-contract provenance). Superseded as the product README by the verified [`README.md`](../README.md). |

The M5 2.0 roadmap's own rule governed the handover: *"Do not replace existing
roadmap until P0-01 maps all items and user/owner confirms authority."* Both
conditions are now met: the mapping (this document) was completed in `M5-P0-01`,
and the **user/owner confirmed authority on 2026-10-30** (recorded in
`source_of_truth.authority_status: owner_confirmed`). Going forward the M5 2.0
roadmap is the authoritative execution ledger for the M5 2.0 program; AS-status
updates remain in the AS roadmap per `AGENTS.md`, and M5 2.0 story tracking
lives in the M5 2.0 roadmap. Multi-agent coordination rules:
[`../M5_2_0_AGENT_PROMPT.md`](../M5_2_0_AGENT_PROMPT.md).

## 2. Scope difference (why the two roadmaps are not duplicates)

- **AS program (`Adaptive_Runtime_Product_Roadmap_v2.json`)** — build the M5
  Agentic Studio product: governance, modular foundation, hardware/model
  portability, secure hybrid access, durable *workflow* execution, beta and
  release. 63 stories, 7 milestones (M0–M6).
- **M5 2.0 program (`M5_2_0_Autonomous_Roadmap.json`)** — turn the existing
  repository into a trustworthy *autonomous coding* runtime: ground truth and
  containment (P0), durable single-agent execution spine (P1), workspace
  safety (P2), qualified isolation and permissions (P3), measured routing and
  bounded delegation (P4), recovery/release assurance (P5), optional
  evidence-gated enhancements (P6). 34 stories.

They intersect where the M5 2.0 program re-verifies or hardens capability the
AS program built (tool authorization, sandbox boundary, side-effect ledger,
workflow state, hardware validation). The M5 2.0 invariants deliberately fail
closed on anything the AS program left unverified.

## 3. Item mapping (all 63 AS stories)

Mapping states: **covers** (M5 2.0 story re-verifies/builds this), **partial**
(overlaps some acceptance criteria), **retained** (stays in the AS program; no
M5 2.0 story needed), **unresolved** (no M5 2.0 counterpart; see §4).

| AS story (title) | AS status | Maps to (M5 2.0) | State | Note |
|---|---|---|---|---|
| AS-001 Pin baseline and reproducible behavior | verified | M5-P0-01, M5-P0-05 | partial | Baseline pin retained; M5 2.0 adds verified-evidence discipline. |
| AS-002 Establish CI quality gates | verified | M5-P0-05, M5-P5-03 | partial | Quality gates re-run and recorded as M5 2.0 evidence. |
| AS-003 Inventory API and subsystem dependencies | verified | M5-P0-02, M5-P0-06 | covers | Integration audit supersedes inventory for execution paths. |
| AS-004 Governance and contributor policy | verified | — | retained | Explicit M5 2.0 non-goal to redo governance. |
| AS-005 ADR process | verified | — | retained | M5 2.0 decisions will use the existing ADR log. |
| AS-006 Threat model and security inventory | verified | M5-P0-02, M5-P3-06, M5-P5-03 | partial | Re-verified against live call paths; incident containment added. |
| AS-007 Typed configuration and versioned contracts | verified | M5-P0-06 | partial | Spine interfaces extend the contract discipline. |
| AS-008 Provider adapter interfaces | verified | M5-P4-01, M5-P4-02 | partial | Routing/delegation measured on top of adapters. |
| AS-009 Storage abstraction and schema migrations | verified | M5-P1-01 | covers | Durable task/action schema and migration safety. |
| AS-010 Central tool authorization policy | verified | M5-P1-02 | covers | All consequential paths must gate through admission. |
| AS-011 Extract workflow orchestration module | verified | M5-P1-05, M5-P1-06 | partial | Durable task state machine on top of orchestration. |
| AS-012 Extract MCP and context boundaries | verified | M5-P3-05 | partial | MCP idempotency and external-effect reconciliation. |
| AS-013 API and provider contract tests | verified | M5-P0-05 | partial | Re-executed as M5 2.0 baseline evidence. |
| AS-014 Deterministic standalone build | verified | M5-P5-02 | partial | Artifact provenance and invalidation. |
| AS-015 Hardware capability schema | verified | M5-P4-01 | covers | RAM admission and measured qualification build on it. |
| AS-016 macOS hardware and memory profiler | completed | M5-P0-05, M5-P4-01 | covers | 24 GB telemetry explicitly blocked until measured on target hardware. |
| AS-017 Model manifest and registry | verified | M5-P4-01, M5-P4-02 | partial | Model qualification/switching consume the registry. |
| AS-018 Resource admission scheduler | verified | M5-P3-02, M5-P4-01 | covers | Unified execution profiles and footprints. |
| AS-019 MLX/oMLX lifecycle adapter | completed | M5-P4-01 | partial | One-resident-model invariant measured. |
| AS-020 Ollama and llama.cpp adapters | verified | M5-P4-01, M5-P4-02 | partial | Portability retained; routing measured. |
| AS-021 Cloud consent and fallback policy | verified | M5-P4-02 | partial | Cloud eligibility requires policy, per invariant. |
| AS-022 Diagnostics UI | completed | M5-P1-07 | partial / deferred | M5 2.0 keeps UI minimal (projection of backend authority); full diagnostics UI deferred. |
| AS-023 16/24/48 GB hardware validation | completed | M5-P0-05, M5-P4-01 | covers | Not reproducible in the Linux evidence environment. The 24 GB tier was measured by an operator run on an Apple M5 on 2026-10-31 ([record](m5-2-0-hardware-qualification-evidence.md)); the 16 GB and 48 GB tiers remain arithmetic guidance. |
| AS-024 Private-network remote gateway | verified | — | retained | Prior-program scope; no M5 2.0 expansion (non-goal). |
| AS-025 Authenticated sessions and device trust | verified | M5-P3-04 | partial | Scoped grants, expiry, revocation extend it. |
| AS-026 CSRF and rate-limit protections | verified | — | retained | Security baseline; must not regress (invariant). |
| AS-027 Remote tool approvals and policy enforcement | verified | M5-P1-02, M5-P3-04 | covers | Admission gate covers all enumerated paths. |
| AS-028 Sandbox and filesystem boundary hardening | completed | M5-P0-03, M5-P0-04, M5-P3-01…03 | covers | Core of the containment/qualification arc. |
| AS-029 Prompt injection and MCP adversarial tests | verified | M5-P3-05, M5-P5-03 | partial | Untrusted-text invariant enforced at admission. |
| AS-030 History privacy retention and export | verified | — | retained | Privacy non-negotiable; regression-tested. |
| AS-031 Independent security review | completed | M5-P5-03 | covers | M5 2.0 release gate requires the signed record; never self-certified. |
| AS-032 Persisted workflow state machine | verified | M5-P1-05 | covers | Durable task state machine and transitions. |
| AS-033 Checkpoints and safe resume | verified | M5-P1-04, M5-P1-05, M5-P2-05 | covers | Leases, fencing, ownership recovery added. |
| AS-034 Side-effect reconciliation and idempotency | verified | M5-P1-03, M5-P3-05 | covers | `side_effect_ledger` must be wired (currently not integrated). |
| AS-035 Cancellation retries and execution budgets | verified | M5-P2-05 | partial | Bounded loops, stagnation detection. |
| AS-036 Structured traces and diagnostics | verified | M5-P1-07 | partial | Minimal task events first. |
| AS-037 Crash and failure injection suite | verified | M5-P5-03 | covers | Fault-injection release gate. |
| AS-038 Workflow quality evaluation | verified | M5-P2-04, M5-P6-03 | partial | Verification evidence now; advanced evaluation optional. |
| AS-039 Guided onboarding and preflight | completed | — | retained | Product UX; integrated (`onboarding_preflight`). |
| AS-040 macOS packaging updates and rollback | completed | M5-P5-02 | partial | Artifact lifecycle/provenance. |
| AS-041 Documentation and migration guide | completed | M5-P0-07 | covers | Ground-truth README replaces marketing claims. |
| AS-042 Release signing SBOM and provenance | completed | M5-P5-02 | covers | Provenance and selective invalidation. |
| AS-043 External beta across Mac configurations | planned | — | unresolved | Requires external human testers; no autonomous M5 2.0 story (see §4, C3). |
| AS-044 Beta remediation and compatibility matrix | planned | — | unresolved | Depends on AS-043 (see §4, C3). |
| AS-045 Demonstration and onboarding media | completed | — | retained | `demo_tour` evidence retained. |
| AS-046 Final security and regression audit | planned | M5-P5-03 | covers | Fault-injection and security release gate. |
| AS-047 Data backup migration and rollback validation | planned | M5-P2-03, M5-P5-01 | covers | Rollback/reconciliation and quarantine. |
| AS-048 Publish reproducible benchmark results | completed | M5-P4-01, M5-P6-03 | partial | Measured routing; advanced calibration optional. |
| AS-049 Release readiness approval | planned | M5-P5-03, ship gate | covers | Release blocked until authorized sign-off. |
| AS-050 Publish v1.0 | planned | M5-P5-03, ship gate | covers | Gated on all mandatory gates. |
| AS-051 Capability negotiation per model and task | verified | M5-P4-01 | covers | Measured model qualification. |
| AS-052 Per-task model switching | verified | M5-P4-02 | covers | Portable handoff and conservative switching. |
| AS-053 Discover and install models | verified | M5-P4-01 | partial | Registry retained; admission measured. |
| AS-054 Switch providers without workflow rewrite | verified | M5-P4-02 | covers | Handoff portability. |
| AS-055 Context reassembly on model switch | verified | M5-P4-02 | partial | Handoff preserves context; reassembly module retained. |
| AS-056 Model quality and compatibility comparison | verified | M5-P4-01, M5-P6-03 | partial | Comparison is optional-tier in M5 2.0. |
| AS-057 Model update and rollback lifecycle | verified | M5-P5-02 | partial | Artifact/invalidation overlap. |
| AS-058 Routing policy and consent tests | verified | M5-P4-01, M5-P4-02 | covers | Measured routing with consent invariants. |
| AS-059 Local model calibration benchmarks | verified | M5-P6-03 | partial | Optional, evidence-gated. |
| AS-060 Versioned model performance profiles | verified | M5-P4-01 | partial | Profiles feed RAM admission. |
| AS-061 Model comparison and benchmark UI | verified | M5-P1-07 | partial / deferred | UI minimalism non-goal; deferred. |
| AS-062 Adaptive model recommendation engine | verified | M5-P4-02, M5-P4-03, M5-P6-03 | partial | Conservative switching first; recommendation optional. |
| AS-063 Community benchmark import with provenance | verified | M5-P5-02, M5-P6-03 | partial | Provenance overlap; import is optional-tier. |

Coverage totals: 63/63 items mapped — 24 **covers**, 30 **partial** (of which
2 are additionally marked deferred: AS-022, AS-061), 7 **retained**, 2
**unresolved** (AS-043, AS-044).

## 4. Conflicts and resolutions

| ID | Conflict | Resolution |
|---|---|---|
| C1 | Two roadmaps claim source-of-truth; `AGENTS.md` names the AS roadmap; the M5 2.0 roadmap requires owner confirmation before replacement. | **Resolved 2026-10-30 (owner confirmation):** `product/M5_2_0_Autonomous_Roadmap.json` is the authoritative execution ledger for the M5 2.0 program. AS roadmap retained unmodified as historical source and AS-status ledger; `AGENTS.md` updated to name both roles. No history deleted. |
| C2 | Status vocabularies differ (`planned/in_progress/completed/verified` vs `not_started/in_progress/blocked/in_review/done/deferred/cancelled`). | Kept separate per ledger; no cross-conversion performed. Mapping table (§3) uses mapping states, not either vocabulary. |
| C3 | AS-043/AS-044 (external beta) have no autonomous counterpart. | Explicitly unresolved: external-human scope is outside the M5 2.0 autonomous story set. Owner decides later. |
| C4 | Naming collision: old milestone `M5` ("Open-source beta") vs product name "M5 2.0" / story prefix `M5-Pn-nn`. | Disambiguated in prose: "AS program milestone M5" vs "M5 2.0 stories". IDs never overlap. |
| C5 | AS program marks many security stories `verified` while the M5 2.0 evidence basis reports unverified bypasses (now audited). | Resolved factually by [`m5-2-0-integration-audit.md`](m5-2-0-integration-audit.md): both are true at different scopes — modules are unit-tested, while runtime integration and live isolation qualification are incomplete. README now states this explicitly. |
| C6 | AS-031 is `completed`, not `verified` (maintainer decision). | Consistent with M5-P5-03: release stays blocked until an authorized, signed review exists. No self-certification. |

## 5. Acceptance evidence (M5-P0-01)

1. **One authoritative roadmap identified** — §1: `product/M5_2_0_Autonomous_Roadmap.json`
   for the M5 2.0 program, with the AS ledger retained for AS status. The
   authority decision that the roadmap rule reserved for the owner was
   **confirmed by the user/owner on 2026-10-30** and is recorded in the
   roadmap's `source_of_truth` block and conflict C1.
2. **Every competing item mapped, deferred, or explicitly unresolved** — §3
   covers all 63 AS stories; AS-043/AS-044 are explicitly unresolved; UI-heavy
   items are marked deferred where the M5 2.0 non-goals apply.
3. **No history deleted** — `product/Adaptive_Runtime_Product_Roadmap_v2.json`
   is byte-identical to the pre-change checkout (git diff shows no change to
   it in this story's commit); the input draft
   [`README_GROUND_TRUTH_DRAFT.md`](../README_GROUND_TRUTH_DRAFT.md) is
   installed rather than discarded.

Commands run: `git diff --stat` (changed-file list in the story tracking),
`python3 -c "import json; json.load(open('product/M5_2_0_Autonomous_Roadmap.json'))"`
(schema parse, exit 0). No automated test covers the cross-roadmap mapping; the
mapping is documentation evidence, checked by reading §3 against both JSON
story lists.
