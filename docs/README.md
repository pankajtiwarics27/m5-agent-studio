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
| [`chat-tool-calls.md`](chat-tool-calls.md) | Chat tool protocols, MCP inventory, read-only policy, and troubleshooting |
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
| [`m5-2-0-security-release-gate.md`](m5-2-0-security-release-gate.md) | Executed M5-P5-03 crash/race/MCP/adversarial gate, measured metrics, residual risks, and `release-blocked` decision |
| [`sandbox-boundary.md`](sandbox-boundary.md) | Sandbox and filesystem boundary hardening |
| [`security-incident-containment.md`](security-incident-containment.md) | Security dependency graphs, configuration generations, incident suspension, fallback qualification, and post-incident requalification |
| [`execution-profiles.md`](execution-profiles.md) | Unified capability descriptors, conservative footprints, admission barriers, and expansion reconciliation |
| [`mcp-idempotency.md`](mcp-idempotency.md) | MCP connector idempotency semantics, queryable-outcome reconciliation, and uncertain-write safe pause |
| [`qualification-suite.md`](qualification-suite.md) | Operator-run sandbox and hardware qualification (what is measured, how to run it, how to read the record) |

## Delivery and verification operations

| Page | Contents |
|---|---|
| [`verification-efficiency.md`](verification-efficiency.md) | Risk-based local/CI selection, evidence reuse rules, and the asynchronous landing policy |
| [`verification-critical-path-plan.md`](verification-critical-path-plan.md) | Measured CI critical path, sharding qualification, and retained experiment evidence |
| [`async-delivery-pipeline.md`](async-delivery-pipeline.md) | Builder/lander handoff, one-final-candidate runbook, measured AP-005 duplication, and fail-closed exact-tree reuse design |

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
[`execution-budgets.md`](execution-budgets.md),
[`mission-authority-envelope.md`](mission-authority-envelope.md) (immutable task/root generations and atomic leaf/root tool admission),
[`side-effect-idempotency.md`](side-effect-idempotency.md),
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
[`local-runtime-lifecycle.md`](local-runtime-lifecycle.md),
[`lmstudio-native-bridge.md`](lmstudio-native-bridge.md) (dormant LC-06 instance lifecycle/routing; no installed qualification),
[`ollama-native-bridge.md`](ollama-native-bridge.md) (unactivated LC-05 native protocol/lifecycle; no installed qualification),
[`omlx-lifecycle-bridge.md`](omlx-lifecycle-bridge.md) (unactivated LC-04 oMLX adapter; native qualification still unknown),
[`shared-memory-admission.md`](shared-memory-admission.md) (dormant LC-03 combined model/action accounting; not active admission),
[`model-lifecycle-coordinator.md`](model-lifecycle-coordinator.md) (dormant LC-02 durable journal/queue; no active model control),
[`model-lifecycle-research-and-proposal.md`](model-lifecycle-research-and-proposal.md) (cross-runtime audit, published research, and unimplemented single-resident coordinator proposal),
[`lc08-workspace-suggestion-audit.md`](lc08-workspace-suggestion-audit.md) (bounded workspace suggestion consent/input/close/no-action evidence; no activation),
[`lc08-legacy-cleanup-audit.md`](lc08-legacy-cleanup-audit.md) (legacy online cleanup failure refusal; HTTP success is not physical release evidence),
[`lc08-direct-unload-route-audit.md`](lc08-direct-unload-route-audit.md) (direct unload route failure privacy; no physical-release claim),
[`lc08-direct-load-route-audit.md`](lc08-direct-load-route-audit.md) (direct load route failure privacy; no physical-load claim),
[`lc08-unload-all-route-audit.md`](lc08-unload-all-route-audit.md) (unload-all inventory/per-model failure privacy; no physical-release claim),
[`lc08-cache-clear-route-audit.md`](lc08-cache-clear-route-audit.md) (cache-clear response privacy; no disk/cache deletion claim),
[`lc08-stream-any-model-audit.md`](lc08-stream-any-model-audit.md) (bounded combo-dispatch delegated-source close; no activation or native qualification),
[`lc08-sdlc-sprints-stream-audit.md`](lc08-sdlc-sprints-stream-audit.md) (bounded sprint worker/critic delegated-model-source close; no activation or native qualification),
[`lc08-agile-sdlc-factory-audit.md`](lc08-agile-sdlc-factory-audit.md) (bounded Agile SDLC factory phase/sprint delegated-source close; no activation or native qualification),
[`lc08-sdlc-feedback-loop-audit.md`](lc08-sdlc-feedback-loop-audit.md) (bounded SDLC feedback router delegated-source close; no activation or native qualification),
[`lc08-gsd-fresh-wave-audit.md`](lc08-gsd-fresh-wave-audit.md) (bounded GSD fresh-wave plan/execute/verdict delegated-source close; no activation or native qualification),
[`lc08-harness-turn-audit.md`](lc08-harness-turn-audit.md) (bounded Harness turn per-step delegated-model-source close; no activation or native qualification),
[`lc08-probe-stream-audit.md`](lc08-probe-stream-audit.md) (bounded audit of the model-switch probe source: no consumer-close gap exists in a coroutine with no yield boundary; regression pins only, no activation),
[`lc08-mcp-summon-audit.md`](lc08-mcp-summon-audit.md) (bounded MCP stdio input/close/admission/cleanup audit; no activation),
[`lc08-hall-summon-audit.md`](lc08-hall-summon-audit.md) (bounded Hall HTTP input/retry/close/cleanup evidence; no activation),
[`lc08-current-status.md`](lc08-current-status.md) (current dormant LC08 implementation, remaining gaps, evidence and next bounded increment),
[`cross-runtime-model-lifecycle-plan.md`](cross-runtime-model-lifecycle-plan.md) (researched implementation sequence, safety contracts, capability gates, and rollout plan; not implemented),
[`model-lifecycle-capability-feasibility.md`](model-lifecycle-capability-feasibility.md) (LC-00 pinned capability matrix, offline synthetic spike, and unresolved qualification gates),
[`moneyprinter-turbo-integration.md`](moneyprinter-turbo-integration.md).

