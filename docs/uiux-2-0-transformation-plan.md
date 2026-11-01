# M5 2.0 UI/UX transformation plan — sequencing, design intent and the parallel-session map

This page is the execution plan for the UI/UX program. It does not replace
anything: the authoritative **status** of every story lives in
[`product/M5_2_0_UIUX_Autonomous_Roadmap.json`](../product/M5_2_0_UIUX_Autonomous_Roadmap.json)
and the binding **protocol** is
[`M5_2_0_UIUX_AGENT_PROMPT.md`](../M5_2_0_UIUX_AGENT_PROMPT.md); the visual
**contract** (tokens, type, components, motion) is
[`design-system/m5-agentic-studio/MASTER.md`](../design-system/m5-agentic-studio/MASTER.md);
the pre-change **audit** is [`ui-audit.md`](ui-audit.md) and the decision log is
[`ui-decisions.md`](ui-decisions.md).

What the ledger does not say, and this page does, is **what actually changes**
in `static/index.html` for each story, **how two sessions work on one HTML file
without destroying each other's work**, and **what has to be true before a
phase is allowed to start**.

Two facts motivate the page:

* the ledger's per-story content is generated: all 40 stories share the same five
  acceptance criteria and the same eight implementation steps, so the ledger can
  sequence the work but cannot tell an implementer what to build;
* the interface is one 4,576-line file that every story has to edit, so
  "coordination" is not a social nicety here — it is the difference between two
  green pull requests and a week of conflict resolution.

Everything below was measured in this checkout at commit `5b07b5b` (main,
2026-10-31). Anchors are given as **selectors, element ids and function names**;
line numbers are recorded as measurements and are expected to drift.

## 1. What the interface is today

| Measure | Value | Where |
|---|---|---|
| Interface files | one HTML file, 4,576 lines, no build step, no framework | `static/index.html` |
| Legacy CSS layer | lines 7–363: 67 hex literals, 85 `rgb`/`rgba()` literals | first `<style>` |
| Design-system layer | lines 364–666: 37 token hex values plus 3 legacy demo overrides | `style#m5-redesign-system` |
| Markup | 1,017 lines: 294 inline `style` attributes, 92 inline `onclick` handlers, 22 hex literals | body |
| Inline script | 2,892 lines: 138 inline `style` strings, 33 inline `onclick` strings, 47 hex literals | `<script>` |
| Panes | 6 sections, in document order: `pane-start`, `pane-harness`, `pane-hall`, `pane-council`, `pane-debate`, `pane-chat` | 1264, 1287, 1404, 1493, 1558, 1587 |
| Tabs | 6 `data-tab` buttons with `title` and `onclick="switchTab(...)"` | `.mode-tabs` |
| Controls | 170 in markup (101 in the shell/chrome, 69 in the panes) + 30 rendered from script templates | `product/M5_2_0_UIUX_Control_Inventory.json` |
| Overlays | 9 `role="dialog"` modal backdrops, 13 native `details` disclosures | body |
| Live regions | 2 (`#connPill`, `#toastStack`) across ~2,900 lines of script | header, shell |
| Focus styling | 12 `:focus-visible` occurrences | both CSS layers |
| Breakpoints | 1100, 880, 860, 820, 600, 390 px, plus two `prefers-reduced-motion` blocks | both CSS layers |
| Themes | saved `m5_theme`, otherwise the system's light/dark preference; dark fallback. Light uses `body[data-theme="light"]` | CSS + `toggleTheme()` |

Read as a design brief, those numbers say three things:

1. **The shell is the product.** 101 of 170 markup controls are chrome, not pane
   content. A "tab redesign" that ignores the header, the mode rail, the
   integrations strip, the telemetry rail and the 9 dialogs changes
   about 40 % of the clickable surface.
2. **Two sources of styling truth.** The design-system layer overrides a legacy
   layer that is still fully live. Every visual fix has two candidate homes, and
   the newer one wins only where it happens to be more specific.
3. **Instance styling outnumbers the system.** 432 inline `style` attributes and
   125 inline handlers mean consistency is currently maintained by copying, not
   by tokens and components.

