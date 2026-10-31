# Documentation

Start here, then follow the pointer you need. Every page below is validated in
CI by `scripts/validate_docs.py`: required sections must exist, no page may
contain placeholder text, and every relative link must resolve.

## Getting started

| Page | Read it when |
|---|---|
| [`quickstart.md`](quickstart.md) | Installing, first run, connecting a model, first task, troubleshooting |
| [`demo.md`](demo.md) | One-command reproducible demo, the committed transcript, and the screenshot set |
| [`onboarding-preflight.md`](onboarding-preflight.md) | You want to know what the Start tab checks and why |
| [`../README.md`](../README.md) | You want the full reference: configuration, API, tools, database |
| [`../BASELINE.md`](../BASELINE.md) | You want the pinned baseline and a clean-checkout reproduction |

## Understanding the system

| Page | Contents |
|---|---|
| [`architecture.md`](architecture.md) | Components, request path, storage, generated artifacts, extension seams |
| [`subsystem-dependency-inventory.md`](subsystem-dependency-inventory.md) | Generated: every endpoint, table, and outbound boundary |
| [`route-security-matrix.md`](route-security-matrix.md) | Generated: per-route authentication expectations |
| [`../docs/adr/README.md`](adr/README.md) | Architecture decision records |
| [`supported-hardware.md`](supported-hardware.md) | Requirements, validated vs expected platforms, capability model |

## Operating it

| Page | Contents |
|---|---|
| [`packaging-rollback.md`](packaging-rollback.md) | Versioned install, update, rollback, artifact integrity |
| [`release-provenance.md`](release-provenance.md) | SBOM, provenance, scans, and the signing hook (unprovisioned) |
| [`migration-guide.md`](migration-guide.md) | Upgrading releases, schema migrations, credentials, restore |
| [`storage-migrations.md`](storage-migrations.md) | Storage seam and schema-migration invariants |
| [`configuration-contracts.md`](configuration-contracts.md) | Every setting, precedence, and validation rule |
| [`privacy.md`](privacy.md) | What stays local, what leaves the machine, retention, deletion |
| [`history-privacy.md`](history-privacy.md) | Export, retention, and redaction specifics |

## Security

| Page | Contents |
|---|---|
| [`../SECURITY.md`](../SECURITY.md) | Safe defaults, unsafe opt-ins, disclosure process |
| [`threat-model.md`](threat-model.md) | Actors, trust boundaries, abuse cases, mitigations |
| [`security-review-package.md`](security-review-package.md) | The independent-review package and boundary inventory (B1–B12) |
| [`security-review-signoff.md`](security-review-signoff.md) | Reviewer record and release gate — currently unsigned |
| [`sandbox-boundary.md`](sandbox-boundary.md) | Sandbox and filesystem boundary hardening |
| [`security-incident-containment.md`](security-incident-containment.md) | Security dependency graphs, configuration generations, incident suspension, fallback qualification, and post-incident requalification |
| [`execution-profiles.md`](execution-profiles.md) | Unified capability descriptors, conservative footprints, admission barriers, and expansion reconciliation |
| [`mcp-idempotency.md`](mcp-idempotency.md) | MCP connector idempotency semantics, queryable-outcome reconciliation, and uncertain-write safe pause |
| [`qualification-suite.md`](qualification-suite.md) | Operator-run sandbox and hardware qualification (what is measured, how to run it, how to read the record) |

## Subsystem contracts

Harness runtime: [`task-events.md`](task-events.md) (durable task events, cursors,
SSE replay, reload/resync, approval reconciliation),
[`version-aware-edits.md`](version-aware-edits.md) (content-version expectations,
compare-and-swap managed edits, durable edit transactions, conflict recovery),
[`edit-rollback.md`](edit-rollback.md) (durable edit batches, version-checked
inverse-patch rollback, action reconciliation, resumable recovery),
[`verification-evidence.md`](verification-evidence.md) (revision-anchored test
evidence, staged checks, baseline-aware failure classification, denial refusal,
declared acceptance coverage),
[`recovery-guard.md`](recovery-guard.md) (durable failure memory, stagnation and
oscillation verdicts, retry/replan/rollback budgets, safe pause),
[`workflow-checkpoints.md`](workflow-checkpoints.md),
[`execution-budgets.md`](execution-budgets.md), [`side-effect-idempotency.md`](side-effect-idempotency.md),
[`mcp-idempotency.md`](mcp-idempotency.md) (MCP idempotency binding, queryable-outcome reconciliation, uncertain-write safe pause),
[`bounded-child-delegation.md`](bounded-child-delegation.md) (child authority/budget subsets, disjoint write leases, durable parent joins, single-agent vs delegation benchmarks).

