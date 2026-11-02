# Compare — the record bar (UIUX-P4-06)

Nothing here was drawn by hand. Every file comes from one command:

```
TMPDIR=/tmp/m5-runner/m5-browser/tmp \
STUDIO_SCREENSHOT_CHROME=/tmp/m5-runner/m5-browser/tmp/chromium \
STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js \
LD_LIBRARY_PATH=/tmp/m5-runner/m5-browser/libs/lib \
STUDIO_PYTHON=.venv/bin/python \
node scripts/compare_continuity_walk.mjs --screenshots docs/media/compare-continuity
```

The walk starts two `scripts/council_readiness_seed.py` processes: one with `--resident-model`
(two resident models, so two panels answer and the arbiter returns a verdict worth exporting) and one
with `--slow-model` (so a run can be stopped before the server saves it, which is how the
*not saved* state is reached honestly rather than by faking a zero).

| File | Size | What it shows |
| :--- | :--- | :--- |
| `compare-record-live.png` | 868 × 635 | right after a run: the criteria table, the measured table, and the record bar — `ran on this page`, *a copy is kept in this browser, so a reload restores it*, `The server saved this run as run 1`, and the export buttons |
| `compare-record-restored.png` | 868 × 427 | the same comparison after `reload()`, the figures identical, the bar now `kept in this browser … not the server's record, and not a live run` |
| `compare-record-server.png` | 868 × 202 | the server's own copy of the run, read back from `GET /api/history/export` (the redacted export) |
| `compare-record-forgotten.png` | 868 × 202 | after `Forget this copy`: `not kept here` — *a reload will lose it* — and no Forget button left |
| `compare-record-light.png` | 868 × 414 | the same state and the same figures in the light theme |
| `compare-record-narrow.png` | 480 × 900 | the bar and its buttons at a 480 px viewport: `scrollWidth - innerWidth = 0` |

The captions above are the walk's own assertions, not a reading of the pixels: the walk compares the
restored cards, statuses, figures, question and run id against the ones on screen before the reload,
and fails if any of them differ.
