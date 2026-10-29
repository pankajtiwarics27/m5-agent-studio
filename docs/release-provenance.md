# Release evidence: SBOM, scans, provenance, signing (AS-042)

**Story:** AS-042 — *Release signing, SBOM and provenance*.
**Acceptance criterion:** SBOM, scans, signed artifacts and provenance available
for release.

This page describes the release evidence the project can actually produce, and
the one piece it deliberately cannot: a signature, because no maintainer signing
key exists in this repository or in CI. Everything else is real, reproducible,
and checked. The signing path is an explicit hook that fails closed rather than
a badge that claims more than it can prove.

## 1. What exists

| Piece | Contract |
|---|---|
| `supply_chain.py` | Pure library (mypy-gated): subject hashing, runtime-closure component collection, CycloneDX SBOM, in-toto/SLSA provenance, secret and dependency scans, signer protocol, release gate. No network, no shell, no root. |
| `scripts/release_supply_chain.py` | CLI: `build`, `verify`, `gate`. |
| `tests/test_supply_chain.py` | 58 tests: SBOM and provenance shape, deterministic rendering, secret/dependency scan outcomes including `unavailable`, signature record validation, command signer round-trip and failure modes, gate matrix, and CLI end-to-end (signed, unsigned, tampered, missing evidence). |

The three commands answer three different questions, and conflating them is how
release tooling ends up lying:

| Command | Question | Exit status |
|---|---|---|
| `build` | Did we write evidence for this artifact? | `0` whenever the evidence files were written |
| `verify` | Does the evidence still describe *this* artifact, and did the scans run? | `1` on drift, a failed scan, or a scan that never ran |
| `gate` | May this artifact be called a release? | `1` until SBOM, provenance, passing scans, and a real signature are all present |

`build` exits `0` even when the gate is closed, so publishing evidence is never
blocked by an unprovisioned key; the release decision is `gate`'s job.

## 2. Producing evidence

```bash
python scripts/release_supply_chain.py build  --version 9.0.1 --output dist/evidence
python scripts/release_supply_chain.py verify --version 9.0.1 --output dist/evidence
python scripts/release_supply_chain.py gate   --version 9.0.1 --output dist/evidence
```

The evidence directory (`dist/evidence/` by default, ignored by Git):

```text
dist/evidence/
├── sbom.cdx.json            CycloneDX 1.5, components sorted, subject digest
├── provenance.intoto.jsonl  in-toto Statement v1, SLSA provenance v1 predicate
├── scans.json               every scan with an explicit status and findings
├── signature.json           detached signature over the artifact digest (only when signing is configured)
└── evidence.json            gate verdict, digests, scan statuses, file list
```

`--deterministic` drops timestamps so repeated builds of the same artifact are
byte-identical; CI relies on that, and `tests/test_supply_chain.py` asserts it.
`--source-commit` overrides the recorded commit (default: `git rev-parse HEAD`).

## 3. What the SBOM describes

The component list is the **installed runtime closure**: the five declared
dependencies in `pyproject.toml` (`fastapi`, `uvicorn`, `httpx`, `httpcore`,
`keyring`) expanded through `Requires-Dist` metadata to every distribution they
pull in — 30 distributions in the reference environment. Requirements guarded by
an `extra` marker are excluded, because optional features are not shipped.

Each component carries name, version, a `pkg:pypi/...` purl, and a licence when
the metadata states one recognisably. Free-text classifier labels such as
`Apache Software License` are emitted as a licence `name`, never as an invented
SPDX `id`, because CycloneDX treats `id` as a claim.

A dependency named in `RUNTIME_DEPENDENCIES` that is not installed is an error,
not a silent omission: an SBOM that quietly drops a component is worse than no
SBOM.

## 4. What the provenance binds

`provenance.intoto.jsonl` is an in-toto Statement whose subject is the artifact
digest, with a SLSA v1 predicate recording:

- `buildType`: `https://m5-agent-studio.dev/buildtypes/standalone-python-artifact/v1`
- `externalParameters.sourceCommit` — required; provenance without a commit is refused
- `externalParameters.version`, `builder`, `pythonVersion`, `platform`
- `externalParameters.sbomSha256` — binds the SBOM file to the statement
- `resolvedDependencies` — the same components as the SBOM

