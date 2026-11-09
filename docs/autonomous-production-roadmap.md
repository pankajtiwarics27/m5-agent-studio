# M5 Agent Studio — Autonomous Production Product Roadmap

**Revision:** 1.2 • **Prepared:** 2026-11-08 • **Status:** owner-requested product specification and proposed execution program; not evidence of implementation, independent security acceptance, or authorization to bypass existing gates.

**Audience:** any implementation agent or human entering without this conversation. Read this document, the companion story ledger, and the agent prompt before proposing changes.

| Companion | Purpose |
|---|---|
| `product/M5_Autonomous_Production_Roadmap.json` | Proposed AP story ledger: dependencies, deliverables, acceptance criteria, evidence and unresolved decisions |
| `M5_AUTONOMOUS_PRODUCTION_AGENT_PROMPT.md` | Standalone session protocol, claim/handoff rules and verification requirements |

**Source snapshot inspected:** `pankajtiwarics27/m5-agent-studio` main at `a67e8b291614f8afdac7c8d38ba51bcc8926d695`. Inspection covered repository guidance, module contracts and selected runtime-status documents; not a full code/security audit, installed-runtime qualification, or fresh end-to-end execution. Refresh against current main before implementing.

**Precedence:** repository `AGENTS.md`, accepted security decisions, existing program ownership and signed approvals remain binding. This program does not replace the AS, M5 runtime, UIUX/UIV2, or LC ledgers, and does not reopen their completed stories. Reconcile overlapping work in AP-001. Drafting this roadmap does not grant automatic execution authority to its stories or to downloaded tools.

## Revision 1.2 — owner clarification and UX direction (takes precedence)

The owner agreed to the integrated conversation-first workspace and focused artifact-review direction. Read [the UX plan](autonomous-production-ux-direction.md), including its two selected concept images, before AP-008/018/019/020 implementation. Preserve current navigation/features and reconcile UI ownership; these concepts are not implemented-screen or release approval.

This revision supersedes earlier blanket exclusions of hosted media inference: local inference remains the default; future explicitly authorised cloud/media services and audio/video application, CLI or MCP integrations must remain architecturally feasible. Agents may discover, evaluate and acquire capabilities inside enforced data, installation, spending and service grants. Discovery is not activation, and no silent cloud fallback is allowed. External reasoning-model use remains a separate unresolved policy decision. Earlier local-only acceptance criteria must be read with this scoped exception; this grants no specific provider, credential, spend or data transfer.

## Contents

1. Product destination and user journeys
2. Decision record: agreed, recommended and unresolved
3. Existing-system map and authority boundaries
4. Product behaviour and contracts
5. Autonomy, discovery and safety
6. Models, context and resource efficiency
7. Evaluation, feedback and accepted benchmarks
8. Experience and specialised output workflows
9. Continuous improvement
10. Delivery phases and story execution
11. Reference registry
12. Verification, release and rollback
13. Independent-agent completion/handoff rules

## 1. Product destination

### 1.1 The final product

M5 Agent Studio becomes a **local-first autonomous production team**. A user supplies an outcome, audience, constraints and an operating envelope. All model inference is local-only. The team plans, researches gaps, selects suitable models/tools, creates artifacts, tests and repairs them, requests useful direction feedback when necessary, and delivers a reviewable result. It can learn validated methods and preferences from previous work and improve its capabilities without weakening its safety controls.

The goal is not multi-agent conversation as an end in itself. It is unusually good output in a small number of areas, with progressively less owner supervision. Initial candidate areas are websites/web experiences and researched faceless/explainer videos. Exact niches, formats and launch order remain an owner decision; do not treat an illustrative workflow as a locked business strategy.

The product may support side-income projects through useful deliverables. It must not promise revenue, audience growth, monetisation eligibility, factual perfection or the elimination of customer/distribution work.

### 1.2 End-state user journey

1. Describe the desired result in ordinary language; select or refine a deliverable type.
2. Confirm only important unknowns: audience, constraints, relevant benchmark and resource/authority envelope. Reuse prior explicit settings when applicable, not guessed consent.
3. See a concise proposed outcome and meaningful milestones. Do not require the user to choose Python packages or manually assemble agents.
4. Before a relevant benchmark exists, receive a small representative direction sample before expensive full production.
5. Give lightweight feedback through text, selection/annotation or optional quick actions. Progress and drafts survive the interruption.
6. Let agents work within their grants. Observe progress, inspect sources/artifacts, pause, stop or take over a browser when needed.
7. Receive the actual website/video/package first, followed by verification results, limitations, provenance and costs. Never substitute a plan or agent summary for the requested artifact.
8. Accept or request revisions. Explicitly accepted versions can become scoped references for subsequent work.
9. Publishing, production deployment, external messages, account changes and spending occur only under applicable explicit authority. Artifact acceptance is not a blanket external-action grant.
10. Successful techniques become candidate reusable skills; improvements are tested, scoped, versioned and reversible.

### 1.3 Non-goals

- Installing every interesting repository or replacing the current app with several overlapping agent frameworks.
- An unrestricted host-desktop bot, invisible background egress, autonomous policy weakening or uncontrolled recursive workers.
- Treating synthetic tests or a workspace directory as proof of physical isolation.
- Assuming local inference is free, every model is interchangeable, or every task benefits from a swarm.
- Mass low-originality content, copying competitors, fabricated claims/testimonials, quota evasion or guaranteed passive income.
- Weight training/RL infrastructure in the first release. Memory, skill learning and configuration optimisation are distinct from weight changes.

## 2. Decision record

### 2.1 User-confirmed requirements

| ID | Requirement |
|---|---|
| D01 | Agents should have broad freedom to find and adapt useful solutions; the condition is safety. |
| D02 | Prioritise unusually good results in a few areas rather than broad mediocre coverage. |
| D03 | Use evaluations and task-appropriate model routing. The system should research relevant models, repositories, runtime developments and papers, then test improvements. |
| D04 | Model roles may differ by requirement. Mistral for scripts, TwIL for coding, and Qwen/Gemma for architecture were hypotheses/examples, not established rankings or mandatory vendors. |
| D05 | User-approved products become reference benchmarks; further enhancements version those references. |
| D06 | Automatic and human-reviewed promotion are both useful. Decide by use case, agent, risk, scope and authority; no single global policy. |
| D07 | Before a benchmark is established, agents may seek early direction feedback. Users must be able to review lightly and redirect the work. |
| D08 | Learn from useful aspects of external projects, not adopt a repository simply because it was shared. |
| D09 | Preserve existing features and functionality. Use the blast-radius approach: enumerate options, affected files, pinned tests, risks and rollback before behaviour changes. |
| D10 | Maintain the simplified, clean Claude-like UI direction and complete, legible previews. Nothing clipped/overlapping; technical test success alone is not visual acceptance. |
| D11 | Deliverables must reach GitHub for owner access; local-only work is not a completed delivery. Never solicit or expose tokens. Report push/PR status honestly. |
| D12 | Never approve screenshot baselines or human/security sign-offs on the owner's behalf. |
| D13 | Model inference is local-only, including routing, reviewer/judge and improvement experiments; no hosted-model fallback. Public-web research remains a separate authorised capability. |
| D14 | Owner pilot hardware is M5, 24 GB unified memory, 1 TB storage. Sequential model load/unload is the existing intended application architecture, not a newly proposed coordinator. Actual wiring/qualification must still be verified. |
| D15 | Evaluate both dense and MoE candidates for specific tasks; neither architecture nor model family establishes suitability without measurements. |

### 2.2 Proposed engineering defaults, not additional owner decisions

- Extend the existing control plane; one authoritative scheduler/admission path, not competing runtimes.
- API/CLI before browser automation; desktop only where necessary.
- Start with a coordinator and a small bounded set of specialists; execution concurrency derives from actual admission/qualification. No fixed model residency or chip assumptions.
- Shared project artifacts with scoped writes; separate worktrees or versioned writes for concurrent coding. Stronger environments for mutually untrusted workers.
- Production and improvement experiments use separate state and promotion paths.
- A small website pilot before a short-video pilot is a suggested order, not an owner-selected launch order.

### 2.3 Decisions that remain open

| ID | Decision | Blocks | Safe work while unresolved |
|---|---|---|---|
| U01 | First specialisation, audience and output format; next specialisation | Final pilot rubrics and launch | Generic contracts, fixtures, adapter research and evaluation harness |
| U02 | M5/24 GB/1 TB confirmed; measure free memory/storage and installed runtime versions | Installed admission, downloads and performance claims | Hardware-independent contracts and mocked tests; never equate nominal 24 GB with available model memory |
| U03 | Permitted public research/connectors and external non-inference services; currency/spending; hosted inference excluded | External-service actions; no hosted-model fallback | Local mocks and explicit deny paths |
| U04 | Allowed folders/accounts/network destinations and retention rules | Real data access and external effects | Disposable fixtures and policy representation |
| U05 | Concrete automatic-promotion thresholds and high-risk review policy | Persistent production promotion | Shadow evaluations and candidate reports |
| U06 | Qualification/activation status of existing lifecycle/sandbox paths | New execution on unqualified backends | Audit call paths, implement refusal tests, coordinate with the owning program |

Ask the owner once for a blocking decision with specific options. Never reopen settled requirements or block unrelated safe stories on all open questions.

## 3. Existing-system integration map

**Existence is not wiring; wiring is not qualification.** Inspect consumers, producer authority and actual tests. Do not duplicate a named capability because a prior document called it missing.

