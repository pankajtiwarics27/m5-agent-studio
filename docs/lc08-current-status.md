# LC08 current status and gap review

## Continuation after this snapshot

The first bounded G1 follow-up is now documented in the
[Council/Debate adapter audit](lc08-workflow-adapter-audit.md): a checked-in
29-owner coverage matrix, two audited shared-adapter owners, coordinated
reasoning-retry suppression and deterministic iterator closure. It adds offline
input/refusal/cleanup and limited real warm-fit/native/standalone evidence, not
all-workflow or installed qualification. The snapshot below remains pinned to its
PR262 base; its next-increment recommendation led to that follow-up, not G1 closure.

The next bounded [Hall HTTP summon audit](lc08-hall-summon-audit.md) adds one
assessed owner (26 remain inventory-only), closes both owned attempt sources and
denies coordinated changed-budget retries. Hall retains save/pin-before-cleanup
semantics; cleanup refusal cannot announce success or erase the completed reply.
It is not an MCP summon audit, LC09 retention change or installed qualification.

The bounded [MCP stdio summon audit](lc08-mcp-summon-audit.md) then adds one
more assessed owner (25 remain inventory-only) and closes the owned dispatch
source on consumer failure. It preserves MCP cleanup-before-save ordering and
existing defaults, not HTTP Hall session/retention parity or native activation.

The bounded [workspace suggestion audit](lc08-workspace-suggestion-audit.md)
adds one more owner (24 remain inventory-only) and closes its source when
rejecting overlong output. Consent, full accepted input, tool-context restoration
and suggestion-only/no-action semantics are preserved, not folder authorization
or native activation.

## Scope of this snapshot

Reviewed against merged base `fc6a304c970fec65a8a324183a83443f71443318`
(PR262). This is a source/test coverage review, not an installed-runtime audit,
completed lifecycle story, accepted ADR or activation plan. LC00–LC11 are the
[draft planning labels](cross-runtime-model-lifecycle-plan.md), not completion claims.

**Current conclusion:** substantial dormant LC08 composition and failure containment
exist. Production composition, complete workflow coverage and qualification do not.
Legacy remains the default. No runtime, model or hardware experiment is authorized
by this document. [ADR0038](adr/0038-coordinated-dispatch-integration.md) remains
proposed/not reviewed. LC09 recovery and LC11 independent acceptance remain separate.

Use this snapshot for present status. The [integration record](runtime-dispatch-integration.md)
is chronological: its early descriptions of missing cold scheduling, controls or
race tests have been superseded by later implementations. A refusal is a safety
boundary, not successful support for the refused feature.

## What already exists

All execution evidence in this table uses offline synthetic native/counter facts.
Existing services, actual coordinator/memory transactions and embedded standalone
kernels are exercised; installed versions/physical memory are not qualified.

