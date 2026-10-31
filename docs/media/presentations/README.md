# What the shell says when it fails

Four captures of the app deliberately starved of an answer, taken by
`node scripts/presentation_walk.mjs --screenshots docs/media/presentations` against
`python3 scripts/demo_tour.py --serve`. They are not mock-ups and they are not a
designer's rendering: the walk aborts the requests with `net::ERR_FAILED`, which is
exactly what a dropped connection looks like to `fetch`, then photographs the result.

| File | What it shows |
|---|---|
| `start-error.png` | Start, preflight refused. The status pill reads `CHECK FAILED`, all six steps from the last successful read are still on the pane, and the error presentation names the cause, says nothing was changed by the attempt, and offers `Re-check` and `Providers`. |
| `chat-error.png` | Chat, the reply stream refused. The message the reader typed is kept in the transcript, no empty assistant bubble is left behind, and `Retry` re-sends it exactly once. |
| `harness-recovery.png` | Harness, an event stream aborted mid-run. Amber, not red: the task is still running on the server. It says how many events are shown, which cursor the timeline resumes from, and offers `Reconnect now` and `Reload from snapshot`. |
| `compare-empty.png` | Compare, nothing run yet. The neutral empty presentation says what will appear, what is kept while the reader stays, and the one action that starts it. |

Together they cover the three kinds across the five wired containers. Council's empty
state is the same component with different words; it is covered by the walk's assertion
rather than a fifth picture, because the demo session seeds a message into Council and a
capture would show the seeded transcript instead.

Regenerate:

    python3 scripts/demo_tour.py --serve            # prints the port and token
    STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
    STUDIO_PUPPETEER_CORE=/path/to/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js \
      node scripts/presentation_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN \
      --screenshots docs/media/presentations

The walk is also the test: it exits non-zero if any of the six paths fail to present a
cause, a kept line, or a working action. See `docs/uiux-2-0-presentations.md` for the
component itself.
