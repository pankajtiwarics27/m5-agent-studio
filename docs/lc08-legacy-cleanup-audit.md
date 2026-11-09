# LC08 legacy model-cleanup audit — bounded follow-up

## Scope and coverage accounting

This follow-up to merged PR267/base `557d797` assesses only the helper
`_unload_omlx_model`'s online, non-coordinated HTTP cleanup branch and the
coordinated-dispatch refusal seam. It does not change the legacy default, activate
a native runtime, install a backend, add routes/schema, alter workspace or
transcript policy, or advance acceptance. The helper is shared by several call
sites; this audit does not claim every caller or workflow was exhaustively
qualified.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
now retains **29 owners / 54 static sites**. Six owners have bounded evidence
(two workflow adapters, Hall HTTP, MCP summon, workspace suggestion, and this
legacy cleanup failure seam); **23 remain inventory-only**. Two Council/Debate
assembly closures remain separately named. Static owner counts are not a
permission or runtime qualification oracle.

## Reproduced failure and behavior change

The unchanged-source baseline had **5 failed / 2 passed** in seven new cases.
When preflight considered oMLX online, the helper ignored all unload response
statuses and swallowed transport exceptions; it still slept and returned normally
for 302, 400, 500, 503, and a private ConnectError. Calling workflows could
therefore proceed as if cleanup had returned normally without a confirmed
successful HTTP response.

The minimal change requires a 2xx response and the existing one-second settle
delay before normal return. Redirects and all non-2xx statuses are rejected; a
failed/raising transport is converted to a generic 503
`legacy_cleanup_unconfirmed` response with `retryable: false`. No backend response
body, URL redirect target, key, or exception detail is exposed. Cancellation
propagates unchanged. Coordinated operation remains on the strict dispatch
boundary; no HTTP fallback was added.

This 2xx is only a successful legacy HTTP response plus the existing delay. It is
**not** a physical unload measurement, memory-zero observation, or proof that
native work stopped. The existing offline/demo branch remains unchanged and is
not counted as physical cleanup evidence. Direct `/api/models/{id}/unload` and
unload-all routes are separate owners and remain inventory-only. No claim of
hardware qualification or memory reclamation is made.

## Executable evidence

[`test_workflow_unload_legacy.py`](../tests/test_workflow_unload_legacy.py) covers
HTTP redirect and 400/500/503 refusal, private transport failure, no delay after
failure, 204 response path, cancellation propagation, and coordinated fail-closed
behavior before any legacy HTTP attempt. The tests use `httpx.MockTransport`; no
machine, installed runtime, backend daemon, or physical unload is exercised.

The focused cleanup/dispatch/native-unload/Hall/MCP/standalone regression passes
**229 tests, 1 upstream warning in11.61s**. Existing coordinated failure paths
remain separately covered and retain their already established different
transcript ordering. In isolated in-memory subprocesses, removing the status
check was caught (**3 failed / 2 passed**) and restoring swallowed transport
errors was caught (**5 failed**); tracked runtime files were not mutated.
Legacy offline/demo semantics and the two direct unload route owners are
deliberately not modified.

## Validation and remaining boundaries

Full local regression completed **5,146 passed, 32 skipped, 1 upstream warning in
1,293.14s**. The focused cleanup/dispatch/native unload/Hall/MCP/standalone suite
passes229/1warning. Ruff, mypy75, docs265, ADR validation, scope-map207 modules in
13 areas, source baseline and frontend syntax pass. App and standalone startup,
auth/origin smokes, screenshot manifest6/6 and demo check pass. The complete app-
derived artifact refresh passes backend302tests/16isolation checks
(unqualified/blocked), UI37/37 checks/7gates/0findings and generated V2 review
with no new gate advancement. Mutants and their exact failures are above.

This is not successful native unload confirmation, physical release, proof of
unloaded model inventory, a route contract change, all-workflow integration,
LC09 recovery, hardware acceptance or independent review. Default backend
selection, legacy runtime dormancy, G1 partial status, open G2–G6/LC09–LC11,
paused/unpassed test8, and proposed/not-reviewed ADR0038 remain unchanged.