The original planning session did not render the checkout: it had no
Chrome/Chromium binary or `puppeteer-core`. A later session made exploratory
captures in a disposable source copy with a temporary workaround, but these are
not formal baseline evidence. The repository screenshot manifest remains at
0 of 6. See the [coordination handoff](uiux-2-0-coordination.md) for the capture
and data-isolation risks. The visual baseline is **blocked**, not green.

## 2. What "transformed" means for M5 2.0

The program's goal is not a new look; it is a surface that answers four questions
at a glance, on every pane: *what am I looking at, what is the system doing right
now, what is the one next action, and what happens if it fails.* Concretely, six
commitments that every phase is measured against:

1. **One obvious next action per region.** One primary button per local context;
   everything else neutral or quiet. Today Harness already has a fixed version of
   this (New session demoted next to Run), and the pattern stops there.
2. **Status comes from the server, and says which state it is in.** Idle, queued,
   running, awaiting approval, success, failed, cancelled, disconnected and stale
   are nine distinct states; today they are a `badge` with a colour class and a
   text that is sometimes upper-cased prose.
3. **Progressive disclosure with a stated home for what is hidden.** Advanced
   settings may collapse, but never disappear and never become palette-only. 13
   native `details` already follow this; the rest of the density should too.
4. **Recovery is a first-class surface.** Every failure path states the cause, the
   retained context and the available actions — the pattern already used by the
   task-event resync path, applied everywhere instead of only there.
5. **One token layer, measured contrast, both themes.** No new hex outside the
   token block; contrast pairs recorded, not assumed.
6. **Keyboard, focus, live regions and reduced motion are acceptance criteria.**
   A story that adds a control adds its accessible name, its focus behaviour and
   its announcement budget in the same change.

Explicit non-goals (from the ledger, restated because they are the first thing a
redesign breaks): no framework migration, no backend or API widening, no new
security privilege, no removal of an existing workflow, no network assets, no
change to routes, task state semantics or approval authority.

## 3. The target shell (P1)

The shell is where the first transformation lands, and it is the only phase that
changes every pane at once. The target structure, keyed to what exists:

| Region | Today | Target | Constraint |
|---|---|---|---|
| Header `.topbar` | brand, version badge (hidden), connection pill, mode tabs *moved into a fixed left rail*, search, density, theme | 56 px bar: identity, connection status, global readiness, search; keep `#connPill` as `role="status"` | ids and handlers unchanged |
| Mode rail `.mode-tabs` | fixed 200 px column at ≥1100 px, horizontal row below | same 6 `data-tab` buttons, add a per-pane section index (in-page anchors) instead of a second navigation system | `Alt+1`…`Alt+6` and `switchTab()` contract unchanged |
| Workspace | `.harness-shell`, `.hall-shell`, `.scroll-body` with per-pane grids | one workspace primitive (header / scroll body / composer) with pane-specific slots | pane section ids unchanged |
| Integrations strip `.skills-strip` | global pill row above every pane | collapsed summary line with a disclosure, height-capped and keyboard scrollable | every pill remains reachable |
| Telemetry rail | 320 px right column, toggle `#monitorToggleBtn`, `renderTelemetry()` | inspector that states what it measures and degrades to a drawer | resize behaviour must be fixed, not reset |
| Status surface | `.status-pill`, `.badge` variants, per-pane status spans | one status component with 9 states, icon + text + token | hue never the only signal |
| Feedback | `#toastStack` (`role="status"`, `aria-live="polite"`) | toasts for transients only; consequential errors/approvals also get a persistent surface | `showToast(msg, kind)` signature kept |
| Overlays | 9 backdrops, ESC per dialog, focus not trapped or restored centrally | shared dialog/drawer controller: title, description, contained scroll, ESC, focus trap and focus return | every `open*Modal`/`close*Modal` keeps working |

Design tokens to add in this phase, on top of the existing semantic palette:
spacing scale (`4/8/12/16/24/32/40`), radii (`6/8/10`), type scale
(`12/13/14/16/18/22`), control heights (`32/36/40/44`), a motion scale
(`120ms` interaction, `240ms` panel, one reduced-motion fallback) and a
`--status-*` family for the nine states. The legacy layer is retired by
conversion, not deletion: a rule moves only when its replacement is proven in
both themes, and the contrast pair goes into the audit table in the same change.

