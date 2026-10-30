---
id: ADR-0002
title: execution-spine-boundaries
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0002: Extract narrow execution-spine boundaries (policy, action ledger, admission)

## Context

The M5 2.0 roadmap (stories `M5-P0-06`, `M5-P1-*`) requires auditable
persistence, policy, and action-admission boundaries for the autonomous
execution spine. The integration audit
(`docs/m5-2-0-integration-audit.md`) found the served application gates all
tool and MCP execution through one inline function (`app._run_authorized_tool`,
authorize -> durable audit -> execute) but expresses the policy, persistence,
and admission logic as inline code with no explicit seams; 37 contract modules
(including `side_effect_ledger.py`) remain unintegrated. A full monolith
rewrite is a roadmap non-goal, and behavior must stay stable while the seams
become testable and auditable.

## Decision

Introduce a stdlib-only module `execution_spine.py` defining three narrow
interfaces — `Policy` (authorization decision), `ActionLedger` (durable intent
and outcome records), and the composing `DurableActionBoundary`
(authorize -> record intent -> execute -> record outcome) — and wire the first
critical entry point (`app._run_authorized_tool`, which gates harness tools and
MCP tool calls) through thin adapters (`_AppToolPolicy`, `_AppActionLedger`)
over the existing inline policy (`authorize_tool_invocation`) and audit tables
(`tool_audit_events`). The module is inlined into the standalone artifact via
`scripts/build_studio.py` (`INLINED_MODULES`). Contract tests
(`tests/test_execution_spine.py`) pin the fail-closed guarantees (policy errors
deny; intent-record failure blocks dispatch; durable records survive result
loss; approval is explicit). Coverage is explicitly partial: the maintained
inventory of entry points not yet crossing these boundaries is
`docs/m5-2-0-bypass-inventory.md`, and complete coverage is not claimed until
independently checked.

## Alternatives

- **Status quo (inline seams in `app.py`)**: not chosen — the guarantees are
  only testable indirectly through the ~460 KB application module, and the
  M5 2.0 stories require named, auditable boundaries.
- **Wire `side_effect_ledger.py` as the persistence seam now**: deferred to
  `M5-P1-03` — it addresses non-idempotent external-effect idempotency, a
  different guarantee than the tool-attempt audit trail; folding both together
  would enlarge this change beyond "minimal extraction".
- **Extract the boundaries into a package with dependency injection
  containers**: not chosen — over-engineering for two adapters and three
  protocols; conflicts with the no-bloat, small-footprint mandate.
- **Rewrite the tool loop around the new boundaries (full spine)**: rejected
  by the roadmap non-goal "no full monolith rewrite"; `M5-P1-*` migrates
  further entry points incrementally.

## Tradeoffs

- **Benefits**: the authorize/record/execute ordering and fail-closed
  guarantees become unit-testable in isolation; future entry points migrate
  against a named interface; the audit trail's role is explicit.
- **Costs/risks**: one more inlined module in the standalone build (handled by
  `INLINED_MODULES`; `smoke_local.py --standalone` verifies self-containment);
  two adapter layers could drift from the inline policy if one is edited
  without the other (mitigated by the full suite exercising `_run_authorized_tool`);
  the boundaries are only as strong as their adapters — the SQLite ledger and
  inline policy remain single points of failure until `M5-P1-01`/`M5-P1-03`.
- **Verified facts**: extraction is behavior-preserving under the full suite
  (717 pre-existing tests) and the 11 new contract tests. **Assumptions**:
  that later stories will route new entry points through these seams rather
  than adding parallel ones.

## Review Status

Not reviewed. Record review status separately from decision status. When
reviewed, identify the reviewer or review role and date in both the metadata
and this section.
