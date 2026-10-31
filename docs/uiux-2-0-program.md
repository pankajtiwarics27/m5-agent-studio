# M5 2.0 UI/UX program — coordination, phases and evidence

The UI/UX program is the second autonomous workstream on this repository. It
shares the repository rules in [`AGENTS.md`](../AGENTS.md) and the coordination
discipline of the runtime program, but it has its own ledger, its own claim state
and its own evidence bar. This page is the short version an agent reads before it
claims anything; the binding contract is
[`M5_2_0_UIUX_AGENT_PROMPT.md`](../M5_2_0_UIUX_AGENT_PROMPT.md).

| Artifact | Role |
|---|---|
| [`product/M5_2_0_UIUX_Autonomous_Roadmap.json`](../product/M5_2_0_UIUX_Autonomous_Roadmap.json) | Authoritative ledger: 40 stories `UIUX-P0-01`…`UIUX-P6-05` across 7 phases, each with per-story tracking, plus the session state and the verified tab registry. |
| [`M5_2_0_UIUX_AGENT_PROMPT.md`](../M5_2_0_UIUX_AGENT_PROMPT.md) | The claim/implement/verify/review/merge protocol every agent follows. |
| [`tests/test_uiux_roadmap.py`](../tests/test_uiux_roadmap.py) | Executable invariants: the registry matches the shipped frontend, `done` requires evidence, dependencies are acyclic, `P0` gates the rest. |
| [`product/M5_2_0_UIUX_Source_Manifest.json`](../product/M5_2_0_UIUX_Source_Manifest.json) | The pinned source surface (`UIUX-P0-01`): SHA-256 of every file the program may touch, the six tabs as the markup declares them, and the 115 routes `app.py` mounts. Regenerate it when the surface moves; `--check` fails CI on drift. |
| [`scripts/uiux_source_manifest.py`](../scripts/uiux_source_manifest.py) | Writes and checks that manifest. Deterministic, parses the files, stamps no clock. |
| [`tests/test_uiux_source_manifest.py`](../tests/test_uiux_source_manifest.py) | Re-derives every manifest claim from disk and proves the drift check actually fails. |
| [`product/M5_2_0_UIUX_Control_Inventory.json`](../product/M5_2_0_UIUX_Control_Inventory.json) + [`uiux-2-0-control-inventory.md`](uiux-2-0-control-inventory.md) | The control inventory (`UIUX-P0-02`): every interactive element attributed to its pane, plus the script-rendered set. Generated — never hand-edited. |
| [`scripts/uiux_control_inventory.py`](../scripts/uiux_control_inventory.py) | Writes and checks the inventory; `--check` fails CI when a control moves without it. |
| [`product/M5_2_0_UIUX_Route_Map.json`](../product/M5_2_0_UIUX_Route_Map.json) + [`uiux-2-0-route-map.md`](uiux-2-0-route-map.md) | The route map (`UIUX-P0-03`): every `fetch` call mapped to the route it can hit, with the calls that match nothing and the routes no browser calls. Generated — never hand-edited. |
| [`scripts/uiux_route_map.py`](../scripts/uiux_route_map.py) | Derives and checks the map from the calls and the route table; `--check` fails CI on drift. |
| [`product/M5_2_0_UIUX_Authorization_Trace.json`](../product/M5_2_0_UIUX_Authorization_Trace.json) + [`uiux-2-0-authorization-trace.md`](uiux-2-0-authorization-trace.md) | The authorization and state trace (`UIUX-P0-04`): for every state-changing browser call, the shared gates, the handler's own refusals, the authority and durable-state subsystems it references one call deep, and whether it can leave the machine. Generated — never hand-edited. |
| [`scripts/uiux_authorization_trace.py`](../scripts/uiux_authorization_trace.py) | Derives and checks the trace from `app.py` and the route map; `--check` fails CI on drift. |
| [`uiux-2-0-baseline-evidence.md`](uiux-2-0-baseline-evidence.md) | The measured baseline (`UIUX-P0-05`): suite and check results with exit codes, the capture plan, and what a machine with a browser has to run. Hand-written; the counts are re-run, not remembered. |
| [`product/M5_2_0_Autonomous_Roadmap.json`](../product/M5_2_0_Autonomous_Roadmap.json) | The runtime program's ledger. Separate scope, separate ownership; a UI/UX session never edits its story tracking. |

