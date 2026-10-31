---
id: ADR-0019
title: artifact-lineage-provenance-and-selective-invalidation
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0019: Content identity separated from provenance, per-artifact trust, and evidence that expires

## Context

`M5-P5-02` requires that lineage and trust be tracked "without a general proof engine":
keep content hashes apart from provenance and confidentiality, record derived artifact
references and the operations allowed on them, invalidate affected evidence after a
source or security change, and implement retention that protects what is still in use.

The repository had three pieces that nearly, but not quite, met this:

1. **Content-addressed storage without trust.** `workspace_snapshots.ContentAddressedBlobStore`
   deduplicates bytes by digest, and `edit_rollback` reuses it for pre-images. Dedup is
   correct — identical bytes are one blob — but the *store* says nothing about where a
   blob came from. A blob captured from a trusted baseline and a blob produced by a
   contaminated run are the same kind of object, so "this content is trusted" was not a
   question the code could ask, let alone answer.
2. **Evidence tied to a revision, not to a security configuration.**
   `verification_evidence.TestEvidence` records the source revision before and after a
   verification command, and `workflow_state` refuses a completion whose revision moved
   mid-run (`workspace_changed_during_verification`). Nothing carried the *security*
   configuration: evidence gathered under one container runtime, image digest or
   hardening-flag set was accepted after those changed, even though `M5-P3-06` already
   treats such a change as a new `ConfigurationGeneration`.
3. **Recovery bytes with no retention guarantee.** `M5-P5-01` freezes a contested
   workspace into a quarantine tree and refuses edits while it is unreleased, but nothing
   in the system knew that those bytes must never be collected. A GC written later
   against "old artifacts" would have deleted the only copy of contested work.

There was also no agreed answer to a subtler question: when the same bytes arrive from a
trusted and an untrusted source, what is the artifact's trust? Any answer that collapses
the two histories — "latest wins", "trusted wins", "average them" — permits laundering:
one trusted sighting would launder everything an attacker had already placed.

## Decision

Add `artifact_lineage.py`, a stdlib-only registry with a durable store
(`$HARNESS_STORE_DIR/artifacts/state.json`, under the shared durable-JSON contract).

- **Dedup the content, never the history.** Content identity is the SHA-256. Each
  sighting appends a `ProvenanceEntry` carrying its own `source_kind`, `source_ref`,
  trust, confidentiality, operation lists, task, and security generation. Nothing
  overwrites an entry. Every derived answer is the **most restrictive** one:
  `effective_trust` is the weakest entry, `effective_confidentiality` the strictest, and
  `check_operation` is deny-wins across entries — an artifact with one `read`-only entry
  cannot be exported because another entry allowed it.
- **Unknown content is trusted by nothing.** A digest with no provenance reports
  `untrusted`/`secret` and refuses every operation (`unknown_artifact`). A parent digest
  named by a derivation but never seen is recorded as a stub, not as a trustworthy root.
- **Invalidation is recorded, cascading, and non-destructive.** `invalidate()` marks
  entries with a reason and cascades to artifacts derived from them (bounded depth), and
  moves evidence computed over them to `stale`. Bytes and history stay readable; they
  stop being usable as if current.
- **Evidence expires against its own inputs.** `record_evidence()` stores the source-file
  digests and security generation the evidence was computed against.
  `accept_evidence()` re-checks them and, on any mismatch, refuses *and writes the refusal
  into the record*, so a later reader cannot flip it back to valid by re-reading a changed
  filesystem. `None` generation means "not checked", never "unchanged".
- **Retention protects what is still in use.** `plan_retention()` keeps an artifact when
  an active task references it, when a caller-supplied protection names it, when a
  recorded pin names it, when valid evidence references it, when it is inside the grace
  window, or when it is confidential (unless secret eviction is explicitly allowed). It
  returns a plan with a reason per survivor; `apply_retention()` deletes only the planned
  set.
- **The recovery path owns its own protection.** `freeze_workspace` records the frozen
  tree digest as an untrusted, confidential artifact that denies `export`/`prompt`, and
  pins it under `quarantine:QUARANTINE_ID`. `workspace_quarantine.recovery_protection()` re-derives
  the same protection set from the quarantine store for any collector, so protection does
  not depend on a caller remembering the pin. `release_quarantine` lifts the pin and never
  deletes the bytes.
- **The live gate is in the completion path, not in the registry.** `workflow_state`
  reads the security generation before it gathers evidence and again before it returns a
  verdict; if it moved, the run returns
  `security_configuration_changed_during_verification` instead of `verified=True`. `app`
  supplies the provider from the incident store. A caller with no provider gets no
  staleness claim, and the report does not pretend otherwise.

## Alternatives

- **Store trust on the blob.** Rejected: that is precisely the merge this story forbids —
  the blob is the identity, provenance is a history, and one field cannot hold both.
- **"Latest sighting wins".** Rejected: lets a hostile write followed by any benign
  re-observation of the same bytes become trusted.
- **Extend `workspace_snapshots` to carry trust.** Rejected: the snapshot store is the
  content-addressed *byte* layer used by rollback on the hot edit path; making it the
  trust authority would couple evidence semantics to snapshot capture and slow the path.
- **A general trust calculus / proof engine.** Rejected by the story's own objective, and
  unsound in practice: the interesting inputs (what a program will do) are undecidable.
  The module records lineage and refuses unjustified promotions; it does not claim
  soundness.
- **Delete invalidated evidence.** Rejected: "this was true once and stopped being true
  because X" is the audit trail an operator needs. Staleness is a status, not a deletion.
- **GC by age alone.** Rejected: it deletes the quarantine copy of contested bytes, which
  is the failure this story exists to prevent.
- **Let `artifact_lineage` import `workspace_quarantine` to find unreleased quarantines.**
  Tried and rejected: the lazy import broke the generated standalone artifact, which
  `tests/test_release_manager.py` caught by scanning `studio.py` for local imports. The
  dependency is inverted instead — the quarantine module exposes
  `recovery_protection()` and the collector passes it in.

## Tradeoffs

- The store grows with every sighting, not every byte: recording the same contended blob
  a thousand times writes a thousand entries. Bounded in practice by the call sites
  (freeze records one entry; verification records one evidence record per attempt), and
  retention collects artifacts, not entries.
- Deny-wins operation checks mean one over-restrictive entry narrows an artifact forever.
  Accepted: narrowing fails safe, and an operator can record a fresh, narrower-scoped
  entry rather than editing history.
- The security-generation gate compares two reads around the verification run; a change
  *outside* that window is invisible to it. It closes the "evidence accepted under a
  different configuration" hole, not every timing hole — and a run with no provider is not
  gated at all, which the API makes explicit rather than silent.
- Evidence pinned by `valid` status keeps bytes alive; a task that never completes could
  pin content indefinitely. Mitigated by active-task protection being explicit input to
  the plan rather than inferred from evidence alone.
- Verified facts: 28 tests in `tests/test_artifact_lineage.py` (18 lineage/retention, 3
  live-gate) plus the store inheriting the 8-clause durable-JSON contract in
  `tests/test_durable_state_contract.py`. The live-gate test fails when the comparison is
  removed. Assumption: a caller can identify its active tasks; when it cannot, nothing is
  treated as active and the grace window plus pins are the only protections.

## Review Status

Not reviewed. Produced by the `M5-P5-02` implementation session on
`arena/6da06a20-m5-agent-studio`; independent review is required before this ADR is
treated as settled.
