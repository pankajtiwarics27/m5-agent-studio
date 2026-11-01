# Compare criteria captures (UIUX-P4-04)

Nothing here was drawn by hand. Every file is a screenshot `scripts/compare_criteria_walk.mjs` wrote
while driving the real Compare pane against `scripts/council_readiness_seed.py`.

```bash
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js
export STUDIO_PYTHON=.venv/bin/python
export LD_LIBRARY_PATH=/path/to/chromium-libs/lib

node scripts/compare_criteria_walk.mjs --screenshots docs/media/compare-criteria
```

The walk starts four fixtures: one plain (a local model answers, and the cloud alias saved without a
key fails), one with `--slow-model` (a reply slow enough to stop mid-panel), one with `--cap-model`
(a reply the runtime cuts at its length limit) and one `--offline` (the server writes the demo text).
Those are the four endings the pane has to draw, and the fixture produces them through the app's own
paths, not by editing the page.

| File | Size | What it shows |
| :--- | :--- | :--- |
| `compare-criteria.png` | 840 × 392 | the criteria strip before any run: three criteria settled by the reply and the finish reason, `cost` with no price configured, and the reader's two criteria as theirs to judge. |
| `compare-partial-failure.png` | 908 × 473 | two panels, one `answered` and one `failed`: `2 panels · 1 answered · 1 failed`, the provider's message quoted on the card that failed with *"Nothing was saved for it."*, and the results table crediting the survivor with `answered, finished, size` and the failure with `—`. |
| `compare-stopped.png` | 908 × 333 | a comparison the reader stopped: `stopped by you`, the partial reply kept, and the criteria column reading `answered, size` — `finished` is not claimed for a reply that was interrupted. |
| `compare-offline.png` | 908 × 567 | no runtime behind the app: `demo text`, credited with a size and nothing else, because a reply the server wrote is not a model answering. |
| `compare-light.png` | 908 × 880 | the criteria, the run line and the results in the light theme. |
| `compare-criteria-narrow.png` | 480 × 900 | the same blocks at a 480 px viewport, captured from the viewport rather than cropped from a wide one. |

The captures are clips of the run as a reader meets it (run line, panels, results), because the
whole-pane crop spends half its frame on the pane's empty floor and the model checkboxes above it are
rebuilt whenever telemetry refreshes.
