# LC08 Council/Debate adapter audit — dormant continuation

## Scope and coverage accounting

This bounded continuation follows the [gap review](lc08-current-status.md) at
merged PR263/base `04ec70b`. It audits `api_council_stream.event_gen` and
`api_debate_stream.event_gen` through `_workflow_orchestrator.stream_model` and
`_workflow_orchestrator.unload_model`, not all Studio workflows or installed native
engines. No production composition, mode switch, hardware instruction, new
canonical tool consumer, schema migration or acceptance decision is added.

The checked-in [owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains all **29 owners / 54 static sites** from the generated
[dispatch inventory](runtime-dispatch-entry-points.md). Tests compare owner/seam
sets with the actual AST inventory, so a new or removed owner cannot silently
retain this mapping. Only the **two shared workflow-adapter owners** are marked
`bounded_workflow_adapter`; **27 owners** remain `inventory_only_in_this_audit`.
This does not erase existing gateway/boundary tests, nor promote the other owners
by association. The two Council/Debate assembly closures are named separately:
they call the shared orchestrator rather than directly matching a wire seam.
The matrix is a coverage index, not a permission/qualification or reachability oracle.

## Findings reproduced before the fix

After repairing offline fixture setup (an absent cloud-provider registry) and
avoiding a trailing-space expectation that contradicted existing request `.strip()`
normalization, the valid original-source baseline was **6 failed / 12 passed**:

- Two real application-route compositions retried reasoning-only responses,
  replacing the stage reasoning mode with `off` and the output budget with4,096.
  The policy lived above the already-fenced dispatch seam. A completed response
  did not authorize another invocation with changed options.
- Two consumer-close and two disconnect cases left the backend iterator open
  until later async-generator finalization. Assertions ran before loop shutdown
  or garbage collection; the common boundary could close its own source only
  after the outer adapter/orchestrator iterators actually reached it.

These observations used a fake coordinated oMLX backend, not qualified oMLX native
inference. They prove application composition behavior, not physical engine effects.
The early invalid fixture failures are not counted as additional runtime defects.

## Changes and invariants

`WorkflowOrchestrator` has an optional application-owned reasoning-retry predicate.
Its default preserves existing provider-neutral legacy behavior. The application
adapter supplies a live predicate refusing this recovery retry in coordinated mode;
local/cleanup identity is not reused as retry authority. Legacy application tests
still observe the original reasoning-only retry with `off`/4,096. No output cap,
reasoning transformation or new retry is added to coordinated execution.

The route owns the orchestrator iterator, the orchestrator owns each stage/attempt
iterator, and the adapter owns common model dispatch. Every forwarding layer now
closes its owned closable source on exit. The provider-neutral seam still tolerates
async iterators without `aclose`. Close/disconnect/cancellation propagates before
return, without claiming native cancellation, completion, memory release or recovery.
No cleanup command, next stage or successful saved run is invented on an interrupted
attempt. Actual native/accounting failure policy remains in the native services.

Existing cleanup refusal is retained: only canonical `confirmed` permits progression.
Pending/refused/uncertain and malformed `ok` replies cannot start a later stage or
save a successful whole workflow. A prior completed stage may already have emitted
its stage-completion event; that does not mean cleanup or the whole run succeeded.
Tests using fake `confirmed` replies prove routing/progression only, not native
physical release. No change to the legacy unload implementation is implied.

## Input and output evidence

The [new regressions](../tests/test_workflow_coordinated.py) cover:

- Both real route assembly functions, long Unicode/security/task input, the
  existing per-seat skill policy (Council critic intentionally receives no skill
  block), complete prior-stage text and unchanged output/reasoning options at the
  immutable boundary. Existing API whitespace normalization is not removed or
  mislabelled as token trimming.
- Unexpected structural calls refuse via the existing common text-consumer gate;
  advertised tools refuse before backend planning. These remain unsupported tool
  consumers, not implementations that preserve or execute native calls/results.
  Council/Debate's current stage schema does not advertise a tool-call history;
  no full tool-pair/multimodal inclusion claim is made here.
- Consumer close at both orchestrator and HTTP-response iterator seams, explicit
  disconnect, actual task cancellation, unconfirmed cleanup, confirmed fake cleanup,
  no whole-run success on refusal, and unchanged generic/legacy retry behavior.
- A single application stage through each real dormant Ollama/LM Studio warm
  executor, exact fit count and native send using existing synthetic native
  fixtures. The prepared-body comparison in that fixture checks final bytes.
  Closing the same adapter mid-native stream also reaches the actual bridge and
  conservatively retains its active request, paused model and600byte charge.
  This is not a full multi-stage cold Council/Debate native qualification or proof
  for oMLX inference; it does not install any producer or broaden a permit.
- Isolated standalone retry/response-close/pending-cleanup behavior for both routes,
  with public application/orchestrator/dispatch sibling fallback blocked before
  test-only aliases are introduced. Actual production bundle imports remain isolated.

**36 new offline tests pass.** The original5 workflow tests also pass. Three valid
isolated shadow-module mutations are caught: restore coordinated retry (2failures),
remove owned iterator closing (4failures), and accept unconfirmed cleanup
(6failures;2malformed-ACK cases still refuse at the boundary). No tracked source was
mutated for those experiments. Full/combined/CI totals are recorded separately only
after execution; historical PR262 results are not proof for this increment.

## Remaining work

G1 is **partially exercised**, not closed. Other workflow owners still need their
own complete assembly/consumer/cleanup proof. Native tool consumers, broader cold
multi-stage/race/recovery behavior, production composition, installed qualification,
unsupported target/control policies and LC09–11 remain open. Legacy stays default;
oMLX target/inference stays unqualified on the generic actor. Test8 remains
paused/unpassed. ADR0038 and independent architecture/security acceptance are not
advanced. Main's restored P7 release-review assertions and imported evidence remain
unchanged in authority; passing CI is not production-security approval.

## Validation for this continuation

The initial combined29-module workflow/native/lifecycle/fit run passed **1,415 tests,
1upstream warning in94.47s** with the first32new cases. Four more partial-backend failure/native-close cases were then added; the final
workflow/generated-contract/docs/ADR suite passed **234 tests,1warning in39.27s**,
including all36new cases. No production
source changed between these two runs. Warning: upstream Starlette/httpx deprecation.

The backend gate initially caught an old literal-source assertion expecting
`async for event in orchestrator.run_stages` after iterator ownership changed to a
scoped close. Both Pipeline order tests now inspect the actual AST and require
run saving outside and strictly after the exhausted/closed stage scope. This is
not a skip or a relaxation of failure/save ordering. Pipeline tests58/58 and the
rerun backend302/16 pass. Shallow checkout history was fetched to verify the actual
pinned baseline/browser-evidence commits, without relabelling evidence or pins.

Bundle4,279,027bytes; generated dispatch/route/subsystem inventories and source/
preservation/backend/performance/review artifacts are refreshed in dependency
order. Source64/6/121, preservation21/272/50, performance21/21/11 and restored
review7/7local/2200visual/15Compare with0findings check clean. The review changes
only its embedded backend smoke path; imported approvals and21restored assertions
are untouched. App/standalone startup/auth/origin smokes, screenshots6/6, frontend
syntax, Ruff/mypy75sources, docs261/ADRs and scope map203/13 pass. These are author
checks, not native/hardware/independent acceptance. Full local regression and
exact-head CI remain pending until separately recorded.

## CI artifact-cascade correction after bc2da21

The initial exact-head PR42564048187 and push42564020144 failed all3Python
jobs; Browser safety passed in both. Their annotations name stale UI artifacts,
including nested documented test commands, not an additional native runtime
failure. The initial local subset did not validate the full app-derived graph.
The incomplete bc2da21 full local run was stopped before regeneration and has
no completed result; it must not be counted as a full pass.

The handler line moves/app digest also require the UI route map, authorization
trace, Recent Work, Start Health and Pipeline Workspace inventories to be refreshed.
Seven failures reproduced locally (107passed alongside them); route-map --check
then independently identified the remaining upstream stale digest. Regenerate in
this dependency order: UI route map → authorization trace → preservation signoff;
Recent Work/Start Health/Pipeline Workspace inventories; then the37-check UI gate
and dependent V2 review. Existing generators update only current source hashes,
handler/refusal lines and their downstream bindings. The signoff remains
`historical-approval-unbound`; no approval is relabelled current, and source pins,
imported browser/CI evidence, assertions, production-security block and runtime
source remain unchanged.

The corrected five-module route/authorization/Start/Pipeline suite passes
**157tests,1warning in13.37s**. Aggregate regeneration now reports37/37artifact
checks,7/7gates,0findings; V2 review reports7/7local/2200visual/15Compare,0findings.
The complete CI-failure regression suite, fresh full local run and exact-head CI
are recorded separately after they execute. This corrects the incomplete artifact
refresh, not LC08 completion or native/independent qualification.

The full targeted CI-failure regression suite subsequently completed:
**290passed,1upstream warning in317.46s**, including aggregate gate consistency,
all documented commands, source-bound route/authorization/Start/Pipeline checks,
restored review and the36workflow regressions. No test assertion or production
source changed for this correction. Exact-head CI and the restarted full local
run remain pending until their own final results are available.
