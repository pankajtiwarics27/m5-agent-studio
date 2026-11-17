# LC08 `api_run_gsd_fresh_wave.gsd_gen` audit — bounded continuation, GSD fresh-wave delegated sources

## Scope and coverage accounting

This increment audits the delegated sources consumed inside
`app.api_run_gsd_fresh_wave.gsd_gen` — the GSD fresh-wave SSE shell — with
respect to one behaviour: who closes the **delegated** source when the outer
GSD frame ends early. Three delegated consumers iterated through bare
`async for`, the same structural defect pinned in the
[stream_any_model combo dispatch audit](lc08-stream-any-model-audit.md),
the [sprint-runner audit](lc08-sdlc-sprints-stream-audit.md), and the
[Agile SDLC factory audit](lc08-agile-sdlc-factory-audit.md):

1. Wave 1 (`stream_any_model(...)`, lead model) — the SPEC & PLAN pass,
2. Wave 2 (`stream_any_model(...)`, lead model) — the fresh-context
   execution loop, called once per executor step,
3. Wave 3 (`stream_any_model(...)`, critic model) — the fresh-context verdict
   pass.

Each loop emits its own SSE frames from inside the body
(`step_content` / `step_reasoning`), so consumer abandonment is visible at
three distinct points. The fix wraps all three delegated calls in
`contextlib.aclosing`. No GSD prompt text, wave order, artifact confinement
semantics, mission envelope, abort/checkpoint/transition accounting, unload
policy, output framing or error text changes. The GSD route remains dormant
for production calls exactly as before. No provider, native engine, native
engine qualification, or installed harness is exercised; tests route every
collaborator and every model source in memory. This does not audit the
SDLC feedback router (`api_run_sdlc_feedback_loop.fb_gen`), the Harness turn
shell, or the model-side wire framing beneath `stream_any_model`.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. `api_run_gsd_fresh_wave.gsd_gen`
is now marked `bounded_gsd_fresh_wave_stream_close`; **5 owners remain
`inventory_only_in_this_audit` (`api_run_harness_turn.event_gen`,
`api_run_sdlc_feedback_loop.fb_gen`, `execute_child_delegation_async`,
`execute_conservative_model_switch_async`,
`execute_conservative_model_switch_async._run_probe_stream`)**. No runtime
activation, hardware qualification, release acceptance, or LC09–11 status
advances.

## Reproduced failure and bounded behavior

Closing the outer GSD iterator mid-wave — the failure mode an SSE bridge
experiences when a client disconnects at a wave-1, wave-2 or wave-3
`step_content` frame — did not close the delegated model source. The source
(suspended inside its own post-yield await) survived the outer frame
untouched; only interpreter-shutdown finalization closed it, after assertions
ran and after the response stream reported closed. Bounded and deterministic
proof: the same sequence executed inside one `asyncio` coroutine leaves the
model source unclosed on the unchanged tree and closed on the fixed tree
(red on the unchanged source; close now observed inside the consumer's
`aclose()`) for each of the three waves.

## Executable evidence and limits

[`test_workflow_gsd_fresh_wave_cancel.py`](../tests/test_workflow_gsd_fresh_wave_cancel.py)
exercises the real `api_run_gsd_fresh_wave` with an in-memory fixture
(real temporary worktree directory, stubbed session DB / task state machine /
harness events / confinement writers, and synthetic `stream_any_model`
dispatchers whose sources observe their own `finally`). The quarantine
lookup, skills context, codegraph tool, git-diff probe, MCP prompt,
opencontext prompt and all model sources never touch a database, subprocess
or the network. It checks:

1. Consumer close at the Wave 1 `step_content` frame closes the delegated
   lead-model source before the consumer's `aclose()` returns (fails on the
   unchanged source; source left to interpreter-shutdown finalization).
2. Consumer close at the Wave 2 `step_content` frame closes the delegated
   per-step lead-model source before the consumer's `aclose()` returns
   (fails on the unchanged source; the test uses a dedicated close sentinel
   so the earlier Wave 1 drain cannot mask it).
3. Consumer close at the Wave 3 `step_content` frame closes the delegated
   critic-model source before the consumer's `aclose()` returns (fails on
   the unchanged source).
4. A wave-model failure raised after at least one content event propagates
   to the outer `except` and yields the terminal `error` SSE frame; the
   source is finalized before the exception reaches the consumer (regression
   coverage).
5. Consumer cancellation delivered while awaiting the Wave 1 model source
   unwinds and closes it (regression coverage).
6. A fully-consumed three-wave run drains the Wave 1/2 lead sources and the
   Wave 3 critic source, emits `turn_done`, transitions the milestones and
   completes the task (regression coverage; pins the wraps against future
   reordering).

This is one bounded pass over three delegated consumers in one SSE shell. It
does not prove production disconnect behaviour against a live ASGI server,
native engine or provider streaming semantics, GSD artifact confinement or
the quality of the SPEC/STATE/VERDICT content, `_unload_omlx_model`'s
physical-release semantics, or any qualification of the remaining
inventory-only owners (including `api_run_sdlc_feedback_loop.fb_gen`), which
stay `inventory_only_in_this_audit`.

## Recording

Coverage moves by one owner: the checked-in matrix now marks
`api_run_gsd_fresh_wave.gsd_gen` `bounded_gsd_fresh_wave_stream_close` with
this test file as evidence; it does not promote any other owner. The
[entry-point inventory](../docs/runtime-dispatch-entry-points.md) remains a
generated static index and is regenerated by
`scripts/runtime_dispatch_inventory.py`; it reports shifted `app.py` line
numbers only, not coverage claims. No production composition, mode switch,
default registration, activation, hardware instruction, schema/migration, or
any human, owner, independent-security, visual or release approval is
claimed or implied.
