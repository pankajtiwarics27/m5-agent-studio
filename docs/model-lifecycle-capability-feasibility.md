# LC-00: Cross-runtime lifecycle capability feasibility

Date: 2026-11-06. Status: **offline investigation prepared; installed qualification not run**.
Related: [implementation plan](cross-runtime-model-lifecycle-plan.md),
[proposed ADR-0031](adr/0031-shared-domain-model-lifecycle-authority.md).

## Scope and evidence levels

The owner authorized starting LC-00 after reviewing the plan. This authorizes source research,
offline fixtures and an ADR proposal, **not runtime takeover, configuration changes, hardware
testing, security signoff, or an accepted ADR**. The program remains oMLX + Ollama + LM Studio.
No runtime instance or installed version has been inspected in this environment.

Use three separate evidence levels:

1. **Upstream contract:** behavior found in pinned source/docs; applies only to the reviewed
   revision/path/backend, not every release or the owner's installation.
2. **Synthetic policy replay:** proposed necessary conditions evaluated on invented normalized
   facts and minimal response excerpts. Proves the oracle behaves as specified, not that a real
   adapter can produce those facts or an engine frees physical memory.
3. **Installed qualification:** opt-in, one-at-a-time owner evidence against a named runtime,
   model/digest/backend, configuration, ownership mode and Studio revision. **Not run for any
   runtime.** No source/fixture result may promote this level automatically.

## Pinned source register

IDs also identify the sources behind the [offline fixture corpus](../tests/fixtures/model_lifecycle/feasibility.json).
Response excerpts are synthetic, not logs copied from a user's runtime. Source revisions are
research snapshots; they are not approved minimum versions or recommended installation commands.

