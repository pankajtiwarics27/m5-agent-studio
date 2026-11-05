# Council state consistency and stream recovery (UIV2-15)

Nothing here was drawn by hand, cropped or re-touched. Every file is a screenshot
`scripts/council_state_walk.mjs` wrote against a running demo app.

```bash
python scripts/demo_tour.py --serve --port 8000            # or any authenticated instance
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js

for vp in 1640x900 1440x900 1280x800 390x844; do
  for theme in light dark; do
    node scripts/council_state_walk.mjs --base-url http://127.0.0.1:8000 --token "$TOKEN" \
      --viewport "$vp" --theme "$theme" --screenshots docs/media/uiv2-v15-states
  done
done
```

The walk seeds the API token into `sessionStorage` before the first document — the
gate the shell itself reads — then drives the Council pane through the states this
story makes say something: a closed session, its reopen by a Human message, a
stream this browser drops, a session deleted outside the pane, and a seat the
server says is still answering. It asserts 19 things on the way and fails the run
when any of them is not true; the closing line is `19/19 checks passed`.

## The files

`<state>-<viewport>-<theme>.png`, 5 states x 4 viewports x light and dark:

| State | What the capture shows |
| :--- | :--- |
| `closed-session` | The notice names the refusal (a summon into a pinned conclusion is a 409) and offers the write that reopens it, while the composer's note-only path stays enabled |
| `reopened-session` | After the Human message: "This session is open again", the conclusion pinned by Lead Arbiter still kept, and the way back to it |
| `lost-turn` | A stream that ended with no terminal frame: the seat, the model and the number of characters that arrived and were discarded, plus the partial text drawn under a "Stream ended · not saved" badge |
| `gone-session` | A session deleted outside the pane: named as gone with the id, its transcript kept on screen, dispatch blocked |
| `still-answering` | A seat the server reports as running: stated as still answering rather than as a failure, with `Read it now` / `Stop watching` |

The lost-turn capture is the one that needed the browser: the pane's own
`consumeSSE()` is what runs out of frames, so the "not saved" card and the notice
come from the code the reader runs, not from a mocked response.
