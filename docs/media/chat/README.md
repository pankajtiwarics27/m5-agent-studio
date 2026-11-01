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

## The context and the tools (UIUX-P2-05)

Captured by `node scripts/chat_context_walk.mjs --screenshots docs/media/chat`, which
writes one turn with a passing and a failing tool call and then sends one real message.

| File | What it shows |
|---|---|
| `context-tools.png` | The context line under the model header (`model demo-local-model · Principal Engineer & UI Architect · reasoning off · tools on (read-only) · 2 messages sent · the configured default project directory`), `Reasoning Trace · 49 chars`, `2 tool calls · 1 failed · 127 ms`, then `OK grep_search step 1 · 120 ms · 45 B of arguments` and `FAILED read_file step 2 · 7 ms · 35 B of arguments`, and below the answer `1.8s · first token 0.41s · 41.2 tok/s · 320 tok`. |
| `streaming.png` | A turn in flight: `streaming… · 1.2s · waiting for the first token · 128 chars`, drawn with its context and its `data-streaming` mark while the transcript reports `aria-busy="true"`. The finished turn above it still shows its own figures - they did not move when this one arrived. |
| `sent-context.png` | A real send (`say READY`, Entered, answered by the scripted runtime): the context recorded on that turn names the model, the persona and the workspace the server actually read. |
| `narrow-context.png` | The same surface at 900px: no horizontal overflow, the context line and the tool rows intact. |

Regenerate those four with the same command as above, pointing it at
`node scripts/chat_context_walk.mjs`. See `docs/uiux-2-0-chat-context.md`.

## Handing the conversation to the Harness (UIUX-P2-06)

Captured by `node scripts/chat_handoff_walk.mjs --screenshots docs/media/chat`, which
writes a conversation with ten tool calls (one failed) and then presses the real button
against the real Harness composer.

| File | What it shows |
|---|---|
| `handed-to-harness.png` | After **Open in Harness**: the reader is in the Harness, the composer holds the brief - the task, `- carried from Chat: nothing has run, and no model has been asked to run it -`, the counts, the recorded context line, the calls with their statuses and the bounds it applied - and the cursor is in the box. `Run` is the reader's to press; the handoff never presses it. |
| `back-to-chat.png` | Back in Chat after **Back to Chat**: the transcript is unchanged (ten tool rows and the answer), the cursor is in the composer, and the sentence beside the button now reads `Already in the Harness composer (2 messages, 10 tool calls); nothing has run.` |
| `handoff-failed.png` | The composer removed from the page and the button pressed: an error named \`The handoff did not happen\`, its cause, the line that the conversation and its tool calls are untouched, and **Retry** / **Dismiss**. The sentence no longer claims the carry happened. |
| `handoff-narrow.png` | The transcript bar at 900px: the sentence stays on one line and the button stays reachable. |

Regenerate those four with `node scripts/chat_handoff_walk.mjs`. See
`docs/uiux-2-0-chat-handoff.md`.

Regenerate:

    python3 scripts/demo_tour.py --serve            # prints the port and token
    STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
    STUDIO_PUPPETEER_CORE=/path/to/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js \
      node scripts/chat_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN \
      --screenshots docs/media/chat

See `docs/uiux-2-0-chat.md` for the roles, the send reasons and the rules.