| Concern | Existing files to inspect first | Integration caution |
|---|---|---|
| App, UI and standalone | `app.py`, `static/index.html`, `studio.py`, `scripts/build_studio.py` | Standalone is generated; regenerate, never patch independently. Preserve routes/payloads and auth/origin behaviour. |
| Execution authority and side effects | `execution_spine.py`, `execution_profiles.py`, `side_effect_ledger.py`, `native_dispatch_executor.py`, `runtime_dispatch_boundary.py` | New adapters cannot bypass admission or create parallel effect identities. Unknown external outcome is not retry permission. |
| Tasks, recovery, budgets and events | `workflow_state.py`, `workflow_checkpoints.py`, `workflow_orchestration.py`, `execution_budget.py`, `task_events.py`, `recovery_guard.py` | Extend durable root-task/milestone state; avoid another independent mission database/state machine. |
| Delegation | `child_delegation.py` | Authority subsets, mutation locks and durable joins already have contracts. Preserve or strengthen them. |
| Grants and isolation | `scoped_grants.py`, `sandbox_boundary.py`, `session_trust.py`, `process_supervision.py`, `private_network_gateway.py`, `workspace_paths.py`, `workspace_quarantine.py` | Grant scopes, subprocess ownership, redirects/DNS, symlinks and cancellation need actual consumer tests. |
| Artifacts and lessons | `artifact_lineage.py`, `verification_evidence.py`, `evidence_lessons.py`, `trusted_transformations.py` | Preserve content identity, selective invalidation, scope and evidence-backed promotion. |
| Memory/context | `context_store.py`, `context_reassembly.py`, `request_fit.py`, `request_fit_admission.py`, `history_privacy.py` | Required task constraints and approvals cannot disappear in a summary or silent truncation. |
| Model registry/discovery/updates | `model_registry.py`, `model_discovery.py`, `model_updates.py`, `capability_negotiation.py` | An inert install plan does not authorize download/execution. Disk inventory is not residency. |
| Model selection/evaluation | `model_recommendation.py`, `model_calibration.py`, `model_comparison.py`, `routing_policy.py`, `routing_experiments.py`, `workflow_evaluation.py`, `benchmark_import.py`, `benchmark_publication.py` | Recommendation and supplied measurements are not live execution proof. Imported benchmarks are untrusted and hardware/config-specific. |
| Runtime lifecycle/resources | `model_lifecycle_coordinator.py`, `native_inference_scheduler.py`, `native_transition_driver.py`, `resource_admission.py`, `shared_memory_admission.py`, `hardware_capabilities.py`, `local_runtime_lifecycle.py`, `omlx_lifecycle.py`, `ollama_lifecycle.py`, `lmstudio_lifecycle.py` | Some composition is dormant/limited. Inspect current LC status before activation. Do not relax residency or fit safety for a faster demo. |
| Video | `moneyprinter_tool.py`, `moneyprinter_mcp.py`, `docs/moneyprinter-turbo-integration.md` | Already has typed tool/MCP contracts; audit wiring and quality gap before adding another wrapper. Process exit/path output is not editorial acceptance or proof of valid safe media. |
| Supply chain | `supply_chain.py`, `security_release_gate.py` | Release provenance is not automatically a dynamic-plugin trust system; extend deliberately. |

