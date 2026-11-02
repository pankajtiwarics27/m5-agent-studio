# Polish layer — captures

Eight PNGs of the surface `UIUX-P5-05` polished, produced by the repository's own walk, not by hand.

```bash
node scripts/polish_walk.mjs --theme dark  --screenshots docs/media/polish
node scripts/polish_walk.mjs --theme light --screenshots docs/media/polish
```

The walk starts `scripts/council_readiness_seed.py` itself, sets the token it mints in
`sessionStorage`, and drives the real app over loopback at a 1440x900 desktop viewport and a
360x740 narrow one. Before it photographs anything it measures: 55 control kinds, each one for a
hover that paints, a press that moves, a ring on focus, and a pointer cursor where the control can
be pressed. Clicks are stopped at the capture phase while it measures, so the page it photographs
is the page it started with.

| File | Theme | Viewport | State |
|---|---|---|---|
| `polish-dark-hover-primary.png` | dark | 1440x900 | the pointer on the primary action, which now has a hover of its own |
| `polish-dark-rest-chat.png` | dark | 1440x900 | Chat at rest: composer, Send, the tools notice |
| `polish-dark-rest-start.png` | dark | 1440x900 | Start at rest: the rail, the step list, the preflight's verdict |
| `polish-dark-narrow-start.png` | dark | 360x740 | Start on a narrow screen |
| `polish-light-hover-primary.png` | light | 1440x900 | the same hover, on the light palette |
| `polish-light-rest-chat.png` | light | 1440x900 | Chat at rest, light |
| `polish-light-rest-start.png` | light | 1440x900 | Start at rest, light |
| `polish-light-narrow-start.png` | light | 360x740 | Start narrow, light |

Both themes are captured because the light theme re-points every colour: a state that is visible
on the dark surface can be invisible on the light one at the same contrast ratio.

Do not edit these images: a capture that was retouched is no longer evidence. Re-run the walk
against a new pane instead. The numbers behind them — 51 of 55 kinds answer the pointer, 41 press,
55 of 55 draw a ring, computed radii `0/6/8/999px`, computed durations `0/140ms` — are in the
walk's own summary and in
[`../../uiux-2-0-polish.md`](../../uiux-2-0-polish.md).
