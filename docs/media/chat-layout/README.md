# Chat's conversation column (UIV2-18)

Nothing here was drawn by hand, cropped or re-touched. Every file is a screenshot a script wrote.

## The after set

```bash
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js

for vp in 1640x900 1440x900 1280x800 390x844; do
  for theme in light dark; do
    node scripts/chat_layout_walk.mjs --viewport "$vp" --theme "$theme" \
      --screenshots docs/media/chat-layout
  done
done
```

The walk starts its own fixtures through `scripts/council_readiness_seed.py` and closes with
`26/26 checks passed` on each of the eight runs. Six shots are taken per run, forty-eight in all.
Each run starts up to three throwaway servers: the default one (two loaded local models), one with
`--slow-model` for the reply in progress, and one with `--long-model` for the reply long enough to
scroll — plus the catalogue's keyless provider row for the failure state, which fails the moment it
is called, so no state here needs a network.

| File | What it shows |
| :--- | :--- |
| `chat-empty-<viewport>-<theme>.png` | Before a word: one short empty sentence in the column, the composer on screen, the controls on one row |
| `chat-populated-<viewport>-<theme>.png` | Two turns: an answer at the column's width and a question aligned to its right edge, the composer under both |
| `chat-long-content-<viewport>-<theme>.png` | A reply long enough to scroll, a code block with its own scrollbar inside its bubble, and an unbroken token that wraps instead of widening the pane |
| `chat-loading-<viewport>-<theme>.png` | A reply mid-stream with Stop offered, the composer still on screen |
| `chat-error-<viewport>-<theme>.png` | A provider that cannot answer: the failure reported above the composer with Retry, the message kept, no empty answer bubble |
| `chat-recovery-<viewport>-<theme>.png` | The same pane answering again, in the same column |

## The before set

`before-uiv2-18/<viewport>-<theme>/` holds the same two states (`empty`, `populated`) served from
`origin/main` at `21c1eef`, before this story, captured with this walk in `--capture-only` against a
detached worktree of that commit. The app in those files is `main`; the walk is this story's, because
the instrument that measures the defect is the same one that measures the fix.

The unmodified pane fails the checks those captures exist to prove: at 1640px the answer bubble is
870px of a 1,300px row with dead space to its right, the composer spans the full width instead of the
column, and the controls are a second row. At 390x844 the same controls take five rows and 314px of
the screen. Those are visible in `before-uiv2-18/1640x900-light/chat-populated-1640x900-light.png`
and `before-uiv2-18/390x844-dark/chat-populated-390x844-dark.png`.