Read `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `docs/lc08-current-status.md`, `docs/runtime-dispatch-integration.md`, `docs/runtime-dispatch-entry-points.md`, accepted ADRs and current story ledgers. The inspected LC status explicitly retains legacy defaults and distinguishes offline evidence from installed qualification; this roadmap grants no LC activation. An agent roster with parallel tasks does not authorize simultaneous local model residency.

Tab identity trap: Council=`hall`, Pipeline=`council`, Compare=`debate`, Chat=`chat`, Harness=`harness`, Start=`start`. Inspect current UI; do not rename/remove existing surfaces in this program by assumption.

Parallel UI work must be reconciled, not overwritten. Earlier sessions prepared Density retirement, a single-row header/Settings overlay, narrow-screen repairs and SSE-check fixes. Do not assume that branch is merged: the inspected main snapshot did not contain `id="settingsModal"`. Preserve the owner's authorised Density removal where that work lands; do not reintroduce it to satisfy old capture helpers. Read current PRs and source before editing shared UI or generated artifacts.

## 4. Logical contracts and state

These describe requirements to reconcile with existing types, not a demand to create new files/classes with these names.

### 4.1 Project/mission

Persist goal, output type, audience, acceptance criteria, current brief revision, relevant benchmark, policy generation, workspace, deadline, tool/compute/spend/storage limits, retention, owner and task graph. States map onto existing state machines: queued/planning/running/verifying/ready-for-review/accepted, with needs-input/paused/failed/cancelled paths. Clarification, safety approval, direction review and final acceptance are distinguishable reasons for waiting.

State/events must be replayable, authenticated and resource-scoped. Reconnection recovers current truth; chat text cannot declare a failed job successful. Schema evolution is additive/versioned and preserves populated stores.

### 4.2 Task/delegation

Persist bounded objective, dependencies, owner/lease/fencing generation, authority subset, input artifact versions, output schema, resource reservation, attempts and terminal result. A child cannot expand its parent's authority or escape the root budget. Parent cancellation/revocation propagates. Dependency cycles are rejected. Changed input invalidates dependent outputs. Independent workers cannot overwrite each other; joins validate outputs before advancing.

### 4.3 Tool operation

Typed input/output, capability and resource scopes, side-effect class, policy revision, timeout/cancellation, idempotency identity where valid, versions, usage and receipt. All nested calls inherit root accounting. Describe retries by semantics: safe read, reconciled write, or blocked uncertainty. Do not promise exactly-once external effects without provider support.

### 4.4 Artifact and evidence

Store content hash, media/type/schema, confined location, producing task/model/config/tool version, input lineage, sources/licensing, checks, review status and retention. Partial drafts remain partial. A verification report distinguishes executed, passed, failed, skipped, stale and blocked checks. A path or `exit 0` alone never establishes a correct website or video.

### 4.5 Feedback, reference and approval

Feedback binds to an exact artifact version plus selected region/timecode/passage where applicable. Preserve raw user feedback alongside any agent interpretation. Separate request-specific direction from reusable preferences and global requirements; ask if material scope is ambiguous.

Reference record: brief, approved artifact hash, approval scope and owner provenance, evaluation results, model/tool/runtime configuration, revision history and limitations. Improvement creates a new version; never overwrite historical approval or silently propagate a style to unrelated projects.

Authority approval binds action, destination/account, content hash, scope, expiry and policy generation. Edits/revocation invalidate it. Direction approval ≠ final acceptance ≠ publish/deploy authority ≠ independent security/screenshot sign-off.

## 5. Safe broad autonomy and capability acquisition

### 5.1 Allowed freedom

Within an explicitly authorised envelope, agents may research sources, inspect repositories, design workflows, download compatible candidates within limits, install/test in disposable environments, create helpers, build, render, evaluate and repair. Broad autonomy is not arbitrary host execution. Discovery does not itself grant activation rights, and zero-price requests still disclose data and consume resources.

Use this progression: **diagnose gap → discover candidates → inspect risks/compatibility → isolated trial → compare → adopt for task → scope/version promotion**. First ask whether the problem is missing information, bad context, model weakness, tool ergonomics, unclear direction or actual resource limitation. Installation is not the universal remedy.

### 5.2 Mandatory enforcement

- All consequential actions pass existing backend authority. Prompts, tool annotations and an LLM review are not the security boundary.
- Default-deny host personal folders, credentials, production accounts, privileged containers, host devices and Docker socket. Explicitly scoped runtime-control endpoints are brokered by trusted services, never general network grants to an untrusted browser/worker.
- Enforce public-fetch/browser egress rules outside the model, including redirect/DNS changes, private/loopback/link-local/metadata destinations and download limits. Local model access is an explicit separate brokered capability.
- External README/skill/tool/page instructions are untrusted. They cannot modify grants, system authority, approvals, tests or audit controls.
- Credentials remain brokered, scoped and redacted. Separate project browser profiles; never silently reuse the owner's main profile/cookies. A login/takeover action does not grant all account actions.
- Inspect dependency hooks, package scripts, model loaders/custom code, tools and native binaries. Remote model code is disabled by default unless separately reviewed and contained. Prefer safe weight formats; a format alone is not a guarantee.
- Enforce spend, inference, tool, browser-step, child-count/depth, storage, CPU/memory and wall-time budgets at dispatch. No budget reset by delegation/retry/provider change.
- Sandboxes need version/config-specific qualification on the actual platform. If required guarantees are absent, block dependent execution. Never fall back silently to host shell/desktop.
- Stop/cancel/revoke terminate owned subprocess trees/jobs and fence late outputs. Unknown in-flight writes require reconciliation. Preserve resumable artifacts and explain incomplete cleanup.
- Redact private source content from logs/screenshots. Retention/export/delete controls apply to browser data, memory, experiment traces and references.
- No quota/account rotation to evade limits, ignoring provider terms, or copyrighted asset reuse without a valid basis. Candidate code/data/model/media/service licenses must each be checked for intended use.

### 5.3 Promotion by consequence

| Change | Default proposal to validate in policy stories |
|---|---|
| Task-specific search/prompt changes using existing grants | Autonomous bounded trial; retain unchanged incumbent if better |
| Existing eligible model route for a task | Automatic selection based on relevant measurements and policy |
| New package/model/skill in disposable lab | Autonomous only under explicit experiment/network/resource grants; inspect before execution |
| Reusable project skill | Evidence-backed scoped promotion if policy allows; versioned rollback |
| Default workflow/model/tool across projects | Held-out evaluation plus limited canary; auto or owner review based on approved risk policy |
| New private-data egress, accounts, spending, publishing or deployment | Requires applicable prior explicit grant or owner decision; no inferred consent |
| Policy enforcement, credential boundary, sandbox, audit or evaluator authority | Outside ordinary self-improvement privileges; reviewed engineering/security change |

The requesting model cannot set its own promotion class. An automatic policy cannot modify its own evaluator, thresholds or safety requirements to pass.

## 6. Models, context and resource efficiency

### 6.1 Task-aware routing

Pipeline: **compatibility → measured capability → policy eligibility → resource fit → route → observe**. Record exact checkpoint/revision, quantisation, runtime/version, chat template, decoding, context, tool/schema/vision support, warm/cold cost and actual memory observations. Treat GGUF/MLX artifacts, speculative companions and standalone models correctly; library names and disk sizes do not prove support or residency.

Candidate roles include researcher, architect/planner, writer, coder, visual analyst and reviewer. Roles are logical: one loaded model may serve multiple roles sequentially. A routing agent may interpret task requirements but must select from validated evidence and cannot invent capabilities or eligibility.

The owner supplied a model-library screenshot showing seven stored entries and 66.8 GB on disk, including Qwen/Gemma/TwIL-labelled entries and GGUF/MLX-related formats. This is historical inventory context, not a verified current model catalog, available RAM, simultaneous-residency budget or benchmark. Re-discover exact IDs/formats rather than reconstruct truncated filenames or hard-code these entries.

Evaluate writing voice/structure, code repair, source use, architecture consistency, structured outputs, tool correctness and refusal behaviour separately. Public leaderboards guide discovery, not automatic replacement. Preserve manual overrides with compatibility/authority checks and explain rejected choices.

### 6.1a Dense and MoE evaluation on the confirmed pilot hardware

The pilot machine is owner-stated M5/24 GB unified memory/1 TB storage. OS, weights, KV cache, browser workers and rendering share finite host resources; disk size is not runtime memory. Retain hardware-independent capability/admission code.

MoE can reduce active computation per token but generally still needs access to the full expert weights. Do not estimate RAM from active parameter count. Any offloading/expert-loading exception requires exact backend support and measured latency/memory evidence. Internal experts need not correspond to human domains and are not independently selectable/loadable specialist models by default.

Compare dense and MoE exact checkpoints/quantisations on task success, total resident memory, context fit, warm/cold latency, tool reliability and energy/resource trade-offs. Do not download additional large models unless an observed capability gap and resource envelope justify it. Existing model inventory forms the initial evaluation pool.

All model inference, including planners, scripts, vision, judges, skill optimisation and generation used by output pipelines, stays local. If the local pool cannot meet requirements, preserve work and report the limitation; no silent hosted fallback. External source research/downloads require their own grants. Existing cloud-provider code elsewhere in Studio need not be deleted; AP must not route through it. This product constraint is not permission to regress established features in another program.

### 6.2 Scheduling and context

Use current lifecycle/resource ownership. On constrained hardware, preserve the qualified conservative residency policy until separately reviewed evidence supports changes. Keep useful models warm when permitted; account for load latency/hysteresis without omitting necessary review work. Browser/render/model memory share host resources; separate accounting leases from verified physical load/unload.

Exact required request assembly and target fit must be validated; do not silently drop the latest request, system constraints, tool results, approvals or needed evidence. Handoffs are portable task state, not assumed portable KV caches. Compression/retrieval must be tested by continuing the task and answering probes about decisions, artifacts, unresolved failures and authority. Compaction cannot weaken grants or convert untrusted content into instructions.

### 6.3 Model/paper watch

Only user-authorised scheduled watches make network requests. Capture provenance, publication/update dates, exact candidate versions, license, hardware/runtime needs and limitations. A paper becomes a hypothesis with measurable success and regression tests, not an unconditional implementation order. Prefer configuration/skills/retrieval changes before weight training. Large downloads and activation remain budgeted; all candidate/judge/optimisation inference must stay local.

## 7. Evaluation, benchmarks and feedback

### 7.1 Four independent meanings of quality

- **Technical:** requested functions work, valid artifacts, usable and accessible layouts, reliable recovery.
- **Factual/evidential:** appropriate sources support claims; uncertainty and contradictions are explicit.
- **Creative:** audience fit, originality, coherent style, narrative/pacing and non-generic execution.
- **Practical/commercial:** usefulness to the intended audience and business objective. This does not establish demand or revenue.

Use deterministic checks, real rendered/media inspection, blinded model comparisons where useful and actual user acceptance. Do not let one LLM score certify all dimensions. Different prompts or fresh contexts reduce some bias but do not establish statistical independence.

### 7.2 Evaluation architecture

Version task datasets/rubrics, required checks, provenance, hardware/runtime/model configs, costs and grader versions. Separate development, validation and held-out tasks; prevent training/tuning on held-out answers. Preserve the unchanged baseline as a candidate. Compare at matched or explicitly reported budget; repeat noisy runs and report variability/failures, not only best samples.

Initial dataset sizing (proposal, calibrate after baseline): 10 research briefs, 5 website briefs and 5 short-video briefs plus dedicated failure/security cases. All-unselected specialties can use synthetic fixtures without implying owner approval of scope. Do not claim broad reliability from this small pilot.

Proposed pilot target: at least 80% of in-scope runs accepted without user repair, alongside intervention time, first-pass success, repair attempts, median/tail latency, cost per accepted artifact and resource use. Thresholds become binding only through the approved evaluation policy. Critical safety/functional requirements are hard gates, not averaged away by creative scores. Record the denominator and why a run was excluded.

### 7.3 Feedback before benchmarks

Present representative samples early: one meaningful website page/interaction; a script/storyboard or short rendered segment; a research finding with sources. Perform obvious technical checks before asking. Ask a focused direction question rather than soliciting generic approval every few tool calls.

Allow free text, selected-element/passage/timecode feedback and optional actions: Continue, Change direction, Improve this part, Use as reference. No mandatory rating questionnaire. Preserve progress and briefly confirm consequential interpretation. Distinguish approve-as-delivered from adopt-as-reference; do not turn every comment into a permanent global rule.

### 7.4 After benchmarks

Retrieve relevant approved references by task/audience/language/style, not just keyword similarity. Agents self-check against them and ask less often. Re-engage when direction changes, conflicting feedback appears, standards cannot be met, context is materially different or authority is missing. Existing references never exempt new facts, code or assets from verification.

## 8. Smooth experience and specialised outputs

### 8.1 UI

Keep one clean entry point, progressive disclosure and artifact-first delivery. Reuse existing surfaces rather than adding another permanent toolbar. Useful views: brief, milestones, work/preview, sources/files, review and optional detailed activity. Show Working, Needs input, Ready for review, Failed, Paused and Cancelled from real state.

Expose pause/stop, exact decisions, resource use and authenticated takeover. Complete legible previews at actual narrow viewports and zoom; no scaled mock masquerading as responsive evidence. Generated HTML/SVG/JS previews are untrusted active content: sandbox/CSP, block secret-bearing origins/network by default, cap resources and avoid access to app credentials. Generative UI cards use allowlisted schemas/components, not arbitrary privileged executable UI.

No removal/reorganisation of Chat, Council, Pipeline, Compare, Harness or their controls without a separate owner decision and preserved feature mapping. Maintain keyboard/focus handling, reduced motion, mobile layout and theme behaviour.

### 8.2 Website workflow (candidate specialisation)

Brief → audience/problem/reference research → design direction sample → implementation → functional/security/mobile/accessibility checks → repair → preview/source/test package → final review → separately authorised deployment.

Require requested interactions to work end-to-end or be explicitly labelled unavailable; no fake checkout/contact forms, invented customer reviews or placeholder backend presented as complete. Check links, forms, inputs, responsive content, console/network failures, secrets, dependencies, assets and license/attribution. Visual checks include hierarchy, typography, spacing, consistency and actual screenshot inspection. Use isolated worktrees for concurrent edits, then integrate and rerun system checks. PR/CI failures return to the responsible worker without bypassing branch protections.

Delivery: source package, local runnable preview, tests/check report, dependency/config instructions, source/asset attribution and limitations. Production credentials, DNS, hosting and external writes are separately scoped.

### 8.3 Research-to-video workflow (candidate specialisation)

Brief/niche/region/language → topic signals → ranked shortlist with dates/evidence → original angle → verified claim pack → script → storyboard/direction sample → assets/narration → scene rendering → edit/subtitles → independent technical/editorial review → repairs → review package → separately authorised publishing.

Trending means observed source/time/region-specific signals; missing API access is not license to fabricate popularity. Use permitted APIs/public access; quotas/terms apply. Mark candidates and uncertain claims honestly. Prefer primary sources for consequential facts and check contradictory evidence. Avoid scraping and lightly rewriting someone else's video as original work.

A shared creative brief should include audience, voice, pacing, visual language, factual depth, intended emotion and originality constraints. Render scenes using suitable methods—stock/generated assets, SVG/diagrams, browser animations or other evaluated tools—rather than forcing every video through one stock-footage template.

Delivery: playable MP4, script/storyboard, captions, thumbnail/title/description drafts, sources and claim mapping, rights/attribution manifest, quality report and costs. Validate decoding, resolution/aspect/duration, timestamps, black/frozen/missing frames, subtitle readability/timing, audio clipping/silence/levels and relevant visual coverage. Watch the entire pilot; generated/transcribed audio can still mispronounce names or change facts. Licensing includes footage, music, fonts, voices and model/provider outputs. Voice cloning needs appropriate explicit consent. Platform monetisation/disclosure policies are checked when publishing; no eligibility guarantee.

A 60–90 second single-language pilot is a suggested low-cost test, not a fixed product limit. YouTube/news/explainer niche and format remain U01.

### 8.4 Other creative production

Novel writing, illustrations, slides and motion graphics are references for craft and consistency, not extra launch commitments. Define a new specialisation through a bounded workflow, rubric, fixtures, resource policy and accepted examples before advertising support.

## 9. Continuous improvement and retained experience

Four distinct layers: memory (what happened/preferences), skills (reusable methods), configuration optimisation (prompts/routing/retrieval/workflows), and weight training. Do not call all four model learning.

Production uses proven capabilities. A separate lab consumes redacted traces/feedback, diagnoses recurring failures, proposes candidates, runs comparisons and preserves losers as evidence when useful. Skill records include scope, prerequisites, permissions, versions, input/output schemas, examples, tests, provenance, known failures and expiry/revalidation triggers.

Promotion stages: experiment → task-specific use → project capability → canary → recommended default, with rollback. The class of change, evidence and policy decide whether promotion is automatic or human-reviewed. A repeated failure can quarantine/revoke a capability; late workers cannot reuse stale grants. Do not hot-mutate a running task's effective prompt/tool contract without explicit checkpoint/version semantics.

Refinement must retain the unchanged incumbent and allow no-op/stop. Detect quality plateaus, scope creep, escalating cost and unsupported claims. A system may honestly stop with a partial deliverable and a precise request for resources/direction rather than loop indefinitely or announce false success.

User approval is valuable preference evidence, but cannot waive factual/security requirements or permit global deployment by implication. Protect evaluation data, acceptance records and policy from the optimiser. Changes to core authority require a separate reviewed engineering path.

## 10. Delivery program

The companion ledger defines **AP-001…AP-032** with dependencies and acceptance criteria. All begin `not_started`; no story is claimed complete because a similar module exists. AP-001 reconciles evidence and may mark a requirement satisfied-by-existing only with actual consumer tests and owner-program coordination.

| Phase | Stories | Exit |
|---|---|---|
| P0 — reconcile and specify | AP-001–003 | Current capability/wiring map; decision register; versioned briefs, rubrics and fixtures |
| P1 — production spine | AP-004–008 | Enforced mission authority, resumable tasks, scoped artifacts, resource limits and legible control UI |
| P2 — research and capability discovery | AP-009–012 | Sourced research, controlled browser, inspected candidates and qualified disposable experiments |
| P3 — models, context and evals | AP-013–016 | Measured eligible routing, context continuity and reproducible challenger comparisons |
| P4 — collaboration and benchmark UX | AP-017–020 | Bounded specialist handoffs, direction samples, lightweight reviews and versioned references |
| P5 — specialised outputs | AP-021–024 | Independently evaluated website and video pilots for the selected scope |
| P6 — learning and improvement | AP-025–028 | Evidence-backed skills, authorised watch jobs, bounded refinement and reversible promotion |
| P7 — external operations and release | AP-029–032 | Scoped publishing/schedules, optional desktop qualification, production acceptance and documentation |

Dependency graph, not numbering alone, defines readiness. Browser/API research may progress while model experiments remain hardware-blocked. P5 specialisations may be reordered after U01. Basic safety/evaluation/review are required before pilots; full automatic system improvement can follow them. AP-030 is optional and cannot become a launch blocker unless a chosen use case requires desktop control.

Illustrative smallest vertical slice: fixed website brief → authorised workspace → one builder → actual browser test → bounded repair → early review sample → accepted preview. Add safe research, measured routing and specialists incrementally. Do not begin with a full multi-framework deployment.

## 11. Reference registry

### 11.1 Interpretation and adoption rules

Every entry below is a research/reference candidate, not an endorsed dependency. README observations can age; verify current maintainer, commit, license and source. Topic directories/star counts are discovery signals only. Record **reference-only / evaluate / adopt / reject / retired**, exact version, rationale, permissions, dependencies, maintenance/security status, resource cost, data egress and replacement/rollback plan. Library licenses do not automatically cover model weights, datasets, assets, hosted services or trademarks. Verify no-license entries before reuse.

Bake off a candidate against existing/direct implementation on representative tasks. Demand typed boundaries, cancellation, receipts and scoped credentials. Reject duplicative control planes or opaque nested autonomy. MCP=tool interface; AG-UI=agent/UI event interface; ACP=agent-client interoperability in relevant projects; none provides sandboxing or authorization by itself. Add protocol adapters only when there is a concrete interoperability need.

### 11.2 All owner-supplied repositories and discovery sources

| Ref | URL | Useful idea/component | Caveat / intended treatment |
|---|---|---|---|
| R01 | https://github.com/PurpleDoubleD/locally-uncensored | Integrated local chat/code/image/video experience; model/media UX | README declares AGPLv3; review obligations before code reuse. Reference first; no requirement to adopt refusal-removal features. |
| R02 | https://github.com/vercel-labs/agent-desktop | Accessibility-tree computer observation/actions; CLI/MCP | Driver, not sandbox. Evaluate inside dedicated guest environment; validate permissions/privacy. |
| R03 | https://github.com/harry0703/MoneyPrinterTurbo/blob/main/README-en.md | Script/footage/narration/subtitle/render pipeline | Existing M5 integration must be audited first. Independent quality/rights checks; not proof of income or originality. |
| R04 | https://github.com/govijr/terminal-mac | Separate macOS guest, MCP observe/act/verify and takeover | README described early release candidate, Apple-Silicon requirements and guided setup; verify maturity and actual isolation. |
| R05 | https://github.com/browser-use/browser-use | Browser automation, structured tasks, local/hosted browser options | Compare with direct Playwright. Own all child budgets/cancellation; cloud and model services have separate costs/privacy. |
| R06 | https://github.com/CopilotKit/openmuse | Durable work, plans/receipts, pause/resume and persistent browser takeover | README alpha; Intelligence/service dependencies; design reference, not automatic full-stack migration. |
| R07 | https://github.com/CopilotKit/OpenBot | Agent computers, tool permissions and interoperable agent workspace | Inspect component seams/security; do not create second authority/scheduler. |
| R08 | https://github.com/CopilotKit/OpenDuet | Proactive/scheduled work, reviews, masked credentials and briefings | Built on OpenBot; examine actual mechanisms rather than trust README safety claims. |
| R09 | https://github.com/CopilotKit/open-research-ANA | Source-backed research canvas and HITL | README directs current work to CopilotKit/CopilotKit `examples/showcases/research-canvas`; recheck location and dependencies. |
| R10 | https://github.com/CopilotKit/OpenDots | Specialist access, shared Spaces/artifacts and review cards | Alpha, computer/services and Intelligence dependencies; references for scoped handoffs/review. |
| R11 | https://github.com/orgs/CopilotKit/repositories?type=all | Discover examples/adapters/testing tools | Not an adoption checklist; recheck maintenance, provenance and overlaps. |
| R12 | https://github.com/topics/agent-swarm | Discover orchestration/delegation approaches | Stars/topic membership do not establish quality/security; avoid fleets without measured need. |
| R13 | https://github.com/OpenHands/openhands | Coding execution, backends, workspaces, engineering automation | Current README at review described Agent Canvas; inspect actual SDK/component versions. Non-sandboxed mode warns of host filesystem access. |
| R14 | https://github.com/langflow-ai/langflow | Repeatable visual workflows, API/MCP export and stepwise inspection | Possible recipe authoring/reference; do not require a node editor or another orchestrator in main UX. |
| R15 | https://github.com/ripienaar/free-for-dev | Affordable service discovery | Catalog is not live pricing/terms evidence. Verify commercial rights, quotas, region, billing and exit cost; never auto-sign up or evade limits. |
| R16 | https://github.com/shubhamsaboo/awesome-llm-apps | Task-specific examples, skills and RAG/workflow patterns | Example quality/security/model portability require own tests; no blind installs. |
| R17 | https://github.com/punkpeye/awesome-mcp-servers | Candidate MCP discovery across domains | Includes experimental servers; protocol/directory labels are not trust guarantees. Validate real side effects and updates. |
| R18 | https://github.com/nexu-io/open-design | Design systems, prototypes, visual assets, slides/motion workflows and craft | Use to improve non-generic output. Verify model/provider/CLI dependencies and code/asset licenses. |
| R19 | https://github.com/wheresryan22/anatomy | Focused SVG/isometric/WebGL explanatory visual skill; precision/craft checks | Specialised technique, not general factual simulator; test local-model ability, accessibility and rendering correctness. |
| R20 | https://github.com/NousResearch | Discover Hermes skills, evaluation, memory and creative production ecosystem | Organisation includes forks, archived research and experiments; identify each project's real status. |

### 11.3 Additional discussed references and libraries

| Ref | URL | Idea to carry forward | Caveat / story links |
|---|---|---|---|
| R21 | https://github.com/OrchestratorInc/agent-orchestrator | Task-owned worktrees/branches, feedback from tests/CI/reviews routed to worker | AP-017/021/022; reference patterns, not another coordinator. |
| R22 | https://github.com/desplega-ai/agent-swarm | Structured results, dependency joins, durable workers and scoped collaboration | AP-005/017; do not inherit large default fleets, exposed endpoints or deployment assumptions. |
| R23 | https://github.com/CopilotKit/pathfinder | Docs/repository/conversation retrieval candidate | Further evaluation only; AP-009/011/015. |
| R24 | https://github.com/CopilotKit/aimock | Repeatable mock model/MCP/search/interoperability tests | Further evaluation only; AP-003/016/032; mocks do not prove live safety. |
| R25 | https://github.com/NousResearch/hermes-agent | Experience-derived skills, recall, subagents/model routes and backends | AP-015/017/025; evaluate mechanism; do not reproduce exclusivity/marketing claims. |
| R26 | https://github.com/NousResearch/hermes-agent-self-evolution | DSPy/GEPA variants, traces, constraint gates and reviewed changes | README marked skill optimisation implemented; tool descriptions/system prompts/code/continuous loop planned. AP-026/028; not autonomous safe core rewriting. |
| R27 | https://github.com/NousResearch/autoreason | Keep incumbent, compare revision/synthesis blind, stopping/no-op | AP-016/027; reported research results not reproduced; fresh judges can share bias. |
| R28 | https://github.com/NousResearch/hermes-toolperf-evals | Real failure taxonomy, deterministic success markers and A/B tool ergonomics | AP-003/016; measure local results, not promised upstream speedups. |
| R29 | https://github.com/NousResearch/hermes-compression-eval | Continuation probes for decisions, artifacts and constraints after compression | AP-015; LLM grading is nondeterministic; retain deterministic checks. |
| R30 | https://github.com/NousResearch/kanban-video-pipeline | Director/cinematographer/renderers/editor, structured handoffs, taste documents and scene review | AP-017/023/024; demo has hosted dependencies; upstream timings not local guarantees. Agent director approval is not owner publishing approval. |
| R31 | https://github.com/NousResearch/autonovel | Canon, voice, outline, propagation debts, mechanical+editorial checks and plateau stopping | Craft/consistency reference, not extra launch domain or commercial fiction proof. AP-015/023/027. |
| R32 | https://github.com/NousResearch/hermes-official-plugins | Lean core, separately maintained integrations, reviewed commit pins and ownership tiers | AP-011/012/028; reviewed pin is not sandbox qualification. |
| R33 | https://github.com/NousResearch/creative-writing-bench | Rubric plus pairwise writing assessment and bias considerations | Fork; inspect upstream https://github.com/EQ-bench/creative-writing-bench. AP-003/016; no direct substitute for user script rubrics. |
| R34 | https://github.com/NousResearch/atropos | Trajectories and RL environment evaluation | README reviewed explicitly marked archived/unmaintained. Research reference only, not launch dependency. |
| R35 | https://github.com/stanfordnlp/dspy | Program/prompt optimisation candidate | AP-026; compare simple baseline first; protect held-out data. |
| R36 | https://github.com/gepa-ai/gepa | Trace-informed reflective candidate optimisation | AP-026; benchmark actual benefit and calls/cost. |
| R37 | https://github.com/imbue-ai/darwinian_evolver | Code-evolution reference from Hermes proposal | Later research only; review AGPL and authority implications; never self-edit safety/eval controls. |
| R38 | https://github.com/microsoft/playwright | Deterministic browser operations, functional/visual checks, capture | AP-010/021/022; dedicated profiles and enforced egress; tests need real installed browser. |
| R39 | https://ffmpeg.org/ and https://ffmpeg.org/ffprobe.html | Render, mux, probe and validate media | AP-023/024; pin builds/features, review codec/license needs, contain malformed inputs. |
| R40 | https://huggingface.co/docs/hub/ and https://huggingface.co/models | Model/paper discovery and versioned provenance | AP-013/026; verify exact revision/license/runtime, safe formats, network/download budget. |
| R41 | https://github.com/jundot/omlx | Local model-serving ecosystem candidate/reference | AP-013/014; verify identity/version against installed runtime and current M5 adapter docs; no blanket GGUF/MLX compatibility claim. |
| R42 | https://agentskills.io/ | Portable skill format | AP-025; instruction format does not grant execution trust or portability of model quality. |
| R43 | https://modelcontextprotocol.io/ | Tool/resource interoperability | AP-011/012; local policy authorizes each operation; server descriptions are untrusted. |
| R44 | https://github.com/ag-ui-protocol/ag-ui | Agent/UI event interoperability | Optional need-driven adapter; no mandate to replace current FastAPI/vanilla UI. |
| R45 | https://github.com/AgentClientProtocol/agent-client-protocol | External coding-agent client interoperability candidate | Verify supported project versions before adoption; optional, not execution authority. |

Tavily/search and Composio/connectors remain optional research/connector candidates, not approved purchases. Hosted model inference is excluded. Narration/image/video generation must use local inference for this scope; any proposed external media service needs explicit clarification and cannot be smuggled in as a non-model tool. Select against U03/U04 and adapters, with explicit egress and costs. React/Next.js/p5.js/Remotion/SVG/WebGL may be output technologies; none is mandatory for the Studio UI or every deliverable. Open-design/Anatomy/Hermes demos are inspiration for craft, not proof of local model output quality.

## 12. Verification, release and rollback

### 12.1 Evidence requirements

For each acceptance criterion: executed command/case, source SHA, environment, actual result, artifact/log location and limitations. Sanitise logs. Keep live tests opt-in; default tests offline and credential-free. Mock success cannot become installed qualification. Include positive, negative, fault and cancellation/restart cases; exercise app and generated standalone where relevant.

Follow current `AGENTS.md` and CI; at inspected snapshot representative commands include:

```sh
python scripts/run_scoped_tests.py --base origin/main --head HEAD --worktree
ruff check .
mypy
python scripts/check_frontend_syntax.py
python -m pytest -q
python scripts/smoke_local.py
python scripts/smoke_local.py --standalone
python scripts/build_studio.py
python scripts/generate_route_matrix.py
python scripts/generate_subsystem_inventory.py
python scripts/generate_roadmap_status.py
python scripts/validate_governance.py
python scripts/validate_security_signoff.py
python scripts/validate_docs.py
python scripts/check_commit_messages.py --base origin/main
```

Run the current UI source/inventory/route/authority/sign-off generators and `--check` gates in dependency order whenever tracked surfaces change. Check latest CI for exact commands. Record full tests and browser checks separately; a scoped pass is not full release acceptance. Generate first, then verify no unintended drift; distinguish intentional checked-in changes from stale artifacts.

Browser smoke must cover old capture helpers as well as new walks. Changes to Density/theme/Settings/focus or control location require updating real browser interactions, not only structural tests. Regenerate screenshots at genuine viewports; never approve a new baseline yourself. Any evidence hash change triggers invalidation/reverification, not copy-editing the hash to green.

### 12.2 Release gates

- Existing feature/API/session/workspace preservation and populated-data compatibility.
- Required installed sandbox/runtime qualification and unresolved security gates honoured.
- End-to-end pilot artifacts accepted within selected scope; no unsupported capability advertised.
- All critical evaluation, secret/egress, budget, cancellation, replay, stale-worker and hostile-input tests pass.
- Real browser/media evidence for user-visible outputs; explicit human reference/creative approvals and independent sign-offs as required.
- Applicable CI green at the implementation head; upstream merges require source/evidence freshness checks.
- README/help accurately distinguish supported, experimental, blocked and unavailable features.
- Reproducible package/standalone and applicable supply-chain checks; no credentials or unlicensed assets committed.
- Branch pushed and PR opened/linked. Do not bypass existing branch protections or merge restrictions.

### 12.3 Rollback

Feature-flag each adapter/workflow/promotion. Revoke grants and quiesce workers before disabling. Retain user artifacts and approval history. Versioned routes/configs support a last-known-good baseline; schema changes need tested additive migration and rollback/forward-recovery strategy. Quarantine failed candidates, invalidate downstream evidence and never delete user work to resolve a version mismatch. An external action may be irreversible; record reconciliation/remediation rather than pretend `git revert` undoes it.

## 13. Independent-agent rules

1. Read current guidance, this specification, ledger, prompt, status and active PRs. Fetch main; document exact base and overlapping owners.
2. Select one authorised ready story; claim it through the established repository protocol. Claims/heartbeats and handoffs are coordination data, not security grants.
3. Trace the existing producer→authority→executor→consumer path. Record reuse/extend/missing/dormant status and open dependencies.
4. Before edits, record blast radius: alternatives, affected files/routes/generated artifacts, pinned tests, data/privacy implications and rollback. Stop for a blocking scope/authority ambiguity.
5. Implement the smallest compatible slice, with tests that would fail before the change. No unrelated refactor, framework migration or silently weakened pin.
6. Preserve evidence and actual test outcomes. Do not set human approvals, independent reviewer fields or completed status on author assertion.
7. Push the story branch and open/update one PR with documented commits. If auth unavailable, export a patch AND uploadable `.txt` equivalent; mark `delivery_pending`, not delivered. Never ask for pasted tokens.
8. Handoff includes objective, base/head, changed files, migrations, acceptance mapping, commands/results/skips, evidence locations, remaining blockers, next ready story and push/PR state.
9. Update only AP tracking plus directly required owning-program records with coordination; preserve historical ledgers. An agent beginning later must need no private chat context.

**Final-product test:** can a user describe a selected outcome, establish direction with lightweight feedback, let a bounded team complete and verify it, receive the real artifact, and later obtain equally good or better work using retained evidence—without risking unrelated files/accounts or supervising every tool call? That is the destination; integration count is not.

## Appendix A — Story cards (ledger revision 1.1)

Generated from the companion JSON. Tracking lives in JSON; regenerate cards when requirements change. All initial stories remain not_started.

### AP-001 — Reconcile programs and trace current production gaps

**Phase:** P0 • **Dependencies:** none • **Optional:** false

Create a source-grounded delta map; prevent duplicate implementations and unauthorized LC activation.

**Deliverables:** AP-to-existing-program reconciliation; Producer/authority/executor/consumer matrix; Program registration proposal and story claim convention.

**Acceptance criteria:**
1. Every required capability is labelled reuse, extend, missing, dormant or blocked with file/test/caller evidence.
2. Existing runtime/UI/LC claims and blockers are preserved; no other program is silently reopened.
3. MoneyPrinter, lesson promotion, routing and durable delegation are explicitly assessed before wrappers are proposed.
4. Current security/installed qualification and generated-file obligations are recorded.
5. One authoritative AP tracking source and owner-approved activation scope are identified; no code activation in this story.

**Inspect first:** `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `docs/lc08-current-status.md`, `docs/runtime-dispatch-entry-points.md`, `product/M5_2_0_Autonomous_Roadmap.json`, `product/M5_2_0_UIUX_Autonomous_Roadmap.json`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R07, R13, R25. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `owner_authorizes_program_registration_and_scope`.

