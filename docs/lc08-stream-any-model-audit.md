# LC08 `stream_any_model` audit — bounded continuation, combo dispatch close

## Scope and coverage accounting

This increment audits the combo-fallback arm of `stream_any_model` — the
OmniRoute-style dispatcher in `app.py` — with respect to one behaviour:
who closes the **delegated** model source when the outer iterator is closed
early or fails part-way. Every other dispatch mode the function owns was
already closing its delegated source: the coordinated-dispatch path wraps
`dispatch.stream(...)` in `contextlib.aclosing`, the cloud-provider arm wraps
`adapter.stream(...)` in `contextlib.aclosing`, and the local oMLX arm wraps
`_stream_omlx_completion(...)` inside the heavy-model slot, likewise in
`aclosing`. The combo arm recursed into `stream_any_model(...)` under a bare
`async for` and abandoned that iterator without closing it.

The fix is one `contextlib.aclosing` wrapper around the delegated call. No
output framing, combo selection/fallback semantics, quota/429/offline
failover, consent or billing policy, model selection, or error text changes;
the route stays dormant for production calls exactly as before. No native
engine is exercised and no installed qualification is claimed; tests inject
synthetic async generator sources for every provider, cloud and local path.
This does not test provider-wire framing, all combos, or every workflow that
consumes this dispatcher.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. `stream_any_model` is now marked
`bounded_combo_stream_close`; **8 owners remain `inventory_only_in_this_audit`
(`_run_sdlc_sprints_stream`, `api_run_agile_sdlc_factory.sdlc_gen`,
`api_run_gsd_fresh_wave.gsd_gen`, `api_run_harness_turn.event_gen`,
`api_run_sdlc_feedback_loop.fb_gen`, `execute_child_delegation_async`,
`execute_conservative_model_switch_async`,
`execute_conservative_model_switch_async._run_probe_stream`)**. No runtime
activation, hardware qualification, release acceptance, test8, or LC09–11
status advances.

## Reproduced failure and bounded behavior

Closing the outer `stream_any_model` iterator mid-stream — the failure mode
an endpoint experiences when a client disconnects — did not close the
delegated model-source generator owned by the combo fallback loop. The
delegated source (and the local oMLX path's heavy-model lease beneath it)
was left pending until finalization at interpreter shutdown, where the
abandoned generator unwound in a context that could not reset the
`m5_heavy_model_lease` contextvar token, surfacing as an unhandled-exception
notification during event-loop shutdown. The same defect applied one step
later in the same path: an error propagated from the delegated source after
at least one content event had been emitted would abandon the delegated
source rather than closing it before the `RuntimeError` reached the caller.

The delegated iterator is now wrapped in `contextlib.aclosing`, matching the
close discipline already used by the coordinated, cloud-provider and local
arms. The change is confined to cancellation cleanup: when the outer frame
is closed, or when a fixture raises part-way, the delegated source's
`finally` runs synchronously before the outer frame resumes. When the
fail-forward decision (`emitted_any` and an exception) re-raises, the source
is closed first. Failure-before-content (`emitted_any` false) already ran
its own source to completion of the except block; the wrapping now closes
explicitly in that branch too.

## Executable evidence and limits

[`test_workflow_stream_any_model_cancel.py`](../tests/test_workflow_stream_any_model_cancel.py)
exercises the real `stream_any_model` against an in-memory combo fixture
(`combo/fx`, two local targets) and a stubbed `_stream_omlx_completion`
source that observes cancellation via `finally`. It checks:

1. Mid-stream `aclosing` from the caller closes the delegated local source
   (fails on the unchanged source; the delegated source is finalized only at
   interpreter shutdown).
2. A fixture error raised after at least one content event closes the
   delegated source synchronously as the exception propagates.
3. A fixture target that fails before emitting anything is closed before the
   successor target is entered, and the successor's content is surfaced.
4. Normal exhaustion of the combo closes the delegated source (passing
   control, incl. the no-fallback-marker single-target shape).

These are in-process tests with synthetic generators; they make no outbound
request and no provider, native-engine, heavy-model admission or acceptance
claim. The tests do not cover disconnects against live ASGI servers, quotas
or billing decisions (fallback policy is unchanged), multi-target cloud
combinations, `ollama/` or co-resident slot behavior, or the remaining
inventory-only owners listed above. Those remain inventory-only and are not
qualified by this audit.

## Recording

Coverage moves by one owner: the checked-in matrix now marks
`stream_any_model` `bounded_combo_stream_close` with this test file as
evidence; it does not promote any other owner. The
[entry-point inventory](../docs/runtime-dispatch-entry-points.md) remains a
generated static index and is not edited here. No production composition,
mode switch, default registration, activation, hardware instruction,
schema/migration, or any human, owner, independent-security, media or
release approval is claimed or implied.
