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

The demo instance cannot produce a pruned log on its own. The walk therefore builds both
cache/log directions from the server's real thirteen events: first a view holding only the
retained seq 5–13, then a stale view still holding seq 1–13 after the server reports a
retained floor of 5. `pruned.png` captures the second direction: all thirteen cards remain
visible, but the four entries outside the reported log window are explicitly view-only.

| File | What it shows |
|---|---|
| `log-open.png` | The Harness at 1440×900 with a newly created session: the **Session log** strip saying `this view holds 1 cached event, seq 1-1 · cursor 1 of 1`, opened to its single row `#1 session/init · standard policy · direct /tmp/m5-demo-…/workspace · lead Qwen3.8-27B`, and — below it — the card that row points at. Before this story this session drew an empty trajectory. |
| `replayed.png` | The same session after twelve policy changes: `13 events`, `following the log from seq 13 · this view holds 13 cached events · replayed 3 times`, rows `#1 session/init`, `#2 standard -> plan (revision 2)`, `#3 plan -> autonomous (revision 3)`…, Replay and Resync in the bar, and the recorded-event cards in the trajectory matching the rows one for one. |
| `pruned.png` | A cache that outlived a server prune: the warning strip says `this view holds 13 cached events`, `4 earlier events pruned, retained from seq 5`, and `4 cached events outside the log's reported retained range seq 5-13`. All thirteen cards remain drawn, so count and cards agree without calling those entries the current log. |
| `short-window.png` | The replayed state at 1440×560: the strip keeps its own line and the trajectory keeps enough of the pane to read, because the record explains the view and must not replace it. |

Regenerate all four with the command above; the walk prints 22 checks and exits non-zero if
any of them fails, so a capture set that exists is a walk that passed. See
`docs/uiux-2-0-harness-timeline.md` for the rules behind them.