## 4. Story-by-story change shape

Each row names the **observable change**, the **anchor** to read first, and the
**proof** a reviewer should demand. "Proof" is a minimum, not a substitute for
the ledger's five acceptance criteria.

### P0 — evidence baseline (gate; no redesign)

| Story | Change | Anchor | Proof |
|---|---|---|---|
| P0-04 Trace backend authorization and state | For every state-changing call in the route map, record the gate in force (bearer token, host/origin check, admission gate, approval scope, sandbox policy) and the durable-state transition it drives; publish a generated trace + page, and derive the UI rule it implies | `app.py` auth/origin middleware, `/api/harness/*`, `/api/harness/sessions/{session_id}/events`, `product/M5_2_0_UIUX_Route_Map.json` | trace covers every non-GET call, names the refusing gate, and a test fails if a route loses its recorded gate |
| P0-05 Capture baseline screenshots and test results | Baseline suite results with commands, exit codes and environment; 6 screenshots through the real pipeline | `scripts/capture_screenshots.mjs --dry-run`, `scripts/validate_screenshots.py`, `scripts/demo_tour.py --check` | results recorded verbatim and six captures validated; missing prerequisites recorded as `blocked`, with the gate remaining closed |
| P0-06 Approve preservation and rollback baseline | Owner sign-off on the preservation matrix and the rollback path, not an agent conclusion | `feature_preservation_matrix`, `docs/ui-decisions.md` | an explicit owner approval recorded in the ledger; agents may prepare, not sign |

### P1 — shell and design system

| Story | Change | Anchor | Proof |
|---|---|---|---|
| P1-01 Tokens, typography, density, themes | One token block: keep the 37 existing values, add space/radius/type/control/motion/status families, convert the highest-traffic rules out of the legacy layer, record measured contrast per pair in both themes | both `<style>` layers, `:root` at 366, `body[data-theme="light"]` at 394 | a static check that no new hex appears outside the token block; contrast table with measured ratios; both themes on all six panes |
| P1-02 Navigation shell | Rail order and labels matched to the registry, per-pane section index, active state from `aria-current`, density control honoured at every breakpoint | `.topbar`, `.mode-tabs`, `#mainLayout`, `switchTab()` (2008), `toggleDensity()` | 6 tabs, 6 shortcuts, `data-tab` values unchanged; keyboard walk across the rail |
| P1-03 Status and evidence components | One status component rendered from a state value; evidence blocks (mono, copy affordance, provenance line) shared by Harness, Pipeline, Council, Compare | `.status-pill` (674), `.badge`, `#toastStack` (720), `showToast()` (1695) | a state-matrix test: 9 states render icon + text; announcements only on transition |
| P1-04 Overlays, toasts, shortcuts | Dialog controller with focus trap and return; drawer variant for the inspector; one shortcut registry feeding the palette and the `title` hints | 9 `role="dialog"` backdrops, `openCommandPalette()`, `renderCommandPaletteList()` | keyboard-only open → use → close → focus returned, for every dialog |
| P1-05 Empty, error, recovery presentations | Three shared presentations with cause, retained context, and the actions that exist; wired into the existing error paths before any pane redesign uses them | `.onboarding-note`, `#hallStream` empty block, `#debateSynthesisArea`, task resync path (~3331) | each presentation exercised in a test with a refused request and a disconnected stream |

### P2 — Start and Chat

