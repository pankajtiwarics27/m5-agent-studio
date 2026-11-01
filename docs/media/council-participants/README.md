# Council participants and readiness — captures

Produced by `scripts/council_readiness_walk.mjs`, which starts
`scripts/council_readiness_seed.py` twice: once over a stand-in oMLX admin API on loopback, and
once with `--offline` (no runtime). The current walk captures the mixed-readiness, post-load,
offline-blocked, narrow, and light-theme states. Every current capture is a browser screenshot
of `#pane-hall`; none was made by hand.

| File | What it shows |
| :--- | :--- |
| `council-readiness-mixed.png` | four seat assignments with different answers: `READY` (resident, 16.60 GB), `NEEDS MEMORY` (needs 12.05 GB, with 4.20 GB of the 20.80 GB ceiling free), `NEEDS KEY` ("groq" holds no key), and `NEEDS LOAD` (3.80 GB loads on the first token). The incomplete roster blocks round invocation. |
| `council-seat-loaded.png` | the same pane after **Load now** on the fourth seat: telemetry reports the model resident and its row turns `READY`. |
| `council-readiness-offline.png` | the preview machine with all four local assignments set: every row says `DEMO TEXT`, and round, spawn-and-convene, and single-seat dispatch are blocked before a request is sent. |
| `council-readiness-narrow.png` | readiness rows at 480 px wide, with no horizontal overflow. |
| `council-readiness-light.png` | the readiness states in the light theme. |
| `council-summon-failed.png` | retained from the original walk: an invocation that passed readiness but then failed at the provider. It demonstrates post-preflight failure feedback, not a readiness bypass. |
| `council-demo-replies.png` | retained historical capture from before offline dispatch was blocked. Do not read it as current offline behavior; the current walk verifies that path sends no summon request. |

The current walk regenerates the first five files. The last two are preserved as historical
context from the original implementation. Current captures are 1440 px wide except the narrow
one, which is 480 px by design.
