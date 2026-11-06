# Chat streaming, tools, copy and recovery — captures

UIV2-20 made four claims in this pane true and photographed the two that had never been exercised.

## After (`*.png`, 48 files)

Six states × four viewports × two themes, written by `scripts/chat_recovery_walk.mjs` against
fixtures it starts itself. Each of the eight viewport × theme runs scored **25/25** checks.
Viewports: `1440x900`, `1280x800`, `1024x768`, `390x844`. Themes: `light`, `dark`.

| state | what it shows |
| --- | --- |
| `empty` | nothing said, nothing to hand over, and the hint that says so |
| `populated` | a reply with a fenced code block and its labelled Copy control, a tool call open under its status word, and the turn's counts |
| `long-content` | a 25-point reply, pinned to the bottom, reporting its own timing |
| `loading` | a stream in progress with Stop offered, then a stopped turn saying who stopped it and how many characters were kept |
| `error` | a keyless provider's refusal, with `Retry` and the sentence "nothing ran, so sending it again repeats nothing" |
| `recovery` | a turn that ran a tool, offered as `Ask again (1 tool call already ran)` — a new question with the failed attempt kept |

## Before (`before-uiv2-20/`, 16 files)

The same walk, in `--capture-only`, pointed at `origin/main` at `2b0c35f` — the empty and populated
states at the same four viewports and two themes. There the Copy button says "Copied!" whatever the
clipboard did, no fixture can produce a tool call at all, and there is no stopped-turn detail. The
walk scores 7/14 there.

## Regenerate

```bash
node scripts/chat_recovery_walk.mjs --viewport 1440x900 --theme dark \
  --screenshots docs/media/chat-recovery
```

`python3 scripts/uiux_chat_recovery_inventory.py` inventories what these images are evidence for.
