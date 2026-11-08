# Cross-runtime model lifecycle: audit, research, and proposed design

Research date: 2026-11-06. Status: **proposal for owner review; not implemented or an accepted ADR**.
Repository audited: `67e9430d1e30ec066f74daa286c6636ac0f1839c`.
Scope: session model switches, residency, context admission, latency, and quality across
local oMLX, Ollama, LM Studio, and explicitly authorized remote/cloud inference.

Follow-up: [implementation plan and deeper research](cross-runtime-model-lifecycle-plan.md)
adds native request-profile consistency, deferred unload/tokenizer-load behavior, shared
model/tool memory accounting, and explicit capability/ownership release gates. It refines
cold preflight into metadata checks before a switch and exact request-fit checks only after
the target can be safely admitted. Both documents remain proposals, not implemented behavior.

## Executive recommendation

Build **one model lifecycle coordinator per shared hardware resource domain**, with
runtime-specific lifecycle adapters. For this owner's constrained local machine, use
**strict single-resident-model mode**, not merely one active inference request per server.
Keep the selected model warm between turns; unload it only for a verified switch, explicit
release, configured idle expiry, or memory pressure. Execute local specialist/critic stages
sequentially without dropping their required verification work.

Reuse the existing admission primitive, portable handoff, worker fencing, authority checks,
and cost-aware affinity logic. Close their production wiring gaps rather than introduce a
second autonomous router. Separate four concepts currently conflated in places:

1. OpenAI/Anthropic/native **wire format**;
2. **execution location and egress** (local, remote self-hosted, cloud, or unknown);
3. **hardware resource domain** (which allocations actually share memory);
4. **model residency**, independent of active requests.

Quality preservation is a release criterion, not a claim that arbitrary models are
interchangeable. No proposal can guarantee zero cold-load latency while allowing only one
resident model. The achievable goal is fewer unnecessary switches, bounded transparent waits,
preserved task semantics, and measured end-to-end improvement.

## 1. Current implementation: what exists and where it falls short

All code locations below refer to the audited repository revision, not upstream engines.

| Area | Observed implementation | Consequence |
|---|---|---|
| Routing | `app.py:2730–2785`, `is_local_omlx_model` / `parse_model_route`: Ollama explicitly returns false for local-oMLX and uses route kind `cloud` with an OpenAI-wire virtual provider. | Adapter dispatch classification is not execution location. This does **not** establish cloud disclosure; the owner's supplied logs show loopback inference. |
| Inference admission | `app.py:1763–1814,4838–4930`: `_heavy_model_slot` wraps only the oMLX leaf; generic provider streams bypass it. | No shared oMLX/Ollama/LM Studio residency barrier on this path. |
| Lease accounting | `model_admission.py`, `HeavyModelAdmission.release`: after the last request lease, `_resident_model` becomes `None`; release performs no runtime I/O. | `active_leases == 0` proves no tracked active request, **not** unloaded weights or reclaimed memory. |
| Capacity | `model_admission.py`, `limits_from_environment`: detected/configured capacity, a default 80% request reservation, and a documented fallback capacity. | Useful conservative policy, not a model/context/concurrency-specific memory measurement. Unknown capacity must not become measured evidence. |
| oMLX unload | `app.py:5753–5784`: helpers use POSTs, fixed sleeps, and exception suppression. `_unload_other_omlx_models` appends IDs without checking POST success or a subsequent residency observation. | A returned list is not verified unload completion. An unavailable observation is not an empty runtime. |
| Explicit loading | `app.py:5810–5821`: load route forwards to oMLX admin, without acquiring the shared heavy-model slot. | Explicit loads must join the same lifecycle coordinator, not remain a bypass. |
| Turn preparation | `app.py:18907–18908`: Autonomous Turn calls idle-model eviction before the streaming leaf acquires its request lease. | Observe/check/unload/load is not one atomic cross-request operation; another session can intervene. |
| Workflow sequencing | `workflow_orchestration.py:166–168` and `app.py:4957–4964`: sequential stages can auto-unload, using the application-provided local-oMLX predicate/unloader. | Good orchestration seam; does not itself coordinate other local engines or prove memory release. |
| Portable handoff | `app.py`, `_execute_session_model_switch`, especially `15496–15516`; `routing_policy.py`; ADR-0018. | Substantial existing safety machinery: fencing, unresolved-action checks, grants, budgets, portable messages, and selective high-risk semantic confirmation. Physical admission/eviction here is still oMLX-specific. |
| Optional anti-thrashing | `routing_experiments.py`, `AffinityHysteresisPolicy` and `execute_sequential_local_handoff`; ADR-0024. | Preserve calibrated cost/dwell/hysteresis ideas. The sequential helper releases/acquires **accounting leases**, not engine weights; `app.py` has no call to this helper. Deterministic experiment results do not prove cross-runtime physical residency. |
| Lifecycle contract | `local_runtime_lifecycle.py`, `OmlxLifecycleAdapter`: injected transport, safe errors, telemetry, cancellation. No production use of the class was found outside its definition/tests. | A useful starting seam, not already-installed lifecycle enforcement in `app.py`. |
| Other switching contracts | `model_switching.py`, `provider_switching.py`: capability/semantic decisions with focused tests; no production calls to `switch_model` / `switch_provider` found. | Do not confuse standalone contract coverage with end-to-end integration. `switch_model` uses word counts, not the target chat tokenizer. |
| Ollama discovery | `app.py:3596–3636` reads `/api/tags`; `local_provider_adapters.py` has discovery adapters. | Installed models, disk sizes, and discovery success are not resident instances, active context, or inference readiness. |
| LM Studio | No dedicated LM Studio lifecycle adapter/reference found in the inspected Python, README, or UI. Generic configured OpenAI-compatible inference is the available integration path. | Treat first-class residency/context management as new integration work, not an existing guarantee. |
| Prompt admission | `app.py:4300–4340,18910–18965,19063–19066`: generic adapter sends assembled messages; Harness requests up to 4096 output tokens on non-oMLX routes. | No Ollama active-context-aware request budget on this path. The base Harness template alone is 6,358 characters; this is **not** a token count or a breakdown of the owner's request. |
| oMLX prompt guard | `app.py`, `_trim_messages_for_omlx_prefill`, around `4578–4636`: character-based reduction can trim system text and latest input. | Not a provider-neutral token budget. Do not expand this tail-trimming approach as the quality-preserving solution. |