### AP-002 — Resolve mission scope and operating-envelope decisions

**Phase:** P0 • **Dependencies:** AP-001 • **Optional:** false

Turn open decisions into explicit scoped configuration without blocking unrelated offline work.

**Deliverables:** Decision register U01-U06; Hardware/runtime inventory recipe; Policy and budget examples using fictional fixtures.

**Acceptance criteria:**
1. Primary/next specialisation and audience are owner-selected or explicitly pending.
2. Owner-stated M5/24 GB/1 TB is recorded; runtime versions, actual free resources and qualified headroom are measured separately without hard-coding the platform.
3. Provider spending/egress/account/file grants are explicit and versioned; absence denies access.
4. Automatic promotion classes and review boundaries are documented without inferred consent.
5. Unresolved decisions list exactly which stories/actions are blocked and what safe work can proceed.

**Inspect first:** `hardware_capabilities.py`, `onboarding_preflight.py`, `routing_policy.py`, `scoped_grants.py`, `docs/model-lifecycle-capability-feasibility.md`.

**Existing test anchors:** `tests/test_scoped_grants.py`, `tests/test_routing_consent_policy.py`

**References:** R15, R40, R41. **Decision dependencies:** U01, U02, U03, U04, U05, U06. **Approval class:** `owner_decisions_for_real_hardware_accounts_and_budgets`.

