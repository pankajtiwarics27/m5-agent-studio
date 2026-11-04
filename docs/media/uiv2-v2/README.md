# UIV2 (visual-fidelity V2) capture sets

Six capture sets produced for the V2 visual-fidelity program
(`product/M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json`):

- `baseline/` — 53 before-screenshots of the real app at pinned HEAD
  `9a584a36f1fbe29ba467562dd12a705626769f00` (UIV2-01). Produced by
  `node scripts/capture_screenshots.mjs --manifest docs/media/uiv2-v2/baseline/manifest.json`
  against a disposable copy served by `python3 scripts/demo_tour.py --serve`
  (six tabs × 1640x900/1440x900/1280x800/390x844 × light/dark), plus the MCP hub
  overlay (`overlay/<set>/mcp-hub.png`) opened through the real header control by
  a small driver in the session workspace. Isolation per issues #108/#117: never
  captured from a working checkout. Pane file names follow the published
  convention (`pipeline.png` is the `council` tab, `compare.png` is `debate`).
  Evidence and recipe: `docs/uiv2-v2-baseline-evidence.md`.
- `reference/` — 36 rendered fixtures of the encoded approved reference
  (UIV2-03), produced by `node scripts/uiux_v2_reference_capture.mjs` from
  `docs/media/uiv2-v2/reference/wireframe.html` (six panes × 1640x900/1440x900
  light+dark, 1280x800, 390x844 light). Spec and approval gate:
  `docs/uiv2-v2-reference.md`.
- `before-uiv2-05/` — 20 before-screenshots of the real app at HEAD `c56c65a`
  (UIV2-05 baseline: harness pane at 1640x900/1280x800/390x844 in light and dark,
  plus all six panes at 1440x900 in light and dark), same recipe and isolation as
  `baseline/`. Shell metrics measured on the running app at the same commit are in
  `uiv2-05-shell-metrics-before.json` (header 108px at 390x844, skills strip up to
  227px tall, inspector a permanent 320px column at 1280).
- `after-uiv2-05/` — 22 after-screenshots of the real app at the UIV2-05 commit
  (same matrix as `before-uiv2-05/`, plus the inspector drawer open at 1280x800
  dark and 390x844 light), same recipe and isolation. Measured shell metrics:
  `uiv2-05-shell-metrics-after.json` (header 56px at every width, nav 200px at
  >=1280 and a 64px icon rail at 800-1279, inspector 320px column at >=1440 and a
  320px drawer below that, central pane >= 736px, no horizontal overflow at any
  viewport, all six tabs visible at every width, skills strip one 42-47px row).
- `before-uiv2-06/` — 3 before-screenshots of the real app at `dee72ff` (UIV2-05
  tree): harness at 1440x900 light/dark and 390x844 dark with the permanent
  telemetry rail still expanded.
- `after-uiv2-06/` — 10 after-screenshots of the real app at the UIV2-06 commit:
  all six panes at 1440x900 dark (per-pane inspector tabs), harness at 1440x900
  light and 390x844 dark, plus the header Telemetry popover open at 1440x900 in
  light and dark (all four telemetry cards, model load/unload actions included).
- `after-uiv2-04/` — 19 after-screenshots of the real app at the UIV2-04 commit
  (approved V2 palette applied): six tabs at 1440x900 light, 1440x900 dark and
  390x844 light, plus the MCP hub overlay at 1440x900 light, produced by
  `node scripts/capture_screenshots.mjs --manifest <six-pane manifest>`
  against a disposable copy served by `python3 scripts/demo_tour.py --serve`
  (same isolation as `baseline/`). Compare with `baseline/` for the
  before/after of the token change.

- `before-uiv2-07/` — 18 before-screenshots of the real app at `995606c` (six panes
  at 1440x900 light, 1440x900 dark and 390x844 dark), captured with the same recipe
  as `baseline/` against a disposable `demo_tour.py --serve` copy. Audit at that
  commit: body 14px, page titles <=18px (no page title element), standard controls
  13-38px tall, five visible primaries on the Start pane, space scale 4/6/8/12/16px.
- `after-uiv2-07/` — 18 after-screenshots of the real app at the UIV2-07 commit (same
  matrix). Audit after: body 14px, one 22px page title per pane, space scale
  8/16/24/32/40px, --control-min-h 44px with text inputs/selects/composers on the
  same token, exactly one page-level primary per pane at 44px (only 24px chips and
  badges remain below 44px, the documented --target-min affordances), accent used
  as text measured 7.0-7.7:1 (dark) and 4.8-5.1:1 (light) against every surface.

Each capture directory carries an `index.json` with sha256, byte size, viewport
and theme per shot.
