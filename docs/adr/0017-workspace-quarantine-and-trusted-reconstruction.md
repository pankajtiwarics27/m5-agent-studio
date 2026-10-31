---
id: ADR-0017
title: workspace-quarantine-and-trusted-reconstruction
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0017: Byte-preserving workspace quarantine, baseline-plus-accepted reconstruction, and honest local irrecoverability

## Context

`M5-P5-01` requires that, after suspected contamination of a workspace, M5 mutations of the
affected workspace freeze and its bytes are preserved; that recovery happens by
reconstructing a *separate* workspace from a trusted baseline plus the changes an operator
accepted; that changes nobody can attribute are preserved separately rather than promoted;
and that verification is re-run under qualified isolation — with a host-level compromise
reported as *unresolvable locally* rather than papered over.

The code that existed before this decision could not express any of that:

1. **Rollback is not quarantine.** `edit_rollback.py` (`M5-P2-03`) undoes *known* edit
   batches from their pre-images. It has no notion of a workspace that is frozen, no copy of
   bytes it did not write, and no reason to preserve a file an unknown writer created.
2. **Snapshots are not evidence.** `workspace_snapshots.py` stores content-addressed
   pre-images for paths a managed edit touched, keyed per session. A file dropped into the
   workspace by something other than a managed edit has no snapshot at all, so "restore the
   snapshot" silently *drops* the very modification under suspicion.
3. **Incident containment is not workspace recovery.** `sandbox_boundary.SecurityIncidentStore`
   (`M5-P3-06`, ADR-0016) suspends guarantees and profiles and holds process containment
   barriers, but it never copies workspace bytes and never builds a replacement workspace.
4. **Nothing enforced a freeze.** Any recovery design that only *records* quarantine is a
   report, not a control: the next `write_file` would keep mutating the workspace whose
   evidence is being reconstructed.

The asymmetry that drives the design: destroying a byte is irreversible, admitting an
unknown change is irreversible (the attack becomes the baseline), and re-running a
verification suite on a possibly compromised host proves only that the compromise tolerates
that suite.

## Decision

Add `workspace_quarantine.py`, a stdlib-only module with three steps and one reported verdict.

- **Freeze preserves, and blocks.** `freeze_workspace()` copies every byte of the affected
  workspace into `$HARNESS_STORE_DIR/workspace_quarantine/QUARANTINE_ID/tree`, records size and SHA-256
  per entry in a `0600` `manifest.json` (private `0700` record directory), then makes the
  frozen tree read-only (`0500` directories, `0400` files). Symlinks are recorded as links and
  never followed, so a link pointing at a host file cannot pull host bytes into the evidence.
  The source workspace is never modified. `app._workspace_quarantined_denial()` consults
  `find_active_quarantine()` in the managed-edit gate, so every `write_file` / `edit_file`
  is refused with `error_code: workspace_quarantined` until an operator calls
  `release_quarantine()`. Unreadable quarantine state denies rather than admits (fail closed).
- **Reconstruct from baseline plus accepted changes only.** `plan_reconstruction()` classifies
  every difference between the frozen tree and an operator-supplied trusted baseline as
  *accepted* (named by the operator) or *ambiguous* (nobody named it). `reconstruct_workspace()`
  copies the baseline to a **new** destination (refusing an existing directory, so user work is
  never overwritten), overlays the accepted files after re-verifying each against the manifest
  digest, and writes ambiguous files to a sibling `DESTINATION.ambiguous` tree with an
  `AMBIGUOUS.md` index. An operator naming a path that was never preserved is an integrity
  error, not a licence to invent it.
- **Report recoverability honestly.** `classify_recoverability(host_compromise, isolation_qualified)`
  returns `unresolvable_locally` when the host may be compromised and no qualified isolation
  exists to verify under, else `recoverable_locally`; the result record carries
  `isolated_verification: verified_under_qualified_isolation | unqualified_isolation`. This
  sandbox is unqualified, so the honest answer today is the first one.

Freezing, accepting, and releasing are operator actions. Nothing in this module accepts a
path because untrusted text asked for it.

## Alternatives

- **Extend `edit_rollback` with quarantine.** Rejected: rollback semantics are "undo what the
  ledger says I did"; quarantine must preserve what *nobody* has a record of. Merging them
  would make the preservation guarantee depend on an edit ledger that by definition does not
  cover the attacker's writes.
- **Restore from `workspace_snapshots`.** Rejected for the reason in Context §2: snapshot
  restoration deletes precisely the unattributed changes that must be preserved.
- **Freeze by chmod-ing the live workspace in place.** Rejected: it mutates the evidence,
  destroys the user's ability to inspect their own work, and is not recoverable if the
  process dies mid-way.
- **Auto-accept changes newer than a cutoff, or trust anything not matching a known-bad
  hash.** Rejected: that is exactly "silently trust an unknown modification".
- **Delete ambiguous files after review.** Rejected: unreviewed data loss. They are kept in a
  separate tree, labelled, and referenced from the result record.
- **Re-run the verification suite on the affected host and report success.** Rejected: on a
  host that may be compromised, that proves nothing; `unresolvable_locally` is the truthful
  verdict until qualified isolation exists.

## Tradeoffs

- Quarantine costs one full copy of the workspace on disk, and a second copy per
  reconstruction. Accepted: the alternative is losing the only copy of contested bytes.
- The frozen tree is read-only, so *removing* a quarantine requires an explicit
  `chmod`/`rm -rf` by an operator rather than a convenience method. Accepted: evidence should
  not be one API call away from deletion.
- Freezing is enforced at the managed-edit gate, so a process that writes to the workspace
  without going through M5 tools (for example a supervised shell command an operator runs) is
  not stopped by the freeze. Reproduced honestly rather than overstated: this decision
  freezes *M5 mutations*, and full host-level write containment is a different guarantee
  (ADR-0016 / unqualified isolation).
- Reading quarantine state on every managed edit costs a directory listing and a manifest
  read; a corrupt or unreadable store denies the edit, so a broken store is louder than an
  admitted write.
- Verified facts: 18 tests in `tests/test_workspace_quarantine.py`, including an end-to-end
  refusal through `_execute_harness_tool`. Assumption: an operator can identify a trusted
  baseline; if they cannot, the correct outcome is *no reconstruction*, and the module has no
  code path that guesses one.

## Review Status

Not reviewed. Produced by the `M5-P5-01` implementation session on
`arena/98681cc6-m5-agent-studio`; independent review is required before this ADR is treated as
settled.