### AP-003 — Establish versioned evaluation briefs and failure fixtures

**Phase:** P0 • **Dependencies:** AP-001 • **Optional:** false

Define the output-quality evidence bar before optimising models or swarms.

**Deliverables:** Versioned briefs/rubrics and baseline protocol; Development/validation/held-out partition; Fault/security fixture inventory.

**Acceptance criteria:**
1. Technical, factual, creative and practical criteria are separately represented.
2. Baseline includes a single-agent strategy and comparable resource accounting.
3. Fixtures include injection, invalid artifacts, uncertain writes, cancellation, stale workers and lost context.
4. Small initial dataset sizes and acceptance targets are labelled provisional until calibrated.
5. Held-out answers cannot be modified/read by ordinary optimisers; real private traces are scrubbed before use.

**Inspect first:** `workflow_evaluation.py`, `failure_injection.py`, `benchmarks`, `tests/test_workflow_evaluation.py`.

**Existing test anchors:** `tests/test_workflow_evaluation.py`

**References:** R16, R24, R28, R33. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-004 — Enforce a mission-wide authority and budget envelope

**Phase:** P1 • **Dependencies:** AP-001, AP-002, AP-003 • **Optional:** false

Extend the existing admission path so every project action is scoped and root-budgeted.

**Deliverables:** Mission-to-existing-task authority mapping; Nested budget/grant propagation; Revocation and envelope tests.

**Acceptance criteria:**
1. API, MCP, scripts, browser and media dispatch use the same authority checks.
2. Child/retry/provider changes cannot reset or expand root budget and scopes.
3. Revocation/policy generation invalidates queued and stale operations.
4. Network-free startup and deny-by-default behaviour remain intact.
5. Positive/negative tests exercise actual application consumers, not just standalone contract helpers.

**Inspect first:** `execution_spine.py`, `execution_budget.py`, `execution_profiles.py`, `scoped_grants.py`, `app.py`.

**Existing test anchors:** `tests/test_execution_spine.py`, `tests/test_execution_budget.py`, `tests/test_scoped_grants.py`

**References:** R07, R25. **Decision dependencies:** U03, U04, U06. **Approval class:** `normal_story_authorization`.

### AP-005 — Compose durable mission tasks and recoverable joins

**Phase:** P1 • **Dependencies:** AP-004 • **Optional:** false

Reuse durable task/checkpoint primitives for versioned project plans and task dependencies.

**Deliverables:** Mission graph/state mapping; Lease/checkpoint integration; Restart and false-completion tests.

**Acceptance criteria:**
1. Dependency cycles and illegal transitions are rejected.
2. Completed artifacts survive restart and resume validates current grants/budget/input versions.
3. Stale workers cannot overwrite successors or mark incomplete work successful.
4. Parent joins validate required outputs and report partial/failed children explicitly.
5. Populated-store migrations preserve sessions and user data; no destructive reset fallback.

**Inspect first:** `workflow_state.py`, `workflow_checkpoints.py`, `recovery_guard.py`, `task_events.py`, `child_delegation.py`.

