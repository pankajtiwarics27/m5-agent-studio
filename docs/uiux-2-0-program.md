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

Nothing in this program is shipped: 40 stories, 0 done, `next_recommended_story`
= `UIUX-P0-01`. Planned work is never described as available in `README.md`
before the owning story closes with evidence.
