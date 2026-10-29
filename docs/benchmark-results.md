# Reproducible benchmark publication

AS-048 provides an explicit packager for integrity-checked AS-059 calibration
profiles. `scripts/publish_benchmark_results.py` does not launch a model,
probe hardware, publish to a remote service, or fabricate values. Benchmark
collection remains the caller-supplied probe described in
[`model-calibration.md`](model-calibration.md); this tool packages resulting
raw profile JSONL records with their configuration, methodology, caveats, and
source digests for the calibration and packaging code.

> Receiving side: [`community-benchmarks.md`](community-benchmarks.md) covers
> importing a published bundle from somebody else, including license, provenance
> and hardware-identity validation and the untrusted-until-verified rule.

## Bundle contents and refusal rules

Each profile is re-loaded through `model_calibration.load_profile`, which
verifies line digests and recomputes the profile from raw samples. The bundle
preserves those original JSONL bytes and includes their SHA-256, profile and
input digests, a derived AS-060 `ProfileKey`, failure counts, config digest,
common hardware/OS/methodology context, and SHA-256 digests of both the AS-059
calibration module and the packaging script. A canonical bundle digest covers
the complete manifest and raw records.
Output is written atomically to a caller-selected path. Bundle digests detect
accidental or inconsistent edits; they are not signatures or proof that a
measurement came from the claimed hardware.

The packager refuses profiles with mismatched sample counts, duplicate model
builds, different hardware/OS/methodology contexts, non-digest prompt-set
fields, unsupported arbitrary hardware metadata, or free-form failure text
that may contain paths or prompt material. Failure reasons are only allowed
when they are the standard timing inconsistency or an exception type. The
packaged AS-059 inputs contain a prompt-set digest, never prompt text. Review
the selected raw profiles and caveats before distributing a bundle; the tool
cannot authenticate the caller or the physical device.

## Usage

1. Run AS-059 `run_calibration` with the declared warm/cold plan and save each
   profile using `save_profile`.
2. Copy `benchmarks/benchmark-config.example.json` to an external working
   location. Set the exact methodology and repetition counts, and replace the
   template caveats with what was and was not measured. AS-059 discards
   warmups, so the declared warmup count cannot be checked from persisted
   samples.
3. Explicitly package the verified raw profile files:

   ```bash
   python scripts/publish_benchmark_results.py \
     --config /path/to/release-config.json \
     --profile /path/to/model-a.jsonl \
     --profile /path/to/model-b.jsonl \
     --output /path/to/benchmark-bundle.json
   ```

4. Review the bundle digest, the full raw samples, model/hardware metadata,
   and caveats before deciding whether to publish or commit it. The command
   only writes the requested local file; it performs no network upload.

## Current evidence boundary

No physical-device raw benchmark bundle is checked into this repository.
`README.md`'s author-reported performance table remains explicitly
unverified, and AS-023 still lacks representative runs on physical 16/24/48
GiB machines. Synthetic fixtures verify the package format only; they do not
satisfy or imply those hardware results. AS-048's reproducible packaging
workflow is implemented, while publication of reviewed physical-device raw
results remains outstanding.

## Evidence

`tests/test_benchmark_publication.py` covers bundle creation and raw-byte
preservation, exact configuration/sample-count matching, context and identity
refusals, tampered profile files, prompt/error/hardware disclosure refusals,
canonical bundle tampering, atomic writes, and the command-line path. Fixtures
are synthetic. No live benchmark run or target-hardware result is claimed.
