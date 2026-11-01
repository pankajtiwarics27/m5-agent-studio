# Council verdict captures (UIUX-P4-03)

Every image here came from the repository's own pipeline. Nothing here was drawn by hand, cropped,
or edited: each file is a screenshot the walk wrote.

```bash
# the two fixtures the walk starts, and the browser it drives
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js
export STUDIO_PYTHON=.venv/bin/python
export LD_LIBRARY_PATH=/path/to/chromium-libs/lib

node scripts/council_verdict_walk.mjs --screenshots docs/media/council-verdict
```

The walk starts `scripts/council_readiness_seed.py` twice: once as a runtime that answers (each
catalogue model with its own paragraph, so four seats can produce two voices), and once with
`--offline`, where the server writes the same demo text for every seat. Both are states the pane
has to survive, and the second is the one a results pane could mistake for consensus.

| File | Size | What it shows |
| :--- | :--- | :--- |
| `council-verdict-unpinned.png` | 619 × 451 | Four replies from two models and nothing pinned: `4 replies · 2 voices · no decision pinned`, and the two repeated pairs named — `Repeated wording: Seat 1 and Seat 4 (100 %); Seat 2 and Seat 3 (100 %)`. |
| `council-verdict-pinned.png` | 619 × 164 | The pinned conclusion exactly as the server stored it, credited to the seat that wrote it. |
| `council-verdict-decision.png` | 619 × 547 | The verdict after the Arbiter pinned its ruling: the seat whose reply became the decision (`is the decision`), the reply the decision repeats (`mostly in the decision`, 100 %), and the two replies it does not carry, named under **Dissent** with `Each stays in the transcript above, as written.` |
| `council-verdict-narrow.png` | 480 × 900 | The same block at a 480 px viewport, captured from the viewport rather than cropped from a wide one. |
| `council-verdict-light.png` | 619 × 547 | The same readings in the light theme. |
| `council-verdict-offline.png` | 920 × 1669 | The whole Hall with no runtime behind it: `0/4 READY`, `DEMO TEXT` on every seat, and the block saying `4 replies · 1 voice` — `One voice, not 4: Seat 1, Seat 2, Seat 3 and Seat 4 (87 %) answered with the same words.` |

The two 100 % readings in `-decision.png` are the point rather than a coincidence: the fixture's
Arbiter seat and its Architect seat point at the same model, so they wrote the same paragraph, and
the pane says so instead of counting the agreement as support.