## What the program is for

The product already works: six panes, one shell, one vanilla frontend, a local
model gateway, durable task state, approvals and recovery. The program's job is
to make that surface calm and legible — clear hierarchy, honest status, visible
evidence, recovery you can follow — **without losing a capability**. The
preservation matrix in the ledger and the screenshot manifest are how that is
checked rather than asserted.

Non-goals, recorded in the ledger: a backend architecture rewrite, a framework
migration without measured justification, new security privileges, and removal of
existing complex workflows.

## Phase gates

| Phase | Stories | Gate before the next phase starts |
|---|---|---|
| `P0` | 6 | Pinned HEAD and source manifest, every tab and control inventoried, frontend-handler-to-route map, backend authorization trace, baseline screenshots and test results, owner-approved preservation matrix. **No redesign work happens before this closes.** |
| `P1` | 5 | Design tokens, navigation shell, shared status/evidence components, accessible overlays and shortcuts — with no route or capability change. |
| `P2` | 6 | Start and Chat are usable end to end, including empty, loading, streaming, error and recovery states, with an explicit Chat-to-Harness handoff. |
| `P3` | 7 | Harness and Pipeline render durable task state, approvals, diffs, artifacts and pause/cancel/resume/recovery from authoritative state. |
| `P4` | 6 | Council and Compare show participants, stages, disagreement and measured provenance without collapsing the two workflows. |
| `P5` | 5 | Cross-tab consistency plus keyboard, screen-reader, responsive and bounded-streaming regression. |
| `P6` | 5 | Automated preservation end-to-end tests, adversarial permission/recovery UI tests, full screenshot review, README/onboarding verification, release gate and handover report. |

## Evidence a UI story has to produce

A story is `done` only when its `tracking` block carries all of this, and
`tests/test_uiux_roadmap.py` refuses a `done` story without it:

1. **Commands actually run**, with exit codes, against the branch: `pytest`,
   `ruff`, `mypy`, `scripts/check_frontend_syntax.py`,
   `scripts/demo_tour.py --check`, `scripts/validate_screenshots.py`,
   `scripts/validate_docs.py`, and `scripts/build_studio.py` whenever
   `static/index.html` changed.
2. **An acceptance-criterion-to-evidence map** — one line per criterion, naming
   the test, the screenshot, the walk or the file that proves it.
3. **Screenshots through the real pipeline**
   (`node scripts/capture_screenshots.mjs`, then
   `python3 scripts/validate_screenshots.py`). A manifest entry refreshed without
   a capture is a fabrication, and a story that changes a pane without moving its
   shot is not done.
4. **The preservation-matrix rows it touched**, with "reachable before, reachable
   after" stated explicitly.

Capture needs a Chrome/Chromium binary and `puppeteer-core`, which are
deliberately not vendored. A machine without them records the visual check as
`blocked`; it does not claim it passed.

## The pinned source manifest

The roadmap was written from GitHub web extracts, so its statements about the
current UI start as hypotheses. `UIUX-P0-01` closed that gap with a manifest
built from the checkout rather than from prose:

* **13 pinned files** — `static/index.html`, `app.py`, the generated `studio.py`,
  the design-system contract, the screenshot manifest, the UI audit and the seven
  scripts that build, inventory, verify, map or trace the surface — each with its
  SHA-256, size and line count;
* **the six tabs** in document order, with `dom_id`, visible label, `title`
  attribute, shortcut and `switchTab` target, parsed from the markup;
