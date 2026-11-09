# LC08 SSD-cache clear route audit — bounded follow-up

## Scope and coverage accounting

This audit covers only the existing `api_clear_ssd_cache` online legacy HTTP
branch and its coordinated boundary. It adds no endpoint, activates no runtime,
and does not clear a real cache during tests. The explicit offline/demo response,
coordinated command path and legacy default remain unchanged. It makes no
physical disk/cache, model-state, or hardware qualification claim.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. Ten owners now have bounded evidence;
**19 remain inventory-only**. Council/Debate assembly closures remain separately
named. This is not comprehensive workflow or permission coverage.

## Reproduced failure and behavior

The unchanged-source baseline for the eight cases was **5 failed / 3 passed**:
redirect, HTTP400/500/503 and transport errors were not normalized, and backend
error content could be returned through the route as HTTP200 JSON. Invalid JSON
also escaped without a private route error.

The online route now rejects redirects and all non-2xx results and maps transport
or JSON failures to fixed private 503 detail `legacy_cache_clear_unconfirmed`
with `retryable: false`. No backend response body, redirect target, or exception
message is exposed; cancellation propagates. Valid 2xx JSON is returned unchanged.
The coordinated route check remains before legacy HTTP. Offline demo output stays
explicitly marked `demo: true`; its hard-coded demo count is not cache-clear
measurement. No physical cache deletion is inferred from the response.

## Executable evidence and limits

[`test_workflow_cache_clear_route.py`](../tests/test_workflow_cache_clear_route.py)
uses mock HTTP responses for redirect/error statuses, transport failure, invalid
JSON, valid JSON, task cancellation, and the no-HTTP demo marker. A standalone
failure case runs from the generated single-file app with public sibling modules
blocked. No model, real SSD cache, backend service, or hardware is used.

The focused cache-clear/dispatch/standalone regression passes **103 tests, 1
upstream warning**. The full suite passes **5,177 tests, 32 skipped, 1 upstream
warning in872.02s**. Two isolated in-memory mutants were detected: dropping the
status check (**3 failed / 5 passed**) and removing private error normalization
(**5 failed / 3 passed**). The complete app-derived artifact refresh passes
backend302 tests/16 isolation checks (unqualified/blocked), UI37/37 checks/7
gates/0 findings, and V2 review without advancement. Ruff, mypy75, docs269,
ADR validation, 211-module/13-area scope map, frontend syntax, baseline,
app/standalone auth/origin smokes, screenshots6/6, and demo check pass.

The change does not qualify native lifecycle, disk reclamation, all direct-control
routes, LC09 recovery, hardware acceptance, or independent review. G1 remains
partial; G2–G6/LC09–LC11 remain open; test8 remains paused/unpassed; ADR0038
remains proposed/not-reviewed.
