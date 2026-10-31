# Status component evidence (`UIUX-P1-03`)

Two captures of the same demo instance — the tree before the component and the tree
after it — taken through `scripts/capture_screenshots.mjs`. They exist because "one
status component" is a claim a diff cannot settle: the interesting part is that the
old badges *painted the same colour*, and that is only visible by looking.

| Directory | Tree | Panes |
|---|---|---|
| `before/` | `dea6b30` (main before this story) | Start, Harness |
| `after/` | this branch | Start, Harness |

Start and Harness carry five of the six converted surfaces between them: the
onboarding summary and its new evidence block on Start, the run status, the Docker
state and the memory chip on Harness. The sixth, `#sdlcProgressBadge`, only renders
inside a run, so the browser walk covers it instead.

Before: `OK`, `READY`, `RUNNING`, `3 OF 6 STEPS READY` render as flat grey chips,
because the redesign layer flattened every `.badge` variant onto `--text` on
`--bg-hover`. After: each is the component — a label in `--text`, a dot and a hairline
in its tone — and Start carries an evidence block whose basis (`measured`, `derived`,
`unverified`, `unavailable`) is part of the claim rather than a footnote.

Each directory's `index.json` records the viewport and the sha256 of every image.
Those checksums prove the files are the files; they do not prove which panes were
rendered, which is why `scripts/status_walk.mjs` draws every tone and every state in a
browser and reports 74 checks.

Regenerate (needs a browser and a running demo, see [`../../demo.md`](../../demo.md)):

```bash
node scripts/capture_screenshots.mjs --base-url URL --token TOKEN \
  --panes start,harness --out docs/media/status/after

node scripts/status_walk.mjs --base-url URL --token TOKEN
```

The `before/` set cannot be regenerated from this branch: it is the same demo in the
same panes on `dea6b30`.