| Story | Change | Anchor | Proof |
|---|---|---|---|
| P2-01 Start readiness | Readiness card: summary badge becomes a status component, `#onboardingSteps` becomes a checklist with per-step state and one action, re-check states its cause and time | `#pane-start` (1264), `refreshOnboarding()`, `startGuidedFirstTask()`, `openProvidersModal()` | states: ready, blocked, checking, failed probe; no fabricated readiness |
| P2-02 Start recent work and launch | Recent sessions and the launch action live next to readiness, with the model/workspace the task will use shown before launch | `startGuidedFirstTask()`, session lists, `#onboardingFirstTask` | launching from Start lands in the same Harness state as launching from Harness |
| P2-03 Start health and workspace | System health (provider reachability, local runtime, memory) and workspace selection with explicit unknown/unavailable states | `#onboardingNotes`, `saveWorkspaceDirectory()`, telemetry payload | unavailable is distinguishable from unknown in both themes |
| P2-04 Chat composer and hierarchy | Composer with visible mode (model, persona, reasoning), message hierarchy (role, model, time, tokens), one primary action | `#pane-chat` (1587), `#chatModel`, `#chatPersona`, `#chatReasoning` | empty, streaming, stopped, failed states; composer keyboard-operable |
| P2-05 Chat streaming and tool transparency | Streamed tokens batched, a visible "what is happening" line for tool calls, bounded history; unload control stays reachable | `consumeSSE()` (2736), `renderMarkdown()`, `unloadAllModels()` | DOM node growth bounded during a long stream; `aria-live` announcements not per token |
| P2-06 Chat to Harness handoff | Explicit handoff: promote a chat to a Harness task with workspace, model and transcript retained, and a recovery path if Harness refuses | Chat composer, `startFreshHarnessDraft()`, harness session creation | handoff and its refusal both exercised; nothing duplicated or silently dropped |

### P3 — Harness and Pipeline

| Story | Change | Anchor | Proof |
|---|---|---|---|
| P3-01 Task creation and settings hierarchy | Left sidebar keeps session list + workspace + new session; the control bar keeps model, critic and policy visible and moves the rest into one disclosed "Run settings" group with the current values summarised | `#pane-harness` (1287), `.harness-bar`, `#harnessAdvanced` | all 17 inventoried Harness markup controls reachable, plus dynamic controls; defaults and payload unchanged |
| P3-02 Durable timeline and replay | Trajectory stream becomes an event timeline (step, tool, approval, result) rendered from the server's events, with replay from a cursor | `#harnessStream`, `renderStoredHarnessEventHtml()`, `ensureLiveStepCard()`, task-event resync | replay after reload reproduces the same timeline; cursor semantics unchanged |
| P3-03 Approvals and permissions | Approval cards state who asks, for what, the scope, the expiry and the refusal consequence; the client never enables an action the server would refuse | `appendWorkflowApprovalCard()`, approval endpoints from the route map | adversarial test: a fabricated client-side approval cannot enable execution |
| P3-04 Verification, diff, artifacts | Verification result, diff and artifacts share one evidence block with provenance; the diff dialog keeps refresh and apply-to-worktree and offers copy | `#harnessDiffModal`, `applyHarnessWorktree()`, `#harnessActiveStatusBadge` | apply remains an explicit operator action subject to server checks; artifacts listed with source |
| P3-05 Pause, cancel, resume, crash recovery | One control cluster with the states the backend actually has, plus a recovery banner after a restart | `#harnessAbortBtn`, `abortHarnessRun()`, checkpoint/resume endpoints | each of pause/cancel/resume proven against server state, not client state |
| P3-06 Pipeline stages and artifacts | The 5 step cards become a stage timeline with duration, model and status per stage, artefacts attached to their stage | `.stepper`, `#step-card-0..4`, `selectStageFilter()`, `#councilStagesContainer` | filter selection announced; stage status from stream events |
| P3-07 Pipeline failure, recovery and full-run retry | Failed, stopped and completed outcomes are distinct; exact-payload retry reruns all four seats from Architect; an explicitly labeled browser-local checkpoint can restore partial output after reload, distinct from the server record; no stage-resume claim | `startPipelineRun()`, `retryPipelineRun()`, checkpoint presentation and `GET /api/history/export` | error/close/stop, exact-payload replay, browser restore and server-record readback are tested; every retry reruns all four seats; checkpoint provenance and limits are disclosed |

### P4 — Council and Compare

