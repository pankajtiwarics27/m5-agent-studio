# Harness recovery captures

Seven PNGs of the surface UIUX-P3-05 added, produced by the repository's own walk, not by hand.

```bash
node scripts/harness_recovery_walk.mjs --screenshots docs/media/harness-recovery
```

The walk starts `scripts/harness_recovery_seed.py`, which boots the ordinary isolated demo
environment and drives the real state machine into five stopped states: `ensure_root_task`,
`transition_task`, `save_checkpoint`, `request_pause`, `cancel_task` and a real
`ProcessSupervisor.persist_barrier_hold()`. The four states a run can be left in are therefore
produced by the code the routes call, and the walk's presses go through `POST …/pause`,
`…/resume` and `…/cancel` like an operator's would. The demo has no model runtime, so the
`turn/start` and `turn/end` bookends are staged; nothing the panel reads is.

| File | State |
|---|---|
| `paused-panel.png` | a paused task: the cause, the checkpoint it kept, resume and cancel |
| `resume-prepared.png` | the answer to Resume: prepared at step 4, nothing run, and the control that continues it |
| `cancel-armed.png` | the first press of cancel: the confirmation, before anything is fenced |
| `cancelled-panel.png` | cancelled: what was contained, what survived, and why no resume exists |
| `crash-recovered.png` | a run whose worker died: the restart's own pause, named with its reason code |
| `blocked-panel.png` | a blocked task: the refusal stated, the action disabled, the barrier named |
| `hostile-reason.png` | a reason that is markup, drawn as text in the panel and in the log card |

Do not edit these images: a capture that was retouched is no longer evidence. Re-run the walk
against a new pane instead. Every capture is of the real app served from this checkout over
loopback, at a 1440x900 viewport.
