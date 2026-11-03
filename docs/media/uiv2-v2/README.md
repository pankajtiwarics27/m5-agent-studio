# UIV2 (visual-fidelity V2) capture sets

Two capture sets produced for the V2 visual-fidelity program
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

Each capture directory carries an `index.json` with sha256, byte size, viewport
and theme per shot.