**Existing test anchors:** `tests/test_workflow_state.py`, `tests/test_workflow_checkpoints.py`, `tests/test_child_delegation.py`

**References:** R06, R22, R25. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-006 — Extend artifact identity, provenance and selective invalidation

**Phase:** P1 • **Dependencies:** AP-005 • **Optional:** false

Make useful, checked artifacts the unit of delivery and collaboration.

**Deliverables:** Typed artifact/evidence records; Confined preview/export references; Dependency invalidation and retention tests.

**Acceptance criteria:**
1. Artifact bytes, input versions, producer/config and checks have verifiable identities.
2. Input changes invalidate affected descendants without deleting unrelated work.
3. Path traversal, symlink escape, mismatched hashes and unsafe active previews are refused.
4. Partial outputs and missing checks cannot appear accepted or complete.
5. Retention/export/delete preserve privacy and scoped reference history.

**Inspect first:** `artifact_lineage.py`, `verification_evidence.py`, `workspace_paths.py`, `history_privacy.py`, `static/index.html`.

**Existing test anchors:** `tests/test_artifact_lineage.py`

**References:** R10, R18. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-007 — Implement bounded execution, cancellation and truthful recovery

**Phase:** P1 • **Dependencies:** AP-004, AP-005, AP-006 • **Optional:** false

Make autonomous work stop, resume and report failure honestly.

**Deliverables:** Owned subprocess/browser job lifecycle; No-progress and repair-attempt limits; Fault/restart/cancellation evidence.

**Acceptance criteria:**
1. Cancellation stops dispatch and terminates owned work with measured cleanup latency.
2. Late outputs are fenced; unresolved external effects remain uncertain and require reconciliation.
3. No-progress, repeated repair, deadline and memory/storage exhaustion stop boundedly.
4. Runtime isolation absence blocks dependent execution rather than switching to host shell.
5. User receives retained partial artifacts and an actionable limitation instead of false success.

**Inspect first:** `process_supervision.py`, `execution_budget.py`, `sandbox_boundary.py`, `side_effect_ledger.py`, `recovery_guard.py`.

**Existing test anchors:** `tests/test_execution_budget.py`, `tests/test_side_effect_ledger.py`, `tests/test_sandbox_boundary.py`

**References:** R06, R13. **Decision dependencies:** U06. **Approval class:** `normal_story_authorization`.

### AP-008 — Expose clean mission controls and artifact-first status

**Phase:** P1 • **Dependencies:** AP-005, AP-006, AP-007 • **Optional:** false

Provide a legible progress/review surface without undoing the approved UI simplification.

**Deliverables:** Progress/artifact views; Pause/stop/needs-input UX; Accessible narrow and reconnect evidence.

**Acceptance criteria:**
1. Status derives from durable backend state and recovers after reconnect.
2. Preview/files/sources and resource use are available without a permanent control-heavy toolbar.
3. Pause, stop and pending decisions preserve drafts and show real outcomes.
4. Keyboard/focus, light/dark, actual phone widths and 200-percent zoom remain usable.
5. Untrusted artifact previews cannot access Studio credentials or privileged origin/network APIs.
6. Existing six-pane feature/control routes remain mapped and working.

**Inspect first:** `static/index.html`, `app.py`, `task_events.py`, `scripts/uiux_source_manifest.py`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R06, R10, R18. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

**Revision 1.2 additional acceptance:** Satisfy applicable requirements in docs/autonomous-production-ux-direction.md; preserve existing features and obtain actual implementation review, not concept-derived approval.

### AP-009 — Add source-backed safe search, reading and repository inspection

**Phase:** P2 • **Dependencies:** AP-003, AP-004, AP-006, AP-007 • **Optional:** false

Research missing information with provenance and enforced public-source boundaries.

**Deliverables:** Search/read adapters; Claim/source and contradiction records; Hostile-source/redirect/download tests.

**Acceptance criteria:**
1. Sources record URL, retrieval date, publication date if known and supporting excerpts/provenance.
2. Unsupported facts, missing dates and contradictions remain explicit.
3. Private/loopback/link-local/metadata access and redirect/DNS bypasses are blocked at the network boundary.
4. External instructions cannot expand grants or trigger installation.
5. Repository inspection is read-only and respects request/download/storage budgets.
6. Provider access failure yields honest incomplete research, not invented citations.

**Inspect first:** `private_network_gateway.py`, `mcp_client.py`, `scoped_grants.py`, `artifact_lineage.py`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R09, R15, R23, R40. **Decision dependencies:** U03, U04. **Approval class:** `normal_story_authorization`.

### AP-010 — Qualify a controlled browser worker and human takeover

**Phase:** P2 • **Dependencies:** AP-007, AP-009 • **Optional:** false

Evaluate Browser Use/direct Playwright through a narrow governed worker interface.

**Deliverables:** Browser adapter bake-off; Dedicated session and takeover controls; Real-browser cancellation/privacy evidence.

**Acceptance criteria:**
1. Compare candidate versus deterministic browser baseline on the same tasks.
2. Each project uses an isolated profile/context; no automatic personal-profile reuse.
3. All navigation/download/tool activity remains within grants and nested budgets.
4. Authenticated takeover pauses conflicting agent actions and safely resumes.
5. Hostile pages, private-network attempts and unexpected popups cannot obtain added authority.
6. Stop/restart closes or recovers owned browser jobs and session state without secret leakage.

**Inspect first:** `mcp_client.py`, `private_network_gateway.py`, `process_supervision.py`, `sandbox_boundary.py`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R05, R06, R38. **Decision dependencies:** U04, U06. **Approval class:** `normal_story_authorization`.

### AP-011 — Discover and inspect capability candidates without activation

**Phase:** P2 • **Dependencies:** AP-009 • **Optional:** false

Diagnose gaps and produce structured tool/model/skill/service proposals.

**Deliverables:** Capability-gap classifier; Candidate registry/proposal schema; License/permission/dependency assessment.

**Acceptance criteria:**
1. Distinguish missing facts, method, model capability, context, tool and hardware gaps.
2. Discovery can conclude that no new dependency is needed.
3. Candidates record source revision, maintenance, license, install hooks, resource cost, egress and privileges.
4. Directory stars and MCP annotations never establish trust.
5. Unlicensed/unknown or incompatible candidates are blocked for affected reuse, not silently installed.
6. Discovery itself has no execution or production activation authority.

**Inspect first:** `capability_negotiation.py`, `model_discovery.py`, `supply_chain.py`, `scoped_grants.py`.

**Existing test anchors:** `tests/test_model_discovery.py`

**References:** R01, R11, R12, R15, R16, R17, R23, R32, R43. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-012 — Run disposable capability experiments with receipts

**Phase:** P2 • **Dependencies:** AP-007, AP-010, AP-011 • **Optional:** false

Permit broad safe experimentation only inside a qualified, explicitly granted lab.

**Deliverables:** Experiment environment/lifecycle; Pinned install and artifact receipts; Quarantine/revoke/cleanup paths.

**Acceptance criteria:**
1. Package/model/skill install and execution require the applicable experiment/network/resource grants.
2. Untrusted candidates have no host mounts, production credentials or privileged runtime sockets.
3. Install hooks/custom loaders and binaries are inspected and constrained.
4. Every trial records exact versions, effects, checks, cost and cleanup outcome.
5. Failure or unavailable qualification blocks trial; no host fallback.
6. Successful task-local use does not silently become global capability or policy.

**Inspect first:** `sandbox_boundary.py`, `supply_chain.py`, `workspace_quarantine.py`, `scoped_grants.py`, `process_supervision.py`.

**Existing test anchors:** `tests/test_sandbox_boundary.py`, `tests/test_scoped_grants.py`

**References:** R02, R04, R07, R13, R32. **Decision dependencies:** U02, U04, U06. **Approval class:** `normal_story_authorization`.

### AP-013 — Build measured model capability and compatibility profiles

**Phase:** P3 • **Dependencies:** AP-003, AP-004, AP-011 • **Optional:** false

Evaluate exact model configurations instead of routing by brand or parameter count.

**Deliverables:** Model/profile schema extensions; Task-quality probes; Version/hardware-calibration evidence.

**Acceptance criteria:**
1. Profiles include runtime/checkpoint/quantisation/template/context/tool/vision support and measurement provenance.
2. Separate model companions/artifacts, installed disk inventory, readiness and residency.
3. Writing, coding, architecture, source handling and schema/tool reliability are measured separately.
4. Model-card claims/imported benchmarks are not local qualification.
5. Unknown capabilities fail explicitly and manual overrides remain bounded.
6. Mistral/TwIL/Qwen/Gemma role suggestions remain hypotheses until tested.
7. Dense and MoE candidates are compared on total resident memory, active computation, context/KV-cache, exact runtime support and actual task quality; low active parameters do not establish low RAM use.
8. MoE internal experts are not treated as separately loadable coding/writing agents without a specifically verified runtime capability.

**Inspect first:** `model_registry.py`, `model_calibration.py`, `model_recommendation.py`, `benchmark_import.py`, `benchmark_publication.py`.

**Existing test anchors:** `tests/test_model_calibration.py`, `tests/test_model_recommendation.py`, `tests/test_model_discovery.py`

**References:** R25, R33, R40, R41. **Decision dependencies:** U02, U03. **Approval class:** `normal_story_authorization`.

### AP-014 — Compose policy-aware routing and resource-safe scheduling

**Phase:** P3 • **Dependencies:** AP-004, AP-007, AP-013 • **Optional:** false

Select eligible models per task stage without bypassing existing lifecycle qualification.

**Deliverables:** Routing evidence/decision records; Warm/cold and affinity-aware scheduling; Fit/ownership/fallback tests.

**Acceptance criteria:**
1. Compatibility, measured quality, grants and actual resource fit all precede dispatch.
2. No unsupported simultaneous residency or dormant native activation is introduced.
3. All model inference remains local; hosted-model fallback is refused even when local fit or quality is insufficient; report a scoped limitation instead.
4. Load/unload accounting is not confused with physical completion; uncertainty remains charged.
5. Routing explains selected/rejected models and preserves manual override boundaries.
6. Compare end-to-end quality, latency, switch count and cost with existing baseline.

**Inspect first:** `model_lifecycle_coordinator.py`, `native_inference_scheduler.py`, `request_fit_admission.py`, `routing_policy.py`, `routing_experiments.py`, `resource_admission.py`.

**Existing test anchors:** `tests/test_request_fit.py`, `tests/test_request_fit_admission.py`, `tests/test_routing_experiments.py`, `tests/test_routing_policy.py`

