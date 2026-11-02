# Cross-tab terminology (UIUX-P5-01)

Nothing here was drawn by hand. Every file comes from one command:

```
TMPDIR=/tmp/m5-runner/m5-browser/tmp \
STUDIO_SCREENSHOT_CHROME=/tmp/m5-runner/m5-browser/tmp/chromium \
STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js \
LD_LIBRARY_PATH=/tmp/m5-runner/m5-browser/libs/lib \
STUDIO_PYTHON=.venv/bin/python \
node scripts/terminology_walk.mjs --screenshots docs/media/terminology
```

The walk starts `scripts/council_readiness_seed.py --resident-model …` so a real comparison answers,
then opens each pane with **that pane's own keyboard shortcut** (Alt+1…Alt+6, read out of the rail's
`data-shortcut`) and reads every status word the DOM holds — text, tone, and the computed
`text-transform` of the box it lands in.

| File | Size | What it shows |
| :--- | :--- | :--- |
| `terminology-start.png` | 1440 × 900 | Start: the preflight verdict (`ready` / `needs you`) and the recent-work pills, in the shared words |
| `terminology-harness.png` | 1440 × 900 | Harness: the run badge in the same vocabulary the session rows use |
| `terminology-hall.png` | 1440 × 900 | Council: `ready` on the session badge, `0/4 ready` on the readiness count, `needs load` / `needs key` per seat — and `ok` / `halted` in the memory rail, lowercase, where they used to be shouted by the heading's CSS |
| `terminology-council.png` | 1440 × 900 | Pipeline: the seat pills and the run total |
| `terminology-debate.png` | 1440 × 900 | Compare: two panels answered, both reading `answered` in the ok tone |
| `terminology-chat.png` | 1440 × 900 | Chat: the same words on the composer and the tool row |
| `terminology-narrow.png` | 480 × 900 | Compare at 480 px: no horizontal overflow, no word shortened or shouted |
| `terminology-light.png` | 1440 × 900 | the same words in the light theme |

The walk fails if any chip's text is not composed of vocabulary words, if any chip is uppercase by
its text **or by computed style**, if one word ever appears with two different tones, or if a page
error occurs.
