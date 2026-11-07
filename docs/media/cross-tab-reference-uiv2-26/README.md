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

## Integration recapture required

The candidate and human decision below are **historical**, bound to clean HEAD `cafdd08`. After that
review, `origin/main` `1b940bc` added the owner-requested task-first Harness/shared-shell layout.
The old approval has therefore been removed from the current contract rather than reused. A fresh
48-capture candidate and explicit human approval are required on the clean integration HEAD.

## Superseded pre-integration candidate and what was opened

The approved UIV2-03 reference is a structural contract, not a pixel-clone target. Authored D1–D3
annotations explain the wrapping integrations band, preserved global controls, and each pane's
intentional feature-preserving difference.

The final candidate is bound to clean HEAD `cafdd08242f095c546a8e5546f80cbfc4791c343`, candidate
manifest SHA-256 `efbe1c23dcba32ceec6618db271e605eaf2532f76b13d1c4d3aa099a7c38191b`, and explicit
human approval `user:Arena ask_user uiv2-26-settled-approval=approve, 2026-11-07`. It has 48/48
captures and 1904/1904 checks. A second independent candidate matched all 48 live and all 48
reference structural signatures exactly. The subsequent approved `--check` exited 0 with
**2000/2000 checks**. Exact signatures, image digests, and the approval reference are committed in
`product/M5_2_0_UIUX_V2_Cross_Tab_Approval.json`; the candidate PNGs/manifests stay local.

Agent-opened comparisons (not a substitute for the human decision):

- `candidate/light/1440x900/start/comparison.png`: task and one blocker first; recent work and
  diagnostics remain below the primary action.
- `candidate/light/390x844/harness/comparison.png`: wrapping integrations, real empty session/model
  controls, and no horizontal overflow.
- `candidate/dark/1440x900/hall/comparison.png`: session/transcript hierarchy with readiness evidence
  and no invented conclusion.
- `candidate/dark/1280x800/council/comparison.png`: task, All stages plus four real stages, and
  factual empty output.
- `candidate/light/1640x900/debate/comparison.png`: question first, explicit models, folded criteria.
- `candidate/dark/390x844/chat/comparison.png`: Persona and Reasoning share one usable second row.
  An earlier frame exposed a real 20px select/label overlap; the final CSS correction is covered by
  non-overlap and >=44px select-width assertions in both themes.

The first approved check also exposed a 51px Council transcript shift caused by capturing two
readiness states. The final walk waits for the seeded demo's settled readiness verdict and uses the
canonical 1200ms settle interval. The superseding candidate received its own explicit approval;
the earlier hash was not silently reused.
