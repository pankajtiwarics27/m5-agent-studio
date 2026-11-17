# LC08 `api_run_harness_turn.event_gen` audit — bounded continuation, Harness turn delegated sources

## Scope and coverage accounting

This increment audits the delegated sources consumed inside
`app.api_run_harness_turn.event_gen` — the multi-step Harness `Turn -> Step`
SSE shell — with respect to one behaviour: who closes the **delegated** source
when the outer Harness frame ends early. The shell consumed the lead model
through a bare `async for` loop, the same structural defect pinned in the
[stream_any_model combo dispatch audit](lc08-stream-any-model-audit.md), the
[sprint-runner audit](lc08-sdlc-sprints-stream-audit.md), the
[Agile SDLC factory audit](lc08-agile-sdlc-factory-audit.md) and the
[GSD fresh-wave audit](lc08-gsd-fresh-wave-audit.md):

1. `stream_any_model(...)` with the live `lead_model`, once per `Turn -> Step`
   iteration, `step_max_tok` 2048 for a local oMLX model and 4096 otherwise.

The loop emits its own SSE frames from inside the body (`step_reasoning`,
`step_content`, `fallback_switch`, `fallback_notice`), so consumer abandonment
is visible at several distinct points, and the loop has a second early-exit
path of its own: the `_HARNESS_ABORT_FLAGS` membership check `break`s out of
the loop body while the delegated source is still suspended. That `break` is
in scope for this audit because it is the shell deciding to stop consuming, and
it left the same source open. The fix wraps the delegated call in
`contextlib.aclosing`. No Harness prompt text, step order, plan/resume
semantics, budget accounting, tool authorization/approval, checkpoint,
transition, unload policy, output framing or error text changes. Nothing about
which models may be reached, or under which consent, changes.

This audit deliberately does **not** cover
`api_run_sdlc_feedback_loop.fb_gen`: that owner is claimed by the open PR #317
on another fixed Arena branch, and this session must not duplicate a claimed
owner. It does not cover `execute_child_delegation_async`,
`execute_conservative_model_switch_async` or
`execute_conservative_model_switch_async._run_probe_stream` either; they stay
`inventory_only_in_this_audit`.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. `api_run_harness_turn.event_gen` is
now marked `bounded_harness_turn_stream_close`; **4 owners remain
`inventory_only_in_this_audit`**. No runtime activation, hardware
qualification, release acceptance, or LC09–11 status advances. ADR-0038 stays
proposed and unreviewed; `runtime_activation_authorized` stays `false`.

## Reproduced failure and bounded behavior

Closing the outer Harness iterator at a `step_content` frame — the failure mode
an SSE bridge experiences when a client disconnects mid-step — did not close
the delegated model source. The source, suspended inside its own post-yield
await, survived the outer frame untouched; only interpreter-shutdown
finalization closed it, after assertions ran and after the response stream
reported closed. The same is true of the shell's own `_HARNESS_ABORT_FLAGS`
`break`: control left the `async for` body without closing the iterator it was
reading from, so the abandoned source stayed open for the rest of the turn.

Both are proven the same way as the earlier audits: the sequence runs inside
one `asyncio` coroutine, the delegated source observes its own `finally`, and
the assertion asks whether it closed **before the consumer finished** rather
than at interpreter exit. Red on the unchanged tree, green on the fixed tree.

What the wrap does *not* fix: it makes the shell close what it stops reading.
It does not make the abandoned step's partial output a completed step, and it
does not roll back a tool effect that already committed. Those are separate
semantics and are unchanged.

## Executable evidence and limits

[`test_workflow_harness_turn_cancel.py`](../tests/test_workflow_harness_turn_cancel.py)
drives the real `api_run_harness_turn` with an in-memory fixture. Every
collaborator the shell reaches before or during a step is substituted in the
test: the session DB, message store, harness event log, worker-lease
coordinator and task state machine, the portable-handoff and scoped-grant
stores, the evidence-lesson store, workspace snapshots, the execution budget
recorder, the tool executor and workspace-quarantine lookup. Every model source
is a synthetic async iterator that records its own closure. The tests touch no
database, subprocess, network or real worktree content.

Cases:

1. **Red/green — consumer close at `step_content`.** The client disconnects
   after the first content frame; the delegated lead source must be closed
   before the consumer's `aclose()` returns. Fails on the unchanged source
   (the source is left to interpreter shutdown) and passes on this branch.
2. **Red/green — the shell's own abort `break`.** `_HARNESS_ABORT_FLAGS` is
   populated for the session while a step is streaming, which is the in-process
   cancel path a Stop button takes; the shell must close the delegated source
   it abandons on that `break`, not leave it to shutdown finalization.
3. **Failure propagation.** A model error raised after at least one content
   frame surfaces as the terminal `error` SSE frame, and the source is
   finalized on the way out.
4. **Cancellation while awaiting the source.** A `Task.cancel()` delivered
   while the shell is blocked inside the delegated iterator unwinds and closes
   it; the turn lease is released in the same unwind.
5. **Fully-consumed run.** A one-step run with no tool calls drains the source,
   emits `turn_done`, and reaches the expected terminal state — this pins the
   wrap against a future reordering of the loop.
6. **Tool-call step still drains before effect execution.** A step that does
   request a tool must finish reading the source before any tool is executed,
   so the wrap cannot move an effect ahead of the model's final frame.

This is one bounded pass over one delegated consumer in one SSE shell. It does
not prove production disconnect behaviour against a live ASGI server, native
engine or provider streaming semantics; it does not test the quality of the
step output, `_unload_other_omlx_models`' physical-release semantics, or any
qualification of the remaining inventory-only owners. Physical model release,
installed-runtime behaviour and provider wire framing stay unqualified.

## Recording

Coverage moves by one owner: the checked-in matrix marks
`api_run_harness_turn.event_gen` `bounded_harness_turn_stream_close` with this
test file as evidence; it does not promote any other owner and does not touch
the owner claimed by PR #317. The
[entry-point inventory](../docs/runtime-dispatch-entry-points.md) remains a
generated static index and is regenerated by
`scripts/runtime_dispatch_inventory.py`; it reports shifted `app.py` line
numbers only, not coverage claims. No production composition, mode switch,
default registration, activation, hardware instruction, schema/migration, or
any human, owner, independent-security, visual or release approval is claimed
or implied.
