# M5 2.0 hardware qualification evidence (M5-P4-01)

Evidence record for roadmap story `M5-P4-01` (Measured model qualification and
RAM admission), produced by an **operator run of the qualification suite on the
target machine** on 2026-10-31, against `main` at
`d0e47f0098e56b336c72a342cc405af2beb3e4db`. The tool is
[`scripts/qualify_environment.py`](../scripts/qualify_environment.py) with
[`scripts/qualify_environment.sh`](../scripts/qualify_environment.sh) as its
wrapper; its contract is [`qualification-suite.md`](qualification-suite.md)
(ADR-0010), and its regression tests are
`tests/test_qualify_environment.py` and `tests/test_model_probe.py`.

The suite cannot run in the repository's CI container: no 24 GB Apple Silicon
machine and no model server exist there. This document therefore records a run
that happened **outside** CI, on the hardware the story is about. The JSON and
Markdown artifacts stay on the operator's machine (`dist/` is git-ignored) — the
numbers below are transcribed from them, and the artifact names are given in
[§9](#9-artifacts-and-reproduction) so the record can be re-checked locally by
anyone who has the files.

## 1. Environment fingerprint (observed)

| Property | Observed value |
|---|---|
| OS | `Darwin` 25.6.0 (macOS 26.6.1) |
| Chip / architecture | `Apple M5` / `arm64` |
| CPUs | 10 |
| Memory | 25,769,803,776 bytes = 24.0 GiB |
| Python (suite process) | 3.15.0 |
| Repository revision | `d0e47f0098e56b336c72a342cc405af2beb3e4db` |
| Record generated | 2026-10-31T07:50:36Z |
| Swap total at start | 7,516,192,768 bytes (7.00 GiB) |
| Swap in use at start | 6,638,209,597 bytes (6.18 GiB) |
| Pages throttled | 0 |

The machine was **already using 6.18 GiB of swap** before any measurement began.
That is the honest context for every swap number below: a "no growth" result is a
statement about this already-loaded machine, not about a quiet one.

## 2. Story tasks, and what was actually observed

| Story task | Result | Evidence |
|---|---|---|
| Measure load / prefill / swap / thermal behaviour | **Partly measured.** Load, first-token and end-to-end timings and swap growth measured; thermal pressure **not** readable | `p4.model_load` pass, `p4.pressure` pass, `p4.thermal` blocked (§4, §5, §6) |
| Build representative per-category coding cases | **Tooling exists, not exercised in this run** | the suite ships `--coding-cases` with a schema and tests; `p4.coding_cases` was skipped because no case file was passed |
| Record completed case evidence and uncertainty | **Not collected in this run** | no case file, no replay of the prompts |
| Implement one-heavy-model admission | **Observed live in the serving path** | `p4.single_heavy_model` and `p4.admission_wired` (§3) |

Sequential switching between heavy models is `M5-P4-02`, not this story.

## 3. Checks (all 8 recorded by the run)

| Check | Status | Detail as recorded |
|---|---|---|
| `p4.machine.fingerprint` | pass | Apple M5 / arm64 / 24.0 GiB |
| `p4.single_heavy_model` | pass | first=admitted; second=refused; after_release=admitted |
| `p4.admission_wired` | pass | first stream admitted; second refused while resident=True; stream after release=admitted |
| `p4.priors_not_verified` | pass | level=unknown; note='no comparable benchmark profile' |
| `p4.pressure` | pass | peak swap 6.18 GiB; growth 0.00 GiB |
| `p4.model_load` | pass | 3/3 runs exited 0 |
| `p4.coding_cases` | skip | pass `--coding-cases FILE` to run them |
| `p4.thermal` | blocked | 2054 samples taken but none were readable |

Story verdict as recorded by the suite: `M5-P4-01: qualified`, `checks_run: 8`,
`failed: []`, `not_executed: []` — the two non-passing checks are declared
`required: false` by the suite, which is why they do not change the verdict. They
are still recorded as gaps below rather than being dropped.

## 4. Model-load measurement

Probe command: `python3 scripts/model_probe.py --base-url http://127.0.0.1:8000/v1`
(against a local oMLX server, `runtime: openai-compatible`, `temperature: 0.0`),
driven through the suite with `--model-command` and `--model-runs 3`.

Model reported by the server and the probe: **`Gemma-4-21B-REAP-4bit`**. Prompt
31 tokens, output 64 tokens, 153 output characters — one fixed probe prompt, not
a benchmark suite.

| Run | Wall | First token | End to end | Exit |
|---|---|---|---|---|
| 1 (cold load) | 19.856 s | 16,893.7 ms | 19,701.8 ms | 0 |
| 2 | 3.323 s | 185.0 ms | 3,163.5 ms | 0 |
| 3 | 3.194 s | 159.4 ms | 3,066.0 ms | 0 |

Swap during the model-load check: before 6,638,209,597 bytes, peak
8,006,402,048 bytes (7.46 GiB), growth 1,384,969,667 bytes (1.29 GiB). The first
token of the cold run is ~91× the warm-run figure: the cost of admitting a heavy
model, not of decoding.

Two honest gaps in this measurement:

- **No per-process peak memory.** The probe was run without `--server-pid`, so
  `peak_memory_bytes` is `null` in every run. The suite records its absence; it
  does not estimate one.
- **No model digest.** The probe was run without `--model-digest`, so the
  persisted calibration profile (`dist/model-profile.jsonl`, written by
  `--profile-out`) records `model_digest: unknown`. A profile that cannot name
  its weights cannot be matched to a routing prior, which is consistent with
  `p4.priors_not_verified: level=unknown` — the measured profile did not upgrade
  any routing confidence, and no such claim is made here.

## 5. Memory-pressure measurement

`--pressure-gb 16`: 16.0 GiB requested in 8 MiB blocks, each written, over
40.176 s.

| Field | Bytes | GiB |
|---|---|---|
| Swap used before | 6,638,209,597 | 6.18 |
| Swap used at peak | 6,638,209,597 | 6.18 |
| Swap used after | 6,621,432,381 | 6.17 |
| Growth | 0 | 0.00 |

Reading: touching 16 GiB of a 24 GiB machine did **not** grow swap, and swap
actually fell slightly by the end. This satisfies the story's "24 GB pressure
measured" criterion as a measurement, and it is not a claim that the machine is
comfortable — it was already 6.18 GiB into swap before the run, and the
model-load check on the same run then pushed swap up by another 1.29 GiB (§4).

## 6. Thermal: not measured

`p4.thermal` sampled the platform 2,054 times during the load phases and got zero
readable readings: on Apple Silicon `pmset -g therm` prints `CPU_Speed_Limit`
only after a power or thermal event has been recorded, and this machine's output
carried none. `powermetrics` would need root and is never invoked by the suite.

Consequence, stated plainly: **no throttling claim is made.** The check is
declared non-required, so the verdict stands, but "thermals were fine" is not
something this record supports.

## 7. Acceptance criteria

| Criterion | Disposition | Evidence |
|---|---|---|
| Second simultaneous heavy load rejected under policy | **Met** | `p4.single_heavy_model` (capacity policy: first admitted, second refused, admitted after release) and `p4.admission_wired` (the real `app.stream_any_model` funnel: second stream refused while `resident=True`, admitted after release) |
| Routing does not treat public priors as verified | **Met** | `p4.priors_not_verified`: routing confidence is `unknown`, note `no comparable benchmark profile` |
| 24 GB pressure measured | **Met** | `p4.pressure`: §5 |

Both halves of the admission criterion are covered on purpose: the primitive
(`resource_admission` through `model_admission`) and the serving path
(`app.stream_any_model`) are separate facts, and only the second one is what a
user experiences.

What the record does **not** establish:

- Thermal behaviour under sustained load (§6).
- Coding-case completion per category (§2): the tooling is tested, but no case
  file was supplied on this run, so no case evidence or uncertainty exists yet.
- That the 80 % reservation constant matches a real model footprint: the
  measurements above describe one 4-bit model on one 24 GiB machine, not a
  per-model sizing model.
- Any container-isolation guarantee: the sandbox suite on the same machine
  returned `unqualified` (§8).

## 8. Two hardware runs, and the sandbox run beside them

Three records were produced on this machine on 2026-10-31. Keeping all of them
is the point — the first one failed, and it failed for a reason worth writing
down.

| Record | Suite | Verdict | Why |
|---|---|---|---|
| `qualification-20261031T074346Z` | `--hardware` | **unqualified** | `p4.model_load` 0/3: every run died in argparse with `--server-pid: invalid int value: ''` |
| `qualification-20261031T074459Z` | `--sandbox --runtime docker` | **unqualified** | `p3.adversarial.memory_limit`: a 3 GiB allocation completed under `--memory 512m` (`detail: exit 0`) |
| `qualification-20261031T074929Z` | `--hardware` | **qualified** | `p4.model_load` 3/3 (§4) |

The first hardware run failed on the **operator's command**, not on the product:
the pid for `--server-pid` was produced by inlining
`$(pgrep -f 'mlx_lm.server' | head -1)` into the `--model-command` string outside
its quotes. `pgrep` matched nothing, the shell substituted an empty string, and
the malformed flag was frozen into the command the suite executed three times.
The correction was to drop `--server-pid` (which only makes `peak_memory_bytes`
`null`, §4) and re-run **once**: this was an invocation defect repaired, not a
check re-run until it went green. The failed record is kept rather than deleted.

The sandbox record matters for this story only as a boundary: **container
isolation did not qualify on this machine**, so nothing in this document may be
read as evidence for it. The failing check is recorded in
[`qualification-suite.md`](qualification-suite.md) and `M5-P3-01` stays
`blocked`; the cause is under investigation and is not waived.

## 9. Artifacts and reproduction

On the operator's machine (`dist/` is ignored by git, so these are not in the
repository):

```
dist/qualification/qualification-20261031T074929Z.json   # this record
dist/qualification/qualification-20261031T074929Z.md
dist/model-profile.jsonl                                 # model_calibration profile, digest unknown
```

Reproduction on a 24 GB Apple Silicon machine with the sandbox image already
pulled and a model server answering:

```bash
curl -s http://127.0.0.1:8000/v1/models | head -c 400
PYTHON=.venv/bin/python ./scripts/qualify_environment.sh --hardware \
  --pressure-gb 16 --model-runs 3 --timeout 600 \
  --profile-out dist/model-profile.jsonl \
  --model-command 'python3 scripts/model_probe.py --base-url http://127.0.0.1:8000/v1'
```

Exit codes are the suite's contract: `0` qualified, `1` unqualified (an observed
failure — fix it or record it), `2` blocked (a setup gap; nothing is claimed).
Numbers from a different machine or a different model are a different record:
this document is evidence about one Apple M5 on 2026-10-31, not a performance
guarantee.