| Capability | Current implementation | Evidence anchors | Limit that remains |
|---|---|---|---|
| Common dispatch and bypass fencing | Immutable coordinated dispatch ahead of legacy mutation; explicit control/cleanup routing; direct legacy and generic-HTTP guards | [boundary](../runtime_dispatch_boundary.py), [application seams](../app.py); [boundary tests](../tests/test_runtime_dispatch_boundary.py) | Static inventory plus selected executable tests is not all-workflow consumption proof; legacy remains served by default. |
| Warm inference and full-wire fit | Concrete Ollama/LM Studio executor, exact prepared-request/profile fit and terminal-gated structural calls/usage | [executor](../native_dispatch_executor.py), [fit service](../request_fit_admission.py); [executor tests](../tests/test_native_dispatch_executor.py) | No installed counter/qualifier/transport/live-authority producer; not oMLX inference qualification. |
| Cold-to-warm inference | Bounded `NativeInferencePlan` actor, pure pre-disruption readiness, concrete lifecycle stages and final warm handoff | [scheduler](../native_inference_scheduler.py), [driver](../native_transition_driver.py); [inference tests](../tests/test_native_inference_scheduler.py), [driver tests](../tests/test_native_transition_driver.py) | No default application actor composition or durable task/session orchestration. |
| Explicit load | Optional Ollama/LM Studio `NativeLoadPlan`, real load-purpose slots and durable provider-specific confirmation receipts | [load tests](../tests/test_native_load_control.py), [confirmation tests](../tests/test_native_load_confirmation.py) | Not a future-request fit guarantee; no oMLX target qualification or default HTTP installation. |
| Explicit unload | Release-only schema-v4 foundation, concrete Ollama/LM/oMLX source jobs and optional `NativeUnloadPlan`; physical proof, real zero settlement and durable retirement are separate | [release kernel tests](../tests/test_model_lifecycle_release.py), [native unload tests](../tests/test_native_unload_control.py), [oMLX unload tests](../tests/test_omlx_unload_control.py) | Source-only oMLX support is not target/inference support. Already-absent success, unload-all/cache and reviewed populated-schema migration remain absent. |
| Race and ownership containment | Selected same/independent actor control–inference races, both Ollama/LM switching directions, oMLX source release; exact queued FIFO/lease/stage identity | [load races](../tests/test_native_load_control.py), [unload races](../tests/test_native_unload_control.py), [lease ownership](../tests/test_native_scheduler_ownership.py), [stage ownership](../tests/test_native_transition_ownership.py) | These cases do not exhaust every workflow, external-process/native effect or restart/session race. |
| Interruption accounting | One fenced current-intent/model hold across actors, drivers, native bridges and fit-counter failures; genuine uncertainty stays charged, stale cleanup cannot pause successors | [memory hold](../shared_memory_admission.py); [native hold tests](../tests/test_native_interruption_holds.py), [counter hold tests](../tests/test_request_fit_interruptions.py) | Accounting is not native cancellation, physical release, retry permission or LC09 recovery. |
| Canonical gateway and standalone | `_coordinated_gateway_chat` preserves native call IDs/argument strings; embedded private package shares actual persistence identities and blocks sibling fallback | [gateway/executor tests](../tests/test_native_dispatch_executor.py), [gateway/scheduler test](../tests/test_native_inference_scheduler.py) | Other text-tool consumers refuse advertised native tools before planning, or unexpected structural calls; they are not yet canonical workflow consumers. |

`NativeDispatchExecutor.command` still refuses controls: the optional load/unload
support is on `NativeInferenceScheduler`, not on the warm executor. Saying “all
controls unavailable” or “cold actor not implemented” without this distinction is
now incorrect. Likewise, full explicit-control/inference and cross-runtime race
coverage is incomplete, not nonexistent.

## Remaining gaps, grouped by dependency

These review-local IDs are pointers, not new accepted roadmap stories.

| ID | Gap / current evidence | Required exit evidence, not achieved by this review |
|---|---|---|
| G1 — workflow input and consumers | The [static entry inventory](runtime-dispatch-entry-points.md) lists Hall, Harness, GSD/SDLC, child/critic, MCP, workflow cleanup and probes/calibration. Common fences and the gateway are tested, but this does not establish every caller's complete assembly and canonical consumption. | Per-owner mapping from input assembly to final counted bytes and accepted output; executable full-input positive cases where supported, pre-native refusal where unsupported, tool-call/result identity and close/cleanup assertions. A static match or zero-send refusal cannot be labelled successful workflow integration. |
| G2 — production composition and authority | `app._local_runtime_dispatch` creates a legacy boundary with no native backend; scheduler factories default to absent. No qualifier, native transport, live grant, counter, proof or memory producer is installed. | Separately reviewed composition/ownership and installed-capability evidence; all required producers and policies fail closed before disruption. No mode switch or default registration in this documentation increment. |
| G3 — unsupported target/control semantics | Generic actor oMLX target load/inference, already-absent success, unload-all/cache, coordinated combos and unowned auto-eviction remain refused. | Separate explicit semantics and qualification for each affected path; actual operation ownership, physical proof and independent memory settlement. Never generalize source-only support or treat ACK/empty inventory as completion. |
| G4 — remaining adversarial coverage | Existing races/receipts/holds are strong bounded cases, not every application path, external effect or recovery point. | Extend per-path mutation, late-effect, cancellation, restart/fencing, nested-inference and control races against the applicable producer/consumer. Test unsupported cases as refusal, not fake readiness. |
| G5 — LC09 and data evolution | Durable tasks/session handoff/checkpoints/grants/budgets/recovery are not completed by the inference actor. Private old schema versions refuse; no populated migration/backfill/reset procedure is added. | Separately reviewed LC09 exactly-once/restart/worker/grant/budget integration and any required additive migration evidence, preserving user data and uncertainty barriers. |
| G6 — review, UI and hardware gates | ADR0038 is proposed. LC10 migration/progress UI and LC11 release/security qualification stay separate. Test8 remains paused/unpassed. | Independent reviewer decisions and separately authorized installed/hardware evidence. No CI/mutation count promotes acceptance. Resume no hardware test automatically. |

