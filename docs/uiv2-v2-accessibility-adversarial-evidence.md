# UIV2-28 — measured accessibility and preservation evidence

This page records executed results for **UIV2-28**. The executable coverage contract is in
[the adversarial contract](uiv2-v2-accessibility-adversarial.md). PNGs, logs and run manifests are
source-hashed local evidence under `docs/media/uiv2-28-accessibility/`; that directory is
Git-ignored so captures cannot become unreviewed release inputs.

## Baseline and final results

Base: `adbce75385e5e5ce359aaace65501ed6b27fa0ef` (the merged UIV2-27 head).

| Gate | Reproduced baseline | Final executed result |
| --- | --- | --- |
| Focused pre-change unit/DOM tests | 79 passed | See the final focused/full-suite results in the story ledger |
| Existing keyboard walk | 12/15 | **15/15**, five captures |
| Existing responsive walk | 16/27 | **27/27**, six captures |
| Computed contrast probe | Six dark brand-chip readings at **2.41:1** | All rendered contrast assertions in the adversarial gate pass |
| New accessibility adversarial walk | Product defects reproduced during 85/116 → 87/116 → 102/116 → 112/116 → 114/116 diagnostic runs | **120/120 checks, 62 captures**, including four stronger nested-scroll/modal-end checks |
| Approved-target independent candidate | Not applicable | **48/48 variants, 2104/2104 checks, 144 PNGs** at clean HEAD `c674c3b` |
| Human-approved structural check | Previous signature rejected the intentional 18→24px target change | **48/48 variants, 2200/2200 checks**, explicitly approved clean-HEAD baseline |
| Council state matrix | Existing UIV2-27 gate | **184/184 checks, 40 captures**, eight viewport/theme variants |
| Long-conclusion visual matrix | Existing UIV2-27 gate | **72/72 checks, 16 captures**, eight variants |
| Complete control/API preservation suite | UIV2-27 was 21/21 | **21/21 rows, 32/32 steps, 0 failures in 446.0s** |
| Integrated PR CI | First run exposed two stale inventories on Python 3.11/3.12/3.13 | Runs **42415167284** and **42415162547**: all four jobs passed on `a5817f9` |

The legacy baseline totals include test-instrumentation defects as well as product defects; they
are not represented as eleven separate UI bugs. The walk repairs retain real Tab/Enter/Space
interaction, visible-focus assertions, offscreen/target measurements, touch reachability and the
horizontal-scroller exception report. Closed native `<details>` descendants are excluded only
when they are not rendered or tabbable.

## Acceptance evidence

- **No unintended focus trap:** all 12 normal-size pane/theme rows are traversed with real Tab
  presses. Tab must leave the pane and a repeated focus key fails. Pipeline is synchronized to a
  real selected stage so asynchronous overview mounting cannot replace the ledger under the test.
- **Intentional modal containment:** eight Tab presses remain inside the route-created long
  Council conclusion dialog at desktop and phone 200%.
- **Escape restores focus and scroll:** both recovery rows start from outer=96px and pinned
  conclusion=72px. The active outer host is `hallStream` at desktop 200% and `.hall-main` at phone
  200%; the dialog scrolls to its terminal marker without moving either underlying region, then
  Escape restores the Expand trigger and every original offset exactly. The separate 72-check
  matrix also proves independent conclusion scrolling, keyboard resizing and focus restoration.
- **200% zoom:** all 48 viewport/theme/pane rows use half the CSS viewport with device scale
  factor two. Unreachable clipping, targets below 24 CSS px and document overflow fail.
- **Measured contrast:** computed CSS Color 4 colors are composited over solid ancestors and
  evaluated with WCAG relative luminance (4.5:1 normal text, 3:1 large text). Gradients/images are
  failures, not silently inferred backgrounds.
- **Screen-reader-facing semantics:** accessible names and positive `tabindex` are checked on the
  real DOM. This is not represented as NVDA, JAWS or VoiceOver certification.
- **Reduced motion:** computed animation, transition and smooth-scroll behavior is checked under
  `prefers-reduced-motion: reduce`.
- **Real states and APIs:** the fixture creates/closes/rereads a Council session through its HTTP
  routes. Anonymous access is required to fail and bearer access to succeed. Unexpected outbound
  requests and browser page errors fail the run.

## Before/after and visual evidence

- Original keyboard baseline: `docs/media/uiv2-28-accessibility/before-keyboard/` and
  `before-a11y.log`; final: `after-keyboard/` and `after-a11y.log`.
- Original responsive baseline: `before-responsive/` and `before-responsive.log`; final:
  `after-responsive/` and `after-responsive.log`.
