# Council deliberation stages and evidence — captures

Produced by `scripts/council_deliberation_walk.mjs`, which starts
`scripts/council_readiness_seed.py` twice: once over a stand-in oMLX runtime that answers seat 1
in reasoning only and cuts seat 3 at its length limit, and once with `--offline` (no runtime at
all — the preview machine). Every image is a screenshot taken by that walk: nothing here was drawn by hand.

| File | What it shows |
| :--- | :--- |
| `council-deliberation-evidence.png` | a whole run in the Council pane: four contract rows, four handoff rows, and the evidence table with one row per seat — Architect `no output` (it answered in reasoning only), Critic `cut at the length limit`, Builder and Judge `answered` |
| `council-deliberation-capped.png` | seat 3's card alone: `ALLOWED budget 2400 tok · reasoning off · no skills · cut at the length limit`, the handoff row saying the Architect's reasoning was carried, and the artifact row under it |
| `council-deliberation-narrow.png` | the evidence table at 480 px wide, with no horizontal overflow |
| `council-deliberation-light.png` | the same table in the light theme |
| `council-deliberation-offline.png` | the machine with no runtime: every row is `demo text`, every produced cell says `demo text, no tok/s`, and the stepper reads `demo` where a rate used to be |

`council-deliberation-evidence.png` is the whole 1440 px viewport. The other three are element
captures: `council-deliberation-narrow.png` was taken at a 480 px viewport, and the card and
light-theme captures are the width the Council pane is at 1440 px (about 920 px). The fixture
itself produces the states: `--reasoning-model` answers in `reasoning_content` only, and
`--cap-model` ends in `finish_reason: "length"`.
