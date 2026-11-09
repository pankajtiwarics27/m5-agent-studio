# LC08 unload-all route audit — bounded follow-up

## Scope and coverage accounting

This audit assesses only the existing `api_unload_all` online legacy HTTP path and
its coordinated boundary. It adds no endpoint or request/success response shape,
activates no runtime, and makes no physical-release or hardware-qualification
claim. Explicit offline/demo behavior and the existing coordinated command path
remain unchanged.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. Nine owners have bounded evidence;
**20 remain inventory-only**. Council/Debate assembly closures remain separately
named. This is not comprehensive workflow integration or an authority oracle.

## Reproduced failure and minimal change

The unchanged-source baseline for nine new cases had **7 failed / 2 passed**. The
route returned the listed model IDs as unloaded even when individual unload
requests received 302, 400, 500, or 503. It also ignored an unsuccessful or
redirected inventory response, and raw transport/malformed inventory failures
escaped the route's error boundary.

The online path now validates the inventory response, requires its `models` field
to be a list of object rows with IDs for loaded non-virtual models, rejects
redirects and non-2xx responses both for inventory and each unload request, and
converts transport/parse/shape errors to a fixed private 503
`legacy_unload_all_unconfirmed` response. Cancellation propagates. The success
response retains the existing `{status, unloaded}` shape and includes an ID only
after its individual unload endpoint returned 2xx. Coordinated dispatch remains
before legacy HTTP and unchanged; offline demo keeps its explicit `demo` result.

If a later model fails after earlier 2xx responses, the route returns the same
unconfirmed 503 and does not roll back prior effects. The caller should re-read
model inventory; the list itself is not proof of physical unload or memory
release. Tests use mock transport only, with no installed runtime, daemon, or
hardware operation.

## Executable evidence and limits

[`test_workflow_unload_all_route.py`](../tests/test_workflow_unload_all_route.py)
checks individual and inventory redirect/error statuses, transport and malformed
inventory, successful loaded/nonvirtual filtering, and task cancellation. The
standalone suite invokes a representative failure case with public sibling
imports blocked. The full local suite passes **5,169 tests, 32 skipped, 1 upstream warning in
1,309.18s**. Focused unload-all/dispatch/standalone regression passes104tests,
1 upstream warning. Two isolated in-memory mutants were detected: removing
response-status checks (**4 failed / 5 passed**) and removing error normalization
(**7 failed / 2 passed**). The full app-derived artifact refresh passes backend302
tests/16 isolation checks (unqualified/blocked), UI37/37 checks/7 gates/0 findings,
and V2 review without gate advancement. Ruff, mypy75, docs268, ADRs, 210-module/
13-area scope map, source baseline, frontend syntax, app/standalone auth/origin
smokes, screenshots6/6, and demo check pass.

Direct load/unload owners are separately assessed; unload-all does not qualify
model load, native runtime lifecycle, complete call-site integration, LC09
recovery, hardware acceptance or independent review. G1 remains partial; G2–G6
and LC09–LC11 remain open; test8 remains paused/unpassed; ADR0038 remains
proposed/not-reviewed.