| Story | Change | Anchor | Proof |
|---|---|---|---|
| P4-01 Participants and readiness | Seat roster shows role, model, locality and readiness before a round; memory implications of local seats stated | `#hallRoster`, `#hallSeatsSettings`, `#hallSeatArch..Judge` | four roles with the real model strings; unavailable seat cannot start a round |
| P4-02 Deliberation stages and evidence | Round structure visible: who spoke, in what order, what each contributed | `#hallStream`, `runHallFullRound()`, `summonSingleAgent()` | streaming and completed rounds render the same structure |
| P4-03 Consensus, disagreement, results | Verdict area separates agreement, dissent and the arbiter's decision, with the evidence each rests on | `#hallConclusionBanner`, `summonConclusion()`, `#hallStatusBadge` | dissent preserved, never averaged away |
| P4-04 Compare criteria and results | Model selection with stated criteria, side-by-side results with per-model status and cost | `#pane-debate` (1558), `#debateModelChecks`, `#debateGrid` | select/deselect, running, partial failure, all-failed |
| P4-05 Measured performance and provenance | Latency, tokens and cost per model from the response, not from the client clock | `#debateSynthesisArea`, `runDebate()`, `syncDebateArbiter()` | figures traceable to the response payload |
| P4-06 Saved comparison and export continuity | Save, revisit and export a comparison without collapsing it into a single answer | `#d-synth`, synthesis area, session storage | reload restores the same comparison |

### P5 — consistency and accessibility

| Story | Change | Anchor | Proof |
|---|---|---|---|
| P5-01 Terminology and statuses | One vocabulary across panes (run/turn/round/stage; idle/queued/running/awaiting approval/…) and one status component | all six panes, `.badge` usages | a grep-level vocabulary test plus a manual tab walk |
| P5-02 Keyboard and screen reader | Every action reachable by keyboard, visible focus everywhere, landmarks, names on all controls, announcement budget per region | 12 `:focus-visible` rules, 58 controls without ids, `role="status"` regions | recorded keyboard walk; screen-reader pass recorded as performed or not performed |
| P5-03 Responsive and touch targets | 44 px targets on narrow viewports, no clipped composers, inspector reachable as a drawer, no full app zoom required | breakpoints at 1100/880/860/820/600/390, `.hall-composer`, `.skills-strip` | measurements captured at 1280/1024/768/390/320 in a real browser |
| P5-04 SSE performance and bounded DOM | Batch token rendering, cap history, no reflow per token, bounded memory during long streams | `consumeSSE()` (2736), `ensureLiveStepCard()`, telemetry renderer | node-count and frame measurements recorded per pane |
| P5-05 Visual and interaction polish | Spacing, alignment, icon weight, motion timing, hover/focus parity across panes | token layer + components from P1 | before/after screenshot pairs per pane |

### P6 — regression, review and release

| Story | Change | Anchor | Proof |
|---|---|---|---|
| P6-01 Feature preservation E2E | Automate the preservation matrix: every control reachable before is reachable after | matrix + control inventory | automated journey per pane, run in CI |
| P6-02 Adversarial permission and recovery UI | Client cannot forge approval, admission or recovery state; refused actions show the server's refusal | approval/admission paths | negative tests, with the refusal message shown verbatim |
| P6-03 Full screenshot review | All 6 panes, both themes, five viewports, all named states | screenshot pipeline | manifest + validation green, review recorded |
| P6-04 README, docs, onboarding | Only verified behaviour described; quickstart matches the shipped UI | `README.md`, `docs/quickstart.md` | doc validation plus a first-run walk |
| P6-05 Release gate and handover | Gate record, known limitations, handover | `release_gates` | owner-signed gate |

## 5. Evidence and visual capture limitations

