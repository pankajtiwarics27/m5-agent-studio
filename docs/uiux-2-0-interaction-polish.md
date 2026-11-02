# UIUX-P5-05 supplemental evidence — shared-toolbar focus parity

## Context and scope

PR [#168](https://github.com/pankajtiwarics27/m5-agent-studio/pull/168) merged UIUX-P5-05 at commit `8ea7e9a`; the roadmap already records P5 as complete. This page is a supplemental implementation and evidence note on top of that merged work. It does **not** reopen the story, change its tracker status, replace its generated polish inventory, or make an approval or independent-review claim.

The merged P5-05 work added the shared interaction layer. Browser measurement on that merged baseline found one remaining shared-toolbar mismatch: pointer hover and keyboard focus painted different backgrounds and borders for the neutral Search, Density, and Theme actions. The only visual rule added by this follow-up is `.top-actions .btn:focus-visible:not(:disabled)`, paired with the existing button hover declarations. It keeps the global 2px focus ring. This follow-up did not change `.mode-tab` or pane-control styling.

A separate keyboard-interaction gap was found in Compare history: Find and full-export temporarily disabled their focused buttons, sending focus to `<body>`. After each async request, focus now returns only when the saved action had `:focus-visible` and focus is still on `<body>`; it is not taken from a user who moved elsewhere. This is a keyboard-continuity fix, not part of the toolbar screenshot comparison. No route, API, persistence schema, authorization, or model-call behavior changed.

## Measured interaction evidence

The browser walk visits Start, Harness, Council, Pipeline, Compare, and Chat; checks a real pointer hover against keyboard focus on the shared Theme action; verifies `:focus-visible` and the existing 2px ring; and captures the resulting pane state. Its baseline was captured from the merged UI without this follow-up's toolbar focus selector. The after state was captured from the same merged UI with that selector applied.

| State | Dark desktop | Light desktop | Dark narrow (390×844) |
|---|---:|---:|---:|
| Before: hover/focus surface mismatches | 6/6 panes | 6/6 panes | 6/6 panes |
| After: hover/focus surface matches, ring visible | 6/6 panes | 6/6 panes | 6/6 panes |

The measured before/after surfaces were:

| Theme | Hover background / border | Focus before / after |
|---|---|---|
| Dark | `rgb(41, 46, 51)` / `rgb(82, 90, 98)` | Before `rgb(27, 30, 33)` / `rgb(52, 57, 62)`; after matches hover |
| Light | `rgb(239, 240, 237)` / `rgb(183, 188, 183)` | Before `rgb(255, 255, 255)` / `rgb(217, 220, 216)`; after matches hover |

The 2px focus ring remained visible in all six panes in both themes (`rgb(168, 180, 255)` dark; `rgb(72, 88, 200)` light). The browser captures and indexes are under `docs/media/uiux-p5-05/`:

- `before-dark/`, `before-light/`, `before-narrow/` and `after-dark/`, `after-light/`, `after-narrow/`: six standard pane screenshots per set, covering dark/light desktop and a 390×844 narrow viewport.
- `before-focus-dark/`, `before-focus-light/`, `before-focus-narrow/` and `after-focus-dark/`, `after-focus-light/`, `after-focus-narrow/`: six interaction-state screenshots per set, with computed hover/focus measurements in each `index.json`.
- `scripts/validate_uiux_interaction_polish_screenshots.py --check` verifies all **72 PNGs in 12 sets**, their dimensions, pane order, byte counts, and SHA-256 digests.

All captures used the seeded isolated demo with a generated throwaway token and temporary store. The walk makes no model call, creates no real session, and changes no workspace setting.

## Verification

- `scripts/uiux_interaction_polish_walk.mjs`: 6/6 panes pass in dark desktop, light desktop, and dark 390×844 after state; the same six panes show the measured mismatch in each before state.
- Browser-enabled suite: `tests/test_screenshot_capture_browser.py`, `tests/test_compare_history.py`, and `tests/test_uiux_interaction_polish.py` — **42 passed**. The Compare-history browser walk includes keyboard activation through async Find and verifies focus remains on the Find action.
- Focused roadmap, sign-off, interaction-polish, and Compare-history tests — **63 passed**; preservation sign-off generation and `--check` are current.
- Screenshot evidence validator — **72 PNGs verified**.

## Limits

This addendum measures the shared-toolbar hover/focus surface and keyboard-focus continuity for two existing Compare-history actions. It is not a screen-reader evaluation or a full contrast certification. The image evidence documents the visual toolbar refinement; the browser regression documents history focus restoration. They are distinct results and should not be conflated.
