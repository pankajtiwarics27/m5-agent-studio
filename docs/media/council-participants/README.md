# Council participants and readiness — captures

Produced by `scripts/council_readiness_walk.mjs`, which starts
`scripts/council_readiness_seed.py` twice: once over a stand-in oMLX admin API on loopback, and
once with `--offline` (no runtime, no saved alias — the preview machine). Every image is a
screenshot of `#pane-hall` taken by that walk; none was made by hand.

| File | What it shows |
| :--- | :--- |
| `council-readiness-mixed.png` | the four seats with four different answers: `READY` (resident, 16.60 GB), `NEEDS MEMORY` (needs 12.05 GB, 4.20 GB of the 20.80 GB ceiling free), `NEEDS KEY` ("groq" holds no key), `NEEDS LOAD` (3.80 GB loads on the first token), and the summary and basis line above them |
| `council-seat-loaded.png` | the same pane after pressing **Load now** on the fourth seat: the runtime reports the model resident and the row has turned `READY` |
| `council-summon-failed.png` | three seats answered and the fourth (the alias with no key) did not: one block naming the seat, quoting the provider, saying nothing was saved for that model, with Retry / Check readiness / Ask one model |
| `council-readiness-offline.png` | the offline machine: all four seats are `DEMO TEXT`, because the local runtime is not answering and the server writes the reply itself |
| `council-demo-replies.png` | a round on that machine: each reply carries its seat and role, is labelled a demo reply, and carries no tok/s badge |
| `council-readiness-narrow.png` | the same rows at 480 px wide, with no horizontal overflow |
| `council-readiness-light.png` | the same rows in the light theme |

The captures are 1440 px wide except the narrow one, which is 480 px by design.
