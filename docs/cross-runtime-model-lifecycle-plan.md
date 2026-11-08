# Cross-runtime model lifecycle implementation plan

Date: 2026-11-06. Status: **researched draft for owner review, not implementation approval**.
Planning baseline: `5618952`; runtime source baseline: `67e9430`.
This expands the [initial audit and research](model-lifecycle-research-and-proposal.md)
with the owner's clarified requirement and a second upstream source/API review.
Nothing here changes runtime configuration, public APIs, roadmaps, or security signoff.

## 1. Product outcome and scope

Keep broad access to models through **oMLX, Ollama, and LM Studio**. Do not replace oMLX,
require every model to have an MLX conversion, or install vLLM as a new prerequisite.
Studio should coordinate safe model residency while each engine owns its inference internals.

On the owner's constrained shared-memory machine:

- Keep at most one independently resident local model instance, including idle time.
- Keep that instance warm across ordinary turns; switching incurs a real, visible transition.
- Preserve session objective, necessary context/evidence, permissions, budgets, pending actions,
  tool protocols, and required independent critic/specialist work across a switch.
- Prevent conflicting loads and stop safely on uncertainty. Do not claim external runtimes can
  never crash, or that two different models will produce identical answers.
- Maintain existing explicit cloud/network/billing consent. Cloud inference is not a second
  local weight allocation; it need not force an otherwise safe local resident to unload.

A model instance includes its necessary architecture components, such as a vision projector.
Those components must be included in its memory profile. An independently loaded draft,
embedding, reranker, or router model is an additional resident, not a hidden exemption. An
embedded MTP head is not automatically a second independent model. Identify real allocations,
not just names or the number of API entries. Unsupported auxiliary visibility blocks strict
qualification; do not silently disable a required feature to obtain a pass.

First release: one active local generation per memory domain, with bounded queueing. This
matches the conservative direction of current heavy-request admission; it is not a claim of
maximum concurrent throughput. Qualified same-model concurrency/batching can follow measured
memory admission. Native single-request optimizations and warm caches remain engine-owned.

## 2. Additional research that changes the plan

Primary source was checked where available. Reports below are regression leads, not proof of
bugs on the owner's installed versions. No live runtime was contacted.

### 2.1 oMLX: acknowledgment is not release, and versions matter

At upstream revision `2165e75726ba83d9651dbaced61c97728da77bba`:

- The public unload route delegates to `request_unload` and can return **202, status
  `unloading`**. A response cannot be treated as an immediately freed slot. The existing
  Studio helper sleeps and suppresses failures; replace that contract with observed completion.
- The public route is API-key protected. Admin list/load/unload routes have their own auth
  dependencies. Do not assume inference credentials authorize every admin path.
- `/tokenize` and `/v1/tokenize` obtain an engine lease. Token counting is therefore a
  potentially **load-capable** operation, not automatically a harmless preflight probe.

Sources: [public server routes](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/server.py),
[admin routes](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/admin/routes.py),
[engine pool](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/engine_pool.py).

