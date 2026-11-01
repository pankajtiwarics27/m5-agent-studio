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
| `health.png` | The health card with the **Demo data** pill and `Basis: unverified`, because this host has no model runtime and `/api/telemetry` therefore answered with its simulated payload. Four rows - the runtime (not reachable, `CHECK`), unified memory (`4.8 / 24 GB used · cap 20.8 GB`), containers (`2 running · OrbStack`) - and the claim that says the first two rows describe no machine while the workspace row still does. |
| `workspace.png` | The same pane after `Set / Create` was pressed with a scratch directory: the row and the recent-folders select show it, the Harness's own field holds the same string, and the note says the preflight is being re-read so the checklist agrees. |
| `failed.png` | `POST /api/workspace/directory` refused. The error presentation names the cause, says which directory is *still* the project directory, and offers Retry and the Harness. The row above still shows the old directory, because it is still the one in force. |
| `offline.png` | `/api/telemetry` refused. The card drops to **Offline** and `Basis: unavailable`, the three telemetry rows say *not measured yet* instead of keeping numbers the pane can no longer see, the failure box owns the recovery, and the workspace row is untouched - it never came from telemetry. |
| `health-narrow.png` | Both cards at 900px: no horizontal overflow, rows still legible. |

`lead.png` and `narrow.png` come from `scripts/start_walk.mjs` (UIUX-P2-01);
`recent.png`, `handoff.png` and `empty.png` come from `scripts/start_recent_walk.mjs`
(UIUX-P2-02), which creates the session it shows and deletes it again; the five health
and workspace captures come from `scripts/start_health_walk.mjs` (UIUX-P2-03), which
sets a scratch directory, does the refused write and the refused telemetry read, and
restores the original directory in a `finally` block. Each walk owns its own file names,
so running them in any order cannot replace another story's evidence.

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
    node scripts/start_health_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN \
      --screenshots docs/media/start

See `docs/uiux-2-0-start.md`, `docs/uiux-2-0-recent-work.md` and
`docs/uiux-2-0-start-health.md` for the components and their rules.