**References:** R25, R40, R41. **Decision dependencies:** U02, U03, U06. **Approval class:** `normal_story_authorization`.

### AP-015 — Preserve context, preferences and artifact continuity

**Phase:** P3 • **Dependencies:** AP-005, AP-006, AP-014 • **Optional:** false

Test the ability to continue correctly after compression, retrieval and model handoff.

**Deliverables:** Context selection and reassembly integration; Continuation probe bank; Privacy and project-isolation tests.

**Acceptance criteria:**
1. Required brief, unresolved failures, current artifacts, approvals and authority survive or are explicitly reconstructed.
2. No silent truncation of required input to force fit.
3. Continuation probes verify decisions and artifact trails, not only summary readability.
4. Retrieved memory/skills retain provenance and cannot upgrade untrusted instructions.
5. Cross-project/private information cannot leak through recall.
6. Model switch/restart tests validate portable state without assuming KV-cache portability.

**Inspect first:** `context_store.py`, `context_reassembly.py`, `request_fit.py`, `routing_policy.py`, `evidence_lessons.py`, `history_privacy.py`.

**Existing test anchors:** `tests/test_context_reassembly.py`, `tests/test_evidence_lessons.py`

**References:** R23, R25, R29, R31. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-016 — Implement reproducible challenger and workflow evaluations

**Phase:** P3 • **Dependencies:** AP-003, AP-006, AP-013, AP-015 • **Optional:** false

Compare models, tools, prompts and teams without self-grading or hidden budget differences.

**Deliverables:** Evaluation runner/report integration; Blinded/held-out comparisons; Metric and scorer failure tests.

**Acceptance criteria:**
1. Run records pin exact task/config/tool/model/environment and include all failures.
2. Compare single-agent and specialist strategies under reported comparable budgets.
3. Deterministic checks and independent factual/user evidence accompany model scores.
4. No-op incumbent and held-out regression tasks participate.
5. Scorer failure, missing evidence and stale source hashes fail closed.
6. Report variability and intervention/cost/latency metrics; never promote on a fabricated score.

**Inspect first:** `workflow_evaluation.py`, `model_comparison.py`, `verification_evidence.py`, `benchmarks`.

**Existing test anchors:** `tests/test_workflow_evaluation.py`

**References:** R24, R27, R28, R33, R35, R36. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-017 — Integrate bounded specialists and versioned handoffs

**Phase:** P4 • **Dependencies:** AP-005, AP-006, AP-007, AP-014, AP-015, AP-016 • **Optional:** false

Enable useful collaboration without a second coordinator or uncontrolled fan-out.

**Deliverables:** Typed specialist tasks and joins; Worktree/scoped-write ownership; Team versus baseline evidence.

**Acceptance criteria:**
1. Authority and remaining budget are strict parent subsets.
2. Workers use versioned input/output artifacts and cannot overwrite conflicting scopes.
3. Cancellation/failure/restart joins are durable and bounded.
4. Reviewer receives brief/evidence rather than an instruction to endorse the builder.
5. Logical parallelism does not imply concurrent local inference.
6. Retain a team topology only where measured trade-off improves over simpler execution.

**Inspect first:** `child_delegation.py`, `workflow_orchestration.py`, `artifact_lineage.py`, `execution_budget.py`.

**Existing test anchors:** `tests/test_child_delegation.py`, `tests/test_workflow_evaluation.py`

**References:** R07, R10, R21, R22, R25, R30. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-018 — Present early direction samples before expensive production

**Phase:** P4 • **Dependencies:** AP-008, AP-016 • **Optional:** false

Let users establish direction without supervising every operation.

**Deliverables:** Direction-checkpoint logic; Representative sample and focused question UX; Continue/redirect/resume tests.

**Acceptance criteria:**
1. Unbenchmarked tasks propose a small relevant sample before expensive full production.
2. Sample has passed basic technical checks and names what feedback is needed.
3. Feedback pause preserves artifacts, job state and draft.
4. Direction approval does not imply final acceptance or publish permission.
5. Benchmarked similar tasks skip unnecessary check-ins but escalate material uncertainty.

**Inspect first:** `static/index.html`, `workflow_state.py`, `workflow_checkpoints.py`, `artifact_lineage.py`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R06, R09, R10, R18. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

**Revision 1.2 additional acceptance:** Satisfy applicable requirements in docs/autonomous-production-ux-direction.md; preserve existing features and obtain actual implementation review, not concept-derived approval.

### AP-019 — Capture lightweight scoped review and preference corrections

**Phase:** P4 • **Dependencies:** AP-018 • **Optional:** false

Translate natural feedback into explicit bounded revisions without global preference drift.

**Deliverables:** Feedback records and optional annotation UX; Revision/interpretation workflow; Conflict/scope/accessibility tests.

**Acceptance criteria:**
1. Free text and optional selected region/passage/timecode/quick actions are available without mandatory scoring.
2. Feedback pins artifact version and preserves original user wording.
3. Material interpretation is confirmed; ambiguity/conflicts are not silently generalised.
4. Revision invalidates only affected downstream evidence/approvals.
5. A user can correct/delete a retained preference and inspect its scope.
6. Keyboard/mobile/zoom remain usable and paused work resumes correctly.

**Inspect first:** `static/index.html`, `context_store.py`, `history_privacy.py`, `artifact_lineage.py`, `workflow_state.py`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R09, R10, R18. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

**Revision 1.2 additional acceptance:** Satisfy applicable requirements in docs/autonomous-production-ux-direction.md; preserve existing features and obtain actual implementation review, not concept-derived approval.

### AP-020 — Create versioned owner-approved reference benchmarks

**Phase:** P4 • **Dependencies:** AP-006, AP-016, AP-019 • **Optional:** false

Reuse accepted examples while preserving approval scope and history.

**Deliverables:** Accepted-output/reference schema; Relevant retrieval and versioned refinement; Approval/invalidation audit tests.

**Acceptance criteria:**
1. Record original brief, exact artifact, user provenance, approval scope, checks and configuration.
2. Continue-direction, final-acceptance, use-as-reference and publish grants are separate.
3. Enhancements create a new version; prior acceptance cannot be silently rewritten.
4. References are retrieved by relevant audience/language/task/style and do not force identical layouts.
5. Agent cannot synthesize owner approval, security sign-off or screenshot approval.
6. Changed facts/code/assets still require new verification despite a style reference.

**Inspect first:** `artifact_lineage.py`, `evidence_lessons.py`, `verification_evidence.py`, `context_store.py`.

**Existing test anchors:** `tests/test_artifact_lineage.py`, `tests/test_evidence_lessons.py`

