# Harness verification evidence — captures

Five PNGs of the surface UIUX-P3-04 added, produced by the repository's own walk, not by hand.

```bash
node scripts/harness_evidence_walk.mjs --screenshots docs/media/harness-evidence
```

The walk starts `scripts/harness_evidence_seed.py`, which boots the ordinary isolated demo
environment, builds two small git workspaces, and lets the product's own
`TaskStateMachine.verify_task_completion()` decide: one workspace passes its test, one fails.
The reports it writes - revision, environment, exit code, transcript digest, classified
failures, acceptance coverage - are the verifier's, and the events are appended with
`_append_harness_event()`, the writer a turn uses. The demo has no model runtime, so the
`turn/start` and `turn/end` bookends are staged; nothing between them is.

| File | State |
|---|---|
| `verdict-card.png` | a verified turn drawn as a verdict card in the trajectory |
| `verdict-record.png` | the record behind it: the verdict, the contract, what was observed |
| `rejected-card.png` | a rejected turn, with the code, the classification and the failing test |
| `rejected-record.png` | the record for the rejection, with the criterion that was not covered |
| `no-verdict.png` | a session with a contract and no verdict, drawn as an absence |

Do not edit these images: a capture that was retouched is no longer evidence. Re-run the walk
against a new pane instead. Every capture is of the real app served from this checkout over
loopback, at a 1440x900 viewport.
