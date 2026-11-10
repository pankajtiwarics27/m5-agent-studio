---
id: ADR-0040
title: artifact-identity-completeness-and-confined-references
status: accepted
date: 2026-11-10
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0040: Versioned artifact identity, completeness and confined references

## Context

`artifact_lineage` (M5-P5-02, [ADR-0019](0019-artifact-lineage-provenance-and-selective-invalidation.md))
records content digests, per-sighting provenance, deny-wins operation verdicts, evidence
freshness and retention. It answers "what are these bytes, where was this seen, and is the
evidence built on it still current". AP-001's reconciliation
([record](../autonomous-production-ap001-reconciliation.md)) records what it cannot answer
and what AP-006 therefore must: a content digest does not say *which version* this is
(producer, producer configuration, input versions), a caller cannot tell a finished,
checked artifact from a partial one, `source_ref` is a free-form string that nothing
confines, and there is no export or deletion path that keeps the reference history without
also keeping the content.

AP-006 requires checked artifacts to be the unit of delivery: verifiable identities for
bytes/inputs/producer/configuration/checks, input-change invalidation that spares unrelated
work, refusal of traversal, symlink escape, hash mismatch and unsafe active previews,
refusal to present partial or unchecked output as complete, and retention/export/delete
that preserve privacy and scoped reference history. The work must stay local and dormant:
it may not activate providers, models, network access, browsers, accounts or later AP
stories.

## Decision

Extend the existing artifact lineage sidecar instead of adding a second artifact store or a
second control plane. The store moves to `schema_version` 2 with three additive parts:

1. **Version identity.** A provenance entry may carry `producer`, `producer_version`, a
   producer-configuration digest (`config_sha256`), named input versions (`inputs`) and
   declared `required_checks`. `identity_sha256` is the SHA-256 of the canonical JSON over
   the content digest plus exactly those fields, so the same bytes produced under a
   different configuration or from a different input version are a different version — and
   a reader can recompute the identity instead of trusting a label.
2. **Check state and completeness.** Check outcomes are append-only records per artifact
   and check name; the newest outcome per name decides. `artifact_status` reports
   `complete` only when the artifact declares required checks and every one of them last
   passed. Missing checks, a failed check, absence of declared checks, and prior
   invalidation all report `partial` or `invalidated` with `complete=False`, and
   `require_complete` raises rather than letting a caller treat an unfinished artifact as
   accepted. Re-verifying after a fix clears the failure; the failure itself stays in the
   record.
3. **Confined references, scoped export and deletion tombstones.** `confine_reference`
   resolves one reference inside a caller-owned root component by component with `lstat`,
   refusing parent traversal, absolute escape, any symlinked component and non-regular
   files, and optionally requiring an exact digest match. `check_preview` refuses unknown,
   invalidated, untrusted, secret (unless explicitly allowed) and digest-mismatched
   content, and reports completeness so an unfinished artifact is labelled rather than
   hidden. `export_artifact` gates on confidentiality and the deny-wins verdict, marks a
   partial export `complete=False`, and appends the export to history. `delete_artifact`
   requires explicit confirmation and a reason, refuses pinned artifacts, valid evidence
   and current descendants, and writes a tombstone holding the identity, reason and scoped
   reference history (source kind, task id, trust, confidentiality, declared checks) while
   dropping the content and the free-form source reference.

Migration is additive and non-destructive. A populated v1 document carries every field v2
adds as optional, so it is read without rewriting and the next atomic store write stamps
schema 2 with every blob entry, evidence record and pin intact. A store written by a newer
schema is refused; a v1 binary refuses a v2 store rather than silently dropping the
identity fields, so recovery is "re-apply the newer module" or "restore a snapshot taken
before the upgrade", never a reset.

## Alternatives

- **A separate `artifact_identity` module and store.** Rejected: two stores for the same
  bytes would let the two disagree, and the deny-wins operation verdicts, evidence
  freshness and retention protection all already exist in one place.
- **Keep the identity in SQLite beside the execution spine.** Rejected for the same reason
  the original store lives outside SQLite: artifact lineage must be readable from failure
  paths where a transaction is not available, and AP-005 already owns the mission tables.
- **Treat "no declared checks" as complete for backward compatibility.** Rejected: it is
  exactly the false-completion AP-006 forbids. Legacy v1 artifacts report `partial` with
  `checks_not_declared` until someone declares and runs their checks, which is honest and
  costs an existing caller nothing, because no existing gate reads the new status.
- **Serve previews by validating the artifact digest only.** Rejected: a digest match says
  nothing about whether the path is inside an approved root or whether a symlink was
  swapped in after the build, which is the actual escape.
- **Erase an artifact on delete.** Rejected: erasing the reference and approval history
  makes a later audit unable to say what was once delivered. A tombstone keeps the
  identity and the history and drops the content.

## Tradeoffs

- Check outcomes are last-writer-wins per check name. A caller that records a pass after a
  real failure clears the failure; that is deliberate (re-verification must be possible)
  and the failure stays visible in `checks_for`, but a reviewer has to read the history
  rather than a single verdict.
- An artifact version identity is only as meaningful as the producer and configuration
  digests the caller supplies. The module validates their shape and binds them into the
  digest; it cannot prove the caller described its own producer truthfully.
- Confinement is a per-call filesystem check, not a reservation: bytes can change between
  the check and the read. The digest re-check narrows that window and turns a changed file
  into a refusal rather than a silently different preview.
- The store grows: check history, exports and tombstones are append-only, bounded per
  record but not globally collected. Retention still collects only artifact blobs, so the
  history files need their own future policy if usage grows.
- `schema_version` 2 means the previous build refuses the store. That is the intended
  fail-closed behaviour, and it makes downgrade a deliberate act rather than a silent loss.

## Review Status

Not reviewed. This record was written by an agent implementing AP-006 and records decisions,
not an independent review; `reviewed_by` and `reviewed_on` stay `none` until a human
reviewer signs the independent security/release review that AGENTS.md requires. Owner,
security, screenshot, media, signing and release approvals are not claimed here.
