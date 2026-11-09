# Bounded child-delegation stream-close audit

## Scope

This continuation assessed only `execute_child_delegation_async._stream_sub`, the
`stream_any_model` consumer identified in the runtime-dispatch inventory. It did
not assess the sibling `_unload_omlx_model` callbacks, child authority, provider
routing, the full delegation lifecycle, or other workflow owners.

## Finding and bounded fix

The child consumer previously used a bare `async for`. If processing a yielded
item failed before normal iterator exhaustion, the child caught the consumer
error and continued cleanup without explicitly closing the provider iterator.
This was reproduced with an offline async iterator that retained a reference to
itself: without explicit closure, the iterator's `finally` did not run before
child execution returned. `_stream_sub` now owns the source with
`contextlib.aclosing`, so early consumer failure closes it before outer child
cleanup proceeds.

The permanent regression is
`tests/test_child_delegation.py::test_model_backed_child_closes_stream_when_consumer_fails`.
It exercises iterator ownership with a fake source and is not provider, runtime,
native, or hardware qualification. The original implementation was tested as a
counterfactual and failed by reaching the cleanup timeout; the explicit-close
implementation passes. In addition to rebuilding `studio.py`, the affected UI/UX
source manifest, route map, authorization trace, preservation package, and
runtime-dispatch inventory were regenerated. UIV2-29 backend safety evidence was
rerun and its source-bound summary regenerated. The 40-story UI/UX release gate
checks at 37/37 artifacts and 7/7 phase gates. The derived UIV2-30 review was
regenerated to match the backend evidence and now truthfully reports three
existing stale browser-source/metric findings; it remains blocked. No UIV2 story
ledger, owner approval, visual signoff, or acceptance verdict was advanced.

## Verification and limits

The final full suite passed on the updated active branch: **5,213 passed, 53
skipped, 1 upstream warning** in **1,003.14 seconds**. Skips include opt-in
container/browser checks and the separately parked UIV2-30 review tests; no live
container or browser opt-in was enabled. The focused owner/runtime/UI-evidence
group passed **240 tests**. `ruff check .`, configured mypy (76 sources), Python
compileall, frontend syntax, canonical and standalone local smoke tests,
generated-artifact checks, governance/security/docs validation, demo transcript,
and screenshot validation passed. The UI/UX release gate reports 37/37 checks,
7/7 gates, and zero findings. UIV2-29 reports 302 backend tests and 16 isolation
checks, with live isolation still unqualified and blocked. The regenerated UIV2-30
review remains blocked on three existing source/metric-evidence findings; no
acceptance verdict was advanced. No live model/provider/runtime was contacted.
Required current-head PR CI checks remain separate and have not run.

This closes one iterator-ownership gap only. It does not establish provider-side
cancellation, native/runtime behavior, physical resource release, full child
workflow coverage, or LC-08 completion. The remaining inventory-only owners and
all independent/native qualification gates remain open.