Upstream issue #4378 describes an older public-unload path that stranded active streams.
GitHub now marks it **closed/completed**; the inspected source uses the revised path. Plan a
held-stream regression and qualify installed versions, not an assumption that the old bug
still exists. [3](https://github.com/jundot/omlx/issues/4378)

**Design consequence:** drain before unloading; handle deferred completion; version-test
routes/auth; send tokenizer work through the same residency barrier. Preserve oMLX's native
same-model reuse and scheduling rather than replace its engine pool.

### 2.2 Ollama: one load configuration must survive through inference

At upstream revision `d63b702609c4300a2ca07be2bdb0adb9834725e5`:

- `scheduleRunnerForModel` resolves per-request/model/default runner options; `needsReload`
  compares effective runner configuration. Changing context configuration can require a reload.
- The OpenAI request type does not expose native `options.num_ctx`. PR #16825 is **open,
  not merged** as checked through GitHub; its non-null merge preview SHA is not a merged release.
- Empty native generation/chat requests can load a model. Native unload requests signal runner
  expiry before returning. Neither a successful HTTP reply nor an empty installed-model list
  alone proves complete physical release.
- Chat processing has truncation/context-shift controls and defaults that need version-specific
  validation. Do not inherit permissive truncation while promising unchanged context.

Sources: [routes](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/routes.go),
[scheduler](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/sched.go),
[OpenAI request types](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/openai/openai.go),
[extension PR](https://github.com/ollama/ollama/pull/16825).
Official compatibility docs describe a model configuration approach, rather than standard
OpenAI per-request context allocation. [3](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx)

**Design consequence:** prefer a first-class native Ollama inference adapter for coordinated
sessions, applying the same approved load options on load and every inference. Keep existing
route strings. Prove tool calls, output limits, reasoning, structured outputs, stop conditions,
streaming, cancellation, and usage semantics before migrating a session. A native preload
followed by differently configured generic inference is not an acceptable context guarantee.
Do not create model variants or change global Ollama settings implicitly.

### 2.3 LM Studio: instance-level management, no invented cancellation guarantee

The native v1 REST API is documented from LM Studio 0.4.0; older v0 compatibility is not a
substitute for validated native v1 lifecycle behavior. [1](https://lmstudio.ai/docs/developer/rest)
The list API exposes loaded instances/configured context; load can echo final configuration;
unload accepts an exact instance ID. Those are the appropriate primitives, while inference can
remain on the compatible API if it demonstrably targets the intended instance/configuration.
[Model list](https://lmstudio.ai/docs/developer/rest/list) ·
[Load](https://lmstudio.ai/docs/developer/rest/load) ·
[Unload](https://lmstudio.ai/docs/developer/rest/unload)

The reviewed REST pages do not establish server-side operation IDs or an authoritative
cancel-load/status protocol. SDK abort examples are not proof of REST cancellation semantics.
The official Python SDK documents applying a chat template, tokenizing it, and reading a
loaded model's context length. That is a concrete counting route to evaluate, but text-only
examples do not prove parity for tools, reasoning and multimodal requests. Do not invent a REST
token-count endpoint or a private WebSocket protocol. If the maintained SDK is necessary,
propose an explicit optional integration dependency/ADR and verify configured-host transport,
auth/pinning and no implicit discovery before adoption.
[1](https://lmstudio.ai/docs/python/tokenization)
Auto-Evict covers JIT models, not all manually loaded instances.
[Official TTL/Auto-Evict](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict)

**Design consequence:** make instance targeting/configuration preservation a capability gate;
ambiguous native operations retain a resource hold. Add SDK dependencies only if a focused
compatibility investigation proves they are necessary and the owner accepts that decision.

### 2.4 llama-swap: the closest additional implementation reference

Inspected revision `1249e7c8b53b56dcb079a117fe1aa8ab5fa414cb`:

- The scheduler separates policy decisions from process effects; tracks queued requests,
  active swaps, generations, reservations, and in-flight requests; joins waiters for the same
  target; retries queued work on completion; and prevents eviction of a serving process.
- Its process contract separates `starting`, `ready`, `stopping`, and `stopped`, and makes
  readiness/start a state-machine operation instead of a racy check-then-start sequence.
- Group configuration includes persistent/non-exclusive exceptions. Copying an example group
  does not establish Studio's strict resource-domain invariant.

Sources: [scheduler](https://github.com/mostlygeek/llama-swap/blob/1249e7c8b53b56dcb079a117fe1aa8ab5fa414cb/internal/router/scheduler/fifo.go),
[process contract](https://github.com/mostlygeek/llama-swap/blob/1249e7c8b53b56dcb079a117fe1aa8ab5fa414cb/internal/process/process.go),
[group policy](https://github.com/mostlygeek/llama-swap/blob/1249e7c8b53b56dcb079a117fe1aa8ab5fa414cb/internal/router/group.go).
Issue #1209 reports a queued cross-target request not being re-dispatched; GitHub marks it
closed/completed. Use its interleaving as a deterministic regression, not a claim that the
inspected scheduler still has that defect. [2](https://github.com/mostlygeek/llama-swap/issues/1209)

**Decision:** borrow explicit lifecycle/effect separation and queue tests, but do not introduce
llama-swap as a mandatory daemon. A process proxy does not implement Studio's handoff/grants,
attached LM Studio ownership, context admission, or durable uncertain-action reconciliation.
Revisit optional managed-process integration only if direct adapters cannot meet qualification;
never stack two independent eviction authorities for the same domain.

### 2.5 vLLM and llama.cpp: useful ideas with important limits

vLLM documents paged KV management, batching, prefix caching, and sleep modes. It does not
remove the need to coordinate independently running servers. Level-1 sleep retains weights
in host RAM, so it is not evidence of freed physical unified memory on Apple Silicon.
[Sleep documentation](https://docs.vllm.ai/en/latest/features/sleep_mode/)
Its security guidance warns against production development-mode management/RPC endpoints.
Do not enable those as an expedient lifecycle integration. [1](https://docs.vllm.ai/en/stable/usage/security/)

llama.cpp's router documentation is another single-engine reference. Its described memory
policy assumes unlimited host memory and can proceed when measurement fails; that is unsuitable
as this application's unified-memory safety guarantee. A reported metrics-autoload issue also
reinforces the need to qualify supposedly observational endpoints. Neither a GET method nor
a `/metrics` name proves absence of loading side effects.
[1](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
[4](https://github.com/ggml-org/llama.cpp/issues/23096)
This was documentation/issue review, not an audit of the router implementation.

### 2.6 Research papers and a newly identified application budget interaction

The initial report covers ServerlessLLM (OSDI 2024), Llumnix (OSDI 2024), SGLang (NeurIPS
2024), RouteLLM (ICLR 2025), and S-LoRA (MLSys 2024), with applicability limits and citations.
For this pass, publisher material and selected design sections of **Clockwork (OSDI 2020)**
were also reviewed. Its separation of controller actions, explicit load/unload, and admission
before work are relevant; deterministic small-DNN timing and its GPU/host-memory hierarchy
are not transferable latency guarantees for autoregressive LLMs on a shared-memory laptop.
[1](https://www.usenix.org/system/files/osdi20-gujarati.pdf)

In Studio, `model_admission.py` reserves a default 80% of capacity for a heavy request;
`execution_profiles.py` separately declares an 8 GiB action-memory pool and subprocess
footprints. These independent policies do not by themselves prove their combined demand fits.
This is a source-level budget gap, not an observed OOM or a measurement of actual allocation.
**The plan must include model, KV/workspace, tools/subprocesses, and OS/application headroom
in a shared resource envelope.** One model alone is not a sufficient memory guarantee.

## 3. Architecture and guarantee boundary

### Responsibility split

```text
Session / gateway / workflows / probes / explicit controls
                         |
      authority, handoff and fully assembled request
                         |
       Shared lifecycle + context/memory admission
          |               |                 |
       oMLX adapter   Ollama adapter   LM Studio adapter
          |               |                 |
       native engine scheduling/cache/memory safeguards
```

Cloud/remote requests retain existing consent gates. A remote endpoint has its own explicitly
configured resource domain; a loopback endpoint may still proxy cloud. Never infer egress,
locality, billing, or independently available memory from wire format or a provider name.
Unknown location/domain cannot bypass the conservative gate.

### Ownership modes

- **Attached/shared:** observe only explicitly configured runtimes during authorized operations;
  reject conflicting/unknown external residents. Never claim exclusive control or evict other
  applications' work. Existing terminal-launched `ollama serve` belongs here unless an explicit
  coordination agreement changes its operational use.
- **Coordinated use:** owner authorizes Studio to manage identified instances and agrees other
  clients will not load models independently. This enables normal automatic switches but is an
  **operational assumption**, not enforced isolation of an unauthenticated local server.
- **Managed/isolated:** only advertise stronger exclusivity after launch supervision and access
  isolation are implemented and qualified for that runtime/platform. Starting a dedicated server,
  changing ports/auth/firewalls, or terminating a process requires separate explicit approval.
  Do not claim process ownership alone blocks another client from using an exposed endpoint.

First rollout can support attached/coordinated use honestly. A hard requirement for enforced
machine-wide exclusivity blocks strict certification until managed isolation is proven. Do not
hide that limitation with polling, an “exclusive” checkbox, or a lease. Multiple users/hosts
and independently launched older Studio binaries remain outside the new coordinator's control.

### Shared ownership and persistence

Proposed typed responsibilities (names are illustrative, not new shipped modules/APIs):

| Responsibility | Reuse / extension |
|---|---|
| Runtime identity and observations | Extend existing registry/discovery/lifecycle contracts; separate wire, location, resource domain, instance identity, digest and load-config fingerprint. |
| Pure transition/admission policy | Extend `model_admission.py` / `resource_admission.py`; separate residency reservation from active request lease; one policy authority. |
| Durable operations and reservations | Use `runtime_persistence.py` short transactions and an additive versioned migration; operation journal and domain-generation compare-and-swap. |
| Coordinator effect runner | Bounded native HTTP calls outside transactions, generation-checked outcomes, deadlines, observation/reconciliation. |
| Total memory envelope | Reconcile inference profiles with `execution_profiles.py` action reservations; avoid counting one allocation twice or treating independent pools as independent physical RAM. |
| Session semantics | Reuse `routing_policy.py`, execution fencing/budgets, action ledger, checkpoints, and task events. |

Use one authoritative coordinator process per owner/machine resource namespace, with a
machine-scoped owner-only lock/state location independent of workspace/session selection.
Separate databases or worker processes must not create separate “one model” authorities.
Other supported workers delegate to that authority or refuse admission. A process-local lock
or per-task epoch is insufficient. Cross-platform locking must be qualified; unsupported
locking fails closed, not silently to a Python boolean.

Durable records need domain ID, runtime/instance identity, approved profile fingerprint,
operation/request IDs, stage, generation/owner/boot identity, deadlines, cancellation intent,
observation version/freshness, reservation, outcome certainty, and bounded redacted evidence.
Store references/hashes rather than raw prompts, credentials, environment dumps, or upstream
bodies. A missing/corrupt journal with possible residents is not an empty domain.

## 4. Safety protocol and context admission

### Non-negotiable invariants

1. No target load, inference-triggered autoload, tokenizer load, or helper load on a conflicting
   domain before source release and operation quiescence are established.
2. Request completion does not release residency; an idle resident still consumes a reservation.
3. A lifecycle HTTP timeout/disconnect/cancellation does not prove the upstream operation stopped.
4. `unknown`, `unreachable`, and stale observations do not mean `empty`.
5. No arbitrary permission change, prompt tail-cut, reasoning downgrade, model replacement,
   download, server setting change, or cloud fallback to manufacture a fit.
6. No forced mid-stream eviction or replay of consequential tools during switching.
7. Task grants, budgets, evidence, pending actions, and session execution state remain authoritative.
8. All Studio load-capable paths use one boundary; unsupported generic local backends do not
   bypass it while coordinated mode is active.
9. Single residency and total memory fit are separate checks; both are required.
10. A readiness/capability/tokenizer probe must declare loading/egress effects and receive the
    same admission/consent as the operation it could trigger.

### Transition protocol

```text
VALIDATE -> QUEUE -> RESERVE -> DRAIN -> SNAPSHOT -> UNLOAD
         -> VERIFY_RELEASE -> LOAD -> VERIFY_INSTANCE/CONTEXT
         -> VERIFY_REQUEST_FIT -> HANDOFF_CONFIRM -> COMMIT_READY

Any unresolved physical effect -> RECONCILING / SAFE_PAUSED
```

- Validate permissions, declared capabilities, available local files, approved load profile,
  and feasibility **before disrupting the source**. Do not load a target merely to preview it.
- Queue with deadlines/cancellation and preserved session dependency order. Join same-profile
  target waiters without sharing confidential prompts/results. Initial policy is bounded FIFO;
  later affinity must have aging/dwell bounds so a busy resident cannot starve other sessions.
- Drain at an execution boundary; reconcile current tools/approvals first. Do not hold a global
  lifecycle transaction/lock while waiting for human approval or a tool, nor let that wait
  bypass the resource reservation. Distinguish session execution ownership from a model lease.
- Persist the portable snapshot and transition intent before effects. Keep the source logically
  selected/pending until the target is validated; show “switching,” not a false ready state.
- Request an authorized source unload; await authoritative absence/not-loading and appropriate
  memory/quiescence evidence. Fixed sleeps and blanket HTTP-400-as-success are not completion.
- Load with the approved configuration, then verify actual instance, effective context, resident
  set, memory pressure/headroom and request fit. Never generate a warmup answer as a health probe.
- Run existing required semantic handoff confirmation only under target admission and authority.
  Preserve paused sessions as paused. For an already authorized running workflow's internal
  role transition, continue only through its existing execution lease/budget checks, without
  inventing a new human Run requirement for every critic stage.

**Ambiguous operations:** record uncertainty and block new conflicting loads until supported
operation-status/cancellation or managed supervision proves no old load can finish later.
One absent snapshot is insufficient while a timed-out request is unresolved. Epoch fencing
prevents stale Studio commits, but cannot fence an arbitrary engine's physical actions.
Do not auto-replay a timed-out load; adapter idempotency must be proven, not inferred from HTTP.
If no such proof exists, require an explicit operator recovery with runtime reconciliation.
A failed target leaves a recoverable paused session; rollback to source is another verified
sequential transition, never an eager error handler loading a second instance.

### Two-stage context checks, not an impossible universal cold preflight

Some runtimes need a loaded engine to tokenize/render exactly. Therefore:

1. **Before source release:** validate model metadata, capabilities and an approved context/
   memory profile. Use a matching already-installed tokenizer only when it can be used without
   an unadmitted model load, download, or unbounded memory allocation. Metadata max context is
   not active context; disk size is not peak memory.
2. **After target is safely the only resident:** verify actual context and count the exact
   serialized request with a supported native counter or qualified matching local tokenizer.
   If counting is unavailable, do not claim a guaranteed fit; block strict readiness until a
   qualified method exists. Debug/private renderer endpoints are not production dependencies.

Count system/security text, tools, skills/memory, handoff, complete tool-call/result pairs,
history, latest user input, templates and multimodal expansion, plus requested output/reasoning
reserve. Do not double-count template overhead when native counts already include it.
Reject before inference if it cannot fit; expose safe count/limit/confidence/component totals.
A supported runtime reject-without-truncation guard is defense in depth, not proof of preflight
counts. Disable permissive truncation/context shifting only through version-supported controls;
otherwise the adapter cannot qualify for unchanged-context mode.

For the observed **7,326 input / 4,096 allocated** case, create exact offline failure fixtures.
Do not assume the entire excess is system text, or that 8K/16K is always a safe replacement.
A per-model context increase needs approved memory headroom and consistent load/inference
configuration. Summary/lazy-tool selection is separately reviewed behavior; full history remains
durable and safety anchors/current objectives are not silently removed.

## 5. Work breakdown and dependencies

`LC-00`…`LC-11` are **draft planning labels**, not accepted roadmap IDs or completed stories.
Each implementation story needs its own reviewable PR on an Arena-assigned active branch.
Do not implement this program as one monolithic change on the planning branch. Architecture,
public API/config migrations and independent security review are separate approval gates.

| Story | Scope / likely source seams | Depends on | Exit evidence |
|---|---|---|---|
| **LC-00: Capability feasibility and ADR proposal** | Offline fixtures/source-backed capability matrix for installed-version qualification, native context controls/token counting, lifecycle auth/completion and ambiguous-operation recovery. Propose ADR for ownership/persistence/protocol. | Owner review of plan | Per-runtime supported/unknown/unsupported results; unresolved tokenizer/cancel/isolation gaps explicitly block affected guarantees. No runtime mutations to “discover” support. |
| **LC-01: Identity and observations** | Registry/discovery and `local_runtime_lifecycle.py`: typed runtime/instance/domain/egress, metadata vs active context, freshness/unknown states. Preserve route strings and current conservative consent. | LC-00 | Malformed, missing, stale, remote-proxy, alias and duplicate-instance fixtures; no observation triggers loading. Contract modules registered in packaging, standalone build, mypy and test scopes. |
| **LC-02: Durable coordinator and bounded queue** | Pure transition policy plus additive SQLite journal, machine-scoped coordinator ownership, residency/request leases, generation checks and reconciliation. No live adapter activation. | LC-01 | Multi-process, duplicate waiters, cancellation, queue completion/fairness and crash-boundary tests; no DB transaction spans I/O. Old/future/corrupt schema and migration rollback covered. |
| **LC-03: Shared memory admission** | Model/load-profile reservations integrated with action/subprocess memory accounting and pressure observations. Avoid hard-coded hardware tiers and arithmetic double counting. | LC-02 | A model fitting alone but not with admitted tool work is refused; expansion/pressure/unknown capacity fails closed; existing policy/grant semantics preserved. |
| **LC-04: Verified oMLX lifecycle** | Replace sleep/suppressed-error unload semantics through a version-qualified adapter; deferred unload, auth, loading/busy/pinned/helper visibility. Keep engine inference internals. | LC-00–03 | 202 is pending, 400 is not universally success, held active streams drain, failed unload causes zero target loads; tokenizer cannot bypass admission. Adapter remains unactivated until LC-08. |
| **LC-05: Native Ollama lifecycle and inference** | Tags/show/ps separation, native load/unload, approved `num_ctx` profile on every request, canonical stream/tool/reasoning/usage translation. | LC-00–03 | Load/inference profiles match; tool/result identity preserved; no silently relaxed reasoning or truncation; timed-out load retains hold. Native vs compatible-wire semantics tested before opt-in migration. |
| **LC-06: First-class LM Studio adapter** | Qualified native v1 instance management/config verification, exact inference-instance routing, auth and explicit unsupported-version behavior. Avoid mandatory SDK dependency unless proven necessary. | LC-00–03 | Multiple/manual instances, changed context, JIT/autoevict, unavailable APIs, wrong instance and ambiguous cancel fixtures. Missing capability does not fall back to fake success. |
| **LC-07: Full request-fit admission** | Central assembled-request preparation in all workflows; exact/qualified counters after safe target admission, output/reasoning reserve, typed sanitized errors. Do not generalize the current oMLX character tail-trimming guard. | LC-04–06 | 7,326/4,096, tokenization-load, template/tool overhead, Unicode, multimodal bounds, unknown counters and changed context fixtures; essential input remains unchanged. |
| **LC-08: Every load-capable path through the boundary** | `stream_any_model`, explicit load/unload-all routes, workflow unload callbacks, fallback, tokenizer/probes/calibration, critic/child delegation and model-control helpers. Remove direct operational bypasses. | LC-02–07 | Entry-point inventory with positive/negative tests; concurrent explicit-load/inference races; zero native load requests without a valid domain operation; legacy/generic paths cannot bypass coordinated mode. |
| **LC-09: Session handoff and recovery integration** | `_execute_session_model_switch`, Harness/GSD/SDLC internal role transitions, task events, worker fencing, grants, action ledger, checkpoints and budgets. | LC-08 | Exactly-once task effects, grant/budget preservation, mid-tool/approval pauses, stale worker, failed target and restart recovery; running internal stages and paused manual sessions retain their distinct semantics. |
| **LC-10: Progress, control and migration UI** | Authenticated versioned lifecycle observations/events and existing settings/model selection surfaces. Backend changes belong to preceding runtime stories; UI work does not silently widen APIs. | LC-08–09 | Clear selected vs pending vs ready state, bounded/cancellable queue, refresh/replay, unknown/conflict explanations, no fake ETA; existing workspace/sidebar/auth behavior preserved by browser tests. |
| **LC-11: Qualification and release gate** | Fault-injection/security gate, support matrix, opt-in hardware recipe and baseline comparison; measured warm-retention/fairness tuning only after safety. | LC-00–10 | Full CI plus one-at-a-time owner evidence; quality/latency/memory thresholds agreed before run; independent security acceptance remains unsigned until an independent reviewer acts. |

The adapters can be developed independently after common contracts settle, but **do not
activate partial cross-runtime guarantees**. Production exposure requires LC-08/09 coverage;
final rollout requires LC-11. A diagnostic-only context error improvement may be reviewed
separately, but cannot be described as completion of test8 or of this program.

## 6. Test strategy and acceptance gates

### Offline deterministic gates

Use injected transports, clocks and held completions, fake runtime allocators, temp databases,
and subprocess test workers. No network/model/cloud/MCP endpoint, downloaded model/tokenizer,
or GPU is needed. Test decisions and effect traces, not just strings or helper methods.

| Scenario | Required assertion |
|---|---|
| Same instance/profile, next turn | No unload or reload; request-fit and authority checks still run. |
| Two cold targets requested together | One transition owns the shared domain; the other is eventually served or explicitly expires/cancels. |
| Source serving while switch queued | No eviction/load until protected work ends; cancellation removes only its own waiter. |
| Same target, several waiters | One load; separate authorized contexts/results; all waiters bounded. |
| Source unload 202 or false acknowledgment | Never load target until verified completion; error/unknown does not free reservation. |
| Late successful load after HTTP timeout | The domain stays held; a new target is not loaded based on one stale empty snapshot. |
| Crash around any external effect/commit | Restart reconciles journal vs engine; stale completion cannot commit or cause replay. |
| Context tokenizer causes load | It must be inside domain admission; preview does not accidentally start a second model. |
| Native inference uses a different profile | Reject or explicit sequential reconfiguration; never silently reload with a larger allocation. |
| Memory fits model but not active tools | Combined envelope rejects/queues before conflicting work; no starvation/deadlock on nested work. |
| External/manual/pinned/helper resident | Refuse conflict or request explicit authorized resolution; no model-ID-based ownership inference. |
| Cloud/proxy/remote transition | Original consent, billing, confidentiality and no-partial-output-replay constraints remain enforced. |
| Tool/approval/handoff boundary | No tool replay, broadened grant, lost evidence, reset budget or automatic resume of a paused task. |

Add trace/property tests that count **allocated or potentially allocating** instances, not
just leases. Mutate the release verifier to immediate-success, bypass one load path, remove
queue wake-up, release on timeout, and change the inference context profile: each mutation
must be caught. Tests using synthetic allocators prove control ordering, not real peak RAM.

### Automated integration and security gates

Before merging implementation, run the full Python 3.11/3.12/3.13 matrix and required browser
suite; no bypass or author-created security acceptance. Add SSRF/redirect/pinning, credential
redaction, unauthorized lifecycle mutation, operation-ID replay, event authorization, corrupt
journal/schema, orphan owner and external-process tests. Check nested inference does not
re-enter a different model or hold a domain while waiting for another worker that needs it.

Regenerate `studio.py`, route/subsystem manifests and affected UI inventories with their
own generators. New modules require setuptools, standalone inclusion, mypy and test-scope
registration. Preserve existing APIs through documented additive/versioned migrations; do
not reinterpret old `resident_model` counters as verified physical telemetry.

### Opt-in owner hardware gate — one test at a time

No new hardware instruction is issued by this document. When implementation/CI are ready,
first inventory actual versions/configuration/ownership on explicit consent, then return to
**the paused test8**. It must complete `READY` with no tools/files changed under a fitting,
approved profile, or truthfully block with an actionable reason. Blocking safely is not a pass.
Only after that succeeds should separately authorized model-switch cases follow.

Record model digests/quantization, runtime versions, active context/concurrency/load profile,
all configured engine resident/loading states, model/helper/action/process memory, OS pressure
and swap deltas, unload/load/re-prefill times, queue wait, TTFT and end-to-end latency. Sample
transitions and preserve failures, not just before/after screenshots. Zero observed overlap
cannot prove no unobserved transient or external client activity; report sampling limits.

Agree performance/quality thresholds before collecting comparative results. Recommended
acceptance shape: zero Studio-caused overlapping allocations in deterministic traces;
zero observed cross-runtime dual residency in qualified hardware transitions; no OOM/runtime
crash attributable to coordinated scenarios; no omitted required context/tool/critic stages;
no regression in paired verified task completion; and owner-approved warm-turn/switch p50/p95
latency and memory-pressure bounds. Sample count, repetition count and numerical tolerances
remain explicit owner-approved protocol decisions, not invented measured results.

## 7. Rollout, rollback and decisions before implementation

- Introduce contracts/adapters dormant; qualify support explicitly. No startup probes, automatic
  process takeover, global context enlargement, model download, benchmark, or cloud fallback.
- After all-path integration, enable coordinated mode explicitly per resource-domain binding.
  A mode claim must show supported runtimes, ownership assumptions, approved profiles and
  unresolved capability gaps. Existing generic inference remains accurately labeled unmanaged
  outside that domain; it cannot operate as an unguarded escape hatch inside coordinated mode.
- Use sanitized typed reasons such as context over budget, runtime unsupported, domain conflict,
  ownership unknown, release unverified and operation uncertain. These are proposed semantics,
  not already-shipped error codes/endpoints.
- Disabling or rolling back must drain/reconcile active operations, retain uncertain holds and
  preserve pending sessions. Do not disable safety while operations continue, clear the journal
  to “fix” a blockage, or downgrade a database silently. An older binary ignoring the coordinator
  is an external client, not a safe rollback procedure.
- Native cache retention/idle expiry/fairness tuning follows measured qualification. Do not add
  an always-resident router, speculative second model, full GPU stack replacement, or automatic
  quality-based model substitution in the initial release.

Before implementation approval, resolve these decisions:

1. Accept the recommended initial scope: attached/coordinated-use control with honest limits,
   or require managed isolation as a hard first-release prerequisite. No technical exclusivity
   is claimed for a terminal-started server merely because the owner agrees not to share it.
2. Approve the native Ollama adapter direction and supported-version/tokenizer feasibility
   gate; unavailable strict capabilities block that runtime until solved, rather than degrade.
3. Approve per-model memory/context profiles and the opt-in measurement protocol before any
   owner's runtime configuration or hardware tests change.
4. Assign accepted runtime story IDs/ADRs and independent security reviewer through normal
   governance. This draft does not modify the historical runtime/UI ledgers or claim a review.

## 8. Research and verification record

This planning pass inspected pinned upstream source in oMLX, Ollama, and llama-swap, pinned
LM Studio documentation (`8c7159be3ffe364af10aa8ae6f5a7570e9402089`), current official API/security
pages, relevant GitHub issue/PR state, and the paper material described above. Existing local
code was re-inspected around routing, admission, lifecycle controls, persistence, workflows,
execution profiles, and durable task events. Supporting temporary upstream source copies are
not committed. Links to mutable docs describe research-time observations, not installed-version
certification. The support matrix is an implementation gate, not a completed hardware result.

No production source, config, permissions, model state or public protocol changed. This is a
planning deliverable. No live inference or hardware benchmark ran; manual test8 remains paused.
Validation in this planning turn: **243 passed** in 26.90s across 15 existing focused suites
(admission, lifecycle/discovery, model/provider/portable switching, workflows/routing, SQLite
persistence, resource/execution profiles, worker fencing, task events and consent), with one
upstream Starlette/httpx deprecation warning. Documentation validation passed with 238 files
scanned; the 64-file UI source manifest and whitespace checks passed. These are unchanged-code
regressions, not tests of an implemented coordinator. Full multi-Python CI, browser tests,
hardware qualification and independent security review were not run for this docs-only plan.