### The observed failure and its limits

The owner's test8 logs establish **7,326 input tokens against an active 4,096-token
context**, HTTP 400 before generation. They do not establish two simultaneously loaded models.
Context overflow and cross-runtime co-residency are distinct risks that need one coordinated
admission design. Increasing context alone can increase memory demand and does not fix model
switching. `/api/tags` returning 200 is not evidence that inference will fit.

## 2. Runtime documentation and GitHub findings

Primary documentation and pinned repository files were inspected. Upstream capabilities
must be negotiated against the **installed version**; these references are not evidence of
what is installed on the owner's machine.

### Ollama

- The official FAQ documents explicit unloading with an empty `/api/generate` request and
  `keep_alive: 0`, idle retention, concurrency controls, and additional context memory with
  parallel requests. `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1` are useful
  **per-Ollama** defensive settings, not cross-engine coordination. Keeping every response
  at `keep_alive: 0` would incur unnecessary reloads. [1](https://docs.ollama.com/faq)
- `GET /api/ps` lists resident models and includes `context_length`, `size`, `size_vram`,
  digest, and expiry in the documented response. Use it for residency and allocated-context
  observations, not `/api/tags`. Field absence on older versions is unknown, not zero.
  These fields alone do not establish ownership or enumerate all active requests.
  [1](https://docs.ollama.com/api/ps)
- The pinned scheduler has request references, expiry/unload channels, waits for unload
  completion, and runner reload decisions. Learn from its separation of request lifetime
  and runner lifetime. Its lock cannot coordinate a separate oMLX/LM Studio process.
  [Scheduler source](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/sched.go)
- The inspected OpenAI request type exposes a `keep_alive` extension but no `num_ctx`
  field. Do not assume native options can be passed through an arbitrary OpenAI body.
  Context control needs a version-supported native mechanism or an explicit operator
  configuration; `max_tokens` is an output limit, not context allocation.
  [OpenAI adapter source](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/openai/openai.go)

### LM Studio

- Native `GET /api/v1/models` distinguishes model files, maximum context, and
  `loaded_instances[].config.context_length`. A file can have multiple loaded instances;
  lifecycle identity must use instance ID, not just a friendly model name.
  [3](https://lmstudio.ai/docs/developer/rest/list)
- Native `POST /api/v1/models/load` accepts `context_length` and can echo final load config.
  Native `POST /api/v1/models/unload` takes `instance_id`. Use explicit native lifecycle
  control where supported, with inference still using the compatible wire if suitable.
  [1](https://lmstudio.ai/docs/developer/rest/load)
  [Unload API](https://lmstudio.ai/docs/developer/rest/unload)
- Official TTL/Auto-Evict documentation says auto-eviction affects **JIT-loaded models**;
  manually loaded models are unaffected. Therefore Auto-Evict does not guarantee even
  one total LM Studio resident, much less one across all engines.
  [Official lifecycle documentation](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict)
- Documented versions differ: current native v1 routes must not be assumed on older
  installations. Probe declared capabilities on explicit connection, with bounded requests;
  do not fabricate a successful fallback when lifecycle APIs are unavailable.

### oMLX

- The pinned engine pool implements eviction-before-load memory checking, same-engine
  reuse, LRU victim selection, pinning, in-use leases, and pending unload handling. It skips
  in-use/active entries when choosing an LRU victim. These are useful lifecycle patterns,
  but the pool is local to its own server.
  [Engine pool source](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/engine_pool.py)
- The inspected admin routes expose list/load/unload operations. Authentication, path shapes,
  deferred completion, aliases, and status fields must be version-tested by the adapter;
  do not rely solely on a generic OpenAI model list or a fixed one-second sleep.
  [Admin routes source](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/admin/routes.py)
- PR #730 proposes `single_model_mode`, but GitHub reports **closed, not merged**, with no
  merge commit. The proposal also exempts pinned models. It is evidence of a relevant
  problem/design discussion, **not a supported setting to prescribe**.
  [2](https://github.com/jundot/omlx/pull/730)

### Remote/cloud

A compatible HTTP wire does not imply local compute, and a loopback endpoint can proxy
remote models. Ollama's official compatibility documentation explicitly includes cloud
models through its local server. Location/egress and billing need trusted configuration
and explicit consent; never infer locality solely from the provider label or hostname.
[Official Ollama compatibility documentation](https://docs.ollama.com/api/openai-compatibility)

Cloud inference does not allocate a second set of local model weights. An authorized cloud
turn can leave the single local resident idle if memory policy permits; unloading it on every
cloud call would add needless return latency. Normal application memory still needs headroom.

## 3. Published papers: what transfers and what does not

The following are published conference papers, not treated as benchmarks of this product.
Publisher abstracts and selected design sections were inspected; this is not an exhaustive
systematic literature review or an independent reproduction of their experiments.

| Paper | Relevant result/design principle | Application here and boundary |
|---|---|---|
| **ServerlessLLM**, OSDI 2024 | Multi-tier checkpoint loading and startup-time-aware scheduling make load locality/cost important. [2](https://www.usenix.org/system/files/osdi24-fu.pdf) | Measure cold/warm load and re-prefill costs; keep weights on disk and avoid needless reloads. Its distributed GPU servers, custom loader, PCIe hierarchy, and speedups are not a ready-made Apple unified-memory solution. |
| **Llumnix**, OSDI 2024 | Runtime scheduling and coordinated migration address heterogeneous demand; migration uses explicit resource coordination. [2](https://www.usenix.org/system/files/osdi24-sun-biao.pdf) | Borrow explicit states, reservation, drain, and failure handling. Do **not** copy KV tensors between unrelated models/runtimes: its compatible-instance migration is a different problem. |
| **SGLang / RadixAttention**, NeurIPS 2024 | Prefix-aware KV reuse and cache-aware scheduling reduce repeated prefill; referenced cache entries cannot be evicted while in use. [1](https://proceedings.neurips.cc/paper_files/paper/2024/file/724be4472168f31ba1c9ac630f15dec8-Paper-Conference.pdf) | Preserve stable prompt prefixes and native same-model caches; use bounded affinity where ordering allows. Cache reuse remains an engine capability, not a portable cross-model session format. |
| **RouteLLM**, ICLR 2025 | Learned routing uses preference evidence to trade cost and response quality between stronger/weaker models. [1](https://iclr.cc/virtual/2025/poster/30737) | Use measured eligibility/quality rather than model size/name as a proxy. The paper does not solve physical residency, agent tool safety, or universally guarantee coding-task quality. A learned router is optional later, not the first fix. |
| **S-LoRA**, MLSys 2024 | Many adapters share one base model with specialized paging/batching. [1](https://proceedings.mlsys.org/paper_files/paper/2024/file/906419cd502575b617cc489a1a696a67-Paper-Conference.pdf) | Potential later optimization only for a validated shared-base adapter fleet. It cannot make unrelated Gemma/Qwen/Llama weights or GGUF/MLX engines interchangeable. |

Related GitHub projects inspected: the pinned READMEs of
[ServerlessLLM](https://github.com/ServerlessLLM/ServerlessLLM/blob/9f50241baa5386e06a9321c51f19a9ef5f964c2b/README.md),
[SGLang](https://github.com/sgl-project/sglang/blob/3bcde0c80eeb07b40d3334c4b3d7341602efd089/README.md), and
[RouteLLM](https://github.com/lm-sys/RouteLLM/blob/0b64fdafe049e596a3f5657c219329f24af24198/README.md).
No code was copied or new serving stack adopted. The actionable conclusion is **coordinate
lifecycle first; optimize routing/cache reuse second**. Published speedup figures are not
performance promises for this repository or the owner's machine.

## 4. Proposed architecture

### 4.1 One coordinator, two kinds of lease

Extend existing typed resource-admission primitives with:

- **Residency reservation:** persists while weights/caches are loaded, including idle time.
- **Request lease:** protects active inference and any related in-progress lifecycle operation.

A request finishing releases its request lease, not its residency reservation. Residency is
released only after verified unload. A failed/ambiguous observation enters `unknown`, retaining
a conservative reservation and blocking conflicting loads. No timeout or expired worker lease
is, on its own, proof that memory has been freed.

Serialize switches for the same resource domain. For one unified-memory local machine,
oMLX/Ollama/LM Studio share that domain regardless of their ports. Remote self-hosted hardware
belongs to a separate explicitly configured domain; unknown relationships must not be assumed
independent. GPU deployments need both device and host-memory budgets, without double-counting
Apple CPU/GPU views of the same physical unified memory.

Use a durable coordinator owner/fencing generation and a single authoritative scheduler. An
in-process lock alone is insufficient for multiple Studio workers/processes. Reuse the
existing persistence/worker-fencing mechanisms; do not hold SQLite transactions during network
I/O. On restart, reconcile journaled intents against observed runtime state before new loads.
Stale owners cannot publish successful completion or authorize new work. An epoch is not
a fence enforced by an arbitrary model server: if an HTTP lifecycle call times out, the
server may still finish it later. A single “absent” snapshot is insufficient while that
operation is unresolved. Keep the domain blocked until supported operation-status/cancellation
or managed supervision proves the old operation cannot subsequently load a model; otherwise
require operator reconciliation. Cancelling the HTTP client alone is not that proof.

Strict mode counts **all resident model instances**, including embeddings, rerankers, local
router classifiers, and speculative draft models. No invisible auxiliary-model exemption.
If a runtime internally loads helpers that cannot be observed/disabled, strict single-instance
support is unavailable until that capability is resolved. A future measured multi-resident
mode is separate, explicit owner-authorized work, not the default proposed here.

### 4.2 Capability-driven adapters

Keep inference adapters separate from lifecycle adapters; preserve existing route strings.
Proposed adapter contract: observe installed/running instances, report capabilities and
observation freshness, load with approved configuration, unload an exact instance, and confirm
completion. Every observation carries source/version, timestamp, confidence, and unknown fields.

Runtime capabilities include lifecycle control, instance identity, effective context,
token counting, active-request visibility, native cache behavior, and cancellation/drain
semantics. Missing lifecycle support means `unsupported`, not success. Opaque generic
OpenAI-compatible local endpoints can offer inference, but strict coordinated switching
requires a supported lifecycle interface or an explicit manual-release workflow.

Adapters must preserve URL/IP pinning, redirect refusal, auth, bounded timeouts, cancellation,
secret redaction, and audit. New native paths are a documented/tested runtime API change.
Never pass arbitrary lifecycle bodies to an endpoint merely because its wire is OpenAI-like.
Runtime observations and model-supplied descriptions are data, not authority to start engines,
raise memory limits, enable outbound traffic, download weights, or evict other users' models.

### 4.3 Switching state machine

```text
REQUESTED -> VALIDATING -> QUEUED/RESERVED -> DRAINING_SOURCE
          -> SNAPSHOTTING -> UNLOADING_SOURCE -> VERIFYING_RELEASE
          -> LOADING_TARGET -> VERIFYING_TARGET -> RESTORING_CONTEXT
          -> READY_FOR_RESUME

Any uncertain physical outcome -> RECONCILING / SAFE_PAUSED
```

1. **Validate before disruption:** permission/egress/billing consent, model availability,
   required tools/vision/structured output/reasoning semantics, task state, target context,
   and estimated/measured memory feasibility. Missing weights do not trigger automatic pull.
   Same instance plus same effective load configuration is a no-reload path, after validating
   the new request still fits. A context/load-config change can require a real reload.
2. **Reserve atomically:** one switch owner per domain; bounded, cancellable queue. Preserve
   session order and side-effect dependencies. Coalesce compatible same-target lifecycle
   demand where safe; do not allow two waiters to independently unload/load.
3. **Drain at a safe boundary:** no mid-token forced switch or abandoned tool transaction.
   Finish/cancel the current inference using supported semantics, then prove it stopped.
   Browser disconnect is not proof the backend runner stopped. Existing execution fencing,
   unresolved consequential-action gates, and approval reconciliation remain authoritative.
4. **Snapshot:** save the portable handoff, objective, recent exact turns, evidence/file refs,
   pending actions, budget and policy revisions. Reuse existing high-risk semantic checks.
5. **Unload and verify:** evict only authorized managed instances; check command response and
   bounded-poll authoritative residency until absent. Where available, verify allocator/process
   memory stabilization/headroom. A POST acknowledgment or sleep is insufficient. RSS alone
   need not fall to zero because caches/allocators differ; adapters must define conservative
   completion evidence. Unverifiable release blocks target loading.
6. **Load and verify:** apply an owner-approved context/load profile, verify exact instance,
   digest/config/capabilities, effective context, resident set, and available memory. Never
   load target before source-release evidence on the shared domain. Stop on unexpected extra
   residents or pressure. Native runtime memory guards remain necessary for load-time spikes.
7. **Restore:** rebuild text context and target-specific serialization, run required semantic
   confirmation only after safe target load, and commit the session model selection after
   readiness. Preserve existing paused/resume semantics; do not auto-run a paused session.

Journal idempotent operation IDs and stages for crash reconciliation. A failed target load
leaves a recoverable paused session, not falsely completed work. A rollback to the source must
itself verify the target is absent and run sequential admission; no blind reload-on-error,
no replay of tools, and no unsanctioned cloud fallback. Failure after physical completion but
before a session commit must reconcile that difference explicitly.

### 4.4 Context and memory are admission inputs, not afterthoughts

Use the **effective allocated context**, not the model-card maximum:

```text
rendered_input_tokens + requested_output_reserve + template/safety_margin
    <= effective_context_tokens

weights + KV/request memory + runtime workspace + auxiliary allocations
    + OS/application headroom <= usable domain memory budget
```

Count the fully rendered target request: security/system text, tool schemas/catalog,
memory/skills, handoff, history, user text, and multimodal expansion where relevant.
Avoid double-counting chat-template overhead if the native counter already includes it.
Use an already-installed tokenizer/native counter where supported; never download tokenizers
implicitly. Estimates must be labeled, conservative, and reject uncertain fits near the limit.
If accurate counting/allocated context is unavailable, strict guaranteed-fit mode must decline
until configured/qualified; a character/word count is not proof.

Budget output including reasoning where applicable. Do not silently shorten a required review,
disable reasoning, reduce quantization quality, or enlarge context to force admission. Memory
profiles must be keyed by runtime/version, model digest/quantization, context length, parallel
slots, backend, auxiliary features, and relevant hardware profile. Model disk size is not peak
resident memory. Transformer KV formulas are estimates and do not universally describe hybrid,
recurrent, sliding-window, or multimodal architectures.

Context reduction policy, if the owner approves it:

- Keep safety/authorization anchors, current objective, unresolved actions, and necessary
  recent tool evidence intact. Bound optional memory/skills and use explicit authorized tool
  selection/lazy catalog expansion, without silently removing required capability.
- Keep full history/evidence durably; derive a clearly marked compact view with provenance
  and retrieval references. Preserve complete tool-call/result pairs. Retrieval/summary
  generation must itself fit the single-model budget and require applicable authorization.
- If required information still cannot fit, return a typed `context_budget_exceeded` before
  inference with safe counts/limits, their confidence, and operator choices. No raw upstream
  body, secret prompt, automatic larger model, or automatic cloud disclosure.
- For the observed 7,326/4,096 failure, the input alone cannot fit. Neither a permission change
  nor another identical retry can fix it. Context enlargement requires explicit memory-aware
  approval; the proposal does not prescribe a universal context size.

### 4.5 Latency and quality policy

- **Sticky session affinity:** reuse the selected qualified model when it can satisfy the task.
  Extend the existing hysteresis/cost concepts, not a new always-on LLM router. No secondary
  local classifier that defeats strict single-resident mode.
- **Account for real costs:** compare measured expected task benefit against queue wait,
  checkpoint/drain, unload/verify, cold load, semantic confirmation, and target re-prefill.
  Quality/capability/safety constraints are hard gates, not costs that can be traded away.
  Unqualified candidates are not eligible for automatic quality-preserving substitution; an
  explicit manual choice is not evidence of equal task quality. Identical model names in two
  runtimes do not establish identical weights, templates, behavior, or a reusable instance.
  Within eligible choices, switch only when benefit exceeds calibrated cost plus hysteresis,
  or on explicit owner selection/verified failure. Dwell/cooldown are configurable policies,
  not universal magic constants.
- **Fair warm scheduling:** prefer existing resident work only within bounded dwell/aging and
  deadline limits. Do not starve another session or reorder its dependent operations.
- **Preserve multi-agent quality:** required independent critic/specialist roles remain present
  and run sequentially. Group stages only when dependency/evaluation semantics allow it;
  do not batch away required feedback cycles merely to reduce switches.
- **Native cache reuse only:** stable trusted prefixes, authorized same-model warm caches,
  and supported disk caches can reduce re-prefill. No KV portability between arbitrary models,
  no cross-session confidential prefix exposure, and no routine SSD-cache purge on every switch.
  Cache persistence is subject to local privacy/retention settings.
- **No speculative second load:** no startup prewarming, background model downloads,
  double-resident prefetch, hidden draft model, or benchmark prompts without authorization.
- **Truthful progress:** show validating, queued, draining, unloading, verifying, loading,
  ready/paused, with cancellation and measured/estimated wait labels. Never show “ready” from
  discovery alone or “memory safe” from an accounting lease alone.

## 5. Guarantee boundary: managed versus externally shared servers

A Studio lock cannot stop another program from sending a load request directly to Ollama,
oMLX, or LM Studio. Polling cannot eliminate that race. Therefore:

- **Managed/exclusive mode:** strict end-to-end enforcement requires all loads/inference using
  those runtime instances to pass through the coordinator, with appropriate access isolation,
  ownership, and resource limits. A dedicated managed server instance may require explicit
  owner consent and startup/install supervision; it is not created by this proposal.
- **Attached/shared mode:** observe configured servers on authorized operations, identify
  external/pinned/busy instances, and pause on conflicts. Do not evict other clients' models
  automatically. Model IDs matching Studio's selection are not ownership evidence. Unknown
  activity/ownership cannot be advertised as exclusive control.
- **Unknown or unavailable server:** not evidence of zero residents; keep a safe hold until
  reconciled or explicitly resolved by the operator. Do not kill runtime processes as routine
  cleanup or silently overwrite server-global settings.

Strict single-resident operation across arbitrary externally controlled servers is not
something the application can honestly guarantee. If it is a hard requirement, managed
access is part of the solution, not just an implementation detail.

## 6. Alternatives and decision

| Alternative | Decision and reason |
|---|---|
| Raise Ollama context globally and resume | May unblock a particular fit with explicit memory-aware approval, but does not solve co-residency or future sessions. Not the architecture. |
| Set each runtime to “one model” | Defense in depth only. Each could still hold one on the same machine; manual/pinned models and helpers complicate semantics. |
| Unload after every response | Avoids long idle residency but sacrifices warm latency unnecessarily; use one warm resident and explicit verified switches. |
| Keep two models and trust OS paging | Rejected for the owner's strict policy; memory-pressure stalls and OOM undermine speed and reliability. |
| Add a router LLM that always stays loaded | Rejected for strict mode; adds another allocation and inference dependency. |
| Generic OpenAI proxy only | Useful inference compatibility, insufficient lifecycle, instance, context-allocation, or memory-control contract. |
| Adopt a distributed serving stack wholesale | Not justified for this local-first application; borrow principles without importing a cluster architecture or unsupported speedup claims. |
| Unified verified lifecycle plus affinity | **Recommended:** closes the cross-runtime control gap while preserving existing session/security machinery. |

## 7. Implementation sequence and acceptance evidence

These are proposed separate runtime stories/PRs, not work silently started under an old UI story.
One story per branch/PR; new long-lived contracts require an ADR proposed for owner review.
No roadmap status, accepted ADR, public API, or security signoff changes are made here.

1. **Identity and context readiness:** distinguish wire/location/domain; observe active context
   and residency; add sanitized typed context/unknown-capability errors. Preserve current route
   strings and consent behavior until explicit migrations are reviewed. Reproduce test8 with
   offline fixtures; no automatic retry or context expansion.
2. **Unified residency coordinator and adapters:** integrate oMLX/Ollama/LM Studio lifecycle,
   durable transition ownership, source-release verification, and both lease types. Cover every
   load-capable path: explicit load/unload, chat/gateway, Harness, council/debate/workflows,
   critic/child delegation, fallback, semantic probes, calibration and benchmarks.
3. **Session handoff integration and progress:** preserve existing grants/budgets/fencing/action
   reconciliation, add cancellable bounded queues and crash recovery, show truthful switch
   stages. Provider errors remain redacted; residency unknown is a visible blocked state.
4. **Measured optimization:** enable calibrated affinity/idle retention/cache optimizations only
   after the safety path is proven. Optional learned routing or multi-residency remains separately
   authorized. Calibrate policy against representative local workloads, not public model priors.

Required offline regressions include same-model reuse; same-model context reconfiguration;
A-to-B and oMLX-to-Ollama-to-LM Studio switching; simultaneous tabs; explicit-load/inference
races; fallback and child/critic entrypoints; deferred unload; false acknowledgment/HTTP error;
malformed/stale/unknown residency; lost connection; cancellation at every state; crash before
and after each physical transition; stale-owner completion; manually loaded/pinned/external
models; helper allocations; unsupported APIs; partial target loads; and secret/permission/
budget/tool-schema preservation. A source-unload failure must prove **zero target-load calls**.
Context fixtures must include 7,326/4,096, tool schema/template overhead, output/reasoning
reserve, multilingual text, missing tokenizer, and changed active context after load.

Hardware acceptance must be an **explicit opt-in owner run**, one test at a time, after offline
coverage and full Python 3.11/3.12/3.13 plus browser CI. Start with the still-paused test8,
then separately authorize model-switch scenarios. Record runtime versions/configs, digests,
context, resident instances across all engines, process/allocator memory, memory pressure,
swap activity, unload/load/re-prefill timing, first-token and end-to-end p50/p95, queue wait,
verified completion, tool validity, and required critic outcomes. Sample transitions, not just
before/after screenshots. Zero observed dual residency is necessary evidence but does not
prove no unobserved transient or control over external applications.

Compare warm-turn and switch-heavy workloads against the unchanged baseline using paired
representative tasks and repeat runs. Approve quality/speed thresholds before measurement;
report variance, failed runs, and unknown memory fields. No performance/quality improvement
is established by this document. Independent security acceptance remains separate.

## 8. Evidence from this research turn

- Source audit and upstream documentation/GitHub reads only; no model load/unload, live model
  inference, runtime configuration changes, model download, cloud benchmark, or permission
  change. Public research requests carried research queries, not workspace/session content.
- Installed the project's declared development environment in ignored `.venv` for offline checks.
- Ran existing focused suites: `test_model_admission`, `test_local_runtime_lifecycle`,
  `test_local_provider_adapters`, `test_ollama_discovery`, `test_model_switching`,
  `test_provider_switching`, `test_portable_handoff_and_switching`,
  `test_workflow_orchestration`, and `test_routing_experiments`: **89 passed** in 20.10s,
  with one upstream Starlette/httpx deprecation warning. No live endpoints were used.
- These are unchanged-contract regressions, **not tests of an implemented coordinator**.
  No hardware benchmarks, full multi-Python CI, browser suite, or independent review ran here.
- Manual QA test8 remains paused and unpassed. No runtime fix is claimed.
