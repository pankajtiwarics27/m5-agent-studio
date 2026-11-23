# AP-008: session checkpoint evidence references

## Scope and authority

The existing authenticated Harness session task route reads the latest checkpoint with `verify_integrity=True`, returns its owning task ID and attached `EvidenceRef` values (`kind`, `ref_id`, `sha256`, `summary`), and separately exposes an integrity error. The producer can record committed action references, supplied artifact paths and digests, or a Git-diff digest; references can also originate in other checkpoint transitions. They are not necessarily files. Display a bounded, inert, session-fenced list in the existing verification and artifacts modal, using only the verified latest checkpoint and checking its task ID against the selected session. Never fall back to the unverified checkpoint list. Distinguish unavailable, corrupt, foreign, malformed and empty records. Escape all untrusted fields, bound counts and lengths, and do not turn references into links or fetches.

An evidence pointer is not a versioned artifact or source, not a workspace copy, and its recorded digest is not a fresh verification of content. The checkpoint integrity check covers the stored bundle, not current files. Do not imply completeness, reachability, approval or validation. Global lineage is not session-owned; label it accordingly. No new route, authority, execution, persistence, file access, artifact viewer, or provider effect is added. Previously saved verdicts and snapshots are not artifact versions. Missing session-specific artifact-version/source functionality remains unfinished.

## Validation and rollback

Exercise populated, empty, malformed, corrupt, foreign, hostile and oversized refs, plus unavailable task reads and session switching. Use the actual durable session route and browser keyboard/mobile checks, and source-bound inventory and release gates. Rollback reverts the display and its generated artifacts; no migration is needed.