Execution and policy: [`routing-consent-policy.md`](routing-consent-policy.md),
[`capability-negotiation.md`](capability-negotiation.md),
[`resource-admission-scheduler.md`](resource-admission-scheduler.md),
[`model-admission.md`](model-admission.md),
[`execution-profiles.md`](execution-profiles.md),
[`execution-budgets.md`](execution-budgets.md),
[`mission-authority-envelope.md`](mission-authority-envelope.md),
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
· [`../product/M5_2_0_Autonomous_Roadmap.json`](../product/M5_2_0_Autonomous_Roadmap.json) (M5 2.0 autonomous-runtime program ledger)
· [`../product/M5_2_0_UIUX_Autonomous_Roadmap.json`](../product/M5_2_0_UIUX_Autonomous_Roadmap.json) (M5 2.0 UI/UX program ledger, 40 stories)
· [`../product/M5_Autonomous_Production_Roadmap.json`](../product/M5_Autonomous_Production_Roadmap.json) (authoritative AP tracking ledger; downstream stories remain separately authorized)
· [`autonomous-production-roadmap.md`](autonomous-production-roadmap.md) (AP requirements and dependency graph)
· [`autonomous-production-ux-direction.md`](autonomous-production-ux-direction.md) (owner-selected concept direction, not implementation approval)
· [`autonomous-production-ap001-reconciliation.md`](autonomous-production-ap001-reconciliation.md) (AP-001 source/caller/test gap map, ownership reconciliation and claim convention)
· [`autonomous-production-operating-envelope.md`](autonomous-production-operating-envelope.md) (AP-002 U01–U06 register, target-host inventory recipe, fictional deny-by-default policy/budget examples and promotion boundaries)
· [`autonomous-production-evaluation-briefs.md`](autonomous-production-evaluation-briefs.md) (AP-003 four-part quality rubric, provisional baseline/partitions, protected held-out boundary, synthetic failure fixtures and private-trace scrub contract)
· [`autonomous-production-u06-call-path-audit.md`](autonomous-production-u06-call-path-audit.md) (read-only U06 call-path audit of lifecycle/sandbox refusal paths; source-level evidence, not a qualification or authorization)
· [`autonomous-production-research.md`](autonomous-production-research.md) (AP-009 source-backed safe search, reading and repository inspection; offline contract module; not activated)
· [`autonomous-production-browser-worker.md`](autonomous-production-browser-worker.md) (AP-010 governed browser worker: per-project isolation, takeover, grant confinement, hostile-page refusal and recovery; real-browser evidence; not activated)
· [`autonomous-production-discovery.md`](autonomous-production-discovery.md) (AP-011 capability-gap classification, candidate proposals and license assessment; offline, no install or activation authority)
· [`autonomous-production-experiment-lab.md`](autonomous-production-experiment-lab.md) (AP-012 delivered: disposable experiment lab with a fail-closed host-qualification gate, grant-gated trial admission, containment refusals, trial receipts and quarantine; nothing executes in the module; not activated)
· [`model-capability-profiles.md`](model-capability-profiles.md) (AP-013 measured model capability and compatibility profiles: configuration-pinned measurements, claims that never qualify, explicit unknown with bounded overrides, dense/MoE memory arithmetic; offline and advisory, not activated)
· [`routing-decision-records.md`](routing-decision-records.md) (AP-014 policy-aware routing and resource-safe scheduling: five ordered gates with unreached gates recorded, hosted fallback refused with a scoped limitation, sequential residency, charged-vs-observed memory accounting; dispatches nothing, not activated)
· [`continuation-contracts.md`](continuation-contracts.md) (AP-015 continuity checklist for compression, retrieval and handoff: required state survives or is reconstructed, probes test decisions not readability, recall keeps provenance and project isolation, no KV-cache portability; offline, not activated)
· [`challenger-evaluation.md`](challenger-evaluation.md) (AP-016 reproducible challenger and workflow evaluations: pinned run records with failures kept, comparable budgets, scores that need deterministic and independent evidence, no-op incumbent and held-out tasks, fail-closed scorers; offline, not activated)
· [`bounded-specialists.md`](bounded-specialists.md) (AP-017 bounded specialists and versioned handoffs: strict parent-subset authority and budget, versioned handoff artifacts with disjoint write claims, durable and bounded dispatch/join with restart reconciliation, reviewer briefs that cannot ask for endorsement, logical parallelism separated from local inference, and topology retention only on measured trade-off; offline, not activated)
· [`ap019-scoped-feedback.md`](ap019-scoped-feedback.md) (AP-019 lightweight scoped review and preference corrections: the scope a correction was given, never wider; confirmation of what the words meant that is explicitly not acceptance; stale feedback shown rather than applied; conflicts shown rather than averaged; the original wording kept, and a record that names the run it belongs to)
· [`ap020-reference-benchmarks.md`](ap020-reference-benchmarks.md) (AP-020 versioned owner-approved reference benchmarks: one approved artifact version and exactly what it settles; the four grants kept separate; a machine cannot approve; an enhancement is a new version and the earlier acceptance is never rewritten; retrieval by audience, language, task and style that never scores layout; and coverage read from the store rather than the request body)
· [`ap021-website-draft-workflow.md`](ap021-website-draft-workflow.md) (AP-021 a complete website draft workflow: an audience and an explicit out-of-scope before work starts; every requested interaction carries a verdict and "unsupported" is an answer while silence is not; no unsourced testimonial, logo or integration badge; assets that name their source, their licence, or the person who must resolve unknown rights; a bounded repair loop whose exhaustion returns a visible partial rather than an exception; and deployment, DNS and production credentials as constants that cannot be set)
· [`ap022-website-quality-qualification.md`](ap022-website-quality-qualification.md) (AP-022 qualifying website output quality and delivery: a check that did not run is not a pass, and a report nobody signed is not a delivery; six layout cells measured separately so a broken phone layout cannot hide behind a passing desktop one; secrets and placeholder copy scanned by the module rather than trusted to the probe; creative usefulness left to a named person; and an empty claims list so the report states which unmeasurable claims it refused to invent)
· [`ap023-sourced-video-drafts.md`](ap023-sourced-video-drafts.md) (AP-023 sourced original short-video drafts: "trending" is a dated, regional, sourced claim or it is a labelled candidate, and a not-trending sentence is not punished for being honest; a contradiction deleted between research and narration is refused; a scene that serves nothing approved is drift and a script against a changed direction needs a named owner; every asset composes AP-021's rights vocabulary and unauthorised voice cloning is unrecordable; the three existing MoneyPrinterTurbo surfaces are audited before a fourth is written; and the draft ships a playable file with captions that line up while upload stays a separate grant)
· [`ap024-video-quality-qualification.md`](ap024-video-quality-qualification.md) (AP-024 qualifying video technical and editorial quality: black, frozen and missing frames counted separately with a position, and a check that never ran blocking like one that failed; loudness bands and a true-peak ceiling with clipping counted on its own; four editorial axes scored against written anchors instead of one composite number a nobody can audit; watched coverage measured as ranges so a final review of the first thirty seconds is refused and a sample is visible as one; rights and consequential claims re-checked at the last gate against the research bundle; and seven monetisation, virality and platform-compliance guarantees refused by name)
· [`ap025-evidence-backed-skills.md`](ap025-evidence-backed-skills.md) (AP-025 extracting evidence-backed reusable skills: one verified success is task-local and sharing needs independent sessions, public data and a matched comparison against the incumbent; every skill states its prerequisites, permissions, a test example and its limitations or it is not recorded; failures are retained as context and structurally cannot be rendered as guidance; permissions come from the existing grant taxonomy so extraction cannot launder authority; and a quarantine stops the next retrieval while the task that already pinned a version keeps it)
· [`ap026-improvement-lab.md`](ap026-improvement-lab.md) (AP-026 researching model, paper and tool improvements: a watcher exists only with a namespaced human grant naming its hosts, cap and expiry, and the cap is checked before the fetch; a candidate is an exact version with an access date, a licence and stated limitations, never "latest"; an experiment needs a failing trace, a measured baseline and a threshold it could miss, with compatibility and download/memory checks running before the trial; held-out tasks, evaluators and authority controls are fingerprinted so the optimiser cannot edit its own exam and a removal counts as a change; only prompt, skill and config surfaces are optimisable by name while weights, fine-tuning and RL rollout are refused, and every report states weights_changed: false; a candidate naming a hosted dependency never reaches the trialler, and ambiguity about hosting is refused rather than resolved. Offline; no route consumes it and no research was actually performed)
· [`ap027-bounded-refinement.md`](ap027-bounded-refinement.md) (AP-027 bounded refinement with incumbent preservation and stopping: the unchanged output is on every ballot exactly once, a "revision" carrying the incumbent's digest is refused as a no-op, and the accepted version must defend itself each round; a verdict names its model family and session so self-grading is refused and one-session comparisons are one opinion, with every round recording bias as `possible` or `mitigated_by_independent_context` and never `none`; factual, functional and safety requirements are hard by kind, so a failed check makes the variant ineligible before its style score is read and a pass with no evidence is refused; rounds, reviewer calls and tokens are budgeted across every reviewer and variant and checked before scoring; the stop is `goal_met`, `no_progress`, `budget_exhausted`, `rounds_exhausted`, `capability_gap` or `insufficient_evidence`, a change is justified only by a met goal, and the preserved version is the accepted one rather than the latest candidate; a recorded capability gap makes the result `partial` and `claim_success()` raises. Offline; no route consumes it)
· [`ap028-promotion-policy.md`](ap028-promotion-policy.md) (AP-028 promoting and rolling back improvements by risk and evidence: risk is classified from concrete effects, never from a risk level the proposer supplies, and only escalates, with the ceremony sized by the strongest effect; the four classes and their boundaries are read from the operating envelope, so owner decision U05 keeps every class manual without a hard-coded `False`, while an enabled class would still need owner-approved thresholds and held-out results tied to the candidate and evaluator digests; new egress, accounts, spend and sensitive defaults need a grant naming that exact effect and are not satisfied by an owner approval; authority controls are a closed set whose edit needs an independent reviewer and a canary; a canary regression must name the artifacts it withdraws or state that none existed, stops at its stage and rolls back to the recorded target; the ledger is append-only with no replace/delete method, unmentioned artifacts are never called valid, and in-flight tasks keep the version they pinned. Offline; no route consumes it and no promotion was performed)
· [`ap029-governed-missions.md`](ap029-governed-missions.md) (AP-029 governed recurring missions and external delivery: a schedule exists only with a human grant naming its project, cadence, expiry, run cap and specific purposes, so a `draft` grant says nothing about `publish`; a draft has no way to send itself and the only bridge to an external effect takes a separate matching grant; an external action binds account, destination, content hash, policy revision and expiry, and one changed byte invalidates it; retries reconcile rather than resend, with an unresolved probe or an absent idempotency key stopping the action instead of guessing; a restart on the same slot is the same run id and a duplicate delivery, a missed slot is recorded rather than back-filled, and an action re-recorded under a new idempotency key is refused as a different action wearing the same name; a global pause, per-project stop and named-recipient notification threshold stop runaway activity; and a suggestion may not claim causation without a named experiment or change public output at all. Offline; no route consumes it, nothing was scheduled and nothing was sent)
· [`guest-desktop.md`](guest-desktop.md) (AP-030 optional guest-desktop capability: per-task justification against simpler API/browser tools, platform qualification only from observation, host/guest boundary for files, clipboard, accounts, egress and screenshots, no host fallback on guest failure, and a shipped-disabled adapter that publishes its limitations; offline, not qualified)
· [`bounded-mission-execution.md`](bounded-mission-execution.md) (AP-007 offline fail-closed subset: mission stop with measured cleanup latency, late-result fence, bounded limits, isolation requirement and recovery report; U06 resolved 2026-11-17 by owner decision, no production caller, nothing activated)
· [`../M5_AUTONOMOUS_PRODUCTION_AGENT_PROMPT.md`](../M5_AUTONOMOUS_PRODUCTION_AGENT_PROMPT.md) (AP claim, evidence and handoff protocol)
· [`uiux-2-0-program.md`](uiux-2-0-program.md) (UI/UX phases, gates and evidence bar)
· [`uiux-2-0-transformation-plan.md`](uiux-2-0-transformation-plan.md) (what each UI/UX story changes, and how two sessions share one HTML file)
· [`uiux-2-0-control-inventory.md`](uiux-2-0-control-inventory.md) (generated inventory of every control in the six panes and the shared chrome)
· [`uiux-2-0-route-map.md`](uiux-2-0-route-map.md) (generated map of every frontend fetch call to the route it hits)
· [`uiux-2-0-authorization-trace.md`](uiux-2-0-authorization-trace.md) (generated trace of what can refuse each state-changing call, and what state it touches)
· [`uiux-2-0-pipeline-artifacts.md`](uiux-2-0-pipeline-artifacts.md) (generated P3-06 Pipeline layer: each stage card states what its seat was handed and what it produced - the upstream characters, the seat it feeds, its own chars/tokens/tok-s, and "produced nothing" when it did - and one live run line says how many seats finished, how much output the run produced, how many handoffs carried content, and whether the server saved it at all; a stopped pipeline is never given a run id)
· [`uiux-2-0-pipeline-recovery.md`](uiux-2-0-pipeline-recovery.md) (generated P3-07 Pipeline layer: a failed seat is a named state with the provider's own message and the output that survived; a stopped run and a dropped stream are drawn as different things; Retry re-posts the payload that failed and says all four seats run again because the server has no single-seat entry point; the salvage is kept in this browser, capped and never called the server's record, offered after a reload and restored without re-running anything; and a saved run says it is on record with the id the server returned and is read back through `GET /api/history/export`)
· [`uiux-2-0-council-participants.md`](uiux-2-0-council-participants.md) (generated P4-01 Council layer: each of the four seats names its role and says what the seat is for; a verdict read from `/api/telemetry` and `/api/providers` says READY, NEEDS LOAD, NEEDS MEMORY, NEEDS KEY, NOT LISTED or DEMO TEXT with the action that exists and the age of the reads; the rows and the round share one plan; a demo reply is labelled one and carries no tok/s badge; and a failed seat is a block naming the seat and quoting the provider instead of a toast)
· [`uiux-2-0-council-deliberation.md`](uiux-2-0-council-deliberation.md) (generated P4-02 Council layer: every stage card states what its seat was allowed - the token budget, the reasoning mode and the skills decision, published on `stage_start` rather than mirrored in the pane - and what it carried forward, which is `content or reasoning`; a reply the runtime cut at its length limit or at the run's budget says so, demo text carries no throughput figure anywhere in the pane, and one evidence table states allowed/given/produced/carried/verdict per seat with a copy-as-markdown control)
· [`uiux-2-0-council-verdict.md`](uiux-2-0-council-verdict.md) (generated P4-03 Council layer: a result states consensus, disagreement and the decision instead of pinning one blob of prose - the pinned conclusion is credited to the seat whose reply it is, every other reply is measured against it by shared wording (the fraction of its 4-word sequences the decision also contains, a text measure and labelled as one), replies the decision does not carry are named as dissent and left in the transcript, and a room whose seats answered with the same words says `N replies · M voices` rather than presenting a repeated answer as agreement)
· [`uiux-2-0-compare-criteria.md`](uiux-2-0-compare-criteria.md) (generated P4-04 Compare layer: the comparison states what it is judged on before it runs - three criteria the reply and the runtime's finish reason settle, `cost` with the reason it is empty, and the reader's own criteria as theirs to judge - then every panel gets one of ten statuses read from the response rather than the event, a stopped comparison keeps its partial reply and says it was stopped without claiming `finished`, and one results table reads each model against the criteria while refusing to price or rank the answers)
· [`uiux-2-0-compare-provenance.md`](uiux-2-0-compare-provenance.md) (generated P4-05 Compare layer: every figure the pane prints is a figure the response carried - elapsed time, time to first token, reply tokens and decode throughput read from the `usage` the server sends with each reply, and the run's own total from `debate_done` - while the page times nothing, a demo reply is given no figure at all instead of the demo path's invented throughput, a panel that never reported says why instead of showing a zero, and cost stays a stated refusal because no price is configured for any model)
· [`uiux-2-0-compare-continuity.md`](uiux-2-0-compare-continuity.md) (generated P4-06 Compare layer: a comparison survives the page that made it - `#debateRecord` states exactly one of four sources (`live`, this browser's copy, the server's redacted export, or *not kept here*), a restored comparison never passes for a live run and names the models the list no longer offers, the run id from `debate_done` is either stated as `run N` or as `the server did not save this run`, the copy this browser keeps is capped with its truncation stated, and the whole comparison leaves the page as markdown or JSON with every reply and the arbiter kept separate and no winner picked)
· [`uiux-2-0-terminology.md`](uiux-2-0-terminology.md) (generated P5-01 cross-pane layer: one `UI_STATUS` table names every state once, with its word and its tone, so the same failed run is `failed` in all six panes instead of `FAILED`, `ERROR` and `Check failed`; the server's own state names are mapped onto that table rather than restated, a state it has never seen keeps the server's word instead of a wrong one, the words that are deliberately *not* statuses are listed with their reasons, and `.status` no longer inherits the uppercase of the heading above it)
· [`uiux-2-0-keyboard-a11y.md`](uiux-2-0-keyboard-a11y.md) (generated P5-02 keyboard and screen-reader layer: every stop in each pane's tab order is walked with real Tab presses and asked for its accessible name, its visible focus ring and its size; the two composers draw focus on the box because their textarea's own ring is off, the starter cards, preset chips, skill pills and session rows are `role="button"` controls that Enter and Space press through one delegated handler instead of an inline attribute, the event stream that takes focus to be scrolled draws a ring, and the dismiss-only backdrop is the one click target with no tab stop, with the reason stated)
· [`uiux-2-0-responsive.md`](uiux-2-0-responsive.md) (generated P5-03 layout layer: the six panes are measured at 1440, 1024, 768, 480 and 360 with the document's sideways overflow, every control's on-screen position and every hit area read off the rendered box; a `<select>` may no longer set the width of its row, a centred pane shell has a definite width, `--target-min: 24px` is the floor a finger is given and density does not move it, the one strip that is allowed to overflow scrolls and the walk names the scrollers it relied on, and the six breakpoints are a documented list rather than a drifting set of widths)
· [`uiux-2-0-stream-performance.md`](uiux-2-0-stream-performance.md) (generated P5-04 streaming layer: a token costs the message being written rather than the conversation around it — the transcript compares its own shape and rebuilds one node, a burst of tokens is coalesced into one render per frame while the frames that end a turn render immediately, the harness stream draws the newest 300 cards of the log and says how many it is holding back and that Replay re-reads the whole log from the server, a live event is one card in and one card out unless an approval changed or the window moved under it, and the client cache holds 5,000 events while `harnessCursor` and `retained_from_seq` stay the truth)
· [`uiux-2-0-polish.md`](uiux-2-0-polish.md) (generated P5-05 polish layer: every corner is a `--radius-*` token and every duration a `--dur-*` one, `transition: all` is gone, every pressable kind has a `:hover` that paints and an `:active` that moves, every focusable control draws the shared ring with the composers' box-drawn ring kept and documented, a disabled control says `not-allowed` without fading past reading, and the reduced-motion block still neutralises all of it; the walk that renders it walks both themes and captures dark and light at 1440x900 and 360x740)
· [`uiux-2-0-interaction-polish.md`](uiux-2-0-interaction-polish.md) (supplemental P5-05 evidence: six panes in dark/light desktop and narrow viewports, measured toolbar hover/focus parity, 72 indexed screenshots, and a distinct Compare-history focus-continuity regression)
· [`uiux-2-0-preservation-e2e.md`](uiux-2-0-preservation-e2e.md) (generated P6-01 preservation map: the ledger's 21 must-preserve rows each name the walk checks, pytest nodes, route probes and wired controls that prove them, and `scripts/preservation_suite.py` executes exactly that list - one throwaway server, every route refused without a token and answered with one, the named tests, then every walk - printing one line per row so "preserved" is the output of a command rather than a reading of a page)
· [`uiux-2-0-adversarial.md`](uiux-2-0-adversarial.md) (generated P6-02 refusal map: every status the security boundary and the handlers can answer a write with, the server's own words for each (checked verbatim against `app.py`), what the pane promises to keep, and the proof that both halves hold - plus every mutating `fetch` in the shell sorted into guarded, self-reporting or a finding, so a write that reads the body and never the status cannot hide)
· [`uiux-2-0-screenshot-review.md`](uiux-2-0-screenshot-review.md) (generated P6-03 review: every committed capture across the program traced to the script that made it, checked against the index beside it, measured for the theme its name claims, and read by a person who wrote down what it shows - plus the re-captured published set, the before/after of this story, and the coverage grid that says which pane has never been photographed in which theme or width)
· [`uiux-2-0-readme-onboarding.md`](uiux-2-0-readme-onboarding.md) (generated P6-04 verification: the README, Quickstart, demo page, onboarding contract and Start page checked against the tree - every relative link resolving, every fenced command classified with a reason and the read-only ones executed, and every claim about the preflight, the port, the token file or the generated bundle compared with the source that owns it)
· [`uiux-2-0-release-gate.md`](uiux-2-0-release-gate.md) (generated P6-05 release gate: every artifact's `--check` executed with its exit code, the seven phase gates computed from the stories, the five acceptance criteria mapped to the artifact or CI job that answers each, and every recorded known regression carried with the story that found it) · [`uiux-2-0-handover.md`](uiux-2-0-handover.md) (the handover report generated from the same payload: what changed, what held, how to verify it in five minutes, and what is not claimed)
· [`uiux-2-0-baseline-evidence.md`](uiux-2-0-baseline-evidence.md) (measured baseline: suite results with exit codes, and the capture plan that needs a browser)
· [`uiux-2-0-p0-signoff.md`](uiux-2-0-p0-signoff.md) (generated P0-06 package: what must be preserved, controls, routes, boundary and rollback — approved by the owner 2026-10-31)
· [`uiux-2-0-tokens.md`](uiux-2-0-tokens.md) (generated P1-01 token layer: the values P1 builds on, with themes, density and measured contrast)
· [`uiux-2-0-navigation-shell.md`](uiux-2-0-navigation-shell.md) (P1-02 shell contract: tablist semantics, keyboard map and the browser-driven keyboard walk)
· [`uiux-2-0-status-and-evidence.md`](uiux-2-0-status-and-evidence.md) (generated P1-03 component layer: six status tones, four evidence bases, the ten harness states and measured contrast)
· [`uiux-2-0-overlays.md`](uiux-2-0-overlays.md) (generated P1-04 overlay layer: nine dialogs on one controller, the shortcut registry and the inspector drawer)
· [`uiux-2-0-overlays-keyboard.md`](uiux-2-0-overlays-keyboard.md) (P1-04 notification semantics, toast dismissal/timing, and command-palette combobox behavior)
· [`uiux-2-0-presentations.md`](uiux-2-0-presentations.md) (generated P1-05 presentation layer: one shape for empty, error and recovery, with the cause and the kept state as required arguments)
· [`uiux-2-0-start.md`](uiux-2-0-start.md) (generated P2-01 Start layer: the server's next step printed once, the marked row, and why Run is off)
· [`uiux-2-0-recent-work.md`](uiux-2-0-recent-work.md) (generated P2-02 Start layer: recent Harness sessions as buttons, every run state toned, and a task handoff that runs nothing)
· [`uiux-2-0-start-health.md`](uiux-2-0-start-health.md) (generated P2-03 Start layer: four health rows from the payload the monitor rail fetched, the demo payload labelled unverified rather than trusted, and one workspace setting written through the Harness's own route)
· [`uiux-2-0-chat.md`](uiux-2-0-chat.md) (generated P2-04 Chat layer: every turn labelled with its role, number and model, a transcript that follows the reader instead of the stream, and a composer that says what Send will do)
· [`uiux-2-0-chat-context.md`](uiux-2-0-chat-context.md) (generated P2-05 Chat layer: the context every reply was sent printed on it, tool calls with a status word, a step, a time and their whole arguments, and a streaming clock that is measured rather than guessed)
· [`uiux-2-0-chat-handoff.md`](uiux-2-0-chat-handoff.md) (generated P2-06 crossing: Chat carries the task, the counts, the recorded context and every tool call into the Harness composer, and stops there; one sentence, one button, and a way back)
· [`uiux-2-0-harness-settings.md`](uiux-2-0-harness-settings.md) (generated P3-01 Harness layer: what Run will do printed above it from the controls the payload reads, the collapsed advanced group stating its own contents and any change in words, one predicate for Run's state and its sentence, and a banner that answers a brief carried from Chat)
· [`uiux-2-0-harness-timeline.md`](uiux-2-0-harness-timeline.md) (generated P3-02 Harness layer: the trajectory declaring itself a view of a durable log - a strip that states the count, the seq range, the cursor and how the view was filled, one row per held event that points at its card, every retained event drawn including the 25 types that used to render as nothing, a Replay that rebuilds from the log alone, and a frame that no longer moves a reader who is scrolled up)
· [`uiux-2-0-harness-permissions.md`](uiux-2-0-harness-permissions.md) (generated P3-03 Harness layer: an approval stating the permission it records before the click - the policy asking, the tool and its exact arguments, the capability and resource, the one-use limit and the minute it expires - a decision sent with the session that owns it, and a permissions surface that lists what a session holds, the revocation rules that refuse matching actions, and the approvals still waiting, with a revoke that says the rule outlives the grant)
· [`uiux-p3-03-approval-evidence.md`](uiux-p3-03-approval-evidence.md) (supplemental P3-03 fail-closed check: reconfirm a same-session pending approval before sending the existing decision request)
· [`uiux-2-0-harness-recovery.md`](uiux-2-0-harness-recovery.md) (generated P3-05 Harness layer: a stopped run drawn from the record - the state, its reason, the checkpoint it kept, the recovery memory, the budget and the containment holds - with pause, resume, continue, cancel and abort as real presses, every refusal stated as a disabled action carrying the runtime's own reason, and the three recovery events drawn as cards instead of one-line reasons)
· [`uiux-2-0-harness-evidence.md`](uiux-2-0-harness-evidence.md) (generated P3-04 Harness layer: a completion claim drawn as the evidence behind it - one card for the runtime's three outcomes that states the contract digest, the command and its exit code, the revision, the files and the patch - and the record it opens: the contract the session set, the observation the runtime wrote with the criteria an observed test actually covered, the change counted per file from the same diff the dialog draws, the checkpoints and revision a re-run would be tied to, and an absent verdict, contract or diff drawn as an absence)· [`../M5_2_0_UIUX_AGENT_PROMPT.md`](../M5_2_0_UIUX_AGENT_PROMPT.md) (UI/UX claim, review and merge protocol)
· [`uiux-2-0-coordination.md`](uiux-2-0-coordination.md) (read-only readiness check, current handoff and capture risks)
· [`uiux-coordination-ledger-drift-followup.md`](uiux-coordination-ledger-drift-followup.md) (reconciled UI/UX phase summaries and status-walk token-exposure follow-up)
· [`uiv2-v2-accessibility-adversarial.md`](uiv2-v2-accessibility-adversarial.md) (source-bound UIV2-28 keyboard, measured-contrast, 200% zoom, reduced-motion and nested-scroll recovery contract)
· [`uiv2-v2-accessibility-adversarial-evidence.md`](uiv2-v2-accessibility-adversarial-evidence.md) (executed before/after, full visual-matrix and preservation evidence, with explicit approval status)
· [`uiv2-v2-performance-safety.md`](uiv2-v2-performance-safety.md) (source-bound UIV2-29 real-SSE, frame-budget, post-GC DOM/heap, layout-stability, authorization, recovery, ledger, fencing and fail-closed isolation evidence)
· [`uiv2-v2-release-review.md`](uiv2-v2-release-review.md) (generated UIV2-30 final SHA, approved screenshot/CI evidence, truthful README scope, corrections, remaining defects, rollback and exact agent handoff; UI handoff is distinct from the production security release)

## M5 2.0 ground-truth evidence (2026-10-30)

[`m5-2-0-roadmap-reconciliation.md`](m5-2-0-roadmap-reconciliation.md) (how
the two roadmaps map to each other) ·
[`m5-2-0-integration-audit.md`](m5-2-0-integration-audit.md) (verified call
paths, module integration status) ·
[`m5-2-0-bypass-inventory.md`](m5-2-0-bypass-inventory.md) (maintained
known-bypass inventory for the extracted execution boundary, ADR-0002) ·
[`m5-2-0-baseline-evidence.md`](m5-2-0-baseline-evidence.md) (commands, exit
codes, blocked measurements) ·
[`m5-2-0-hardware-qualification-evidence.md`](m5-2-0-hardware-qualification-evidence.md)
(operator-run 24 GB Apple M5 hardware qualification for `M5-P4-01`, 2026-10-31) ·
[`m5-2-0-sandbox-isolation-evidence.md`](m5-2-0-sandbox-isolation-evidence.md)
(the operator run that qualified container isolation on the target host,
`M5-P3-01`, with the residual it measures) ·
[`m5-2-0-sandbox-memory-limit-finding.md`](m5-2-0-sandbox-memory-limit-finding.md)
(the earlier memory-limit failure that run supersedes, kept as recorded) ·
[`m5-2-0-security-release-gate.md`](m5-2-0-security-release-gate.md)
(executed `M5-P5-03` fault-injection & adversarial release gate, metrics, residual risks, and `release-blocked` decision) ·
[`../M5_2_0_AGENT_PROMPT.md`](../M5_2_0_AGENT_PROMPT.md) (parallel-agent
prompt and the multi-agent claim/review/merge protocol).

## Keeping this set honest

```bash
python scripts/validate_docs.py    # required pages, sections, links, no placeholders
```

Adding a required page means adding it to `REQUIRED_DOCS` in that script and
linking it here or from `README.md`; otherwise CI fails with "written but not
published".
