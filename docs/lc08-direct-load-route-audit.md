# LC08 direct model-load route audit — bounded follow-up

## Scope and coverage accounting

This follow-up after merged PR270/base `06d0eee` assesses only the existing
`api_load_model` online legacy HTTP branch and its existing coordinated
dispatch boundary. It adds no endpoint or request/response success shape, installs
or activates a runtime, changes runtime defaults, or makes a hardware/acceptance
claim. The explicit offline/demo branch, legacy default, and the separate unload
route are unchanged.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
retains **29 owners / 54 static sites**. Eight owners now have bounded evidence;
**21 remain inventory-only**. Council/Debate assembly closures remain separately
named. This is a bounded review, not a comprehensive workflow or permission
oracle.

## Reproduced failure and minimal change

The unchanged-source baseline for the seven route cases was **5 failed / 2
passed**. For an online legacy backend, redirect responses could flow through as
normal JSON, error responses returned status-specific `HTTPException`s containing
raw backend bodies, and transport errors or invalid JSON escaped without the
route's fixed private error contract.

The online branch now rejects redirects and non-2xx responses, converts transport
and JSON decode failures to a fixed 503 with `legacy_load_unconfirmed` and
`retryable: false`, and does not reveal backend response bodies, redirect targets,
or exception details. Cancellation propagates unchanged. A successful 2xx JSON
body is passed through as before. The offline branch still returns its explicit
`demo: true` marker, and coordinated dispatch still runs before any legacy HTTP
probe. This change does not claim a returned JSON body is proof of physical load,
resident memory, or qualification.

## Executable evidence and remaining limits

[`test_workflow_load_route.py`](../tests/test_workflow_load_route.py) uses mock
HTTP transport for redirect/400/500/503 statuses, transport failure, malformed
JSON, successful JSON passthrough, and task cancellation. No daemon, model
installation, or hardware is involved. The generated standalone artifact includes
a focused failing-response check with public sibling imports blocked.

The full local suite passes **5,160 tests, 32 skipped, 1 upstream warning in
966.94s**. The focused load-route/dispatch/standalone regression passes102tests,
1 upstream warning. In isolated in-memory subprocesses, removing the response
status check was caught (**3 failed / 4 passed**) and removing sanitized error
mapping was caught (**5 failed / 2 passed**). The full app-derived artifact
refresh passes backend302 tests/16 isolation checks (unqualified/blocked), UI
37/37 checks/7 gates/0 findings, and V2 review without a gate advancement. Ruff,
mypy75, docs267, ADRs, 209-module/13-area scope map, source baseline, frontend
syntax, app/standalone auth/origin smokes, screenshots6/6, and demo check pass.

The audit does not assess offline/demo semantics, model usability, physical load,
direct unload, load-all, LC09 recovery, or hardware acceptance. G1 remains
partial; G2–G6/LC09–LC11 remain open; test8 remains paused and unpassed; ADR0038
remains proposed and not reviewed.
