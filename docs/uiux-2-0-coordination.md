# UI/UX autonomous coordination and next-work handoff

This is an operational handoff, not a second roadmap or a release approval. The
[UI/UX ledger](../product/M5_2_0_UIUX_Autonomous_Roadmap.json) owns story state;
[the agent protocol](../M5_2_0_UIUX_AGENT_PROMPT.md) owns claims, review and
handoff. Do not change the separate runtime ledger to make a UI story look done.

## Start a session

```bash
python3 scripts/uiux_coordination.py                 # local only; no network
python3 scripts/uiux_coordination.py --github        # explicitly check GitHub
python3 scripts/uiux_coordination.py --github --json  # machine-readable report
```

The helper is **read-only**. It recomputes local dependency and phase readiness,
reports active stories, checkout cleanliness, open pull requests, explicitly
marked blocker issues and the relationship to GitHub's main branch. It holds a
candidate when GitHub was not checked, main is not known to be included, there
are unmerged local commits or a dirty tree, a UI/UX or unclassified PR is open,
a marked coordination blocker is open, a PR/issue scan may be truncated, or the
ledger is inconsistent. Other-program PRs are shown for shared-file review. A
failed GitHub request exits nonzero rather than presenting local readiness as
global readiness.

A successful exit means the report was generated, **not** that work is safe to
claim; inspect `holds` and `next_candidate`. The helper checks that completed
stories carry the required tracking fields, but does not independently verify
their evidence or CI results. A story ID **mentioned** in a PR is not proof of
ownership; an unidentified PR is conservatively held for manual inspection. The
tool does not inspect all pushed branches, acquire a distributed lock, or prevent
a second agent from claiming simultaneously. Its answer is a point-in-time
advisory, not permission to skip the protocol. In particular, a metadata-only
claim on another branch is invisible until it is announced. At claim time,
publish a draft PR with the story ID in its title immediately after the claim
commit is pushed, then recheck for competing claims before coding. If there is a
collision or any uncertainty, stop and coordinate with the other session; do not
start a second implementation.

For a verified cross-story blocker that needs all UI/UX sessions to stop and
coordinate, put a separate line such as `UIUX-COORDINATION-BLOCKER: UIUX-P0-03`
in the GitHub issue body. `--github` holds all new candidates while such an
issue is open, even if the affected story is already marked done. This is a
conservative program-wide hold, not an ownership claim. Close or remove the
marker only when the responsible sessions have resolved the blocker. Unmarked
issues are **not** automatically interpreted; inspect `gh issue list --state open`
and the ledger's risks as well. The marker is a signal, not a replacement for
review or a distributed lock.

## Active blocker follow-up (2026-10-31)

