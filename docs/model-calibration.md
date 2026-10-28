# Local model calibration benchmarks

AS-059 defines [`model_calibration.py`](../model_calibration.py): the
measurement layer that turns raw timed probe runs into a
`ModelPerformanceProfile` — TTFT, prefill tokens/s, decode tokens/s and peak
memory aggregated separately for warm and cold runs, always pinned to the
exact identity they were measured under, and persisted as repeatable,
self-verifying sample records.

## Calibration-input identity

A profile is only comparable inside one `CalibrationInputs` value: model id,
content digest, revision and quantization (pinned straight from the AS-017
`ModelDescriptor` via `CalibrationInputs.for_model`), runtime name and
version, hardware-profile snapshot, prompt-set digest, context length,
temperature and concurrency. Every one of these changes the inputs digest, so
a runtime upgrade, a temperature tweak, or a new prompt set silently
invalidates comparison instead of corrupting it — the benchmarking safeguards
require that measured numbers may only be compared within their pinned setup.

## Deriving metrics

`derive_sample` converts one `RawRun` (start/first-token/finish timestamps in
milliseconds plus token counts and peak memory) into a
`CalibrationSample`: `ttft_ms` is first-token minus start; prefill throughput
is prompt tokens over the time-to-first-token window; decode throughput is
output tokens over the post-first-token window. Degenerate windows (no time
elapsed) leave the affected rate `None` rather than inventing an infinite
throughput, and semantic inconsistencies (output tokens with no first-token
timestamp) become failed samples with a reason — one bad measurement never
aborts a whole calibration pass. Failed samples keep their error text,
bounded to 500 sanitized characters and free of control characters.

## Aggregation

`calibrate` folds samples into a profile: for each of the four metrics and
each run kind, `MetricAggregate` reports count, median, spread
(maximum − minimum) and bounds over the successful samples only — repeats
are summarized by median plus spread, not a single lucky run. Failed runs
count toward `failure_rate` and never pollute any metric. Warmup runs are
discarded before aggregation entirely, so they leave no audit trace. The
profile mapping (inputs, every sample, every aggregate, audit counters)
digests canonically, which makes the whole profile content-addressed.

## The probe runner

`run_calibration` is the only executing part of the module, and it still runs
nothing: it drives a caller-supplied async probe (e.g. the app wiring it over
the AS-019 lifecycle adapter with a timed chat call on target hardware)
through `warmup_runs` warmups, `warm_repeats` warm iterations and
`cold_repeats` cold cycles with global 1-based sequences. The probe must echo
the requested sequence and kind; probe exceptions become failed samples
carrying only `probe failure: <ExceptionType>` — exception detail strings can
contain prompt content or paths, so they are never stored. `CancelledError`
propagates untouched.

## Reproducible sample store

`save_profile` writes a JSON-lines file: one `profile` header (inputs,
profile digest, audit counts) followed by one `sample` record per run, each
line carrying a canonical digest of its own content, written atomically via a
temp file plus `os.replace`. `load_profile` re-verifies every line digest,
re-checks the header counts against the stored samples, and recomputes the
full profile — aggregates, failure rate and digest — from them. A file whose
samples were edited, reordered or swapped fails even if the attacker fixes
the per-line digests, because the recomputed aggregates no longer hash to the
header's profile digest. The store is tamper-evidence for calibration
archives, not protection for an adversary who rewrites every digest
consistently by re-running `calibrate` itself.

Prompts are never stored — only the prompt-set digest — and nothing in a
profile carries wall-clock timestamps, so a stored calibration cannot leak
user content and replays are deterministic.

## Evidence

`tests/test_model_calibration.py` (12 tests) covers input pinning and
digest sensitivity (runtime version, temperature), descriptor-based
construction, every `RawRun` timing-validation path, metric derivation with
degenerate-window and semantic-mismatch handling, warm/cold median+spread
aggregation with failure-rate counting and duplicate-sequence refusal,
sanitization and bounding of probe error text, probe protocol enforcement
(wrong sequence, wrong kind, non-`RawRun` returns, cancellation), phase
scheduling with warmup exclusion, and the store round trip against
line-tampering, digest-recomputed tampering, corrupt/foreign/empty/missing
files and invalid arguments.
