# Chat persona picker — captures

UIV2-19 drew Chat's persona picker from `CHAT_PERSONAS` instead of three `<option value="…">` tags
whose value was the system prompt. These are the pictures behind that claim.

## After (`*.png`, 48 files)

Six states × four viewports × two themes, written by `scripts/chat_persona_walk.mjs` against
fixtures it starts itself — `scripts/council_readiness_seed.py` seeded with a resident model, plus
`--slow-model` for the reply in progress, `--long-model` for the reply long enough to scroll, and
the catalogue's keyless provider row for the failure.

| state | what it shows about the picker |
| --- | --- |
| `empty` | the three registry personas, the filter beside the picker, one clamped description line, the default selected |
| `populated` | two answers, each sent under the persona named on its context line |
| `long-content` | a long reply scrolling the transcript while the picker keeps its selection |
| `loading` | a stream in progress with the picker still saying which persona is answering |
| `error` | a keyless provider refusing, with the choice exactly where it was |
| `recovery` | the same persona answering afterwards, with the same prompt on the wire |

Each of the eight viewport × theme runs scored **21/21** checks. Viewports: `1440x900`, `1280x800`,
`1024x768`, `390x844`. Themes: `light`, `dark`.

## Before (`before-uiv2-19/`, 16 files)

The same walk, in `--capture-only`, pointed at `origin/main` at `3bd4890` — the empty and populated
states at the same four viewports and two themes. There the select is the whole contract: three
options whose value is a paragraph of prose, no filter, no description, and nothing remembering a
choice. The walk scores 3/17 there, and the fourteen it fails are the story.

## Regenerate

```bash
node scripts/chat_persona_walk.mjs --viewport 1440x900 --theme dark \
  --screenshots docs/media/chat-persona
```

`python3 scripts/uiux_chat_persona_inventory.py` inventories what the images are evidence for.