| ID | Inspected source | Relevant contract |
|---|---|---|
| OMLX-SERVER | [server.py at 2165e757](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/server.py) | Public model management, deferred unload, engine-leased tokenization and media rejection. |
| OLLAMA-ROUTES | [routes.go at d63b7026](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/routes.go) | Load/inference scheduling, ps, metadata cloud proxying, native flags, private debug render. |
| OLLAMA-SCHED | [sched.go at d63b7026](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/sched.go) | Runner reload/expiry and asynchronous operations. |
| OLLAMA-TYPES | [api/types.go at d63b7026](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/api/types.go) | Native `options.num_ctx`, `truncate`, `shift`, private `_debug_render_only`. |
| OLLAMA-PROMPT | [prompt.go at d63b7026](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/prompt.go) | Model/template-dependent prompt rendering and a heuristic image accounting path. |
| LM-REST-LIST | [list.md at 8c7159be](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/2_rest/list.md) | Artifact key vs loaded instance ID, loaded config and architectural max context. |
| LM-REST-LOAD | [load.md at 8c7159be](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/2_rest/load.md) | Approved load configuration and echoed final configuration. |
| LM-REST-UNLOAD | [unload.md at 8c7159be](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/2_rest/unload.md) | Explicit instance unload, not a general cancel-load contract. |
| LM-SDK | [async_api.py at 16796832](https://github.com/lmstudio-ai/lmstudio-python/blob/16796832c1665985c73f5a9e938cbf17b40adf02/src/lmstudio/async_api.py) | Loaded handles, tokenize/count/context RPCs, apply-prompt-template parameters and channel-based loading. |
| LM-TOKEN-DOC | [tokenization at 8c7159be](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_python/4_tokenization/index.md) | Documented text chat template + tokenizer + active context workflow. |
| STUDIO-TEST8 | [original incident/audit](model-lifecycle-research-and-proposal.md) | Observed 7,326/4,096 failure, still paused. Fixture reproduces budget arithmetic only. |

## Capability matrix — source-level, not installed support

**Candidate** means an upstream mechanism exists to investigate. **Gap** means a required
proof or integration is absent. **Unsupported path** applies to the specified reviewed path,
not necessarily every capability offered by that engine. Every installed result is **unknown**.

| Capability | oMLX | Ollama | LM Studio |
|---|---|---|---|
| Catalog without inference | Candidate public/admin routes; auth, aliases and local-state completeness require qualification. | Candidate `/api/tags` for local catalog; `/api/show` is conditional and can proxy cloud metadata. | Candidate native v1 list; distinguish downloaded artifact from loaded instances. |
| Residency / effective context | Candidate richer pool status; helpers/loading and configuration provenance must be explicit. | Candidate `/api/ps`; not proof of no queued/late load or complete system memory accounting. | Candidate `loaded_instances[].id` and `config.context_length`; architectural max/file size are insufficient. |
| Explicit load | Candidate public blocking load or authorized admin route; load options/settings need binding. | Candidate empty native chat/generate + approved options; load-capable even with no prompt. | Candidate native v1 load with approved context and final config; route inference to the exact instance. |
| Unload completion | 202 is deferred; 200/`ok` is still followed by release observations. Drain rather than rely on aborting streams. | Native expiry acknowledgment followed by observed absence/quiescence; cannot release a hold on HTTP success alone. | Exact-instance unload followed by list/quiescence/memory evidence; Auto-Evict is not universal control. |
| Load cancellation / operation certainty | Gap: no installed authoritative completion/cancel protocol established for a timed-out load. | Gap: HTTP cancellation is not physical cancellation; scheduler state may outlive the client. | Gap: SDK has channel cancellation machinery, but the inspected loading coroutine does not establish server-side physical cancellation on a Python timeout. REST operation IDs not established. |
| Exact text/tool counting | Candidate `/tokenize` chat path uses model prompt preparation; gets an engine lease and may reject unsupported engines. | Gap: no qualified public full-request count path found in reviewed router. Counter/template equivalence must be solved before strict activation. | Candidate SDK text template/tokenizer/context methods; production-wire tool/reasoning/config equivalence remains unproven. No REST token endpoint invented. |
| Multimodal counting | **Unsupported path:** reviewed `/tokenize` explicitly rejects image/audio/video/file parts. Need a separately proven method. | Gap: reviewed `chatPrompt` image heuristic is not exact model/media expansion. Different backends/renderers need separate qualification. | Gap: text-tokenizer examples are not proof of media preprocessing/expansion parity. |
| No silent context alteration | Gap: qualify engine/settings path, including tool-result transformation and context policies. Exact count alone does not prove no transformation. | Candidate native `truncate:false` + `shift:false`; qualify backend behavior, output reserve and failure timing. Generic compatible requests do not establish this contract. | Gap: qualify actual template/settings/context overflow behavior, not just a reported context length. |
| Strong exclusivity against other clients | Gap on attached servers. | Gap on terminal-started shared server. | Gap with GUI/other clients loading independently. |
| Combined peak-memory proof | Gap: calibrated model/context/KV/workspace + required components + concurrent tool demand + application/OS headroom. | Same gap; disk sizes and independent admission pools are insufficient. | Same gap; `size_bytes` is artifact size, not measured peak residency. |

No complete version can be labeled strictly supported from this matrix alone. Backend/model
profiles can differ inside one runtime version. Source review must not become a blanket version
comparison such as “version newer than X implies safe.”

## Feasibility findings and recommended choices

### oMLX

`unload_model` returns deferred 202 when active work is being aborted. Studio's existing
`OmlxLifecycleAdapter` can preserve the returned status but is not a completion verifier;
the application's separate unload helper suppresses failures. Neither is a global coordinator.
Future LC-04 must reconcile both, drain first, and distinguish absence from unknown state.

`_tokenize_chat_messages` constructs a chat completion request with messages, tools and template
kwargs and uses shared prompt helpers. This is a promising text/tool path. It also uses model
settings such as tool-result limits; counting the transformed prompt accurately is **not** proof
that essential evidence survived unchanged. Qualify preprocessing under the approved profile.
The tokenization route acquires an engine and raises an error for media or unsupported models.
Do not advertise universal exact token counting, even for oMLX alone.

### Ollama

Prefer a native dialect adapter, not native prewarm followed by an unbound OpenAI request.
Bind a canonical approved load-profile fingerprint to every request, including context and
relevant backend settings. Native `truncate`/`shift` are explicit fields in reviewed request
structs, but model/backend paths differ; `IsMLX` and native renderer paths must not inherit
claims established only for a GGUF/llama backend.

`ShowHandler` branches to cloud metadata proxy/cache paths for cloud references. A loopback
metadata request therefore needs a model-source/egress classification and explicit applicable
consent before it is sent. Do not probe a potentially remote model merely to discover whether
it is remote. Unknown classification is blocked or handled through already authorized metadata.

The reviewed router does not expose a qualified public full-chat token counter. `_debug_render_only`
is private, occurs after scheduling and can return rendered text rather than a full authoritative
count. Do not use that, an embedding request, `num_predict=0`, or a one-token generation as a
covert preflight. Completion usage arrives too late to establish pre-inference output-reserve fit.

**Release blocker:** investigate an already-installed matching tokenizer plus verified renderer
for supported model profiles, or a maintained public native mechanism with full-input/output
admission guarantees. No automatic tokenizer download, executable remote model code, or separate
LLM load. A runtime's no-truncation rejection is valuable defense in depth, not a claimed exact
preflight. Until equivalence is proven, mark strict counting unsupported for that profile.

### LM Studio

Prefer native v1 instance management. The inspected maintained Python SDK offers `tokenize`,
`count_tokens`, `get_context_length`, loaded-instance handles, and prompt-template application.
That makes it worth a focused optional-integration spike before writing private RPC code.

However, `_apply_prompt_template` submits an empty `predictionConfigStack` alongside history
and options. This is not evidence that every Studio tool/reasoning/per-request setting matches
the compatible inference path. SDK `get_or_load` and “any model” conveniences are inappropriate
at a strict lifecycle boundary: they can load or select the wrong instance. Bind known handles.
The loading coroutine awaits a channel result; prediction `.cancel()` examples do not prove
that a timed-out load cannot finish later. Maintain uncertain holds until verified recovery.

No SDK was installed or added as a dependency. An optional dependency requires a focused ADR
or follow-up to this proposal, explicit version constraints, maintained-code review, and tests
for configured-host transport, auth/pinning, lifecycle deadlines and no implicit port discovery.
If those controls cannot be preserved, SDK convenience does not justify adopting it.

### Authority, memory and recovery

The build direction is feasible as a **Studio-owned orchestration protocol**, not an unconditional
machine-wide promise over arbitrary native daemons. Proposed ADR-0031 establishes one authority
per declared shared memory domain with honest ownership modes. Hard enforced exclusivity remains
a managed-isolation requirement, not a property a database lease can create.

Do not clear a domain after a timeout merely because `/ps`/list is empty: a pending operation
may load later. Native operation status, a proven terminal response/ordering contract, or approved
supervision is needed. If none exists, recover visibly with operator assistance and retain the
hold. A terminal-operation flag in a synthetic fixture is a **hypothesis an adapter must prove**.

The LC-03 memory envelope must combine persistent weights/components, profile-dependent caches
and workspaces, concurrent tools/subprocesses and system headroom. The spike's `headroom_verified`
flag does not implement a profiler, calibration method or budget allocator.

## Offline spike and reproducibility

Run only local files; no app import, runtime transport, SDK, environment discovery, downloads,
model inference or server settings are involved:

```bash
python scripts/check_lifecycle_feasibility.py
python -m pytest -q tests/test_model_lifecycle_feasibility.py
python -m mypy scripts/check_lifecycle_feasibility.py
python scripts/validate_adrs.py
```

The [spike script](../scripts/check_lifecycle_feasibility.py) replays **19 synthetic cases**:
10 release-evidence cases and 9 request-fit cases. Its oracle checks proposed necessary
conditions; it does **not normalize native wire responses**. Minimal `wire_excerpt` fields are
illustrative provenance, deliberately not used as substitutes for lifecycle observations.
Profile identifiers are invented opaque labels, not computed production fingerprints.

The corpus includes two hypothetical positive cases returning `candidate_only`; even they
always retain `activation_allowed:false` and `installed_runtime_qualification:not_run` in the
report. Exit 0 means fixture expectations matched, never permission to activate. Exit 1 means
an expectation mismatch; exit 2 means invalid local corpus. Reports omit raw wire/evidence bodies.
There is no output artifact writer or runtime activation option.

Additional tests remove every required fact, reject malformed counts/booleans, check output
reserve and exact fit boundaries, verify nonzero failure exits and non-disclosure of fixture
body text, and restrict script imports to the standard-library offline allowlist. These are
**tests of a research oracle**, not actual adapter or coordinator fault-injection coverage.
Future LC-02/07 tests must replay the cases through real contracts/effect traces; update or
retire the oracle rather than importing it into `app.py` or the standalone distribution.

## Gate disposition and next work

| Gate | Disposition |
|---|---|
| Research register and source-level feasibility | Prepared; pinned sources and reviewed-path limits recorded. |
| Offline expected-policy examples | Prepared; synthetic replay and positive/negative tests only. |
| Architecture decision | Proposed ADR-0031, **not reviewed or accepted**. |
| Installed capability / ownership / memory profiles | Not run; all three runtimes unqualified. |
| Exact Ollama full-request counting | Blocking strict profile activation; no approved public path yet. |
| LM Studio complete inference/counting parity | Blocking strict profile activation; documented SDK route needs a security/parity spike. |
| oMLX media/unsupported-model tokenization | Blocking affected profiles; text-only counter is not a universal workaround. |
| Timed-out physical operation resolution | Blocking automatic recovery where no terminality proof exists. |
| Runtime changes / test8 / release/security acceptance | Not performed; test8 remains paused. |

LC-00 produces the feasibility package; it does not mark the draft program or runtime support
complete. LC-01 can represent these unknown/unsupported capabilities explicitly without enabling
operations. LC-02 can develop durable transitions against fakes. Both must retain activation
blocks until ADR/ownership and runtime-specific proofs are resolved. Do not amend historical
runtime/UI roadmaps, independent signoff, or approved visual baselines for this investigation.

## Verification recorded for this change

The final focused run passed **338 tests across 19 suites in 35.27s**, including
57 spike tests, with one upstream Starlette/httpx deprecation warning. Repository
Ruff passed; mypy passed for 62 existing contract sources and separately for the
spike script. ADR, documentation (240 files), governance, existing security-signoff
consistency, source-manifest and whitespace checks passed. Signoff consistency is
not independent acceptance; the existing deferred/pending record is unchanged.
Full multi-Python/browser CI and hardware tests were not run locally. No production
module, standalone build input, dependency, runtime setting or schema changed.


## LC-04 source recheck and adapter boundary

The [unactivated oMLX bridge](omlx-lifecycle-bridge.md) rechecked the same pinned
[public routes](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/server.py#L3973)
and [pool status builder](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/engine_pool.py#L4754).
Public load is `/v1/models/{model_id}/load`; no fallback admin route is assumed.
`model_context_length` is model metadata; pool status supplies neither exact
native instance/digest nor active-request counts or authoritative physical
release. Exposed aliases, helpers, external/distributed engines and missing flags
must not become an invented complete empty inventory. Source review/strict fake
parsing does not qualify a currently installed version. The adapter therefore
requires external installed-scope attestations and native evidence producers,
none of which is registered or certified by this story.
