# What the Start pane says first

Two captures of the pane in the state this machine was actually in, taken by
`node scripts/start_walk.mjs --screenshots docs/media/start` against
`python3 scripts/demo_tour.py --serve`. The walk reads the same
`/api/onboarding/preflight` response the pane read and holds the pane against it, so
the picture and the assertions describe the same moment.

| File | What it shows |
|---|---|
| `lead.png` | A preflight that is 3 of 6 ready. The lead prints the count, names the next step (`Connect a model source`), says what it means, and offers the action the server attached (`Open providers`). The row that step belongs to carries the `NEXT` chip and `aria-current="step"`; `Ask a model to say READY` says it has no action of its own because it follows the one above. The status pill states the verdict (`3 OF 6 STEPS READY` is not repeated in two places). |
| `narrow.png` | The same pane at 900px: the lead keeps its action on screen and does not overflow. |
| `recent.png` | Recent work, with one session the walk created through the Harness API: its title, `0 steps · standard · workspace`, and a `READY` pill painted by the same state table the Harness uses. Under it, the task composer with its note quoting the preflight — *"the preflight is not ready yet (next: Connect a model source), so Run in the Harness will refuse until that step is done"*. |
| `handoff.png` | The Harness after the handoff: the task the walk typed is in the composer and focused, and no session was created and no stream started — a handoff, not a launch. |
| `empty.png` | Recent work with nothing in it: the empty presentation, why, what appears once a session runs, and the two actions. |

`lead.png` and `narrow.png` come from `scripts/start_walk.mjs` (UIUX-P2-01); the last
three come from `scripts/start_recent_walk.mjs` (UIUX-P2-02), which creates the session
it shows and deletes it again.

The run button underneath is disabled in this state, and the line below it names the
step that is holding it up — the same text is the button's `aria-describedby`, so a
screen reader hears the refusal instead of meeting a dead control.

These are not mock-ups and not a designed ideal: this host has a local runtime
configured but no models listed, which is the ordinary first-run state.

Regenerate:

    python3 scripts/demo_tour.py --serve            # prints the port and token
    STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
    STUDIO_PUPPETEER_CORE=/path/to/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js \
      node scripts/start_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN \
      --screenshots docs/media/start
    node scripts/start_recent_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN \
      --screenshots docs/media/start

See `docs/uiux-2-0-start.md` and `docs/uiux-2-0-recent-work.md` for the components and their rules.