[Issue #110](https://github.com/pankajtiwarics27/m5-agent-studio/issues/110)
is being investigated by `arena/cbed6c70-m5-agent-studio` as a corrective
follow-up to the already-merged route map and authorization trace. This is not a
new roadmap-story claim and does not change another session's tracking. The
follow-up will publish a draft PR, reproduce the missing caller cases in tests,
and coordinate regeneration of the map and trace before requesting review.
`UIUX-P0-05` remains in progress under `arena/c705f453-m5-agent-studio`; do not
take its story or treat its visual baseline as complete.

## Baseline at this handoff

The following is a dated observation, not a live status dashboard. Re-run the
helper before taking work.

- On 2026-10-31, main advanced to commit
  `866ec1c063c0c750d40e219ddfc9107363732fc6`. The ledger recorded **4 of 40**
  UI/UX stories done and recommended `UIUX-P0-05` next. The supplied roadmap was
  already adopted; replacing it with the original attachment would erase tracking.
- `UIUX-P0-03` was marked done by merged
  [PR #109](https://github.com/pankajtiwarics27/m5-agent-studio/pull/109),
  which superseded conflicting PR #104 and separated the runtime changes. At
  merge head `b53f90ece7eb08413ac9952ab6343ce9726ff1cd`, the ported map still
  falsely reported forwarded SDLC URLs and the model-unload candidate as having
  no browser callers. Those findings were posted as a
  [review comment](https://github.com/pankajtiwarics27/m5-agent-studio/pull/109#issuecomment-6542772260)
  before merge and are now tracked as program blocker
  [issue #110](https://github.com/pankajtiwarics27/m5-agent-studio/issues/110).
  Do not use the map's no-caller assertions as preservation evidence until fixed;
  coordinate the follow-up rather than overwriting another session's tracking.
- A parallel session's [PR #107](https://github.com/pankajtiwarics27/m5-agent-studio/pull/107)
  merged the detailed [story-by-story transformation plan](uiux-2-0-transformation-plan.md).
  A different session completed `UIUX-P0-04` in merged
  [PR #111](https://github.com/pankajtiwarics27/m5-agent-studio/pull/111).
  Its authorization trace at merge head `f5d8e20` omits the two forwarded SDLC
  routes described in #110; a
  [review comment](https://github.com/pankajtiwarics27/m5-agent-studio/pull/111#issuecomment-6542857523)
  requested coordinated correction before merge. Update and revalidate the trace
  after the route-map fix; its reported coverage is not exhaustive. The plan and
  this read-only handoff are complementary, not competing ledgers.
- At this checkout the generated source and control checks passed: six panes,
  115 route declarations and 170 interactive markup elements, plus 30
  script-template controls counted heuristically. These are static inventories,
  not proof of browser reachability or functioning routes.
- Before the runtime PR advanced main, Python 3.11.2 on Linux ran the existing
  suite at `71eab0002e8e5b64ecdab7daae06bd47af126689`: **1,508 passed, 1
  skipped** (the opt-in live-container test). At `e96b30e` plus the coordination
  working-tree changes, the full suite ran **1,572 passed, 1 skipped**, with one
  Starlette deprecation warning. Ruff, mypy, the frontend syntax check,
  source/control/route-map drift checks, docs, demo transcript, both local smoke
  checks and standalone regeneration passed. The route-map drift check does not
  test the semantic findings in #110. This is not hardware qualification or
  evidence about future commits.

## What the exploratory browser pass revealed

A disposable source copy at `5b07b5b` was served with the demo's throwaway
database and exercised with Chromium 153 and `puppeteer-core` 25.16. The
existing capture script produced six **temporary** 1440×900, 2× desktop images.
`python scripts/validate_screenshots.py --index /home/user/.cache/m5-uiux-exploratory-shots/index.json`
exited 0 and validated all six in that disposable copy; the cache is not durable.
A temporary Puppeteer shim dismissed the initial authentication prompts;
the unmodified capture invocation had timed out before the token was set. The
images are not committed, are not a P0-05 acceptance baseline, and do not
establish keyboard, screen-reader, dark-theme, narrow-screen, streaming or
recovery behavior.

The pass gives the next stories concrete questions, not permission to change
behavior prematurely:

| Observation | Planned response | Evidence still required |
|---|---|---|
| The shared header, integrations strip and telemetry rail occupy substantial space across all six panes. Harness also places session settings alongside its task composer. | P1 shell hierarchy; P3 Harness progressive disclosure. Preserve every control and shortcut. | Before/after desktop and narrow captures, control-reachability and keyboard checks. |
| In the light-theme Harness capture, several starter-card headings are very pale on white. | P1 theme tokens and P5 contrast regression. | Measured contrast across states and both themes; do not infer a WCAG pass from a screenshot. |
| With the local model runtime unreachable, the backend marks telemetry `telemetry_source: simulated_demo` and the shell displays “Preview Demo”, while individual memory/container panels show example values. | P1 shared status/evidence and P2 system-health work should make simulated data unmistakable at the point of use without inventing live state. | Tests for demo, live, disconnected and error payloads; coordinate backend changes separately if needed. |
| Start's readiness checklist and Chat's empty view are visible, but a screenshot does not exercise setup, a model response, permissions or recovery. | P2 actionable onboarding and Chat states. | Real interaction and failure-path tests, with no implicit model or cloud calls. |

### Safety and capture risks

- `demo_tour.DemoEnvironment` isolates its database, token and HOME, but
  `app.py` currently sets `HARNESS_STORE_DIR` relative to the application source
  directory. Starting the demo from a real checkout can therefore read that
  checkout's existing `.harnesses/sessions` workspace preferences and session
  state; it must not be treated as a fully isolated data environment. The first
  exploratory run exposed a pre-existing test workspace path. Use a disposable
  source copy with its own empty `.harnesses` for investigation; do not capture
  or publish screenshots from a real user's checkout. The runtime/safety handoff
  and sentinel reproduction are tracked in
  [issue #108](https://github.com/pankajtiwarics27/m5-agent-studio/issues/108);
  a durable fix needs isolation tests.
- `scripts/capture_screenshots.mjs` first loads the page before inserting its
  token into `sessionStorage`. With the browser versions above, authentication
  prompts prevented the first navigation from completing. The temporary shim is
  an investigation aid, not a change to the supported capture pipeline. P0-05
  should fix or explicitly resolve this on its own dependency-ready story and
  validate capture without exposing the token to another origin.

## Transformation priorities

These are design outcomes for the existing stories, not a license to skip their
P0 gate or combine multiple stories in one PR.

| Phase | Experience to improve | Preservation and verification focus |
|---|---|---|
| P1 · shared shell | Reduce chrome competition; make navigation, the current workspace and important status easy to scan. Keep telemetry discoverable but give the task more room. | Preserve tab IDs, shortcuts and every tool entry point; label simulated or disconnected data at the point of use; test focus and both themes. |
| P2 · Start and Chat | Make the next setup action clear, then give conversations a legible composer, context and explicit handoff. | Preserve readiness checks, provider consent, model selection and streaming; exercise empty, loading, failure and recovery states. |
| P3 · Harness and Pipeline | Emphasize the task, durable activity and stages; surface approvals, verification, diffs and recovery where decisions are made. | Render authority from backend state; preserve event cursors, approvals, abort, worktrees and existing workflow controls. |
| P4 · Council and Compare | Make participants, disagreement, evidence and comparisons understandable without collapsing distinct workflows. | Retain role/model choices and export continuity; distinguish measured results from examples. |
| P5–P6 · quality and release | Make the experience consistent at narrow and wide sizes, usable by keyboard and assistive technology, and stable under long streams. | Target WCAG 2.2 AA without claiming certification; test reduced motion, bounded event rendering, permission failures and full preservation before release. |

## Sequence after the open work is resolved

1. **P0-03 follow-up:** coordinate resolution of issue #110, regenerate the
   affected map and artifacts, and obtain review and green checks.
2. **P0-04 follow-up:** after the map fix, regenerate and review the merged
   authorization trace so it includes the forwarded state-changing routes. Any
   missing backend behavior is a runtime handoff, not a UI shortcut around the
   admission or approval gates.
3. **P0-05 → P0-06:** capture an isolated, reproducible desktop/narrow and
   light/dark baseline; record real test results, preservation mapping, rollback
   plan and the required owner approval. P0 remains a hard redesign gate.
4. **P1 → P6:** improve the shared shell and statuses; then Start/Chat,
   Harness/Pipeline, Council/Compare, accessibility/performance and release
   regression in the ledger's dependency order. Keep each PR scoped, preserve
   routes and permissions, and describe shipped behavior only after evidence.
