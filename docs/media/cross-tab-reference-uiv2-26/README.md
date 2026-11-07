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

## Approved integrated candidate and what was opened

The approved UIV2-03 reference is a structural contract, not a pixel-clone target. Authored D1–D3
annotations explain the on-demand integrations, explicit Inspector, and each pane's intentional
feature-preserving difference. The candidate incorporates the owner-requested task-first layout
integrated from `origin/main` `1b940bc`.

The final portable candidate is bound to clean HEAD `fd8b8560597de8eb7a91b9ba8014ff9cc4e92aa3`, candidate
manifest SHA-256 `a81c6a5c71fc1cb088163dc649718581f4df09e3ab29c84b0af3010c182d9798`, and explicit
human approval `user:Arena ask_user uiv2-26-latest-main-approval=approve, 2026-11-07`. It has 48/48
captures and **2064/2064 checks**. A second independent candidate matched all 48 live and all 48
reference structural signatures exactly. The subsequent approved `--check` exited 0 with
**2160/2160 checks**. Exact signatures, image digests, and the approval reference are committed in
`product/M5_2_0_UIUX_V2_Cross_Tab_Approval.json`; the candidate PNGs/manifests stay local.

The `21198cd2…` task-first approval was superseded when CI exposed Debian/Ubuntu system-font
geometry drift. The subsequent `d1065e21…` pinned-font approval was superseded when the latest
main refined the task composer and integrations disclosure. The walk now restricts both browser pages to the exact Open Sans regular/bold/italic
files shipped by the pinned Chromium package and records their SHA-256 digests; no geometry tolerance
was added. Diagnostic CI values matched the pinned-font local candidate before the later task-control merge. The earlier
`efbe1c23…` approval at `cafdd08` was likewise superseded after the task-first merge, not reused.

The first integrated candidate exposed the old persistent-Inspector/integrations and
Harness-sidebar assumptions; the contract now positively asserts closed secondary chrome, visible
unified task controls, retained but collapsed empty stages, shared desktop navigation/sessions, and
a closed narrow-screen Sessions panel. Screenshot review also caught D1 outlining closed integrations
content, so the final annotation outlines the visible Tools disclosure instead.

Agent-opened comparisons (not a substitute for the human decision):

- `candidate/light/1440x900/harness/comparison.png`: one left navigation/session surface, a short
  greeting, and one unified composer with model, policy, workflow and Run; secondary chrome is closed.
- `candidate/light/390x844/harness/comparison.png`: usable narrow composer, closed Sessions/Tools/
  Inspector, disabled empty Run, retained stage DOM, and no horizontal overflow.
- `candidate/dark/1640x900/start/comparison.png`: task and one blocker first; recent work and
  diagnostics remain below the primary action.
- `candidate/dark/1440x900/hall/comparison.png`: session/transcript hierarchy with readiness evidence
  and no invented conclusion.
- `candidate/dark/1280x800/council/comparison.png`: task, All stages plus four real stages, and
  factual empty output.
- `candidate/light/1640x900/debate/comparison.png`: question first, explicit models, folded criteria.
- `candidate/dark/390x844/chat/comparison.png`: Persona and Reasoning share one usable second row.

The earlier mobile review exposed a real 20px Chat select/label overlap; the final CSS correction is
covered by non-overlap and >=44px select-width assertions in both themes. A previous approved check
also exposed a 51px Council transcript shift caused by capturing two readiness states. The walk waits
for the seeded demo's settled readiness verdict and uses the canonical 1200ms settle interval; the
assertion was not weakened or retried until green.
