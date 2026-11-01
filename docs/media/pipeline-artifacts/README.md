# Pipeline artifact captures

Eight PNG captures of the Pipeline stage/artifact and full-run retry surfaces, produced by the repository's browser walk rather than edited by hand.

```bash
node scripts/pipeline_artifacts_walk.mjs --screenshots docs/media/pipeline-artifacts
```

The walk starts `scripts/pipeline_artifacts_seed.py`, which boots the ordinary isolated demo environment and wraps the server's own `stream_any_model` so two arrivals the canned demo reply cannot produce exist: a critic that returns usage and no content, and an architect slow enough to stop mid-stream. The success and stop captures use the server's stream and saved-run behavior.

The failure and replay captures are deliberately different: after the real-server journeys, the browser intercepts the existing `/api/council/stream` request and returns synthetic SSE frames. Those frames exercise the front-end error, early-close and retry states without adding an endpoint or claiming a server checkpoint. They are UI-state evidence only, not model output, persisted runs, or evidence that completed stages can be resumed. Retry replays the captured request from Architect and runs all four stages again.

| File | State |
|---|---|
| `pipeline-all-stages.png` | a finished server-backed run: every seat's handoff and artifact rows, including an empty critic |
| `pipeline-run-line.png` | the run line: four of four seats, output, handoffs, and the server-reported run id |
| `pipeline-empty-seat.png` | the seat that produced nothing, with the section the next seat was handed |
| `pipeline-step-chars.png` | the collapsed step cards, each carrying its seat's character count |
| `pipeline-streaming.png` | a server-backed run in flight, with Stop offered and the handoff row updating |
| `pipeline-stopped-run.png` | a server-backed run stopped mid-seat: completed count, not-saved explanation, and full-run retry disclosure |
| `pipeline-failed-run.png` | synthetic SSE error rendered as Failed, with the partial stage output still inspectable |
| `pipeline-replayed-run.png` | synthetic full replay from Architect after the operator activates Retry from start |

Do not edit these images: a capture that was retouched is no longer evidence. Re-run the walk to create new captures. Every image is captured from the app served from this checkout over loopback; the error/replay states use explicit synthetic SSE fixtures as noted above.
