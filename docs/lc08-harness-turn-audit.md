# LC08 `api_run_harness_turn.event_gen` audit — bounded continuation, Harness turn model source

## Scope and coverage accounting

This increment audits the single delegated model source consumed inside
`app.api_run_harness_turn.event_gen` — the Harness turn SSE shell — with
respect to one behaviour: who closes the **delegated** per-step source when
the outer Harness frame ends early. One delegated consumer iterated through
a bare `async for`, the same structural defect pinned in the
[stream_any_model combo dispatch audit](lc08-stream-any-model-audit.md),
the [sprint-runner audit](lc08-sdlc-sprints-stream-audit.md), the
[Agile SDLC factory audit](lc08-agile-sdlc-factory-audit.md) and the
[GSD fresh-wave audit](lc08-gsd-fresh-wave-audit.md):

1. Per-step lead-model `stream_any_model(...)` call — the turn's active
   inference stream, consumed inside the `step_range` loop.

The loop body emits its own SSE frames (`step_reasoning` / `step_content` /
`fallback_switch` / `fallback_notice`) and can `break` early on the live
abort flag (`_HARNESS_ABORT_FLAGS`), so consumer abandonment is visible at
those points. The fix wraps the delegated call in `contextlib.aclosing`.
No prompt text, tool-call parsing, approval/grant semantics, checkpoint or
lease accounting, budget enforcement, mission envelope, unload policy,
output framing or error text changes. The Harness route remains dormant
for production calls exactly as before. No provider, native engine, native
engine qualification, or installed harness is exercised; tests route every
collaborator and the model source in memory. This does not audit the
Harness tool executor, the approval flow, `compact_harness_session_messages`,
the Harness SDLC factory/feedback routers, or the model-side wire framing
beneath `stream_any_model`.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. `api_run_harness_turn.event_gen`
is now marked `bounded_harness_turn_stream_close`; **4 owners remain
`inventory_only_in_this_audit` (`api_run_sdlc_feedback_loop.fb_gen`,
`execute_child_delegation_async`,
`execute_conservative_model_switch_async`,
`execute_conservative_model_switch_async._run_probe_stream`)**.
No runtime activation, hardware qualification, release acceptance, or
LC09–11 status advances.

## Reproduced failure and bounded behavior

Closing the outer Harness iterator mid-step — the failure mode an SSE bridge
experiences when a client disconnects at a `step_reasoning` or `step_content`
frame, or when the abort flag breaks the consume loop — did not close the
delegated model source. The source (suspended inside its own post-yield await)
survived the outer frame untouched; only interpreter-shutdown finalization
closed it, after assertions ran and after the response stream reported closed.
Bounded and deterministic proof: the same sequence executed inside one `asyncio`
coroutine leaves the model source unclosed on the unchanged tree and closed on
the fixed tree (red on the unchanged source; close now observed inside the
consumer's `aclose()`), including the abort-flag `break` path.

## Executable evidence and limits

[`test_workflow_harness_turn_cancel.py`](../tests/test_workflow_harness_turn_cancel.py)
exercises the real `api_run_harness_turn` with an in-memory fixture
(real temporary worktree directory, stubbed session DB / task state machine /
harness events / snapshot store / lesson store, and synthetic `stream_any_model`
dispatchers whose sources observe their own `finally`). The quarantine lookup,
skills context, opencontext prompt, Git-diff probe and all model sources never
touch a database, subprocess or the network. It checks:

1. Consumer close at a `step_content` frame closes the delegated lead-model
   source before the consumer's `aclose()` returns (fails on the unchanged
   source; source left to interpreter-shutdown finalization).
2. Consumer close at a `step_reasoning` frame closes the delegated lead-model
   source before the consumer's `aclose()` returns (fails on the unchanged
   source).
3. The abort-flag `break` inside the consume loop finalizes the delegated
   source (fails on the unchanged source; the abort flag is raised from inside
   the source, as a concurrent `/api/harness/abort` would deliver it).
4. A model failure raised after at least one content event propagates to the
   outer `except` and yields the terminal `error` SSE frame; the source is
   finalized before the exception reaches the consumer (regression coverage).
5. Consumer cancellation delivered while awaiting the model source unwinds and
   closes it (regression coverage).
6. A fully-consumed turn drains the source, emits `turn_done` and completes
   the task through the stubbed verification path (regression coverage; pins
   the wrap against future reordering).

This is one bounded pass over one delegated consumer in one SSE shell. It
does not prove production disconnect behaviour against a live ASGI server,
native engine or provider streaming semantics, Harness tool-call parsing or
execution fidelity, approval/grant flow behaviour, snapshot/quarantine
semantics, context compaction, budget honesty, or any qualification of the
remaining inventory-only owners (including the child-delegation and
conservative-model-switch owners), which stay `inventory_only_in_this_audit`.

## Recording

Coverage moves by one owner: the checked-in matrix now marks
`api_run_harness_turn.event_gen` `bounded_harness_turn_stream_close` with
this test file as evidence; it does not promote any other owner. The
[entry-point inventory](../docs/runtime-dispatch-entry-points.md) remains a
generated static index and is regenerated by
`scripts/runtime_dispatch_inventory.py`; it reports shifted `app.py` line
numbers only, not coverage claims. No production composition, mode switch,
default registration, activation, hardware instruction, schema/migration, or
any human, owner, independent-security, visual or release approval is
claimed or implied.
