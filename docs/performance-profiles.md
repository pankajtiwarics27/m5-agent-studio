# Versioned model performance profiles

AS-060 defines [`performance_profiles.py`](../performance_profiles.py): the
`ProfileVault` that persists the AS-059 `ModelPerformanceProfile` values as
immutable, keyed history — the storage contract the roadmap asks for
("persist immutable profile keyed by hardware, OS, model digest,
quantization, runtime version and benchmark methodology") so that later
layers (comparison UI, recommendation engine) read pinned measurements
instead of mutable current-state records.

## The key

Every history is filed under a `ProfileKey` of exactly six components: the
canonical digest of the hardware snapshot, the `os_id`, the model digest, the
quantization, the runtime version, and the `BenchmarkMethodology` digest
(name, version, and a pinned calibration-schema version so files written for
a different profile layout are refused). Absent quantization or runtime
version normalize to the reserved `unspecified` marker, so "the runtime
never told us" is a distinct, explicit key component rather than an
accidental empty string.

The decisive design choice: keys are **derived from the profile**, not
supplied by the writer. `ProfileKey.derive` reads model digest,
quantization, runtime version and the hardware snapshot off the profile's
own pinned `CalibrationInputs`; a profile without a hardware snapshot cannot
be filed at all. `ProfileVault.append` additionally refuses an explicit
`os_id` that disagrees with the snapshot's `operating_system`/`architecture`
fields, and every read re-checks the stored profile against its key. A
record can therefore never sit under a flattering or outdated label — the
key is a projection of the measurement, and a tampered file whose content no
longer matches its key fails with an integrity error.

For lookups, `ProfileKey.for_lookup(descriptor, hardware=..., methodology=
..., runtime_version=...)` composes the identical key from an AS-017
registry `ModelDescriptor`, so "what does the vault hold for this model
build on this machine" is answerable straight from registry state. Note what
is deliberately *not* in the key: temperature, context length and the other
calibration inputs stay inside the profile — the key pins the six
roadmap-named components, while each version keeps its full setup digest, so
consumers comparing two versions can still see when the measurement context
drifted.

## Immutable, versioned history

One JSON-lines file per key (`profiles-<key-digest>.jsonl`) in the vault
directory: a sealed header (key mapping + `key_digest`, `first_version`,
`latest_version`, `next_version`, `trimmed_versions`) followed by one
version record per committed profile. Versions are 1-based, monotonic and
never reused; each record's `previous_digest` is the record digest of its
predecessor (`sha256:genesis` for the first), so deleted or reordered middle
entries break the chain. Committed versions are never rewritten: `append`
of an identical profile digest is a content-addressed idempotent no-op
(retry-safe), and a genuinely new measurement simply gets the next version
number.

Retention is bounded by `max_versions` (default 32): the oldest versions are
trimmed on append and counted in the header. The dedup guard only knows the
retained window — re-appending a long-trimmed identical profile legitimately
creates a new version.

## Verification on read

Nothing is trusted. Every read (including the one `append` performs before
writing) verifies each line's own digest, reconciles the header counts
against the retained history, walks the version chain, reconstructs each
profile by re-running `model_calibration.calibrate` over the stored samples
— aggregates, failure rate, and inputs digest included — and refuses the
file if the recomputed profile digest, the key agreement, or the os_id
consistency check fails. Writes are atomic (temp file plus `os.replace`),
single-writer by convention. Filenames must match the header's key digest,
so renamed or swapped files are refused too. As with the AS-059 store, this
is tamper-evidence, not protection against an adversary who recomputes the
entire hash chain consistently by re-deriving valid profiles.

The vault stores no wall-clock timestamps and no prompt content beyond what
AS-059 profiles already carry (digests only), and it executes nothing: no
model loads, no runtime contact.

## Evidence

`tests/test_performance_profiles.py` (11 tests) covers append/version/reread
round trips with chain digests and summaries, idempotent duplicate
appends, per-setup key separation vs in-profile temperature drift,
descriptor-driven `for_lookup` (including a runtime-version mismatch
finding nothing), every validation refusal, snapshots without OS fields via
explicit `os_id`, bounded retention with no version-number reuse and the
documented dedup horizon, the full tamper ladder (stale line seal → resealed
line → fixed profile digest → header counts → renamed file → junk), a
content/key disagreement rewrite that fixes *every* digest yet still fails
the agreement check, value-object canonicalization, and quiet empty-vault
lookups.