Model and provider: [`provider-adapters.md`](provider-adapters.md),
[`api-provider-contracts.md`](api-provider-contracts.md),
[`provider-switching.md`](provider-switching.md),
[`model-discovery-install.md`](model-discovery-install.md),
[`model-manifest-registry.md`](model-manifest-registry.md),
[`model-calibration.md`](model-calibration.md),
[`model-diagnostics.md`](model-diagnostics.md),
[`model-comparison.md`](model-comparison.md),
[`model-recommendation.md`](model-recommendation.md),
[`model-switching.md`](model-switching.md),
[`portable-handoff-switching.md`](portable-handoff-switching.md),
[`model-updates.md`](model-updates.md),
[`hardware-capability-schema.md`](hardware-capability-schema.md),
[`hardware-validation.md`](hardware-validation.md),
[`macos-hardware-profiler.md`](macos-hardware-profiler.md),
[`performance-profiles.md`](performance-profiles.md),
[`local-provider-adapters.md`](local-provider-adapters.md),
[`local-runtime-lifecycle.md`](local-runtime-lifecycle.md).

Execution and policy: [`routing-consent-policy.md`](routing-consent-policy.md),
[`capability-negotiation.md`](capability-negotiation.md),
[`resource-admission-scheduler.md`](resource-admission-scheduler.md),
[`model-admission.md`](model-admission.md),
[`execution-profiles.md`](execution-profiles.md),
[`execution-budgets.md`](execution-budgets.md),
[`tool-authorization.md`](tool-authorization.md),
[`side-effect-idempotency.md`](side-effect-idempotency.md),
[`session-device-trust.md`](session-device-trust.md),
[`private-network-gateway.md`](private-network-gateway.md).

Workflows and memory: [`workflow-checkpoints.md`](workflow-checkpoints.md),
[`workflow-orchestration.md`](workflow-orchestration.md),
[`workflow-state-machine.md`](workflow-state-machine.md),
[`workflow-tracing.md`](workflow-tracing.md),
[`workflow-evaluation.md`](workflow-evaluation.md),
[`context-reassembly.md`](context-reassembly.md),
[`failure-injection-suite.md`](failure-injection-suite.md),
[`comparison-board.md`](comparison-board.md),
[`benchmark-results.md`](benchmark-results.md),
[`community-benchmarks.md`](community-benchmarks.md) (importing other people's
measurements without trusting them).

## Project and process

[`product-roadmap-status.md`](product-roadmap-status.md) (generated story status)
· [`../IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md) (remediation depth)
· [`../AGENTS.md`](../AGENTS.md) (working rules for humans and agents)
· [`../CONTRIBUTING.md`](../CONTRIBUTING.md) · [`../CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)
· [`../product/Adaptive_Runtime_Product_Roadmap_v2.json`](../product/Adaptive_Runtime_Product_Roadmap_v2.json) (AS program story ledger)
· [`../product/M5_2_0_Autonomous_Roadmap.json`](../product/M5_2_0_Autonomous_Roadmap.json) (M5 2.0 autonomous-program ledger)

## M5 2.0 ground-truth evidence (2026-10-30)

[`m5-2-0-roadmap-reconciliation.md`](m5-2-0-roadmap-reconciliation.md) (how
the two roadmaps map to each other) ·
[`m5-2-0-integration-audit.md`](m5-2-0-integration-audit.md) (verified call
paths, module integration status) ·
[`m5-2-0-bypass-inventory.md`](m5-2-0-bypass-inventory.md) (maintained
known-bypass inventory for the extracted execution boundary, ADR-0002) ·
[`m5-2-0-baseline-evidence.md`](m5-2-0-baseline-evidence.md) (commands, exit
codes, blocked measurements) ·
[`../M5_2_0_AGENT_PROMPT.md`](../M5_2_0_AGENT_PROMPT.md) (parallel-agent
prompt and the multi-agent claim/review/merge protocol).

## Keeping this set honest

```bash
python scripts/validate_docs.py    # required pages, sections, links, no placeholders
```

Adding a required page means adding it to `REQUIRED_DOCS` in that script and
linking it here or from `README.md`; otherwise CI fails with "written but not
published".