The statement describes the *local* build of `studio.py`. It is not an attestation
that a hosted builder ran: no CI-side signing identity exists, so no claim is made
about one.

## 5. Scans

Two scans run on every build, and each records `passed`, `failed`, or
`unavailable`:

| Scan | Scope | Honest outcome |
|---|---|---|
| `artifact-secrets` | The packaged `studio.py`, streamed in 1 MiB chunks | Six high-confidence credential shapes (AWS access key, private key block, bearer token assignments, and similar). Findings record the identifier and location only — the matched secret is never copied into evidence. |
| `dependency-vulnerabilities` | The same runtime closure as the SBOM, via `pip-audit` | `unavailable` when `pip-audit` is not installed or its advisory data cannot be fetched |

`unavailable` is never reported as `passed`. `verify` fails on it and `gate` stays
closed, with the reason naming the scan and what to install.

Scan findings outside the shipped closure are counted, not silently dropped: the
build venv contains packaging tooling (`pip`, `setuptools`) that is not part of
the artifact, and a full-environment audit reports advisories against it. The
scan detail states how many such findings were excluded from the release
decision. Reviewing that tooling is a separate supply-chain task, not something
this gate can decide.

`pip-audit` is an optional extra so it never becomes a runtime dependency:

```bash
python -m pip install -e '.[dev,supply-chain]'
```

## 6. Signing: an unprovisioned hook, not a badge

Signing needs a key this repository does not have. Rather than emit a
"signature" that proves nothing, the default signer fails closed:

```bash
python scripts/release_supply_chain.py gate --version 9.0.1 --output dist/evidence
# {"ready": false, "reasons": ["Artifact is not signed."], "warnings": []}
```

To sign, provide an external tool as argv — never a shell string:

```bash
export STUDIO_RELEASE_SIGN_COMMAND="/usr/local/bin/cosign sign-blob --yes"
export STUDIO_RELEASE_VERIFY_COMMAND="/usr/local/bin/cosign verify-blob"
python scripts/release_supply_chain.py build --version 9.0.1 --output dist/evidence
```

The contract: the sign command receives a temporary file containing the hex
digest and prints the base64 signature on stdout; the verify command receives the
digest file and the signature file and exits `0` for a valid signature. Setting a
sign command without a verify command is refused outright — an unverifiable
signature is not evidence. A sign command that fails, prints nothing, or produces
a signature that does not verify raises `SigningError`, and no `signature.json`
is written.

`verify` re-runs the configured verify command against the recorded digest, and
reports a mismatch between the configured signer and the recorded signer, so a
re-signed artifact cannot inherit an older record.

## 7. Security and privacy properties

- **No shell.** External tools are invoked as argv lists; nothing is passed to
  `sh -c`, so no evidence value can become a command.
- **No network.** The library and CLI make no outbound request. `pip-audit` is
  the only component that talks to an advisory source, and its failure degrades
  the scan to `unavailable`, which closes the gate rather than passing it.
- **No secrets in evidence.** The secret scan reports identifiers and locations
  only; SBOM, provenance, and gate output contain names, versions, digests, and
  paths, never credentials or prompt content.
- **Local files only.** Evidence is written under the output directory you name;
  nothing is installed, activated, or uploaded. `dist/` stays out of Git.
- **Hardware, model, and provider independence.** Nothing in the evidence path
  reads model configuration, provider credentials, or hardware capability data;
  the platform string is recorded for diagnosis, not used for decisions.

## 8. CI

CI installs `.[dev,supply-chain]`, builds evidence for `studio.py`, runs
`verify`, and uploads the evidence directory as a build artifact. CI does not run
`gate`: without a signing key in CI the gate must stay closed, and wiring a
command that is expected to fail would train people to ignore it. `gate` is a
release-day command that a maintainer runs with the key configured.

## 9. Limits

- No signing key exists here, so no artifact produced in this repository is
  signed today. `gate` therefore reports `state` closed: the story is
  implemented and tested but not verified end to end with a real key.
- Signature transport, key custody, and rotation are maintainer decisions
  outside this story.
- Notarization and hosted-builder attestations are not implemented.
- Reviews of build-time tooling in the venv (`pip`, `setuptools`) are a separate
  task.

Related: [`packaging-rollback.md`](packaging-rollback.md) for install, update, and
rollback of the artifact this evidence describes.