**References:** R10, R25, R31. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization_plus_real_owner_approval_for_reference_records`.

**Revision 1.2 additional acceptance:** Satisfy applicable requirements in docs/autonomous-production-ux-direction.md; preserve existing features and obtain actual implementation review, not concept-derived approval.

### AP-021 — Deliver a complete website draft workflow

**Phase:** P5 • **Dependencies:** AP-009, AP-010, AP-012, AP-017, AP-018, AP-019 • **Optional:** false

Produce a useful runnable website package, not just generated code or a plan.

**Deliverables:** Bounded website recipe and direction sample; Source/preview/config package; Artifact and functional checks.

**Acceptance criteria:**
1. Chosen audience/website scope is explicit; sample direction is reviewed when no relevant benchmark exists.
2. Requested interactions work or are honestly marked unsupported; no fake testimonials/integrations.
3. Research/assets have provenance and applicable rights.
4. Code executes in authorised environment with real browser tests.
5. Repair loop is bounded and partial failure is visible.
6. No deployment, DNS or production credentials are used without separate grants.

**Inspect first:** `app.py`, `static/index.html`, `child_delegation.py`, `sandbox_boundary.py`, `artifact_lineage.py`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R13, R18, R19, R21, R38. **Decision dependencies:** U01, U02, U03, U04, U06. **Approval class:** `normal_story_authorization`.

### AP-022 — Qualify website output quality and delivery

**Phase:** P5 • **Dependencies:** AP-020, AP-021 • **Optional:** false

Validate technical, visual, factual and audience fitness before declaring website support.

**Deliverables:** Website rubric/fixtures and rendered evidence; Independent review/repair path; Owner-accepted pilot report.

**Acceptance criteria:**
1. Critical forms/links/navigation/input flows pass real tests.
2. Actual phone/tablet/desktop and zoom layouts have no clipped or overlapping content.
3. Keyboard/focus/contrast and reduced-motion checks pass applicable criteria.
4. Console/network/secrets/dependency problems and placeholder content are checked.
5. Owner reviews creative usefulness; accepted references are scoped/versioned.
6. Report all evaluated runs, intervention/cost/time and limitations; no revenue claims.

**Inspect first:** `benchmarks`, `workflow_evaluation.py`, `verification_evidence.py`, `static/index.html`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R18, R19, R21, R38. **Decision dependencies:** U01. **Approval class:** `normal_story_authorization_plus_owner_creative_acceptance`.

### AP-023 — Deliver sourced original short-video drafts

**Phase:** P5 • **Dependencies:** AP-009, AP-010, AP-012, AP-017, AP-018, AP-019 • **Optional:** false

Connect topic evidence to a coherent script, visuals, narration and actual rendered output.

**Deliverables:** Video research/script/storyboard workflow; Evaluated media adapter and sample; MP4/captions/source/rights package.

**Acceptance criteria:**
1. Topic popularity is supported by dated region/source signals or labelled candidate/unknown.
2. Important claims link to appropriate evidence and contradictions/uncertainty remain visible.
3. Script/storyboard preserve original angle, voice and visual direction.
4. Reuse/audit existing MoneyPrinter contracts before adding a second integration.
5. Every asset/music/font/voice has rights/provenance; no unauthorised voice cloning.
6. Actual playable draft and captions are returned; upload remains separate authority.

**Inspect first:** `moneyprinter_tool.py`, `moneyprinter_mcp.py`, `docs/moneyprinter-turbo-integration.md`, `artifact_lineage.py`.

**Existing test anchors:** `tests/test_moneyprinter_tool.py`, `tests/test_moneyprinter_mcp.py`, `tests/test_moneyprinter_api.py`

**References:** R03, R18, R19, R30, R31, R39. **Decision dependencies:** U01, U02, U03, U04, U06. **Approval class:** `normal_story_authorization`.

### AP-024 — Qualify video technical and editorial quality

**Phase:** P5 • **Dependencies:** AP-020, AP-023 • **Optional:** false

Ensure the entire video is valid, accurate, coherent and useful rather than merely rendered.

**Deliverables:** Media probes and visual/audio checks; Independent editorial review; Owner-accepted pilot and regression clips.

**Acceptance criteria:**
1. Decode, dimensions/aspect/duration, black/frozen/missing frames and subtitle timing/bounds are checked.
2. Audio silence/clipping/levels, pronunciation and factual narration are reviewed.
3. Scene relevance, pacing, originality and audience fit have explicit rubric evidence.
4. Missing rights or unsupported consequential claims block readiness.
5. Entire pilot is watched; sample approval is not substituted for final review.
6. Report all pilot outcomes/costs/intervention and platform policy constraints without monetisation guarantees.

**Inspect first:** `moneyprinter_tool.py`, `verification_evidence.py`, `workflow_evaluation.py`, `benchmarks`.

**Existing test anchors:** `tests/test_moneyprinter_tool.py`

**References:** R03, R18, R19, R30, R33, R39. **Decision dependencies:** U01, U03. **Approval class:** `normal_story_authorization_plus_owner_editorial_acceptance`.

### AP-025 — Extract evidence-backed reusable skills from work

**Phase:** P6 • **Dependencies:** AP-012, AP-016, AP-020 • **Optional:** false

Turn successful methods into scoped candidates, not unverified automatic instructions.

**Deliverables:** Skill/lesson extraction and schema; Scope/retrieval/revalidation metadata; Positive/negative promotion tests.

**Acceptance criteria:**
1. Skills record provenance, prerequisites, permissions, versions, examples/tests and limitations.
2. One task success can justify task-local use but not automatic global superiority.
3. Failed approaches and corrections can be retained safely without becoming executable instructions.
4. Private data and cross-project authority do not leak into shared skills.
5. Existing evidence_lessons promotion contracts are reused and actual consumers tested.
6. Revoked/outdated skills are quarantined or revalidated; running tasks retain stable versions.

**Inspect first:** `evidence_lessons.py`, `artifact_lineage.py`, `context_store.py`, `trusted_transformations.py`.

**Existing test anchors:** `tests/test_evidence_lessons.py`

**References:** R16, R25, R31, R32, R42. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-026 — Research model/paper/tool improvements and evaluate candidates

**Phase:** P6 • **Dependencies:** AP-011, AP-012, AP-013, AP-016, AP-025 • **Optional:** false

Create a budgeted improvement lab driven by useful hypotheses and real failure traces.

**Deliverables:** Authorised watch jobs and candidate reports; Prompt/skill/config optimisation experiments; Held-out regression and resource evidence.

**Acceptance criteria:**
1. No implicit startup/background outbound requests; schedules require explicit grant.
2. Model cards/papers/repos capture dates, exact versions, license and limitations.
3. Experiments start from diagnosed problems and measurable hypotheses.
4. Installed/runtime compatibility and download/resource limits precede trials.
5. Held-out tasks/evaluators/authority controls cannot be edited by the optimiser.
6. Distinguish memory/skill/config updates from weight training; no automatic RL rollout.
7. Candidate generation, judge/evaluation and optimisation inference remain local; reference projects requiring hosted models must be adapted or remain reference-only.

**Inspect first:** `model_discovery.py`, `model_updates.py`, `model_calibration.py`, `evidence_lessons.py`, `supply_chain.py`.

**Existing test anchors:** `tests/test_model_updates.py`, `tests/test_model_calibration.py`

**References:** R15, R25, R26, R28, R34, R35, R36, R37, R40, R41. **Decision dependencies:** U02, U03, U04, U05. **Approval class:** `normal_story_authorization`.

### AP-027 — Bound refinement with incumbent preservation and stopping

**Phase:** P6 • **Dependencies:** AP-016, AP-017, AP-025 • **Optional:** false

Improve outputs without endless criticism, scope growth or degraded rewrites.

**Deliverables:** Incumbent/revision/synthesis comparison; Plateau/no-progress stop policy; Blind comparison and failure fixtures.

**Acceptance criteria:**
1. Unchanged output is always an eligible candidate.
2. Independent-context comparison records potential shared-model bias.
3. Hard factual/functional/safety requirements cannot be traded for style scores.
4. Iterations and total budget are bounded across all reviewers/variants.
5. Stop when evidence does not justify change and preserve best accepted version.
6. Insufficient capability produces honest partial result and precise escalation, not fabricated success.

**Inspect first:** `workflow_evaluation.py`, `execution_budget.py`, `verification_evidence.py`.

**Existing test anchors:** `tests/test_workflow_evaluation.py`, `tests/test_execution_budget.py`

**References:** R27, R30, R31. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-028 — Promote and roll back improvements by risk and evidence

**Phase:** P6 • **Dependencies:** AP-014, AP-020, AP-025, AP-026, AP-027 • **Optional:** false

Choose automatic or human review according to change scope and consequences.

**Deliverables:** Enforced promotion policy/classifier; Task/project/canary/default stages; Rollback/quarantine/revocation evidence.

**Acceptance criteria:**
1. Risk/authority classification is enforced outside the proposing model.
2. Automatic promotion needs approved thresholds and relevant held-out results.
3. New egress/accounts/spending and sensitive defaults need existing explicit grant or owner review.
4. A candidate cannot modify its evaluator/approval/sandbox/audit controls to pass.
5. Canary regressions trigger bounded rollback and invalidate affected downstream evidence.
6. Prior configurations/references remain recoverable; in-flight tasks stay version-consistent.

**Inspect first:** `evidence_lessons.py`, `model_updates.py`, `routing_policy.py`, `scoped_grants.py`, `artifact_lineage.py`.

**Existing test anchors:** `tests/test_evidence_lessons.py`, `tests/test_model_updates.py`, `tests/test_scoped_grants.py`

**References:** R25, R26, R27, R32. **Decision dependencies:** U05. **Approval class:** `normal_story_authorization_plus_owner_approval_of_promotion_policy`.

### AP-029 — Add governed recurring missions and external delivery

**Phase:** P7 • **Dependencies:** AP-007, AP-020, AP-028 • **Optional:** false

Run useful recurring work and optional publish/deploy without duplicate or unauthorised effects.

**Deliverables:** Schedule/draft queue and notifications; Scoped external action adapter pilot; Replay/reconciliation and global pause tests.

**Acceptance criteria:**
1. Schedules grant only their explicit purpose; draft creation never implies publication.
2. External action binds account/destination/content hash/policy/expiry and invalidates on changes.
3. Provider-supported idempotency and local ledger handle retries; unknown outcome reconciles rather than resends.
4. Restart/missed schedule/revocation tests avoid duplicate effects.
5. Global pause, per-project stop and notification thresholds prevent runaway activity.
6. Metrics/analytics suggestions do not claim causal business success or silently change public output.

**Inspect first:** `side_effect_ledger.py`, `scoped_grants.py`, `execution_budget.py`, `workflow_checkpoints.py`, `app.py`.

**Existing test anchors:** `tests/test_side_effect_ledger.py`, `tests/test_scoped_grants.py`

**References:** R08, R25. **Decision dependencies:** U03, U04. **Approval class:** `normal_story_authorization_plus_explicit_external_action_grants`.

### AP-030 — Evaluate optional guest-desktop capability

**Phase:** P7 • **Dependencies:** AP-010, AP-012 • **Optional:** true

Enable GUI-only tasks only when simpler APIs/browser workers are insufficient.

**Deliverables:** Use-case-specific driver/guest bake-off; Platform qualification and takeover evidence; Host/guest/clipboard/file/egress boundary tests.

**Acceptance criteria:**
1. Chosen task justifies desktop control instead of existing API/browser tools.
2. Exact supported platform/version/resources and setup are verified.
3. Host desktop/files/accounts/clipboard remain outside untrusted guest authority unless separately granted.
4. Shared folders, credentials, networking and screenshots are scoped/redacted.
5. Guest failure cannot fall back to controlling the host.
6. Publish maturity/limitations and keep optional adapter disabled unless qualified.

**Inspect first:** `sandbox_boundary.py`, `process_supervision.py`, `mcp_client.py`, `hardware_capabilities.py`.

**Existing test anchors:** `tests/test_sandbox_boundary.py`

**References:** R02, R04, R07. **Decision dependencies:** U01, U02, U04, U06. **Approval class:** `normal_story_authorization`.

### AP-031 — Ship progressive configuration, onboarding and operations

**Phase:** P7 • **Dependencies:** AP-008, AP-019, AP-020, AP-028, AP-029 • **Optional:** false

Expose powerful capabilities without sacrificing a clear low-supervision experience.

**Deliverables:** Capability readiness and policy UI; Reference/skill/model controls; Accurate help/export/operations UX.

**Acceptance criteria:**
1. Configured, tested, qualified, experimental, blocked and unavailable states are distinct.
2. Users can inspect/revoke grants and correct/export/delete preferences/references.
3. Readiness reports actual provider/runtime results and avoids hidden paid/network setup.
4. Power settings are progressively disclosed; all existing features remain accessible.
5. Costs/limits and pauses/failures are understandable without reading logs.
6. Help and examples describe actual enabled capabilities and separate artifact acceptance from external authority.

**Inspect first:** `static/index.html`, `onboarding_preflight.py`, `start_command_center.py`, `app.py`, `README.md`.

**Existing test anchors:** Identify current tests and add positive, negative and failure cases.

**References:** R06, R07, R10, R18, R25. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `normal_story_authorization`.

### AP-032 — Qualify the production release and independent-agent handoff

**Phase:** P7 • **Dependencies:** AP-022, AP-024, AP-028, AP-029, AP-031 • **Optional:** false

Release only the selected, evidenced scope with preservation, safety and reproducible delivery.

**Deliverables:** End-to-end pilot acceptance pack; Preservation/security/CI/supply-chain evidence; Runbooks, rollback and final handoff.

**Acceptance criteria:**
1. If only one specialisation is owner-selected for launch, explicitly defer the other pilot through an authorised ledger revision rather than fake completion.
2. Existing API/UI/session/workspace features and standalone remain verified.
3. Selected workflows pass versioned held-out/hostile/failure/restart/resource cases and actual artifact review.
4. Installed qualification and independent security/screenshot approvals are real and current; author evidence is not substituted.
5. Applicable CI is green at the implementation head and artifacts/docs claim no more than tested scope.
6. Rollback/migration/restore are exercised; branch/PR and owner acceptance are recorded; no secrets/unlicensed outputs committed.

**Inspect first:** `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `security_release_gate.py`, `scripts`, `product`, `docs`.

**Existing test anchors:** `tests/test_security_release_gate.py`

**References:** R24, R28. **Decision dependencies:** none explicitly; authority still applies. **Approval class:** `owner_release_acceptance_and_existing_independent_security_visual_gates`.