The existing lifecycle plan remains the authority for dependency and acceptance
requirements; these gaps do not loosen it. In particular, the observed
**7,326 input / 4,096 allocated** case is not qualified by an offline fit fixture
or by changing a context setting. Test8 requires its own authorized owner run.

## Recommended next bounded increment

Start with **G1: an executable workflow input/consumer coverage matrix**, not
activation or another broad safety-completion claim.

The first candidate is Council/Debate through `app._workflow_orchestrator` and
[`WorkflowOrchestrator._run_stage`](../workflow_orchestration.py). The application
wrapper forwards stage input and owns unload callbacks; the generic orchestrator
also retains a local reasoning-only recovery retry with reasoning off and a
4,096-token output setting. Existing [workflow tests](../tests/test_workflow_orchestration.py)
exercise legacy recovery and SSE contracts. They do not by themselves establish
which coordinated paths can reach that branch or whether retry is permitted by
current native completion/fit authority. Audit this explicitly; this review has
not reproduced a coordinated runtime defect or authorized removal of legacy
behavior. Do not advertise the candidate as a new supported canonical consumer.

1. Reconcile each owner in the generated static inventory with its actual input
   assembly, common dispatch, tool consumer and cleanup path. Explicitly classify
   supported canonical consumption versus current fail-closed refusal; list any
   untested owner as untested, not covered by association with the common helper.
2. Select one representative non-gateway workflow from that audit for a bounded
   offline regression PR. Capture system/security text, tool definitions, complete
   call/result pairs, history, latest input and output/reasoning options at its
   current seam; compare the immutable request and, where actually supported,
   final provider-wire/counter input without trimming or substitution.
3. Test structural-call handling and consumer close. If that consumer is text-only,
   prove it refuses before native planning/count/send and does not execute tools,
   report successful completion, drop native IDs or invent release. Do not convert
   that negative case into an implementation-support claim.
4. Trace its cleanup separately: pending/uncertain/refused unload must not advance
   as success, and cancellation must not mutate a foreign owner or retire charge.
5. Record what the selected tests prove and what is still missing. Any newly found
   runtime fix or consumer migration needs a scoped follow-up, not silent expansion
   of this status review.

Exit for that increment: checked-in per-owner mapping, concrete positive/negative
regressions for the selected owner, mutation sensitivity for the asserted seam,
app/standalone parity where affected, and clean applicable CI. No schema, route,
default, production registration, hardware run or acceptance-ledger change.
G2–G6 remain open even if that increment passes.

## Validation and historical evidence

PR262's implementation head `72551c878af043bd51e7ec4503b254b616493b8a` completed:

- Full local regression: **5,037 passed, 32 skipped, 1 warning**, exit 0,
  2,418.53 seconds. Skips: 31 local browser-configuration cases and one opt-in live
  container test. Warning: upstream Starlette/httpx deprecation.
- [PR CI42553750308](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42553750308)
  and [push CI42553726845](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42553726845):
  Python3.11/3.12/3.13 and Browser safety green on that exact head.
- [PR262 final result](https://github.com/pankajtiwarics27/m5-agent-studio/pull/262#issuecomment-6731847695)
  supersedes its earlier pending notes. [PR262](https://github.com/pankajtiwarics27/m5-agent-studio/pull/262)
  merged as the base of this review. Those results are historical, not a fresh test
  run of this documentation branch.

P7-PARK-01 was resolved upstream by PR261; all 21 release-review assertions are
restored. Its imported browser/CI evidence and production-security block are
unchanged. Historical LC08 text saying P7 is parked is superseded, not permission
to re-park. UI handoff readiness is not lifecycle/native qualification.

This review changes documentation only. It does not mark any gap closed, run live
native operations, install producers, alter acceptance records or authorize merge
of a future implementation. Validation of this documentation change is reported
separately from the historical runtime results above.