* **the route table** — 115 routes (`38` `GET`, `69` `POST`, `5` `DELETE`, `3`
  `PATCH`) in source order, derived from the decorators rather than from prose;
  which of them a browser call can reach is the route map's answer, below;
* **the screenshot state** — 0 of 6 captured in this environment, recorded as
  such instead of implied.

What it deliberately does **not** claim: which handler calls which route (that
is the route map's job, not the manifest's), that any pane was seen rendered
(capture needs a browser), or that any of it matches an upstream version.

The rule that makes it a pin: a story that changes a pinned file regenerates the
manifest in the same commit, and CI runs

```bash
python3 scripts/uiux_source_manifest.py --check
```

which fails, naming the files that moved, when the two disagree. The manifest and
the ledger share one pin: `pinned_source_commit` must equal the ledger's
`source_commit_sha`, and the tests assert that commit is an ancestor of `HEAD`, so
a fabricated pin cannot pass.

## The control inventory

`UIUX-P0-02` inventoried the UI by parsing it, not by reading it, and the numbers
are worth knowing before any redesign:

* **170 controls in the markup**, attributed to their pane section or to the shared
  chrome, plus **30 more built by inline-script templates** attributed to the
  function that renders them — a checklist written from the markup alone would
  have missed 15 % of the interactive surface;
* **101 of the 170 controls live in the chrome**, more than all six panes together
  (69). The shell, not the tabs, is where most of the clicking happens;
* **58 controls have no `id`**, so no test can reach them by selector until one is
  added or a stable hook exists — a binding constraint on the `P5` accessibility
  and regression stories;
* `start` has 3 controls, `harness` 18, `hall` 19, `council` 16, `debate` 5,
  `chat` 9.

What it does not claim: that a control is *shown* (nothing was rendered), that the
nearest-function attribution is a call graph, or that a missing `id` means a
missing label — a wrapping `label` element is not resolved by this walk.

## The route map

`UIUX-P0-03` answered the question the repository could not answer before:
which frontend call hits which server route. It is derived from the 53 `fetch`
calls, not from handler names — `loadProviders` proves nothing about its URL,
the call inside it does.

* **53 call sites resolve to 61 URLs**: 60 exact matches, **1 dynamic**, **0
  findings** and **0 unresolved** — no dead call, no call whose method disagrees
  with its route, no URL that failed to resolve. Three resolution paths do the
  work: 51 call sites are literal, 1 comes from a local `const`
  (`refreshOnboarding` chooses between `/api/onboarding/preflight?probe=local`
  and the plain path), and 1 is a helper parameter followed through every caller
  — `consumeSSE` takes its URL as a parameter and reaches eight routes, six of
  them directly and two through `_streamSdlcEndpoint`, which forwards its own
  parameter into `consumeSSE`. Each URL carries the chain that supplied it, so
  the map names `startSdlcRun` and `submitSdlcFeedback` instead of stopping at
  the sink.
* **1 dynamic match**: `/api/models/${id}/${act}` picks `load` or `unload` at
  runtime, so both routes stay listed as candidates and the caller is recorded
  as *possible* on each — never as proof of one.
* **57 of 115 routes have a browser caller** (55 proved by a literal argument, 2
  reachable only through a runtime-chosen segment); the other 58 do not — which
  is not dead code, because the CLI, the demo tour and the tests use the same API.
* **Line numbers are published twice**, `script_line` inside the inline script
  and `document_line` in `static/index.html`, because the first read as the
  second is off by all the markup above the script.
* **The caller list is a lower bound whenever a call site stays unresolved**, and
  the map says so in `caller_resolution` rather than leaving a reader to assume
  every URL was traced.

