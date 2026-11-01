# The Harness's durable log, and the trajectory as a view of it

Captures of the Harness pane taken by
`node scripts/harness_timeline_walk.mjs --screenshots docs/media/harness-timeline` against
`python3 scripts/demo_tour.py --serve`.

This host has no model runtime, so no capture shows a finished run. It does not need one:
the durable log the strip describes is written by the app itself. The walk creates a session
through `POST /api/harness/sessions` — which is what makes the server append
`session/init` — and then changes that session's policy twelve times through
`PATCH /api/harness/sessions/{id}`, which is what makes it append twelve
`security/policy_revision` events. The thirteen events in these captures are the server's
own records, read back through `GET …/events` and the SSE stream. Nothing here is a
hand-made event, and no event was injected into the page.

The one state the demo instance cannot produce on its own is a log with a pruned head: its
log is never pruned. The `pruned.png` capture therefore shows the strip reacting to a log
state the walk builds from the events the server really returned (seq 5–13 of the same
thirteen, with a retained floor of 5) — the same shape a pruned log has, and the same
component, saying the same sentence.

| File | What it shows |
|---|---|
| `log-open.png` | The Harness at 1440×900 with a newly created session: the **Session log** strip saying `1 event held, seq 1-1 · cursor 1 of 1`, opened to its single row `#1 session/init · standard policy · direct /tmp/m5-demo-…/workspace · lead Qwen3.8-27B`, and — below it — the card that row points at. Before this story this session drew an empty trajectory. |
| `replayed.png` | The same session after twelve policy changes: `13 events`, `following the log from seq 13 · 13 events held · replayed 3 times`, rows `#1 session/init`, `#2 standard -> plan (revision 2)`, `#3 plan -> autonomous (revision 3)`…, Replay and Resync in the bar, and the recorded-event cards in the trajectory matching the rows one for one. |
| `pruned.png` | A log whose head was pruned: the strip in its warning colour, `9 events held`, `4 earlier events pruned, retained from seq 5`, `this view holds 9 of the 13 events last reported` — and the rows starting at `#5`, because that is all the pane can show. |
| `short-window.png` | The same state at 1440×560: the strip keeps its own line and the trajectory keeps enough of the pane to read, because the record explains the view and must not replace it. |

Regenerate all four with the command above; the walk prints 21 checks and exits non-zero if
any of them fails, so a capture set that exists is a walk that passed. See
`docs/uiux-2-0-harness-timeline.md` for the rules behind them.
