# Compare's question-first layout (UIV2-16)

Nothing here was drawn by hand, cropped or re-touched. Every file is a screenshot a script wrote.

## The after set

```bash
# two local models that can answer, through the stand-in runtime
python scripts/council_readiness_seed.py --resident-model Qwen3.6-35B-REAP-19B --port 8000
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js

for vp in 1640x900 1440x900 1280x800 390x844; do
  for theme in light dark; do
    node scripts/compare_balanced_walk.mjs --base-url http://127.0.0.1:8000 --token "$TOKEN" \
      --viewport "$vp" --theme "$theme" --screenshots docs/media/compare-balanced
  done
done
```

Without `--base-url` the walk starts that fixture itself, so every run begins with two
loaded local models and a stopwatch that has not run down. It asserts 21 things on the way and
closes with `21/21 checks passed` on all eight runs.

| File | What it shows |
| :--- | :--- |
| `compare-collapsed-<viewport>-<theme>.png` | The pane as it opens: the question and the model picks are the composer, the criteria are one folded line that counts what it holds |
| `compare-criteria-open-<viewport>-<theme>.png` | The criteria opened: *Settled by the run* (emerald), *Not settled here* (amber) and *Yours to judge* (cyan), each on its own rail |
| `compare-one-model-<viewport>-<theme>.png` | One model ticked: `1 model selected`, and the note that this is a single answer, not a comparison |
| `compare-two-models-<viewport>-<theme>.png` | Two models run on the same question: two answers at the same width (and the same height above 900px), the results table under them, the counts in the inspector |
| `compare-narrow-390x844-<theme>.png` | The answers after the viewport drops to 390x844 (whatever the run's own viewport): one column, nothing overflowing sideways |

## The before set

`before-uiv2-16/compare-<viewport>-<theme>.png` is the same pane served from `origin/main` at
`5c9a6e3`, before this story, captured with `scripts/capture_screenshots.mjs --panes debate`
against the demo instance. At 1440x900 the fold holds the four-row criteria table and the question
is under it — the defect the story is about. The demo lists one model, so the before set has no
two-model state to compare against.