The first merged version of the map got three of these wrong, and the review in
[issue #110](https://github.com/pankajtiwarics27/m5-agent-studio/issues/110)
caught all three: it did not follow a forwarded helper parameter (so both SDLC
routes looked unused), it recorded a dynamic match's caller on only the first
candidate (so `unload` looked unused), and it published script-relative line
numbers. The generator and the page now do all three honestly, with a
regression test per case — including one that proves an unresolvable hand-off
turns the "no caller" list into a stated lower bound instead of a clean table.

What it does not claim: that any of these calls *works* (nothing was executed;
authorization, request bodies and response handling are out of scope), that a
dynamic match resolves to one route, or that an unused route is unused anywhere
but in the browser. Those limits are written into the generated page and a test
fails if the page drops them.

## The authorization and state trace

`UIUX-P0-03` said where a call goes; `UIUX-P0-04` says what can stop it and what
it changes. The trace covers the **37 distinct state-changing routes** the
browser can call, out of 77 non-GET routes in `app.py` — two more than when the
story closed, because the route map learned to follow a forwarded helper
parameter and recovered `POST /api/harness/sdlc/run` and
`POST /api/harness/sdlc/feedback` (issue #110).

* **The shared boundary is read from the middleware itself**: host allowlist
  (400), cross-origin rejection for state-changing methods (403), bearer token
  compared with `secrets.compare_digest` (401, or 503 when authentication is
  unconfigured), the rate limit on the six expensive POST paths (429) and the
  body-size limit (413). `GET /healthz` and `GET /` are the only routes outside
  the protected prefixes — the shell has to load before the browser has a token.
* **Handler refusals are read from each handler's own raises**: nine distinct
  statuses across the 37 routes, plus one raise whose code is computed at runtime
  and is recorded as *dynamic* rather than guessed into a bucket.
* **Authority and state are read one call deep.** Twelve routes reference an
  authority subsystem (seven directly, five through a helper) and 30 touch
  durable state; a blank cell means "nothing at this depth", never "unguarded".
* **Twenty-two routes can leave the machine** — subprocess, outbound HTTP client
  or the OS keyring, several of them one helper call away. That is the number a
  screen has to respect before it prints the word "local".

What it does not claim: that a reference is a guard, that a computed status is
any particular code, or that a refusal was ever observed — no request was sent.

## The tab registry

The ledger carries a `tab_registry` derived from `static/index.html` and
`docs/media/screenshots/manifest.json`, and the roadmap tests fail if the three
ever disagree. It exists because the plan's labels are not the DOM ids:

| Label in the plan | DOM id (`data-tab`) | Shortcut |
|---|---|---|
| Start | `start` | `Alt+6` |
| Harness | `harness` | `Alt+1` |
| Council | `hall` | `Alt+2` |
| Pipeline | `council` | `Alt+3` |
| Compare | `debate` | `Alt+4` |
| Chat | `chat` | `Alt+5` |

Anyone who greps for a `pipeline` tab will find nothing; the Pipeline pane is
`data-tab="council"`.

## Relationship to the runtime program

The runtime program finished its mandatory stories and qualified container
isolation on the target host on 2026-10-31; its optional `M5-P6-01`…`P6-03` are
claimed elsewhere. The two programs share a repository, not a ledger:

* a UI/UX session never edits runtime story tracking, and a runtime session never
  edits UI/UX story tracking;
* backend behaviour a screen needs is a runtime story or an open risk, never an
  API widening inside a UI pull request;
* both programs use the same merge discipline — one story per branch and pull
  request, merge commit, green checks, review by the non-author.

## Status

Nothing in this program is shipped: 40 stories, 4 done (`UIUX-P0-01` the pinned
source manifest, `UIUX-P0-02` the control inventory, `UIUX-P0-03` the route map,
`UIUX-P0-04` the authorization and state trace), `UIUX-P0-05` (baseline
screenshots and test results) in progress: its test half is measured and
published in [`uiux-2-0-baseline-evidence.md`](uiux-2-0-baseline-evidence.md),
and its visual half is **blocked** — this environment has no Chrome/Chromium and
no `puppeteer-core`, so the six screenshots stay 0 of 6 and the story does not
close on the capture script's dry run. Planned work is never described as
available in `README.md` before the owning story closes with evidence, and no
pane is claimed to have been seen rendered.
