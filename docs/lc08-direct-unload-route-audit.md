# LC08 direct model-unload route audit — bounded follow-up

## Scope and coverage accounting

This follow-up after merged PR269/base `a67e8b2` assesses only the existing
`api_unload_model` online legacy HTTP branch and its existing coordinated
boundary. It adds no endpoint, changes no URL/request/response success shape,
installs or activates a runtime, and makes no physical-release or hardware
qualification claim. The offline/demo branch, legacy default, and distinct
unload-all route are unchanged.

The [static owner map](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. Seven owners now have bounded evidence;
**22 remain inventory-only**. Council/Debate assembly closures remain separately
named. This is a bounded review, not a comprehensive workflow inventory or
permission oracle.

## Reproduced failure and minimal change

The unchanged-source baseline for the seven route tests was **5 failed / 2
passed**. The online route reported generic success for HTTP400, returned a
backend error body as an HTTP200 response for 500/503, and could expose backend
response details or transport exceptions. Redirects and malformed JSON were
also not normalized to a fail-closed outcome.

The route now rejects redirects and every non-2xx response, handles transport
and invalid-JSON failures, and returns a fixed 503 detail:
`legacy_unload_unconfirmed` with `retryable: false`. Backend bodies, redirect
locations, and exception strings are not returned. Cancellation propagates. A
2xx JSON response is passed through exactly as before; it remains only the
backend's response, not physical unload or memory-release proof. Existing
coordinated dispatch is checked before legacy HTTP and unchanged.

The no-runtime demo behavior is intentionally retained and still returns its
explicit `demo: true` marker. That path does not provide native execution or
cleanup evidence. The related `_unload_omlx_model` helper's separate bounded
failure handling is documented in the [cleanup audit](lc08-legacy-cleanup-audit.md).

## Executable evidence and boundaries

[`test_workflow_unload_route.py`](../tests/test_workflow_unload_route.py) uses
`httpx.MockTransport` for redirect/400/500/503 responses, transport failure,
invalid JSON, successful JSON passthrough, and task cancellation. Tests never
connect to a runtime daemon or unload a real model. The standalone regression
runs a failing-response case from the generated single-file app with public
siblings blocked.

The full local suite passes **5,153 tests, 32 skipped, 1 upstream warning in
1,293.48s**. Focused route/dispatch/native-unload/Hall/MCP/standalone regression
passes **228 tests, 1 upstream warning in12.56s**. Two isolated in-memory mutants
were detected: dropping the status check (**3 failed / 4 passed**) and removing
sanitized error mapping (**5 failed / 2 passed**). Full app-derived artifact
refresh passes backend302 tests/16 isolation checks (unqualified/blocked), UI
37/37 checks/7 gates/0 findings and V2 review with no gate advancement. Ruff,
mypy75, docs266, ADRs, 208-module/13-area scope map, source baseline, frontend
syntax, app/standalone auth/origin smokes, screenshots6/6 and demo check pass.

Coverage and this route behavior change do not advance acceptance. Legacy demo
behavior, default backend selection, physical unload, G1 partial status, open
G2–G6/LC09–LC11, paused/unpassed test8, and proposed/not-reviewed ADR0038 remain
unchanged.
