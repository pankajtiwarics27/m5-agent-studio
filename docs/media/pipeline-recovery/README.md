# Pipeline recovery captures

Six PNGs of the surfaces UIUX-P3-07 added, produced by the repository's own walk, not by hand.

```bash
node scripts/pipeline_recovery_walk.mjs --screenshots docs/media/pipeline-recovery
```

The walk starts `scripts/pipeline_recovery_seed.py`, which wraps the server's own
`stream_any_model` so one seat dies after six tokens and another refuses before a token. The
failure is the provider's: the `error` event, the absence of `council_done` and the absence of a
saved run are the server's answers, and `save_run()` is untouched.

| File | State |
|---|---|
| `failed-run.png` | the block for a seat that died mid-stream: the seat, the provider's message, the salvage, and the two presses |
| `failed-run-seats.png` | the step cards at that moment: seats 1–2 done, the third stopped, the fourth never started |
| `checkpoint-after-reload.png` | the same failure after a page reload: the browser's copy is offered, not silently drawn |
| `restored-from-checkpoint.png` | the copy restored — marked `recovered`, the partial seat labelled, nothing re-run |
| `run-on-record.png` | a successful run: on record with the id the server returned, readable back through the export |
| `reloaded-from-export.png` | that run read back from `GET /api/history/export`, four seats and all |

Do not edit these images: a capture that was retouched is no longer evidence. Re-run the walk
against a new pane instead. Every capture is of the real app served from this checkout over
loopback, at a 1440-pixel-wide viewport sized to the element being photographed.
