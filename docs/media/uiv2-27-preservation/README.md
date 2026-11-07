# UIV2-27 — functional preservation and defect-closure evidence

This directory is a **local evidence policy and reproduction recipe**, not a request to commit
screenshots. Its `.gitignore` excludes every PNG and capture/check manifest. The images remain
available for review in the workspace without adding generated binaries to Git.

Every after-frame comes from a running throwaway demo app and the existing DOM, routes and browser
handlers. No screenshot was drawn, cropped to hide a defect, or substituted with mock markup. The
Compare and Council offline cases use the app's own demo protocol; the Council delete walk mocks one
503 refusal and then performs a real successful DELETE. No external model/provider call is needed.

## Baseline and closure

The first complete UIV2-27 run on merged `660b504c1e769928ab9702ce9790a3bb2f7c7747` proved only
**9/21 rows**, with seven failing steps. It was a defect-finding run, not release evidence. The final
complete clean integrated-commit run at `f90f943b95128be6dc58b8c5bc988a7794c4b03d` (implementation
`e11c902a…` plus `origin/main` `22a07ee`) passed **21/21 rows and 32/32 steps in 426.0 seconds**:
13 authenticated / anonymous route probes, 14 pytest nodes, and all 18 walks. Its committed run record
is `product/M5_2_0_UIUX_Preservation_Run.json`, SHA-256
`63cf8748acbad4772ae2e530da592bde0a89835f1c3f054bd38049c33ad8ddab`.
The executable map is **21 rows / 73 provers / 0 findings**.

The fixes are deliberately small:

- successful `council_done`, `debate_done` and `summon_done` terminal frames no longer fall into the
  dropped-stream path at normal EOF;
- a refused Council delete keeps the active id, transcript and session row; only a successful write
  clears them;
- the populated Harness trajectory has a 90 px floor in a short window instead of collapsing to
  80 px;
- Compare waits for actual debate finalization, including queued cards and demo-arbiter provenance;
- existing walks now operate the current native disclosures and six-decision Harness plan instead of
  focusing hidden controls or using the lead model as a supposedly different critic. Assertions for
  authorization, provenance, all six decisions, recovery, focus and scrolling were retained or
  strengthened.

## Visual proof

| Producer | Result | Local evidence |
| --- | --- | --- |
| `uiux_v2_cross_tab_reference_walk.mjs --check` | **48 captures, 2,200/2,200 checks**; six tabs × four viewports × light/dark, each with approved reference and annotated comparison | `approved-check/` (144 PNGs) |
| `council_state_walk.mjs`, eight variants | **23/23 per variant, 184/184 overall, 40 captures** | `council-state/` |
| `uiv2_preservation_visual_walk.mjs` | **72/72 checks, 16 captures**; long pinned conclusion and shared overlay at every required viewport/theme | `council-long/` |
| `harness_timeline_walk.mjs` | **21/21**; short-window trajectory is 90 px while the evidence strip keeps 203 px | `harness-timeline/` |
| `harness_settings_walk.mjs` | **25/25**; six decisions, real critic change, bounded complete handoff, nested keyboard disclosures and six-row narrow layout | `harness-settings/` |
| direct before/after verdict and timeline runs | successful pinned conclusion replaces the false dropped-stream outcome; 80 px trajectory becomes 90 px | `before-after/` |

The long-content desktop runs measured a 96 px conclusion body, 84 px of independent keyboard
scrolling, and a 15 px sticky banner/transcript offset at maximum transcript scroll. At 390×844 the
long transcript uses the narrow page's own reachable flow: no horizontal overflow, the pinned card
and composer remain reachable, and Expand/Escape preserve complete content and trigger focus.

### Frames opened and reviewed

- `before-after/harness-short-window-before.png` and `harness-short-window-after.png`: the after-frame
  retains a visible 90 px trajectory between the 203 px evidence strip and composer.
- `before-after/council-pinned-before.png` and `council-pinned-after.png`: the former no-decision /
  duplicate-reply outcome is replaced by the actual pinned Lead Arbiter conclusion after a terminal
  frame.
