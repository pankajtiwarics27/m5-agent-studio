# Token variant evidence (`UIUX-P1-01`)

Two captures of the same demo instance, taken through
`scripts/capture_screenshots.mjs` from the tree that defines the token layer. They
exist because "themes and density are defined" is a claim a reader cannot check
from a stylesheet, and the baseline set in [`../screenshots`](../screenshots) only
covers the default theme at the desktop viewport. These historical captures predate
the safety checks in [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122):
their checksums prove file consistency, not which panes or variants were rendered.
They require separate provenance review before acceptance.

| Directory | Variant | Viewport | Panes |
|---|---|---|---|
| `light-900/` | theme `light` | 900 × 800 @2 | Start, Chat |
| `compact/` | density `compact` | 1440 × 900 @2 | Start, Chat |

Both are seeded preferences, not post-load DOM edits: the script writes
`localStorage.m5_theme` and `localStorage.m5_ui_density` before the first document,
which is the same path a returning user takes.

Two panes, not six, on purpose: Start is the onboarding surface and Chat is the
conversation surface, so between them they exercise the shell, the strips, the
cards, the form controls and the composer. This is evidence that a theme and a
density render, not a second baseline — the baseline stays the six-image set that
`scripts/validate_screenshots.py` checks.

Each directory's `index.json` records the viewport, the emulated variant and the
sha256 of every image; `tests/test_screenshots.py` fails if an image no longer
matches its index.

Regenerate (needs a browser and a running demo, see
[`../../demo.md`](../../demo.md)):

```bash
node scripts/capture_screenshots.mjs --base-url URL --token TOKEN \
  --panes start,chat --emulate-theme light --viewport 900x800 \
  --out docs/media/tokens/light-900

node scripts/capture_screenshots.mjs --base-url URL --token TOKEN \
  --panes start,chat --emulate-density compact \
  --out docs/media/tokens/compact
```
