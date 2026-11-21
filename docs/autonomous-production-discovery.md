# AP-011 — Discover and inspect capability candidates without activation

**Status:** delivered as an offline proposal module; **not activated**, **not
wired into any production caller**, and **not a production guarantee**. It has
no ability to install, download or run anything. This page records what it
decides and what it refuses to decide.

## Story and owner scope

AP-011 (Phase P2) delivers capability discovery: diagnose what is actually
missing, record evidence-backed candidate proposals, and assess licenses and
install hooks before anyone commits to a dependency. Its dependency AP-009 is
done; it has no decision blockers. Owner approval is recorded in the AP ledger's
`authorization_record` and issue record.

The story's defining constraint is in its title: **without activation**.
`Assessment` refuses to construct with `authorizes_execution`,
`authorizes_activation` or `authorizes_installation` set, and `CandidateRegistry`
deliberately exposes no install, execute, run, download, activate, enable or
apply verb. There is no code path from a proposal to a running capability.

## What the module decides

- **Gap kinds are distinguished, not collapsed (AC1).** `classify_gap` separates
  six real causes — missing **facts**, wrong **method**, **context** exhaustion,
  **model** incapability, missing **tool**, **hardware** shortfall — and reports
  the cheapest explanation when several apply. This is the point of the story:
  installing a tool to fix a missing fact is the mistake it exists to prevent.
- **Discovery may conclude nothing is needed (AC2).** Empty signals return
  `NO_GAP`, and a no-gap registry **refuses to register candidates** rather than
  inventing a dependency to justify itself. A gap whose remedy is "collect the
  evidence" or "change the approach" recommends `no_new_dependency` even when
  candidates are sitting in the registry.
- **Candidates record their whole surface (AC3).** `CapabilityCandidate`
  requires source revision, maintenance evidence, license, install hooks,
  resource cost, egress and privileges. Unknown values stay unknown:
  `Maintenance.is_stale` returns `None` when it cannot be judged, and the
  assessment warns rather than assuming health. Install-time hooks and
  install-time network access are recorded and flagged; network at install time
  blocks outright.
- **Popularity never establishes trust (AC4).** Stars, download counts and MCP
  annotations are stored as an untrusted `popularity_claim` and explicitly
  excluded: `establish_trust` **raises** if they are offered as evidence, and
  every serialised candidate carries `popularity_establishes_trust: false`.
  Trust requires a verified release, a pinned signed artifact or a first-party
  audit, together with named evidence.
- **Unlicensed or incompatible candidates are blocked for the affected reuse
  only (AC5).** `assess_license` distinguishes *unlicensed* ("nobody said") from
  *unknown* ("we have not reviewed it") — both block, for different reasons.
  Copyleft candidates stay usable internally while blocking redistribution,
  bundling and — for AGPL — network-service reuse. Nothing is ever silently
  installed: the registry cannot install.
- **No execution or activation authority (AC6).** Every proposal states
  `discovery_grants: {execution: false, installation: false, activation: false,
  network: false}`. Encoded as a test: `CandidateRegistry` must not expose an
  install/execute/download/activate verb.

## Reviewability

Output is deterministic — sorted keys, sorted candidate order, trailing newline —
so two runs over the same evidence produce byte-identical proposals, and
`digest()` gives a stable identifier for review records. Candidates are assessed
against the repository's standing authority: egress outside the granted
allowlist is flagged as refused, and account, spend and secret access block,
because no current grant permits them.

## What this story does not decide

- **It does not install, download or execute.** Not "does not automatically" —
  cannot: there is no such verb.
- **It does not verify upstream reality.** Recording a `source_revision` or a
  `maintenance` observation records what the caller supplied; it does not fetch
  the repository, check the maintainer or prove a release exists. Evidence that
  needs the network belongs to a story with an egress grant.
- **It does not judge security.** License and privilege assessment is not a
  vulnerability audit; `supply_chain.scan_dependencies` remains the scanner, and
  a proposal is not an SBOM.
- **It does not decide adoption.** A `propose_for_review` recommendation names
  candidates for owner review. Adoption, installation and activation each need
  their own authorization.

## Verification

```sh
python -m pytest -q tests/test_autonomous_production_discovery.py
python autonomous_production_decisions.py
```

No installed-runtime qualification was run and no claim is made about the M5
host; `runtime_activation_authorized` stays `false`; ADR-0040–0045 remain
`not-reviewed`.