- `council-long/long-content-390x844-dark.png`: bounded conclusion, visible focus and transcript on
  the narrow screen without horizontal overflow.
- `council-long/conclusion-modal-1440x900-light.png`: complete long text in the existing overlay with
  a visible Close action.
- `council-state/lost-turn-1280x800-light.png`: the genuinely dropped frame remains explicitly
  unsaved, with the partial text and three recovery actions.
- `council-state/gone-session-390x844-dark.png`: a real external deletion retains the browser's
  non-authoritative transcript and offers a new session or list reload.
- `harness-settings/brief-from-chat.png`: the complete handoff remains in the bounded, scrollable
  composer, with model, permission, workflow, task settings and Run still visible.
- approved-check comparison frames for narrow Start and desktop Harness: global tools, Inspector,
  task controls and the approved structural assertions remain intact.

This was browser geometry, keyboard and image review. It was **not a screen-reader certification**.

## Reproduce

Use an editable install with the development dependencies, the pinned Chromium and Puppeteer Core,
and an isolated temporary directory. In this workspace the browser variables are:

```bash
export TMPDIR=/tmp/m5-browser/tmp
export STUDIO_SCREENSHOT_CHROME="$(cat /tmp/m5-browser/chrome-path)"
export STUDIO_PUPPETEER_CORE="$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"
export STUDIO_SCREENSHOT_CHROME_ARGS=--disable-gpu
export LD_LIBRARY_PATH=/tmp/m5-browser/libs/lib
export STUDIO_PYTHON="$PWD/.venv/bin/python"
export M5_REQUIRE_BROWSER_TESTS=1
```

Run the complete functional gate and regenerate its dependent artifacts in order:

```bash
.venv/bin/python scripts/preservation_suite.py \
  --save product/M5_2_0_UIUX_Preservation_Run.json
.venv/bin/python scripts/build_studio.py
.venv/bin/python scripts/uiux_source_manifest.py
.venv/bin/python scripts/uiux_v2_preservation_matrix.py
.venv/bin/python scripts/uiux_preservation_e2e.py
.venv/bin/python scripts/uiux_v2_cross_tab_reference.py
# Regenerate any downstream inventory reported by --check, in uiux_release_gate.py SOURCES order.
.venv/bin/python scripts/uiux_release_gate.py --check
```

Run the approved six-tab reference gate and its artifact checks:

```bash
node scripts/uiux_v2_cross_tab_reference_walk.mjs --check \
  --out docs/media/uiv2-27-preservation/approved-check
.venv/bin/python scripts/uiux_v2_cross_tab_reference.py --check
.venv/bin/python scripts/uiux_preservation_e2e.py --check
.venv/bin/python scripts/uiux_screenshot_review.py --check
```

For walks that need an authenticated server, start `scripts/demo_tour.py --serve` in a throwaway
store and pass its loopback URL and printed token. The preservation suite's `start_server` wrapper is
one way to do this. The exact matrix commands are:

```bash
for viewport in 1640x900 1440x900 1280x800 390x844; do
  for theme in light dark; do
    node scripts/council_state_walk.mjs --base-url "$BASE_URL" --token "$TOKEN" \
      --viewport "$viewport" --theme "$theme" \
      --screenshots docs/media/uiv2-27-preservation/council-state || exit
  done
done

node scripts/uiv2_preservation_visual_walk.mjs --base-url "$BASE_URL" --token "$TOKEN" \
  --out docs/media/uiv2-27-preservation/council-long
node scripts/harness_timeline_walk.mjs --base-url "$BASE_URL" --token "$TOKEN" \
  --screenshots docs/media/uiv2-27-preservation/harness-timeline
node scripts/harness_settings_walk.mjs --base-url "$BASE_URL" --token "$TOKEN" \
  --screenshots docs/media/uiv2-27-preservation/harness-settings
```

`docs/media/uiv2-27-preservation/.gitignore` must remain in place. Do not commit the PNGs, candidate
manifests, check manifests or capture manifests as release inputs.
