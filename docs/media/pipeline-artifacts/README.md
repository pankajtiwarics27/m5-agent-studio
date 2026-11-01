# Pipeline artifact captures

Six PNGs of the surfaces UIUX-P3-06 added, produced by the repository's own walk, not by hand.

```bash
node scripts/pipeline_artifacts_walk.mjs --screenshots docs/media/pipeline-artifacts
```

The walk starts `scripts/pipeline_artifacts_seed.py`, which boots the ordinary isolated demo
environment and wraps the server's own `stream_any_model` so two arrivals the canned demo reply
cannot produce exist: a critic that returns usage and no content, and an architect slow enough to
stop mid-stream. Nothing else is staged - the SSE events, the character counts, and the saved run
id are the server's answers, and the sizes in the rows are the characters this pane itself
streamed.

| File | State |
|---|---|
| `pipeline-all-stages.png` | a finished run: every seat's handoff row and artifact row, and the empty critic |
| `pipeline-run-line.png` | the run line: four of four seats, the output, the handoffs, and the run id the server saved |
| `pipeline-empty-seat.png` | the seat that produced nothing, with the section the next seat was handed |
| `pipeline-step-chars.png` | the collapsed step cards, each carrying its seat's character count |
| `pipeline-streaming.png` | a run in flight, with Stop offered and the handoff row already counting |
| `pipeline-stopped-run.png` | a run stopped mid-seat: how many seats finished, and that it was not saved |

Do not edit these images: a capture that was retouched is no longer evidence. Re-run the walk
against a new pane instead. Every capture is of the real app served from this checkout over
loopback, at a 1440-pixel-wide viewport sized to the element being photographed.
