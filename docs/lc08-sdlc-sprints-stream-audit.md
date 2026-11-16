# LC08 `_run_sdlc_sprints_stream` audit — bounded continuation, sprint delegated sources

## Scope and coverage accounting

This increment audits the two delegated model loops inside
`app._run_sdlc_sprints_stream` — the CrewAI-style worker loop and the
independent QA (critic) loop — with respect to one behaviour: who closes the
**delegated** model source when the outer sprint frame ends early. Both loops
iterated the recursive `stream_any_model(...)` dispatcher through a bare
`async for`, the same structural defect pinned in the
[stream_any_model combo dispatch audit](lc08-stream-any-model-audit.md) (merged
PR #313). Consumer abandonment at the worker loop's `step_content` yield
leaves the worker source pending until event-loop-shutdown finalization; the
QA loop contains no yields, so `GeneratorExit` can never inject into it, and
the critic wrap is included for parity with the combo fix (see limits).

The fix wraps both delegated calls in `contextlib.aclosing`. No story
selection, backlog/PROGRESS confinement semantics, tool approval, mission
envelope, verification gate, QA verdict parsing, unload policy, abort flags,
output framing or error text changes. The route stays dormant for production
calls exactly as before. No provider, native engine, native engine qualification,
or installed harness is exercised; tests route every collaborator and every
model source in memory. This does not audit the SSE generator shells that
consume this helper (`api_run_agile_sdlc_factory.sdlc_gen`,
`api_run_sdlc_feedback_loop.fb_gen`), the SDLC critic's model-side wire
framing, or the remaining SDLC phases (DISCOVERY/PRD/ARCHITECTURE/backlog).

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. `_run_sdlc_sprints_stream` is now marked
`bounded_sprints_stream_close`; **7 owners remain `inventory_only_in_this_audit`
(`api_run_agile_sdlc_factory.sdlc_gen`, `api_run_gsd_fresh_wave.gsd_gen`,
`api_run_harness_turn.event_gen`, `api_run_sdlc_feedback_loop.fb_gen`,
`execute_child_delegation_async`, `execute_conservative_model_switch_async`,
`execute_conservative_model_switch_async._run_probe_stream`)**. No runtime
activation, hardware qualification, release acceptance, test8, or LC09–11
status advances.

## Reproduced failure and bounded behavior

Closing the outer sprint iterator mid-worker — the failure mode an SSE bridge
experiences when a client disconnects — did not close the worker-model source.
The delegated source (suspended in its own post-first-content await) survived
the outer frame untouched; only `asyncio.run()`'s loop-shutdown generator
finalization closed it, after assertions ran and after the response stream
reported closed. Bounded and deterministic proof: the same sequence executed
inside one `asyncio` coroutine leaves the worker source unclosed on the
unchanged tree and closed on the fixed tree.

Two loops were wrapped: the worker loop (closes on early consumer abandonment
at the `step_content` yield) and the QA critic loop (parity with the combo
fix and the worker loop; the QA loop emits no events of its own, so no
consumer-visible abandonment sequence can reach inside it — consumer
cancellation is always delivered at the leaf await inside the delegated QA
source and unwinds it linearly, which is covered by regression tests).

## Executable evidence and limits

[`test_workflow_sdlc_sprints_stream_cancel.py`](../tests/test_workflow_sdlc_sprints_stream_cancel.py)
exercises the real `_run_sdlc_sprints_stream` with an in-memory sprint
fixture (single P0 story with one acceptance criterion and a verification
command), recording stubs for backlog/PROGRESS confinement I/O, harness
session DB, events, tool approval and tool execution, and synthetic
`stream_any_model` dispatchers whose sources observe their own `finally`.
The harness event sink, mission envelope, `_compute_git_diff`, and all model
sources never touch files, databases, subprocesses or the network. It checks:

1. Consumer close mid-worker closes the delegated worker source before the
   consumer's `aclose()` returns (fails on the unchanged source; worker
   source left to interpreter-shutdown finalization).
2. A worker-model failure raised after at least one content event propagates
   and the worker source is finalized before the exception reaches the
   caller (clears on both trees; regression coverage).
3. Consumer cancellation delivered while awaiting the independent QA source
   closes the QA source as the cancellation unwinds linearly through it
   (clears on both trees; regression coverage; see the note on critic wrap).
4. Normal exhaustion completes the story (stubbed verification tool passes,
   QA returns `VERDICT: PASS`) and drains both delegated sources; asserts the
   backlog write records the story as passed.

This is one bounded pass over two delegated loops. It does not prove
production disconnect behaviour against a live ASGI server, native engine or
provider streaming semantics, SDLC file-confinement behaviour (covered by its
own suites), the QA gate's content soundness, backlog schema handling, or any
qualification of `api_run_agile_sdlc_factory.sdlc_gen` /
`api_run_sdlc_feedback_loop.fb_gen`, which still consume this runner through
bare `async for` loops and remain `inventory_only_in_this_audit`.

## Recording

Coverage moves by one owner: the checked-in matrix now marks
`_run_sdlc_sprints_stream` `bounded_sprints_stream_close` with this test
file as evidence; it does not promote any other owner. The
[entry-point inventory](../docs/runtime-dispatch-entry-points.md) remains a
generated static index and is not edited here. No production composition,
mode switch, default registration, activation, hardware instruction,
schema/migration, or any human, owner, independent-security, visual or
release approval is claimed or implied.
