# LC-08 coordinated integration — in progress

## Checkpoint scope (not story completion)

`runtime_dispatch_boundary.py` supplies an immutable internal routing fence used
by `app.py` and inlined by `scripts/build_studio.py`. Its default is **legacy**, so
this change does not activate native adapters, change routing/settings, claim
cross-runtime safety for legacy mode, load/download a model, take over terminal
`ollama serve`, or resume test8. No production backend, qualifier, native transport,
counter, live permit producer or mode-switch setting is installed.

The generated [entry inventory](runtime-dispatch-entry-points.md) includes common
inference, explicit controls, workflow cleanup, MCP summon, child/critic, model
switch probes, GSD/SDLC and calibration callback paths. It is static evidence,
not proof of native reachability, installed behavior or runtime qualification.
[ADR-0038](adr/0038-coordinated-dispatch-integration.md) is proposed, not reviewed.

## Implemented fence

- Coordinated common dispatch runs **before** legacy message sanitization,
  character trimming, hard-coded output/reasoning caps, demo/online probes and
  process-local heavy-model slots. It owns an immutable bounded JSON copy of all
  application messages/tools/temperature/output/reasoning options, without
  modifying them. This copy is **not** the final provider wire/count proof.
- Internal backend composition explicitly binds oMLX/Ollama/LM Studio aliases.
  Generic local/private/proxy providers are not silently treated as qualified
  cloud/native engines. Existing genuinely remote provider routing keeps its
  original security boundary. No provider/model/port implies qualification.
- Explicit load/unload/unload-all/cache controls and workflow unload callbacks
  route through this boundary before legacy probes/demo branches. Missing backend
  yields fixed 503 diagnostics. Only bounded canonical status replies are accepted;
  raw native bodies/`ok` ACKs cannot masquerade as confirmed completion. Workflow
  cleanup refuses pending/uncertain/refused status instead of advancing.
- Direct legacy stream helpers and generic pinned-HTTP runtime effects are fenced
  even with an installed backend. Unknown/failed executors cannot fall through to
  legacy I/O, cloud substitution, demo, trimming or blind retry. No generic HTTP
  client is passed to the composed backend. The pinned transport gate runs before
  DNS/native I/O and does not weaken original outbound validation/pinning.
- Coordinated combo execution and unowned auto-eviction explicitly refuse until
  full consent/fit/domain-aware orchestration exists. This is not a claim those
  features already work in coordinated mode. Legacy default is unchanged.
- Consumer close/cancellation propagates to the backend iterator; the fence does
  not invent native cancellation, request completion, lease release or settlement.
- Standalone carries the same fence without a sibling import; the isolated-copy
  test exercises refusal outside the checkout. Existing standalone native adapter
  consumption is **not** asserted.

## Required next work / release blocks

LC-08 is **not complete**. The backend protocol is an internal dependency seam,
not authority to perform native operations. Positive backends in this checkpoint's
tests are fakes proving routing and input preservation only. Still required:

1. Concrete canonical native executor tying final provider preparation and LC-07
   pre-disruption/warm counting to actual admitted LC-02/03 operations and the
   LC-04--06 bridge send markers. No always-true backend assumption may become a
   production load permit.
2. Full canonical tool/reasoning/usage packet consumption without interpreting
   reasoning as tool authority, plus request inclusion evidence across workflows.
3. Cold transition and concurrent explicit-control/inference race tests using real
   dormant coordinator/memory/bridge kernels, late effects and owned native facts.
4. Standalone consumption of that executor with isolated module identities and
   shared transaction guards (not just this dependency-free routing fence).
5. Complete legacy/generic bypass/mutation coverage and final full CI. LC-09 still
   owns session handoff/checkpoint/grant/budget/recovery semantics; LC-10 migration
   UI and LC-11 hardware/release/security qualification stay separately gated.

No acceptance ledger, source screenshot approval or independent security signoff
is advanced. Tests1--7 acceptance is unchanged; test8 remains paused/unpassed.

## Validation recorded for this checkpoint

- Initial new fence suite: **50 passed** offline, including complete-input
  forwarding, unavailable backend, fixed control errors, pending cleanup,
  generic-HTTP refusal before DNS, direct helper refusal and isolated standalone.
- Existing admission/chat-tool/request-fit/Ollama/LM Studio bridge regression:
  **440 passed**, one upstream Starlette/httpx warning, before the final static
  inventory addition. Broader/final checks will be recorded when executed.
- Initial configured mypy passed for **72 sources**; no production/native
  qualification follows. No live native endpoint was contacted.

### Follow-up checks and evidence refresh

- The fence suite now has **59 passing tests**, adding private-DNS resolution
  after explicit private-network opt-in, unknown/encoded GET paths, owned
  composition containers, rejected boolean backend configuration, and existing
  HTTP token/origin authorization in front of composed controls.
- **496 combined fence/admission/chat-tool/request-fit/native-bridge tests** passed
  before the three added HTTP authorization cases (one upstream warning).
- Five disposable mutations were caught: common-dispatch bypass, control before
  online probe, dropped resolved-private-IP guard, ignored cleanup confirmation,
  and boolean backend installation. Mutation copies never changed working sources.
- The first broad run finished **4,201 passed, 17 failed, 32 skipped, 1 warning**
  in **791.96 seconds**. Failures were source-bound generated inventory/evidence
  drift and a historical UI release test that compared its implementation pin to
  all future HEAD changes. They are not reported as a clean full-suite run.
- Regenerated affected inventories/authorization trace/preservation package through
  their generators, preserving historical unbound approval and unchanged browser
  assets. Fresh existing UIV2-29 backend safety execution passed **300 tests plus
  16 isolation checks**; live container isolation remains unqualified/blocked.
  Derived performance/release records were regenerated without replacing visual
  evidence, CI records, approval references, source pins or acceptance ledgers.
- The historical UI release test now audits **its recorded implementation range**
  instead of forbidding every subsequent backend change forever. Its existing
  current browser-source digest checks and historical visual/CI approval checks
  remain intact. No screenshots were recaptured or newly approved.
- Targeted repaired inventory/performance/release/fence suites: **248 passed**,
  one upstream warning, before the three new HTTP cases. Existing app and
  standalone isolated startup/auth/origin smoke checks passed. Ruff, configured
  mypy (72 sources), ADR/docs, governance, deferred security-record consistency,
  frontend syntax and baseline verification passed. Full history was fetched to
  make the pinned baseline available in this initially shallow checkout.

These are checkpoint results, not LC-08 completion, architecture acceptance or
permission to activate/merge. Concrete executor, canonical consumption,
coordinator-backed race tests and executor-level standalone consumption remain
open as listed above.
