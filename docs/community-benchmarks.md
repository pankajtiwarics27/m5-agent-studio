# Importing community benchmarks (AS-063)

**Story:** AS-063 — *Community benchmark import with provenance*.
**Acceptance criterion:** validate schema, license, provenance and hardware
identity; label imported results untrusted until locally verified.

[`benchmark-results.md`](benchmark-results.md) covers packaging your own
measurements. This page covers the other direction: taking a bundle somebody else
measured and importing it **without believing it**. Importing is how the project
accepts community numbers; it is not how it endorses them.

## 1. Why an envelope

An AS-060 publication bundle carries digests and a hardware *hash*, not the
hardware itself, and it says nothing about licensing — those are decisions the
packager deliberately leaves to whoever publishes. A community contribution
therefore wraps the untouched bundle in an envelope that adds exactly the three
things an importer needs to adjudicate:

```json
{
  "schema_version": 1,
  "license": {"id": "CC-BY-4.0", "url": "https://...", "attribution": "Example Lab"},
  "contributor": {"name": "Example Lab", "url": "https://..."},
  "hardware": {"schema_version": 1, "operating_system": "macOS", "architecture": "arm64", "...": "..."},
  "bundle": {"...": "the publication bundle, byte for byte"}
}
```

The bundle is **not** re-signed or rewritten. Its own `bundle_digest` is what
import verifies, so an envelope cannot repair an altered bundle.

## 2. Producing a contribution

Do not hand-assemble the envelope. The `wrap` subcommand takes a published bundle
and the hardware snapshot the profiles were measured on, composes the envelope,
and runs the full import validation before writing anything:

```bash
python3 scripts/benchmark_import.py wrap \
  --bundle dist/benchmarks/bundle.json \
  --hardware dist/benchmarks/hardware.json \
  --license CC-BY-4.0 --attribution "Example Lab" \
  --contributor "Example Lab" --contributor-url https://example.invalid \
  --out dist/benchmarks/contribution.json
```

A mismatch between the hardware snapshot and the bundle's published
`hardware_digest` fails here, at the person who can fix it, rather than at the
importer who cannot.

## 3. What import validates

```bash
python3 scripts/benchmark_import.py validate --contribution contribution.json   # check only
python3 scripts/benchmark_import.py import   --contribution contribution.json --registry ~/.local/share/m5-agent-studio/benchmarks
python3 scripts/benchmark_import.py list     --registry ~/.local/share/m5-agent-studio/benchmarks
```

| Check | Rules |
|---|---|
| **Schema** | Envelope `schema_version` is 1; only the five documented keys are allowed, so a contribution cannot smuggle in extra claims; size limits apply; the bundle's own schema version and `bundle_digest` must verify. |
| **License** | The `id` must be one of `CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`, `MIT`, `Apache-2.0`, `ODbL-1.0`, `PDDL-1.0`; licenses that require attribution must carry a non-empty attribution line; URLs must be `http(s)`. Anything else is refused rather than interpreted. |
| **Provenance** | Every profile's raw JSONL must hash to its `raw_sha256`, end with a newline, and re-load through `model_calibration.load_profile`; the recomputed `profile_digest`, `inputs_digest`, `model_digest`, and derived `ProfileKey` must match what the bundle claims; the config must hash to `config_digest`; the methodology must hash to `context.methodology_digest`; sample counts must match the published calibration plan; model identities must be unique. |
| **Hardware identity** | The claimed hardware snapshot must pass the AS-048 hardware allowlist (no private fields); it must hash to `context.hardware_digest`; `context.os_id` must agree with it; and every profile must derive a key with the same `hardware_digest` and `os_id`, so a contributor cannot attach somebody else's profiles to a nicer machine. |

Import never executes contributed code. Raw JSONL is parsed as data and staged in
a temporary directory; the loader that checks it is the same one the publisher
uses. There is no network access anywhere in the path.

## 4. Trust: untrusted until locally verified

Importing records every profile as **`untrusted`**. That is the state the data
stays in, and the registry says so on every read. Imported profiles are not
blended into local recommendations, comparisons, or profiles while untrusted.

Promotion is a separate, explicit act:

```bash
python3 scripts/benchmark_import.py promote \
  --registry ~/.local/share/m5-agent-studio/benchmarks \
  --profile-digest sha256:... \
  --vault ~/.local/share/m5-agent-studio/profiles
```

`promote` succeeds only when the local AS-060 profile vault already holds a
profile **with the same digest** under the **same key** (hardware, OS, model,
quantization, runtime version, methodology). In other words: we measured the same
configuration ourselves and got the same numbers. A contributor cannot promote
their own data by asserting it is correct, there is no "trust everything" switch,
and there is no promotion by reputation.

When the vault holds a *different* profile for the same key, promotion refuses
and says so: either the two measurements disagree, or the configuration differed.

## 5. The registry

`--registry` is a directory holding `imports.jsonl`, an append-only log:

- each line is one `import` or `promotion` record with a canonical
  `record_digest` over its own content;
- every read re-verifies every digest, so an edited registry fails loudly instead
  of serving a promotion that never happened;
- import records always keep `trust: untrusted`; promotions are separate lines, so
  the original claim and the later evidence stay distinguishable;
- re-importing an identical contribution is a no-op rather than a duplicate.

`list` also prints the attribution lines the imported licenses require, which is
the minimum a redistributor needs to satisfy CC-BY-style terms:

```text
2 imported profile(s) in ~/.local/share/m5-agent-studio/benchmarks/imports.jsonl
  untrusted         model-b                  sha256:212f...
  locally-verified  model-a                  sha256:ff93...

Attribution required by imported licenses:
- Example Lab (CC-BY-4.0)
```

## 6. Privacy and independence

- **No contact data.** A contributor is a name and an optional project URL;
  `email`, `handle`, and similar keys are rejected as unsupported fields.
- **No hardware serials.** The hardware snapshot goes through the AS-048 field
  allowlist, which rejects private metadata before it can reach the registry.
- **No prompts.** Import reads profile JSONL only; `prompt_set_digest` is a digest
  by construction, and prompt text is never part of a bundle.
- **No execution, no network.** Nothing contributed is imported, evaluated, or
  fetched over the wire.
- **Hardware/model/provider independence.** Import reads files; it never probes
  hardware, selects a model, or contacts a provider.

## 7. Limits

- There is no UI or HTTP surface for importing: this is a CLI and library, so an
  operator decides what enters the registry.
- Trust is per profile digest, not per contributor; there is no allowlist of
  trusted publishers, and adding one would be a separate decision with its own
  review.
- Promotion proves agreement with a local measurement, not that either measurement
  was taken correctly. Methodology quality remains a human judgement, which is why
  the methodology digest is part of the key.
- Bundles measured on a machine the importer does not have can never be promoted
  here; they can only be imported and read as untrusted data.
