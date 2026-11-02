# Compare provenance captures (UIUX-P4-05)

Nothing here was drawn by hand. Every file is a screenshot `scripts/compare_provenance_walk.mjs` wrote
while driving the real Compare pane against `scripts/council_readiness_seed.py`.

```bash
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js
export STUDIO_PYTHON=.venv/bin/python
export LD_LIBRARY_PATH=/path/to/chromium-libs/lib

node scripts/compare_provenance_walk.mjs --screenshots docs/media/compare-provenance
```

The walk starts four fixtures: one plain (a local model answers, the saved keyless alias fails), one
with `--resident-model <the fits model>` (two resident models, so two panels answer and an arbiter
verdict exists to measure), one with `--slow-model` (slow enough to stop mid-panel) and one
`--offline` (the server writes the demo text).

The point of the walk is not the picture: it installs a `fetch` tee before the page runs, parses the
same SSE frames the app parses, and compares each cell with the `usage` the server sent for that
reply. A figure computed from the browser's clock could not match a server figure to two decimals.

| File | Size | What it shows |
| :--- | :--- | :--- |
| `compare-provenance.png` | 868 × 461 | one panel answered (0.25 s elapsed, 0.04 s to first token, 58 tokens, 41.2 tok/s) and one failed: five dashes and *"the provider failed before it reported any figure"*, with the footer saying the run's own total is not reported because the run did not finish. |
| `compare-provenance-arbiter.png` | 908 × 867 | two answering panels and an arbiter verdict: the card's badge (`41.2 tok/s · 0.26 s · 58 tokens`) and the arbiter's row state the same figures, and the footer gives the run's 3.83 s as the server measured it. |
| `compare-provenance-demo.png` | 908 × 752 | no runtime behind the app: the demo row is five dashes with *"demo text: the reply is the server's, so it carries no measured figure"* — the demo path's 29.5 / 42.1 tok/s appear nowhere. |
| `compare-provenance-light.png` | 868 × 272 | the same block and the same figures in the light theme (the walk asserts they are byte-identical to the dark ones). |
| `compare-provenance-narrow.png` | 480 × 900 | all six columns at a 480 px viewport, captured from the viewport rather than cropped from a wide one. |

The blocks are clips of the results table and the provenance block beneath it, because the model
checkboxes above them are rebuilt whenever telemetry refreshes and a crop that included them could
show a selection the run did not use.
