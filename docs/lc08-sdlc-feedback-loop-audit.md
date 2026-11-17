# LC08 `api_run_sdlc_feedback_loop.fb_gen` audit — bounded continuation, feedback router delegated sources

## Scope and coverage accounting

This increment audits the delegated sources consumed inside
`app.api_run_sdlc_feedback_loop.fb_gen` — the SDLC feedback router SSE shell
(`User Feedback -> Research Team + Online Inspiration -> Delta Backlog ->
Fresh Sprints`) — with respect to one behaviour: who closes the
**delegated** source when the outer feedback frame ends early. Three
delegated consumers iterated through bare `async for`, the same structural
defect pinned in the
[stream_any_model combo dispatch audit](lc08-stream-any-model-audit.md) (merged
PR #313), the
[sprint-runner audit](lc08-sdlc-sprints-stream-audit.md) (merged PR #314) and
the
[factory audit](lc08-agile-sdlc-factory-audit.md) (merged PR #316):

1. Feedback-research (`stream_any_model(...)`, critic model),
2. Delta sprint backlog (`stream_any_model(...)`, lead model),
3. Auto-implement fresh-context sprint execution
   (`_run_sdlc_sprints_stream(...)`, gated on `auto_implement`).

Consumer abandonment at the sprint-delegation `yield ev_chunk` leaves the
delegated runner pending until event-loop-shutdown finalization; that loop is
a consumer-visible close point and is a deterministic red/green proof. The
two model loops contain no yields inside their `async for` bodies (they
append content to buffers), so no consumer-visible abandonment can be
injected into them; their wraps are parity with the combo fix and are
covered by regression/exhaustion tests (see limits).

The fix wraps all three delegated calls in `contextlib.aclosing`. No feedback
parsing, inspiration fetch, backlog confinement semantics, story id
allocation (`STORY-FB-xx`), verification/QA gates, mission envelope, abort
flags, unload policy, output framing or error text changes. The route stays
dormant for production calls exactly as before. No provider, native engine,
native engine qualification, or installed harness is exercised; tests route
every collaborator and every model source in memory. This does not audit the
GSD fresh-wave shell (`api_run_gsd_fresh_wave.gsd_gen`), the harness-turn
shell, or the model-side wire framing beneath `stream_any_model`.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. `api_run_sdlc_feedback_loop.fb_gen`
is now marked `bounded_feedback_stream_close`; **5 owners remain
`inventory_only_in_this_audit` (`api_run_gsd_fresh_wave.gsd_gen`,
`api_run_harness_turn.event_gen`, `execute_child_delegation_async`,
`execute_conservative_model_switch_async`,
`execute_conservative_model_switch_async._run_probe_stream`)**. No runtime
activation, hardware qualification, release acceptance, test8, or LC09–11
status advances.

## Reproduced failure and bounded behavior

Closing the outer feedback iterator mid-sprint-delegation — the failure mode
an SSE bridge experiences when a client disconnects at a sprint-chunk frame —
did not close the delegated sprint runner. The runner (suspended inside its
own post-yield await) survived the outer frame untouched; only
`asyncio.run()`'s loop-shutdown generator finalization closed it, after
assertions ran and after the response stream reported closed. Bounded and
deterministic proof: the same sequence executed inside one `asyncio`
coroutine leaves the runner unclosed on the unchanged tree and closed on the
fixed tree (red on the unchanged source; close now observed inside the
consumer's `aclose()`).

Both model loops (research/delta) were wrapped for parity. Like the PRD and
backlog slots in the factory audit, those loops emit no events of their own,
so no consumer-visible abandonment sequence can reach inside them; consumer
cancellation is always delivered at the leaf await inside the delegated model
source and unwinds it linearly, which is covered by regression tests.

## Executable evidence and limits

[`test_workflow_sdlc_feedback_loop_cancel.py`](../tests/test_workflow_sdlc_feedback_loop_cancel.py)
exercises the real `api_run_sdlc_feedback_loop` with an in-memory fixture
(real temporary worktree directory, seeded `backlog.json`, stubbed session
DB / task state machine / harness events / confinement writers, and
synthetic `stream_any_model` dispatchers whose sources observe their own
`finally`). The git-diff probe, quarantine lookup, skills context,
online-inspiration fetch, and all model sources never touch a database,
subprocess or the network. It checks:

1. Consumer close at the sprint-chunk frame closes the delegated sprint
   runner before the consumer's `aclose()` returns (fails on the unchanged
   source; runner left to interpreter-shutdown finalization).
2. A research-phase model failure raised after at least one content event
   propagates to the outer `except` and yields the terminal `error` SSE
   frame; the research source is finalized before the exception reaches the
   consumer (clears on both trees; regression coverage).
3. Consumer cancellation delivered while awaiting the sprint runner unwinds
   and closes it. The feedback router yields several phase frames before
   the runner is entered, so the test drains frames through
   `sdlc_backlog_ready` first (regression coverage).
4. Normal exhaustion streams both sprint chunks, drains the runner, emits
   `turn_done` and completes the task (regression coverage).
5. A fully-consumed feedback run drains every delegated source (research and
   delta models and the sprint runner), emits `sdlc_backlog_ready` (with the
   synthesized `STORY-FB-01` delta story) and `turn_done`, and completes the
   task (regression coverage; pins the phase wraps against future
   reordering).

This is one bounded pass over three delegated consumers in one SSE shell. It
does not prove production disconnect behaviour against a live ASGI server,
native engine or provider streaming semantics, SDLC file-confinement
behaviour (covered by its own suites), the QA gate's content soundness,
backlog schema handling, or any qualification of `api_run_gsd_fresh_wave.gsd_gen` /
`api_run_harness_turn.event_gen`, which remain `inventory_only_in_this_audit`.

## Recording

Coverage moves by one owner: the checked-in matrix now marks
`api_run_sdlc_feedback_loop.fb_gen` `bounded_feedback_stream_close` with
this test file as evidence; it does not promote any other owner. The
[entry-point inventory](../docs/runtime-dispatch-entry-points.md) remains a
generated static index and is not edited here. No production composition,
mode switch, default registration, activation, hardware instruction,
schema/migration, or any human, owner, independent-security, visual or
release approval is claimed or implied.
