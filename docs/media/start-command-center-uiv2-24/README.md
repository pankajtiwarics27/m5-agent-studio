# Start: the command center (UIV2-24)

Seven states of the Start pane, on four viewports, in both themes — 56 captures taken
from the real app by `scripts/start_command_center_walk.mjs`, beside the three the same
pane gave before the story (`before-uiv2-24/`, served from `ff14655`).

## How these were taken

```bash
STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
STUDIO_PUPPETEER_CORE=/path/to/puppeteer-core.js \
STUDIO_PYTHON=python3 \
  node scripts/start_command_center_walk.mjs --out docs/media/start-command-center-uiv2-24/after
```

The walk starts its own fixture, so nothing here depends on a machine being ready:

- a stand-in runtime the walk owns, whose `/v1/models` it can turn from two models to
  none while the socket stays up — the only way to photograph a port that answers with
  no model behind it;
- `scripts/council_readiness_seed.py --runtime-url`, which brings up a seeded app
  pointed at that runtime;
- state set through the app's own APIs: sessions created and deleted through
  `/api/harness/sessions`, the workspace through `/api/workspace/directory`.

Each run asserts before it shoots (`31/31` on all eight runs). A state that failed its
assertion would have no capture, because the walk exits non-zero.

The before set is the same pane from `origin/main` at `ff14655`, captured with the
repository's own `scripts/capture_screenshots.mjs --panes start`:

```bash
node scripts/capture_screenshots.mjs --panes start --emulate-theme light --out before-uiv2-24
node scripts/capture_screenshots.mjs --panes start --emulate-theme dark  --out before-uiv2-24/dark
node scripts/capture_screenshots.mjs --panes start --emulate-theme light --viewport 390x844 --out before-uiv2-24/narrow
```

## What each state shows

| State | What the pane is doing |
|---|---|
| `empty` | Nothing configured and no recent work: the verdict says what is needed, the composer is still first and usable |
| `populated` | A probed runtime with two models, five sessions, a set workspace: `Ready to run`, and "Nothing is in the way" with the way to the evidence |
| `long-content` | A long draft, long session titles and a long workspace path: the draft keeps the fold, the titles clip with an ellipsis, nothing scrolls sideways |
| `loading` | The readiness read is in flight: the card says it is reading rather than showing a verdict it does not have |
| `error` | The read was refused: the verdict line says so, the retry is in the blocker slot above the fold, and the error record is one click below |
| `recovery` | Re-check pressed from the blocker: the error clears, the verdict comes back, focus lands on the verdict line instead of `<body>` |
| `contradiction` | The runtime's port answers and reports zero models: `Checks disagree`, the basis reads `runtime answered, 0 models`, and the contradiction prints its evidence and an action |

## Reading the before set

`before-uiv2-24/start.png` is the pane the story replaces: `GUIDED FIRST RUN` first, six
rows of checklist under it, and the task composer — the thing the pane exists for —
below the fold at 1440x900. `dark/start.png` is the same state in the dark theme and
`narrow/start.png` at 390x844, where the checklist takes the whole first screen.

The same seven states are captured at 1640x900, 1440x900, 1280x800 and 390x844, in
`light/` and `dark/`, as `<state>-<viewport>.png`.