Every story runs the sequence in
[`uiux-2-0-program.md`](uiux-2-0-program.md#evidence-a-ui-story-has-to-produce)
and records exit codes. Carry forward these environment and safety facts:

* **Browser availability is session-specific.** Capture needs a Chrome/Chromium
  binary and `puppeteer-core`; neither is vendored. On a machine without them,
  record visual evidence as `blocked` rather than treating it as complete.
* **`--dry-run` is not a browser test.** It validates the capture-manifest shape
  and prints the plan; it does not verify routes, token injection or appearance.
  An unmodified real capture timed out on authentication prompts in a later
  session; see the [handoff](uiux-2-0-coordination.md#safety-and-capture-risks).
* **The demo store is not fully isolated.** Until
  [issue #108](https://github.com/pankajtiwarics27/m5-agent-studio/issues/108)
  is resolved, use a disposable source copy with its own empty harness store,
  not a real user's checkout, for captures.
* **Generated artifacts move with the source.** `studio.py`, the source manifest,
  the control inventory, the route map and the subsystem inventory are
  regenerated in the same commit as the change that moves them; CI fails on drift.

Baseline measured by the original planning session on 2026-10-31 (Python 3.11.2,
Linux sandbox; later results are in the [handoff](uiux-2-0-coordination.md)):

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | 1,519 passed, 1 skipped, 1 failed — the failure was `tests/test_baseline.py` on a shallow clone; it passes after `git fetch --unshallow` |
| `.venv/bin/python -m ruff check .` | passed |
| `.venv/bin/python -m mypy` | passed, 58 files |
| `.venv/bin/python scripts/validate_docs.py` | passed, 118 files scanned |
| `node scripts/capture_screenshots.mjs` | not run: no browser, no `puppeteer-core` |

## 6. Working in parallel on one HTML file

The repository has two autonomous programs and, at times, two sessions live. The
rules that keep them from destroying each other are in the protocol; this is the
operational version.

**What may run in parallel:** investigation, review, and tooling or artifact
work that does not touch `static/index.html` (a route map, a trace, an inventory,
a generator). **What may not:** two sessions editing the frontend, the same
generated artifact, or the same ledger block at the same time.

| Hotspot | Why it collides | Rule |
|---|---|---|
| `static/index.html` | single file, every UI story edits it | strictly one UI story in flight; rebase onto `main` immediately before claiming and again before pushing |
| `studio.py` | embeds the frontend; regenerated | never edited by hand; regenerate with `scripts/build_studio.py` |
| `product/M5_2_0_UIUX_Source_Manifest.json` | pins `static/index.html`, `app.py`, `studio.py` and the docs the program owns | regenerate with `scripts/uiux_source_manifest.py`; a story that changes a pinned file regenerates it in the same commit |
| `product/M5_2_0_UIUX_Control_Inventory.json`, `docs/uiux-2-0-control-inventory.md` | regenerate from markup | regenerate, never patch |
| `product/M5_2_0_UIUX_Route_Map.json`, `docs/uiux-2-0-route-map.md` (P0-03) | regenerate from the script | regenerate, never patch |
| `product/M5_2_0_Autonomous_Roadmap.json` | the runtime program's ledger | a UI/UX session never edits its story tracking |
| `AGENTS.md`, `docs/README.md`, `M5_2_0_UIUX_AGENT_PROMPT.md` | both sessions add a row or a rule | small additive edits; expect a trivial rebase, never rewrite the other session's line |
| `product/M5_2_0_UIUX_Autonomous_Roadmap.json` | both sessions write `session_state` and their own story's `tracking` | write only your story's `tracking`; reconcile `session_state` by hand, keeping both sides |

**Claim discipline.** Commit and push claim metadata before implementation.
The ledger on `main` records merged claims, but a claim pushed only to a session
branch is not yet visible there. Open a draft PR with the story ID immediately
after the claim push, recheck open PRs before coding, and coordinate any
collision. A reservation for a dependency-blocked story communicates intent but
is not a ledger claim. Neither a reservation nor the advisory
[readiness helper](uiux-2-0-coordination.md) is an atomic lock; unannounced
branch-only claims can still race.

**State as of this commit (2026-10-31):**

| Item | State |
|---|---|
| `UIUX-P0-01`, `P0-02` | done on `main` |
| `UIUX-P0-03` | done on `main` via PR #109; the false-negative callers in [#110](https://github.com/pankajtiwarics27/m5-agent-studio/issues/110) were fixed in #115, and #114 replaced the map with a lexical scan that publishes unresolved edges as `unknown` rather than absent |
| `UIUX-P0-04` | done on `main` via [PR #111](https://github.com/pankajtiwarics27/m5-agent-studio/pull/111); regenerated on the #114 map, so it inherits the map's coverage limits rather than claiming more than the scan proves |
| `UIUX-P0-05` | done on `main` (#118, merge `f432fe6`): suite results and **6 of 6 screenshots**, captured from a disposable archive copy with a Chromium build distributed on npm; the runbook and the answered capture hold are in [`uiux-2-0-baseline-evidence.md`](uiux-2-0-baseline-evidence.md) |
| `UIUX-P0-06` | historical owner signature recorded on 2026-10-31 in [`uiux-2-0-p0-signoff.md`](uiux-2-0-p0-signoff.md), but it has no bound input hashes ([#126](https://github.com/pankajtiwarics27/m5-agent-studio/issues/126)); the ledger marks P0 complete while the screenshot provenance hold [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122) remains open |
| Review duty | the non-author reviews every story PR; findings on #109 and #111 were posted as comments, not formal GitHub reviews, and #110 stays open until the forwarded calls are traced |

**Merge queue.** One story per pull request, reviewed by the other session,
green on all three CI legs, merged with `gh pr merge <number> --merge` (merge
commit, never squash or rebase, never `--auto`), then the next claim is made
against the updated `main`.

## 7. Risks and decisions this plan cannot make alone

| Risk | Effect | Mitigation in this plan |
|---|---|---|
| Single-file frontend | every UI story conflicts with every other | serialize UI stories; keep investigation and generated tooling parallel; rebase before claim and before push |
| Capture prerequisites or isolation are unresolved | no accepted screenshot evidence, so P0-05 and P6-03 cannot be closed | record `blocked`, use `--dry-run` only for manifest/plan checks, fix auth setup and isolate the demo store before capture |
| Ledger content is generated boilerplate | an implementer can satisfy the five criteria without changing the experience | this page's per-story change shape, plus the preservation matrix as the real check |
| Claim drift | two sessions can start the same story | claim commit first, announce via draft PR and recheck; reservations are not locks |
| Legacy CSS layer still live | a token migration can regress a pane that was not opened | convert by rule with a rendered check per pane; never delete a legacy rule before its replacement is proven |
| Accessibility debt | 58 markup controls have no id, so tests cannot reach them | add stable hooks in P1-03/P5-02 rather than in every story |

Decisions that need the owner, not an agent:

1. **Browser in the loop.** Arrange a safe, reproducible capture environment and
   review the required shots. If prerequisites remain missing, record the blocker;
   a blocked capture is not evidence for closing P0-05 or the release gate.
2. **Parallelism after P1.** The ledger's dependency chain is strictly linear. If
   phases may overlap after the shell lands, the dependency edges need a
   deliberate edit by the owner, not a session quietly starting early.
3. **Density default and theme default.** Both are user preferences today;
   changing the defaults is a product decision.

## 8. Running one story

1. Read `AGENTS.md`, the ledger's `session_protocol`, this page's row for the
   story, then the real DOM, handler and route.
2. Claim: `tracking.status = in_progress`, `progress_percent` in 1–99,
   `owner_session`, `started_at`, `updated_at`; reconcile the counters and
   `session_state`. Push before any code, immediately open a draft PR with the
   story ID and recheck for competing claims.
3. Re-pin: confirm `HEAD`, update the ledger pin and regenerate the source
   manifest if a pinned file moved.
4. Change the smallest thing that satisfies the row, in the existing CSS layer
   and with existing ids and handlers.
5. Exercise the states the row names — empty, loading, streaming, success,
   failure, recovery, narrow viewport, both themes.
6. Run the verification sequence; capture or record `blocked`; regenerate
   artifacts; update the preservation-matrix row and the screenshot manifest.
7. Update the draft pull request with the acceptance-criterion-to-evidence map,
   and ask the other session for review.

## 9. What this page does not claim

This plan does not itself ship an interface change or change a story status.
Exploratory captures described in the [handoff](uiux-2-0-coordination.md) are not
an accepted baseline. The plan predicts where the work goes; the ledger remains
the record of what has actually been done, and `docs/ui-audit.md` remains the
record of what the interface looked like before the program touched it.
