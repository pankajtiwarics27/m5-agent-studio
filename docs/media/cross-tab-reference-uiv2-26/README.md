# UIV2-26 — local cross-tab visual evidence

This directory is a reproduction location and local-evidence policy, not a request to commit
screenshots. `.gitignore` excludes every PNG and candidate/check manifest below it. The committed
contract is `product/M5_2_0_UIUX_V2_Cross_Tab_Reference.json`; its readable review and approval
status are in `docs/uiv2-v2-cross-tab-reference.md`.

## Producers

- `scripts/uiux_v2_cross_tab_reference_walk.mjs` starts an isolated seeded demo, authenticates the
  real browser session, captures all six tabs at four viewports in light and dark, renders the
  approved wireframe at the same variant, and writes an annotated side-by-side comparison.
- Each of the 48 matrix rows produces `actual.png`, `reference.png`, and `comparison.png`. Every
  image is SHA-256 hashed. Pixel RGB delta and changed-pixel ratio are measurements only; no
  blanket image tolerance decides the gate.
- Every row independently checks live and reference DOM structure, responsive regions, palette,
  tab selection, pane order/counts, overflow, and browser safety. Tabs are activated with keyboard
  Enter, not only programmatic state changes.
- `--check` requires a separate explicit human-approved structural baseline and fails closed before
  starting Chromium if none exists. Candidate generation never creates that approval.

## Reproduce

```bash
node scripts/uiux_v2_cross_tab_reference_walk.mjs \
  --candidate --out docs/media/cross-tab-reference-uiv2-26/candidate
python3 scripts/uiux_v2_cross_tab_reference.py \
  --validate-candidate docs/media/cross-tab-reference-uiv2-26/candidate/candidate-manifest.json

# Only after explicit human approval has been recorded:
node scripts/uiux_v2_cross_tab_reference_walk.mjs \
  --check --out docs/media/cross-tab-reference-uiv2-26/check
python3 scripts/uiux_v2_cross_tab_reference.py --check
```

Set `STUDIO_SCREENSHOT_CHROME`, `STUDIO_PUPPETEER_CORE`, `STUDIO_SCREENSHOT_CHROME_ARGS`,
`LD_LIBRARY_PATH`, and `STUDIO_PYTHON` when the pinned browser or Python environment is not on the
default path. The walk cleans up its demo process and temporary workspace.

## Review status

The approved UIV2-03 reference is a structural contract, not a pixel-clone target. Authored D1–D3
annotations explain the integrations band, preserved global controls, and each pane's intentional
feature-preserving difference. Live structural-fidelity approval remains pending until a human
reviews the 48 annotated candidate comparisons; passing candidate checks are not approval.
