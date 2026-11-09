# AP-001 — Program reconciliation and production gap map

**Story:** AP-001 • **State:** done • **Originally claimed:** 2026-11-09T14:34:22Z • **Merged:** 2026-11-09T15:27:46Z

**Original claim base:** `2f91bd3a7d9e835818f31a9005d5fc648e207182` • **Merged-main refresh base:** `c0ab6892071e388a39f12533f4a130aeff11bbe9`

**Original implementation evidence head:** `f35417da46f2a0bd6c5e6364ff875987fb237ee7` • **Merged-main evidence head:** `6e7922e6bfa6524a4c636dc7527108c48f6172cb` • **Merged PR head:** `863ba601cd9e3537aac58906acef4502764e5c0f`

**Delivered claim:** [PR #275](https://github.com/pankajtiwarics27/m5-agent-studio/pull/275) • **Merge commit:** `6a7cd319a94e013258cd51cc7dd6c8559d2d78d7`

## 1. Authority, scope and pre-edit record

The repository owner instructed the originating session on 2026-11-09, after
merging PR [#273](https://github.com/pankajtiwarics27/m5-agent-studio/pull/273),
to start AP-001. The owner then selected **Continue** after that Arena branch
closed, authorizing preservation of the AP-001 audit on this continuation
branch. Those instructions authorize AP-001 reconciliation and program
registration only. They do not activate a runtime path, authorize a later AP
story, approve an external service, or settle decisions U01–U06.

The delivered story was claimed by `arena/6d69f2cd-m5-agent-studio`. It superseded
the interrupted claim on `arena/73573f93-m5-agent-studio`; draft PR
[#274](https://github.com/pankajtiwarics27/m5-agent-studio/pull/274) was closed
after replacement draft PR #275 became the remote coordination claim. Arena
fixes the branch name for the session, so the proposed `ap-XXX-short-slug`
convention could not rename that branch. Post-merge completion is recorded from
`arena/bf895de2-m5-agent-studio` without transferring the historical claim or
authorizing downstream work. A local ledger edit is not treated as an atomic
remote claim.

### Pre-edit blast radius

- **Objective and gap:** establish current source truth for AP requirements,
  classify existing capability as `reuse`, `extend`, `missing`, `dormant` or
  `blocked`, preserve owning-program boundaries, and register an AP
  tracking/claim convention. Named modules and unit tests do not by themselves
  prove live application integration.
- **Options considered:** (1) create a new control plane, rejected because the
  repository already owns authority, workflow, artifact, routing and lifecycle
  contracts; (2) mark similarly named capabilities complete, rejected because
  AP requires consumer evidence; (3) produce a documentation-only delta map and
  registration record, selected as the smallest AP-001-compliant change.
- **Affected files:** this reconciliation, `AGENTS.md`, `docs/README.md`, and
  AP-001/program-registration metadata in
  `product/M5_Autonomous_Production_Roadmap.json`. Product source, APIs,
  payloads, UI controls, persistence schemas and generated runtime artifacts
  remain unchanged.
- **Pinned tests and generated artifacts:** no pin is weakened. Documentation,
  governance, ledger invariants, source-bound checks and generated-artifact
  drift are validated. `studio.py` and generated UI/runtime inventories remain
  generator-owned.
- **Authority, data and resources:** no egress, credential access, private-data
  scope, side effect, provider, model load, spending or concurrent residency is
  added. Existing auth, grants, isolation and sequential lifecycle intent remain
  binding.
- **Compatibility and recovery:** the change is additive documentation and
  coordination metadata. There is no data migration or feature flag. Reverting
  this AP-001 change removes only AP registration/reconciliation; it must not
  rewrite existing program history or user data.
- **Evidence limits:** static callers and offline tests establish contract and
  wiring evidence only. They do not establish installed runtime/sandbox
  qualification, physical model release, visual owner acceptance, independent
  security acceptance, or real media quality.

## 2. Audit method and classification rules

This review inspected the current base, all five program ledgers, relevant
runtime/LC/UI release records, open pull requests, source imports and call sites,
and dedicated tests. An AST import/reverse-consumer scan found **36 project
modules imported directly by `app.py`**. Import presence was then checked
against actual constructors, routes and calls; a transitive import was not
counted as active composition.

The classifications mean:

| Label | Meaning in this audit |
|---|---|
| `reuse` | A current application caller reaches the capability through its authority boundary and a result consumer; dedicated tests exist. This does **not** make the AP story complete. |
| `extend` | A live or durable foundation exists, but AP needs broader state, authority coverage, consumers or acceptance evidence. |
| `dormant` | A module, test suite or catalog registration exists, but no default application producer/executor/consumer path activates it. |
| `missing` | No current end-to-end owning contract or consumer satisfies the named capability. Reusable lower-level primitives may still exist. |
| `blocked` | Real implementation or activation is prohibited by an unresolved owner decision, installed qualification, independent gate, dependency, or active owning workstream. |

Evidence levels remain separate:

1. **module exists** — source can be imported;
2. **unit/contract tested** — synthetic tests exercise the module;
3. **application integrated** — a served route or current workflow calls it and
   consumes the result under authority;
4. **installed qualified** — the exact host/runtime/configuration was observed;
5. **owner/independent accepted** — the required human accepted the exact
   implementation/evidence.

A higher level is never inferred from a lower one.

## 3. Existing program ownership and current claims

The AP ledger is additive. It neither replaces nor normalizes another program's
status vocabulary.

| Program | Authoritative source and current state at the audit base | Ownership preserved by AP-001 |
|---|---|---|
| AS | `product/Adaptive_Runtime_Product_Roadmap_v2.json`: 63 stories — 46 `verified`, 11 `completed`, 6 `planned`; no active claim. | Historical AS status remains in that ledger. The six planned stories are not silently closed or reopened. AS-031 remains completed/deferred, not independently verified. |
| M5 runtime | `product/M5_2_0_Autonomous_Roadmap.json`: 34/34 `done`; no active story. | Done story records remain untouched. External security/signing and host-bound qualification obligations still apply even though story tracking is complete. |
| UIUX | `product/M5_2_0_UIUX_Autonomous_Roadmap.json`: 40/40 `done`; no ledger-local active story. | Existing six-pane preservation, route, control and approval records remain owned here. Concept direction is not implementation approval. |
| UIV2 | `product/M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json`: 30/30 `done`. `docs/uiv2-v2-release-review.md` says UI handoff `READY` but production/security release `RELEASE-BLOCKED`. | No AP story rewrites visual baselines, screenshot approval or release-review evidence. |
| Current shared-UI baseline | PR [#268](https://github.com/pankajtiwarics27/m5-agent-studio/pull/268) merged as `6aca516fc9afce5bbe1f8276b72407c69a00d0c1` from head `bb38ca309a5a4b7a6dcb6b1194b256b91926cc87`, landing the single-row header, Density retirement and Council reachability work with its owner/generated evidence. | AP-008/018/019/020 must reconcile against that merged baseline. AP-001 does not edit `static/index.html` or its generated evidence. |
| LC08/native lifecycle | `docs/lc08-current-status.md` remains the earlier 19-owner snapshot; after merged PR #276, the generated fixture and latest `docs/runtime-dispatch-integration.md` checkpoint record 29 owners/54 sites, 16 bounded assessments and **13 inventory-only owners**. G1–G6 remain open, ADR-0038 is proposed and legacy is the default. | No backend, producer, setting or runtime mode is installed by AP-001. Test8 remains paused/unpassed. PRs #272 and #276 are preserved. |
| AP | `product/M5_Autonomous_Production_Roadmap.json`: AP-001 alone is `done`; 31 stories remain `not_started`. | AP-001 registration is tracking authority, not authorization for AP-002 or any product activation. |

## 4. Producer → authority → executor → consumer matrix

| ID / capability | Producer | Authority / admission | Executor | Result consumer | Evidence and current classification |
|---|---|---|---|---|---|
| C01 — durable Harness task | Authenticated session create/run/resume/pause/cancel routes in `app.py` | `WorkerLeaseCoordinator`, `TaskBudgetState`, `DurableTaskStateMachine`, epoch fencing, containment holds | `workflow_state.py`, `workflow_checkpoints.py`, `recovery_guard.py`, `runtime_persistence.py` | Task-state/event routes and Harness UI | `app.py::_acquire_turn_plan_and_lease_or_409`; `tests/test_durable_task_state_machine.py`, `tests/test_worker_leases_and_fencing.py`: **reuse** for one Harness root task; **extend** for an AP mission/dependency graph. |
| C02 — consequential tool action | Model tool call, Harness tool request, skill call or MCP call | `app.py::_run_authorized_tool` constructs `execution_spine.DurableActionBoundary`; policy, scoped grants, task/epoch, execution profile and durable intent are checked | Existing inner tools, contained skill execution and MCP transports | Durable action/effect ledger, task events, tool result and UI | `tests/test_action_admission_gate.py`, `tests/test_durable_intent_ledger.py`, `tests/test_mcp_idempotency_reconciliation.py`: **reuse** for registered calls; **extend** to browser/media/AP mission dispatch. |
| C03 — revocation and owned-process containment | Grant routes, task cancel/abort, expired/superseded epoch | `scoped_grants.py`, policy generation, task/epoch/worker scopes | `process_supervision.py`, `sandbox_boundary.py`, containment store | Task pause/block, quarantine and operator reconciliation | `tests/test_scoped_grants.py`, `tests/test_process_supervision.py`, `tests/test_security_incident_containment.py`: **reuse**; installed isolation remains host-specific. |
| C04 — edits, artifacts and verification | Managed edits, snapshots, tool output, verification command/result | Workspace confinement/CAS, lineage trust/confidentiality, verification contract and security generation | `edit_transactions.py`, `edit_rollback.py`, `workspace_snapshots.py`, `artifact_lineage.py`, `verification_evidence.py` | Task completion gate, diff/evidence UI, lesson promotion | `tests/test_edit_transactions.py`, `tests/test_artifact_lineage.py`, `tests/test_verification_evidence.py`: **reuse**; **extend** for AP media/source/reference identities and active-preview isolation. |
| C05 — bounded child delegation | `delegate_subagent` tool and delegation HTTP routes | Parent policy/grants/budget subsets, depth limit, path leases, child epoch and unresolved-effect join barrier | `child_delegation.py` plus task state, rollback and process supervision | Durable parent join/completion and benchmark summary | `tests/test_child_delegation.py`, `tests/test_durable_state_contract.py`; live callers in `app.py::run_bounded_child_delegation_async`: **reuse**. |
| C06 — evidence-backed lessons | Explicit promote API or verified turn `candidate_lesson` | Verification-spec digest, project scope, file hashes, lineage/security generation, secret/path checks, A/B and env/config gates | `EvidenceLessonStore.promote_lesson`, retrieval, invalidation and outcome recording | Prompt assembly at turn start/replan/handoff and task events | `app.py` lesson routes and harness-turn calls; `tests/test_evidence_lessons.py`: **reuse** for lessons; **extend** for a richer reusable-skill contract. |
| C07 — trusted transformations | Qualification/promotion/execute/revoke routes | Typed schemas, source trust, grants, lineage and qualification records | `trusted_transformations.py` | Artifact registry, lessons and evaluation routes | `tests/test_trusted_transformations.py`; live `/api/harness/transformations*` callers: **reuse** as a bounded promotion primitive, not a general plugin system. |
| C08 — routing and portable handoff | Provider/local stream choice, model-switch route, handoff reset/resume | `routing_policy.FallbackPolicy/FallbackConsent`, handoff digests/grant snapshots, risk and semantic confirmation | Existing local/provider streaming and switch orchestration | Harness session, handoff history and resumed turn | `tests/test_routing_policy.py`, `tests/test_routing_consent_policy.py`, `tests/test_portable_handoff_and_switching.py`: **reuse** current policy/handoff; **extend** with AP-scoped local-only measured routing. |
| C09 — routing experiments | Authenticated calibration/drift/evaluate/A-B routes | Env/config gate, explicit samples, fallback policy and single-residency refusal | `routing_experiments.py` | Experiment store and route responses | `tests/test_routing_experiments.py`; live `/api/harness/routing-experiments*`: **reuse** for bounded experiments, not production recommendation. |
| C10 — local runtime dispatch/lifecycle | Gateway/local inference and explicit model controls | `RuntimeDispatchBoundary`; legacy defaults; native fit/lease/proof contracts exist | Legacy dispatch is active. Native executor/transition/scheduler kernels have no installed application backend/producers | Existing Chat/Council/Harness consumers; native gateway only when separately composed | `_local_runtime_dispatch()` creates a boundary without a native backend; `native_dispatch_executor` is imported for internal packaging only. LC suites are synthetic: native path **dormant** and activation **blocked**. |
| C11 — context and continuity | OpenContext documents, compacted Harness context, model handoff | Authenticated storage/privacy rules and handoff checks | `context_store.py`, live handoff/lesson logic | Harness prompt, history/export and model-switch flow | `context_store` is **reuse**. `context_reassembly.py` has `tests/test_context_reassembly.py` but no production consumer: **dormant**. |
| C12 — model catalog/recommendation/evaluation | Caller-supplied descriptors, measurements and snapshots | Pure capability/consent/resource checks inside library contracts | `model_registry.py`, `model_discovery.py`, `model_calibration.py`, `model_comparison.py`, `model_recommendation.py`, `workflow_evaluation.py` | Unit-test/library callers only; no default app route composes the graph | Dedicated model/evaluation tests pass at contract scope; no `app.py` consumer: **dormant**. `workflow_evaluation` explicitly does not execute or collect runs. |
| C13 — outbound network and research | Provider/MCP requests and `_fetch_online_inspiration` | Provider/MCP paths use URL validation, DNS/IP pinning and redirect refusal; research-specific source/grant contract is absent | Existing HTTP/MCP clients; `private_network_gateway.py` has no production caller | Provider/MCP responses; inspiration text is truncated and failures are swallowed | `tests/test_pinned_transport.py`, `tests/test_redirect_safety.py`, `tests/test_private_network_gateway.py`: current boundary **extend**; `private_network_gateway` **dormant**; AP source-backed research record **missing**. |
| C14 — browser worker/takeover | Playwright MCP catalog card only (`npx -y @playwright/mcp@latest`) and CI/browser test scripts | Generic MCP approval applies after explicit registration; no AP browser profile/takeover policy exists | No product-owned isolated browser worker | No durable browser-session consumer | Browser test infrastructure is not a product worker. Catalog registration is **dormant**; AP-010 worker/takeover is **missing**. |
| C15 — MoneyPrinter provider preset | `MoneyPrinterAPI` preset at loopback `/v1` | Exact alias/base URL loopback pin and normal provider policy | OpenAI-shaped provider stream, but stock MoneyPrinterTurbo serves `/api/v1` and needs a separate shim | Existing provider UI/streams if an operator supplies a compatible gateway | `tests/test_moneyprinter_api.py`: configuration surface **reuse**; availability/quality is not inferred. |
| C16 — MoneyPrinter MCP surface | MCP catalog card launches bundled `moneyprinter_mcp.py` after operator configuration | Studio MCP approval/idempotency policy; wrapper also validates input, config and ffmpeg | Standalone stdio wrapper delegates to `moneyprinter_tool.run_moneyprinter_video` | Generic MCP result/tool UI and lineage | `tests/test_moneyprinter_mcp.py`; explicit catalog path is **extend** because the Studio client caps calls at 45 seconds and MCP cancellation/progress are absent. |
| C17 — MoneyPrinter native tool | Direct Python caller only | Input/preflight checks inside module; **not** registered in `DurableActionBoundary` | Scrubbed subprocess to local MPT CLI, timeout/kill, output caps and lineage | MCP wrapper or an external orchestrator caller | `tests/test_moneyprinter_tool.py`; no `app.py` registration/consumer: **dormant**. It is not permission to run host media code. |
| C18 — AP mission/reference/schedule/publish layer | None | Lower-level grants, ledgers and budgets exist, but no AP mission/reference/publish authority record does | No production executor | No AP-specific consumer/UI | No current mission database, approval/reference store, recurring scheduler, external-delivery controller or media-quality pipeline: **missing**; real actions are also **blocked** by U01–U06 as applicable. |

### Import-versus-integration findings

`app.py` directly imports 36 project modules and live-integrates most of the M5
execution, task, grant, containment, artifact, lesson, routing and delegation
spine. Two important counterexamples are:

- `context_reassembly.py` and `private_network_gateway.py` have dedicated tests
  but no production consumers;
- the model catalog/discovery/calibration/comparison/recommendation and workflow
  evaluation graph is library-only, while native lifecycle modules can appear in
  a transitive import graph solely because the deliberately unactivated native
  executor is packaged.

Therefore neither a file, import, transitive import nor passing unit test is
reported as live AP integration.

## 5. AP-001 capability classification for all 32 stories

Each row covers the full acceptance bundle for that story. Mixed bundles name
each allowed classification explicitly rather than hiding the weaker part
behind a word such as “partial.” These are gap classifications, not story-status
changes.

| Story / required capability bundle | Classification | Source, test and caller evidence; exact gap |
|---|---|---|
| AP-001 — reconciliation/registration | `extend` | This merged record, `AGENTS.md`, `docs/README.md` and the AP ledger extend repository governance with the map and convention. PR #275 and its full CI provide delivery evidence; `extend` records the implementation approach rather than an unfinished story status. |
| AP-002 — mission scope and operating envelope | `blocked` | `hardware_capabilities.py`, `onboarding_preflight.py`, `routing_policy.py`, `scoped_grants.py`; tests include `test_scoped_grants.py` and `test_routing_consent_policy.py`. U01–U06 remain owner/installed decisions; the current sandbox cannot settle them. |
| AP-003 — versioned briefs/rubrics/failure fixtures | evaluator `dormant`; AP dataset/held-out controls `missing` | `workflow_evaluation.py` is a pure aggregator and `failure_injection.py` is integrated for fault suites; `test_workflow_evaluation.py` and `test_failure_injection.py` are synthetic. No AP technical/factual/creative/practical brief registry or protected held-out partition exists. |
| AP-004 — mission-wide authority/budget | `extend` | C02/C03 are live through `_run_authorized_tool`, task budgets and grants (`test_execution_spine.py`, `test_execution_budget.py`). Browser, direct native MoneyPrinter, research HTTP and future media/script entry points do not yet share one AP root envelope. |
| AP-005 — mission task graph/recoverable joins | `extend` | C01/C05 provide durable root task, checkpoints, leases and one-level child joins (`test_durable_task_state_machine.py`, `test_child_delegation.py`). A general dependency DAG, mission artifact contracts and populated AP migration do not exist. |
| AP-006 — artifact identity/provenance/invalidation | `extend` | C04 provides hashes, lineage, selective invalidation and verification (`test_artifact_lineage.py`, `test_verification_evidence.py`). AP source/licence/reference histories and isolated active-preview handling are absent. |
| AP-007 — bounded execution/cancellation/recovery | live core `extend`; installed execution `blocked` | Budgets, recovery guard, process supervision, uncertain effects and no-host-fallback tests exist (`test_process_supervision.py`, `test_side_effect_ledger.py`, `test_sandbox_boundary.py`). Coverage is not mission-wide; this Linux host has no Docker/Podman and is not the qualified Apple target. |
| AP-008 — mission controls/artifact-first status | `blocked` | Durable events/pause/cancel/evidence UI are reusable, with UIUX/UIV2 preservation suites. No AP mission view or active-preview boundary exists; merged PR #268 is now the shared-UI baseline and future implementation/visual review is still required. |
| AP-009 — sourced safe search/reading/repo inspection | existing network boundary `extend`; gateway module `dormant`; research record `missing` | C13. `private_network_gateway.py` is unit-tested but unused. `_fetch_online_inspiration` does not preserve URL/date/excerpt/contradiction provenance and catches provider failures, so it is not an AP research consumer. |
| AP-010 — controlled browser and takeover | `missing` | C14. Browser walks test Studio; the Playwright catalog entry is not an isolated project profile, durable browser job, egress broker or authenticated human-takeover protocol. U04/U06 also block live activation. |
| AP-011 — inspect capability candidates without activation | model discovery `dormant`; general candidate registry `missing` | `model_discovery.py` creates inert install plans and has `test_model_discovery.py`; `supply_chain.py` is release-script owned. No live generic candidate record covers source revision, licence, hooks, egress, privileges and resource cost. |
| AP-012 — disposable capability experiments/receipts | `blocked`; AP lab `missing` | Sandbox/grant/quarantine/supervision primitives and tests exist, but no experiment receipt/promotion separation exists. Current host isolation qualification is blocked and U02/U04/U06 remain unresolved; no host fallback is permitted. |
| AP-013 — measured model capability profiles | library graph `dormant`; installed measurements `blocked` | C12 and `test_model_calibration.py`, `test_model_recommendation.py`, `test_model_discovery.py`. No app consumer composes exact installed/runtime/model profiles, and this host cannot measure the owner M5/24 GB inventory. |
| AP-014 — policy/resource-aware routing | policy/handoff `extend`; native scheduler `dormant` and activation `blocked` | C08–C10; `test_request_fit_admission.py`, `test_routing_experiments.py`, `test_routing_policy.py`. AP needs a scoped local-only route using measured profiles; legacy remains default and physical lifecycle completion cannot be inferred. |
| AP-015 — context/preference/artifact continuity | stores/handoff `extend`; `context_reassembly` `dormant` | C06/C08/C11. Portable handoff and lesson/project scoping are live, while `context_reassembly.py` is pure/tested only. No AP mission brief/approval/reference reconstruction or continuation-probe runner exists. |
| AP-016 — reproducible challenger/workflow evaluation | `dormant` | `workflow_evaluation.py` and `model_comparison.py` aggregate caller-supplied measurements (`test_workflow_evaluation.py`, `test_model_comparison.py`); no app runner gathers pinned AP runs, held-out evidence, failures, cost and intervention metrics. |
| AP-017 — bounded specialists/versioned handoffs | delegation core `reuse`; AP artifact/reviewer handoff `extend` | C05 and `test_child_delegation.py` prove strict subsets, path locks, cancel/join and single-vs-multi benchmarks. AP still needs versioned mission input/output schemas and evidence-only reviewer prompts; no concurrent model residency is implied. |
| AP-018 — early direction samples | `missing` | Checkpoints/artifacts can preserve a pause, but no AP sample decision state, benchmark lookup, direction-vs-final approval type or reviewed UI flow exists. Concept images are not evidence. |
| AP-019 — lightweight scoped feedback/preferences | `missing` | `context_store.py`, `history_privacy.py` and lineage are reusable, but no artifact-version-bound free text/region/passage/timecode record, interpretation confirmation or preference correction scope exists. |
| AP-020 — owner-approved reference benchmarks | `missing` | Artifact lineage and lessons do not represent owner provenance or separate direction/final/reference/publish approvals. No versioned reference retrieval store/consumer exists; agents cannot synthesize the missing approval. |
| AP-021 — complete website draft workflow | `blocked` | Generic Harness/GSD/SDLC, delegation and browser-test helpers are not a selected AP website workflow. U01/U02/U03/U04/U06, AP-009/010/012/017/018/019 and real browser evidence remain prerequisites. |
| AP-022 — website quality/delivery | `blocked` | Existing UI browser/accessibility/visual machinery is evidence infrastructure, not a website-output rubric/runner. AP-020/021, selected scope, real captures and owner creative review are absent. |
| AP-023 — sourced short-video drafts | existing surfaces `dormant`/`extend`; complete workflow `blocked` | C15–C17 and MoneyPrinter tests prove contracts only. Research/claims/rights/storyboard/captions and playable-pilot consumption are absent; U01/U02/U03/U04/U06 remain unresolved. No second wrapper is justified. |
| AP-024 — video technical/editorial quality | checker pipeline `missing`; pilot acceptance `blocked` | `moneyprinter_tool.py` validates process output/path and hashes MP4s, but no ffprobe/frame/audio/subtitle/editorial/rights/full-watch evidence pipeline exists. AP-020/023 and U01/U03 block acceptance. |
| AP-025 — evidence-backed reusable skills | lesson/promotion core `extend` | C06/C07 and `test_evidence_lessons.py` prove actual turn consumers, project isolation, staleness and A/B gating. A full skill record with prerequisites, executable permissions, stable versions and quarantine/revalidation must extend—not duplicate—these stores. |
| AP-026 — improvement research/watch jobs | model/update contracts `dormant`; scheduled watch `missing`; live use `blocked` | `model_discovery.py`, `model_updates.py`, `model_calibration.py` and tests are library-only. No authorized recurring network producer or protected optimizer exists; U02/U03/U04/U05 govern real experiments. |
| AP-027 — bounded refinement/stopping | evaluation contracts `dormant`; refinement loop `missing` | `workflow_evaluation.py`, `execution_budget.py` and verification evidence are reusable and tested. No incumbent-preserving AP candidate loop, independent-context comparison or plateau/no-op consumer exists. |
| AP-028 — risk/evidence promotion and rollback | bounded lesson/transformation primitives `extend`; program policy `blocked` | C06/C07 plus grants/lineage/model-update contracts provide pieces. U05 thresholds and risk classes are unset; no canary controller protects evaluator/authority state or versions in-flight AP missions. |
| AP-029 — recurring missions/external delivery | `missing` and `blocked` | No scheduler or publish/delivery controller exists. Side-effect IDs, uncertain-write reconciliation, grants, budgets and checkpoints are reusable, but U03/U04 and explicit account/destination/content authority are unresolved. |
| AP-030 — optional guest desktop | `blocked` | No guest-desktop adapter is installed. Generic sandbox/MCP/process primitives do not prove host isolation, clipboard/account boundaries or takeover. It remains optional and disabled pending U01/U02/U04/U06 and exact installed qualification. |
| AP-031 — configuration/onboarding/operations | `extend` | `onboarding_preflight.py`, `start_command_center.py` and current UI expose provider/runtime health; tests cover those current surfaces. AP configured/tested/qualified/experimental distinctions, reference/preference operations and mission cost/pause help are absent. |
| AP-032 — production release/handoff | `blocked` | All named AP dependencies are not complete. `security_release_gate.py --json` is `release-blocked`; AS-031 is deferred/pending and artifact signing is unprovisioned. Current-host installed checks are blocked, and future CI, real artifact review and owner/independent approvals cannot be pre-recorded. |

## 6. Required pre-wrapper assessments

### 6.1 MoneyPrinter

There are already three distinct surfaces and they must not be collapsed into a
single “integrated” claim:

1. The `MoneyPrinterAPI` provider preset is live configuration for an
   OpenAI-compatible loopback gateway. Stock MoneyPrinterTurbo is not compatible
   with that `/v1` surface.
2. The MCP catalog can launch `moneyprinter_mcp.py` after explicit operator
   configuration. Calls use Studio MCP approval, but the current 45-second client
   limit, missing MCP cancellation/progress, local checkout, `uv`, ffmpeg and MPT
   provider configuration make ordinary long renders unsupported.
3. `moneyprinter_tool.py` is a typed subprocess/lineage contract used by the MCP
   wrapper, but is not registered at the central native action boundary and has
   no `app.py` consumer.

AP-001 proposes **no new wrapper**. AP-023 must first choose and justify one
admission-bound execution path, preserve the existing three surfaces, add root
budgets/cancellation and prove a real playable artifact. AP-024 separately owns
technical/editorial/rights evidence; process exit and a path are not acceptance.

### 6.2 Evidence-backed lesson promotion

Lesson promotion is live, not merely a module. Explicit routes and the Harness
turn path call `EvidenceLessonStore`; promotion requires verified provenance,
project scope, anchored file hashes, security/lineage freshness and secret/path
checks. Retrieval is bounded to decision points, gated by configuration and A/B
evidence, and outcomes can invalidate regressing lessons.

AP-025 must reuse this store and its actual consumers. A richer skill format may
extend metadata and stable executable versions, but a second memory/promotion
store would split authority and is rejected.

### 6.3 Routing

Current application routing has three different scopes:

- provider fallback/consent and portable model handoff are live;
- routing experiments are live but evidence/config gated;
- recommendation/calibration/catalog modules are advisory library surfaces;
- coordinated native lifecycle scheduling is dormant and legacy remains the
  default.

AP-014 must compose measured compatibility, AP-local-only inference policy,
grants and resource fit without deleting established provider features used by
other programs. A model name, model-card claim, recommendation score or manual
override is not dispatch authority. No dormant native path is activated here.

### 6.4 Durable delegation

`child_delegation.py` is already live through the tool and HTTP routes. It
narrows authority and budget, prevents recursive delegation and conflicting
writes, gives each child an epoch, blocks joins on unresolved effects, rolls
back rejected edits, propagates cancellation and gates delegation with measured
single-agent comparisons.

AP-005/AP-017 should extend durable mission dependencies and versioned artifact
handoffs around this implementation. A second swarm scheduler or one-resident-
model-per-role design would duplicate authority and violate current resource
ownership.

## 7. Security and installed-qualification obligations

| Obligation | Current truth and AP consequence |
|---|---|
| Authentication/origin/rate/security defaults | `/healthz` is the only unauthenticated route; consequential application calls retain current bearer/origin/policy/grant checks. AP adapters must enter the existing boundary rather than add a bypass. |
| Local-first / no silent fallback | AP model inference remains local-only under revision 1.2. Existing cloud-provider features remain available to their owning product paths, but an AP route may not silently use them. Host execution and external media/research services require separate explicit authority. |
| Independent security gate | `.venv/bin/python scripts/security_release_gate.py --json` returned exit 0 with gate invariants satisfied but decision `release-blocked`: AS-031 is `deferred`/`pending`, 0/12 boundaries are signed adequate, and artifact signing remains unprovisioned. AP-001 cannot sign either gate. |
| Recorded target qualification | The M5 ledger records Apple M5/24 GiB measurements and a host/image-bound Docker qualification. Limits remain: thermal telemetry blocked, coding cases skipped, and the sandbox swap ceiling residual. That evidence is not portable to another machine/configuration. |
| Current session qualification | `.venv/bin/python scripts/qualify_environment.py --all ...` identified Linux x86_64, 3.85 GiB RAM, no Docker/Podman; M5-P3-01 and M5-P4-01 both returned `blocked` (exit 2). No model command, pressure run or live container probe was supplied. |
| Native lifecycle qualification | `scripts/check_lifecycle_feasibility.py` ran in `offline_synthetic` mode, all 19 expected cases matched, `installed_runtime_qualification` was `not_run`, and `activation_allowed` was false. This is refusal/contract evidence only. |
| Browser/media qualification | Browser scripts and MoneyPrinter tests are offline fixtures. No isolated product browser, MoneyPrinter installation, playable pilot, full watch, rights review or actual owner media acceptance was executed. |
| Supply chain/credentials | Dynamic candidates need exact revision/licence/hook/dependency review and disposable qualification. No secret, credential, model, browser profile or generated media is committed. Release SBOM/provenance does not itself authorize plugins. |
| Human approvals | UI concept selection, implementation screenshot approval, artifact acceptance, publish authority and independent security approval remain separate. Author-generated evidence cannot fill those records. |

## 8. Generated-file and source-bound evidence obligations

| Canonical change trigger | Generated/owned outputs and required action |
|---|---|
| `app.py` or `static/index.html` | Regenerate `studio.py` with `scripts/build_studio.py`; regenerate/check route security and subsystem inventories. Never hand-edit the standalone. |
| AS roadmap status | Regenerate `docs/product-roadmap-status.md` with `scripts/generate_roadmap_status.py`. AP tracking does not alter AS status. |
| Runtime-dispatch owners/composition | Regenerate `docs/runtime-dispatch-entry-points.md` and its fixture with `scripts/runtime_dispatch_inventory.py`; re-check LC source-bound docs/evidence. |
| UI source/control/route/authority surfaces | Run `scripts/uiux_source_manifest.py`, `scripts/uiux_control_inventory.py`, `scripts/uiux_route_map.py`, `scripts/uiux_authorization_trace.py` and `scripts/uiux_preservation_signoff.py` in owning dependency order, then affected release/UIV2 checks. Do not copy stale hashes or approvals. |
| Visual/browser implementation | Run actual browser walks/captures at required viewports and zoom, then obtain the required owner review. A source check or concept image is not a screenshot approval. |
| AP ledger/reconciliation only | `product/M5_Autonomous_Production_Roadmap.json` is hand-maintained authority data, not a generated file. Parse JSON, validate story/dependency/status invariants, validate docs/links, and prove all unrelated AP stories remain unchanged. |

AP-001 changes none of the canonical runtime/UI triggers. Generator/check runs
must therefore produce zero tracked drift; any drift is a blocker rather than an
excuse to hand-edit generated files.

## 9. AP registration and remote claim convention

### Authoritative AP tracking source

`product/M5_Autonomous_Production_Roadmap.json` is registered as the single
**AP-program tracking ledger**. `docs/autonomous-production-roadmap.md` is the
requirements specification, this document is AP-001 evidence, and
`M5_AUTONOMOUS_PRODUCTION_AGENT_PROMPT.md` is the operating protocol. None of
them replaces an AS, M5, UIUX/UIV2 or LC authority record.

The owner-approved activation scope is exactly:

> AP-001 reconciliation, registration and evidence on 2026-11-09; no product
> runtime activation and no authorization for AP-002–AP-032.

A dependency becoming `done` establishes graph readiness only. It does not
supply owner authority, U01–U06 decisions, external-service permission or a
human approval.

### Claim convention

1. Fetch current `main`; read `AGENTS.md`, the AP specification/prompt/ledger,
   owning-program records, open PRs and current claims.
2. Select exactly one owner-authorized, dependency-ready AP story. Never infer
   authorization from numbering or readiness.
3. Record `status=in_progress` and `tracking.owner`, fixed Arena branch,
   `base_sha`, starting `head_sha`, UTC claim/heartbeat, delivery state and
   bounded handoff. Commit and push that claim.
4. Immediately open one **draft PR** titled `AP-XXX: ...`; that remote PR is the
   coordination claim. Follow with the PR URL/delivery-state ledger update.
   A local JSON edit is not atomic remote coordination.
5. Use `ap-XXX-short-slug` only where the environment permits branch creation;
   Arena's fixed `arena/...` session branch is an explicit platform exception.
6. Keep one story per branch/PR. Refresh the heartbeat with meaningful pushed
   progress, not empty churn. There is no invented automatic stale timeout:
   inspect PR activity, coordinate explicitly, preserve prior commits/evidence
   and record the takeover reason before superseding a claim.
7. Before merge, fetch main, regenerate source-bound evidence from the combined
   source, execute applicable tests/CI and record actual failures/skips. Never
   transfer a visual/security/owner approval to changed output by implication.
8. `in_review` means implementation/evidence is pushed and the PR is open;
   `done` still requires the ledger's completion convention and required
   approvals. Closing/superseding a PR must leave a durable handoff.

## 10. Acceptance mapping and bounded handoff

| AP-001 acceptance criterion | Evidence in this record |
|---|---|
| Every required capability classified with file/test/caller evidence | §§2, 4 and 5: definitions, live producer/authority/executor/consumer paths and all 32 story bundles. |
| Preserve runtime/UI/LC claims and blockers | §3: exact ledger counts, current UI ownership, LC G1–G6/legacy default and unchanged status vocabularies. |
| Assess MoneyPrinter, lessons, routing and durable delegation | §6, grounded in C05–C10 and C15–C17. No wrapper or duplicate control plane is proposed. |
| Record security, installed qualification and generated obligations | §§7–8, including executed current-host/LC/release results and source-bound generator ownership. |
| Identify authoritative AP source and owner-approved activation scope; no activation | §9 plus the AP ledger/`AGENTS.md` registration. Only AP-001 is authorized and no runtime/UI source changes. |

### What this story does not authorize

- AP-002 or another downstream claim, even after AP-001 merges;
- a MoneyPrinter/browser/model/plugin installation or external account/spend;
- hosted reasoning or silent cloud/media fallback;
- coordinated native lifecycle composition, concurrent model residency or
  resumed Test8;
- UI implementation from concept mockups or changes that overwrite the merged PR #268 baseline or its generated evidence;
- owner screenshot/artifact/reference/publish approval or independent security
  sign-off.

The next AP-program action after AP-001 completion is an explicit owner decision
on whether to authorize AP-002, AP-003, both, or neither. AP-001 does not make
that decision, and a generic continuation does not silently claim downstream
work.

## 11. Executed validation and evidence limits

The initial commands below ran on the AP-001 branch against original base
`2f91bd3` in a Linux x86_64 environment with Python 3.11.2. A merged-main
refresh against `c0ab6892` is recorded separately below. No provider credential,
model endpoint, MCP server, browser worker, MoneyPrinter installation or
external service was used.

| Command / check | Actual result |
|---|---|
| AP ledger/reconciliation invariant script | Passed: 32 unique stories, acyclic dependencies, AP-001 alone `in_progress`, 31 `not_started`, all story IDs and all five classifications published, registration denies runtime/downstream authorization. |
| `.venv/bin/python scripts/run_scoped_tests.py --base origin/main --head HEAD --worktree` | Initial shallow-clone run: 516 passed, 2 ancestry failures, 1 warning. After `git fetch --unshallow origin --tags`: **518 passed**, 1 upstream Starlette/httpx deprecation warning. The environmental failure is preserved rather than reported as a first-pass green run. |
| 30 source-grounding suites covering action admission, tasks/leases, grants/supervision, artifacts/evidence, delegation, lessons/transformations, routing/handoff, context/private-network, model/evaluation contracts, MoneyPrinter and native-dispatch contracts | **607 passed**, 1 upstream Starlette/httpx deprecation warning. These are offline application/contract tests, not installed model/media qualification. |
| `.venv/bin/ruff check .` | Passed. |
| `.venv/bin/mypy` | Passed: 75 source files. |
| tracked Python `compileall` | Passed. |
| `scripts/check_frontend_syntax.py` | Passed: one inline JavaScript block. |
| `scripts/verify_baseline.py` | Passed after full history fetch; pinned ancestor `7904892d759bb66b817bb17b6f4f377874bd6e25`. |
| `scripts/smoke_local.py` and `--standalone` | Both passed loopback startup, health, token permissions, protected API, origin rejection, isolated DB and UI auth-helper checks. |
| docs/governance/security-record checks | Docs passed (274 files scanned); governance passed (7 required files); security-signoff record consistent (`deferred`, `pending`, 12 boundaries, 7 findings). |
| demo/screenshot validators | Demo transcript current; committed screenshot manifest validated 6/6. No new screenshot was captured or approved. |
| runtime/generated checks | `studio.py`, route matrix (121 method/path entries), subsystem inventory (121 endpoints/20 tables) and AS roadmap dashboard regenerated with **zero unintended tracked drift**. Runtime dispatch, UI source/control/route/authorization/preservation, 37/37 UI release gate and UIV2 release-review checks passed. Existing historical visual approval was not changed. |
| `.venv/bin/python scripts/security_release_gate.py --json` | Exit 0 for gate execution; decision remains **`release-blocked`**, not release-ready. |
| `.venv/bin/python scripts/check_lifecycle_feasibility.py` | Exit 0; 19/19 synthetic expectations matched, installed qualification `not_run`, activation false. |
| `.venv/bin/python scripts/qualify_environment.py --all --json /tmp/ap001-qualification.json --quiet` | Exit 2 as an expected environment block: no Docker/Podman and not the Apple M5/24 GiB target; both M5-P3-01 and M5-P4-01 `blocked`. `/tmp` evidence is local/transient and is not committed. |
| JSON parse and `git diff --check` | Passed. |

### Merged-main refresh before the owner-requested merge

PR #276 merged to main while PR #275 awaited review. Main commit
`c0ab6892071e388a39f12533f4a130aeff11bbe9` was merged into this branch rather
than allowing the AP audit to land against stale source-bound evidence. The
refresh preserved PR #276's chat-stream close fix and regenerated artifacts.
It also updated the LC count from the earlier 19-owner snapshot to the latest
fixture: 29 owners/54 sites, 16 bounded assessments and 13 inventory-only.
The direct-project import count remains 36.

- The docs/CI scope reran clean: **518 passed**, one upstream warning.
- The capability run was expanded with PR #276's chat-stream and coordinated
  workflow suites: **646 passed**, one upstream warning.
- Ruff, mypy (75 sources), compileall, frontend syntax, baseline, both smoke
  modes, docs/governance/security-record, demo and screenshot checks passed.
- Runtime/UI/UIV2 source-bound checks passed; rebuilding the standalone, route
  matrix, subsystem inventory and AS dashboard produced no unintended drift.
- Security stayed `release-blocked`; lifecycle feasibility remained 19/19
  synthetic with activation false; current-host sandbox/hardware qualification
  remained blocked with expected exit 2.
- The previous PR head's eight reported GitHub checks were green. At this
  refresh point, the new pushed head still required its own full PR matrix
  before merge; the completed result is recorded below.

The full local pytest suite was not re-run because this documentation/ledger
change maps to the 518-test docs/CI scope; the refreshed 646-test source-grounding
run adds the audited capability and merged-main workflow contracts. Pull-request
CI remains responsible for the full Python 3.11/3.12/3.13 and browser-safety
matrix at the pushed head.
AP-001 moved to `in_review` only after the implementation/evidence commit was
pinned; the earlier invariant run correctly recorded `in_progress` during
construction. At that point CI was pending rather than green. The post-merge
record below closes only AP-001; no current-host isolation, installed native
runtime, physical load/unload, real browser task, playable video, owner artifact
review or independent security acceptance is claimed.

### Post-merge completion record

The owner explicitly requested the merge after reviewing the AP-001 handoff.
The refreshed pull-request head `863ba601cd9e3537aac58906acef4502764e5c0f`
passed CI run `42642415180`: full tests on Python 3.11, 3.12 and 3.13 plus the
browser-safety regression job all completed successfully. PR #275 then merged
at 2026-11-09T15:27:46Z as
`6a7cd319a94e013258cd51cc7dd6c8559d2d78d7`; the merged head is an ancestor of
`main`.

PR #268 subsequently merged as
`6aca516fc9afce5bbe1f8276b72407c69a00d0c1`. This closeout was refreshed from
that descendant rather than treating the formerly open UI claim as current.
The post-merge `main` run for #275 was superseded by the higher-priority #268
merge run, not by a test assertion failure. The closure PR must still pass its
own current-head CI before landing. Local closeout selection collected 518
tests: **497 passed**, **21 intentionally skipped** under `P7-PARK-02`, and one
upstream warning. AP invariants, documentation, governance, security-record
consistency and baseline validation passed.

All five AP-001 acceptance criteria have the evidence mapped in §10, applicable
implementation-head CI passed, and the implementation PR is merged. AP-001 is
therefore `done` under the ledger completion convention. This status does not
make the product release-ready: security remains `release-blocked`, installed
host/native qualification remains blocked/not run, and all 31 downstream AP
stories remain `not_started` and unauthorized.
