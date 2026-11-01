# What Chat shows, and what it says it will do

Captures of the Chat pane taken by
`node scripts/chat_walk.mjs --screenshots docs/media/chat` against
`python3 scripts/demo_tour.py --serve`. The walk writes the transcript it then judges
through the same `chatHistory` shape `sendChatRequest()` builds, because this host has
no live model runtime to answer through — and it sends one real message, which the demo
replays from its scripted runtime, so the answer in `answered.png` is a real stream.

| File | What it shows |
|---|---|
| `transcript.png` | A transcript the walk wrote: `YOU 3` and `MODEL DEMO-LOCAL-MODEL 4` headers with the turn numbers on the right, the summary `4 messages · 2 replies`, and **Jump to latest** because the walk scrolled up before a new turn arrived. The composer's note says what the telemetry probe reported, in amber, rather than promising the send will work. |
| `scrolled.png` | The moment the rule exists for: a new answer appended while the reader is at the top. The view does not move; the jump control appears. |
| `answered.png` | A real send: `say READY` typed and Entered, answered by the scripted runtime — the answer carries its model above it and `42.1 tok/s · 53 tok` below, and the composer is back with Send enabled and Stop gone. |
| `cleared.png` | Clear: the recovery presentation with **Undo** and the count of what was removed, an empty transcript that says what will appear, and Clear disabled because there is nothing left to clear. |
| `failure.png` | `/api/chat/stream` refused: the error presentation with its cause, the promise that the message and the transcript are kept, and **Retry**/**Dismiss**. The empty placeholder the send created is rolled back, so Retry sends one message, not two. |
| `narrow.png` | The pane at 900px: no horizontal overflow and the composer note still visible. |

These are not mock-ups. `transcript.png` and `scrolled.png` show a transcript the walk
wrote on purpose; everything else is the pane reacting to the server it was pointed at.

Regenerate:

    python3 scripts/demo_tour.py --serve            # prints the port and token
    STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
    STUDIO_PUPPETEER_CORE=/path/to/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js \
      node scripts/chat_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN \
      --screenshots docs/media/chat

See `docs/uiux-2-0-chat.md` for the roles, the send reasons and the rules.