- Original computed contrast readings: `contrast-before.json`.
- Final integrated adversarial manifest: `after/capture-manifest.json` — 120/120, 62 captures,
  SHA-256 `741ad39309073084b082876cf6d012d04da124f268ba9f8b1aeb1f24fa305d94` at the time copied.
- Full actual/reference/annotated candidate: `cross-tab-candidate/` — 48 of each image kind;
  clean-HEAD `candidate-manifest.json` passed candidate validation with 2104/2104 checks and has
  SHA-256 `a1dd3b8767b8903a907561a07f6844c5523f1dd1bcbae3fee13f25906bd790d3`.
- Final integrated approved check: `approved-check/check-manifest.json` — 2200/2200 checks,
  SHA-256 `c1e276e6f54e7af998c05bb452f1b8cd96ebd4c2635588fc291ed2ed18ee3d8a`.
- Loading/error/recovery states: `council-state/` and `council-state/matrix.log` — 184/184.
- Long content and nested scrolling: `council-long/capture-manifest.json` — 72/72.
- Complete preservation output: `preservation.log` and the committed
  `product/M5_2_0_UIUX_Preservation_Run.json` (run SHA-256
  `512be9940f9cf2f4c1e4ce375241112a75d27cde678ea868df386f8022e8c0be`).

The candidate preserves the approved normal-phone Hall and Chat geometry. The intentional old-
signature difference is `#harnessProjectSummary`: its target changes from 18px to 24px high, with
the corresponding three-pixel y adjustment. Brand foreground contrast changes pixels but not
geometry. The previous approval was not silently reused: the user explicitly approved the reviewed
structure and then approved the exact clean-HEAD manifest through `ask_user`
(`uiv2-28-visual-approval` and `uiv2-28-clean-head-approval`, 2026-11-08). The binding and complete
approval record are in `product/M5_2_0_UIUX_V2_Cross_Tab_Approval.json`.

`origin/main` through `684c384` is integrated. Its native `.onboarding-shell` scrolling supersedes
this branch's initial outer-pane Start scroller, and its Studio-host path-picker behavior is retained
unchanged. The merged source repeats 120/120 adversarial, 15/15 keyboard, 27/27 responsive and
2200/2200 approved visual checks. The first PR run's Start and Pipeline inventory failures were
regenerated through their owning scripts; their three pytest modules then passed 87/87.

## Preservation and regeneration

No backend source, route, payload, session/workspace policy, authorization rule, sandbox, ledger,
lease fencing or model-memory admission code was changed. Regenerating the V2 matrix now maps
**272 controls and 121 mounted routes**; the additional 29 controls and three routes were already
present in the merged sources but absent from the stale 243-control/118-route artifact. This story
adds no markup control or backend route.

Reproduction commands:

```bash
node scripts/uiv2_accessibility_adversarial_walk.mjs --screenshots docs/media/uiv2-28-accessibility/after
node scripts/a11y_walk.mjs --screenshots docs/media/uiv2-28-accessibility/after-keyboard
node scripts/responsive_walk.mjs --screenshots docs/media/uiv2-28-accessibility/after-responsive
node scripts/uiux_v2_cross_tab_reference_walk.mjs --candidate \
  --out docs/media/uiv2-28-accessibility/cross-tab-candidate --timeout-ms 60000
python3 scripts/preservation_suite.py --save product/M5_2_0_UIUX_Preservation_Run.json
python3 scripts/uiv2_accessibility_adversarial.py --check
```

Use the locked browser environment from `tests/browser/` and the CI Chromium setup. The Council
state and long-content walks require an authenticated disposable fixture, as documented in their
script headers. No provider request or real user workspace is used.

## UIV2-29 source-refresh evidence

The UIV2-29 Harness snapshot/rendering optimization changed the frontend digest but no approved
structural signature. After integrating `origin/main` through `5e7c7ad`, clean implementation HEAD
`02fadd85b619259d4ed4e9170939a86582a2ee52` passed **120/120 checks with 62 captures**. Its capture
manifest is `/tmp/uiv2-29-integrated-accessibility/capture-manifest.json`, SHA-256
`0f9dcc2e51e055ec4037b0c09fccc904b7a2587ada1713ce1c64a8d3d92d8a8a`; the imported run and the
raw manifest both record `dirty=false`. The same integrated source passed **15/15 keyboard**,
**27/27 responsive**, and **2200/2200 approved cross-tab checks**. The final approved-check manifest
is `/tmp/uiv2-29-integrated-cross-tab/check-manifest.json`, SHA-256
`f4d47d490d18574bc5c3e45e05be0ec751fb57f0d61157311234c6796b9e34c1`. The explicit UIV2-28
approval therefore remains the structural baseline; no new visual decision was inferred or required.
