# Preservation approval binding follow-up

This branch follows up [#126](https://github.com/pankajtiwarics27/m5-agent-studio/issues/126)
without changing the recorded owner decision or taking ownership of a UI/UX story.
The existing signature records a historical decision, but does not identify the
exact input hashes approved. The generator currently places it next to hashes of
current artifacts, even after those artifacts change.

The schema-3 package distinguishes `unsigned`, `historical-approval-unbound`,
`approval-inputs-match` and `historical-approval-input-drift`. Its binding reports
current and owner-recorded digests separately. An absent legacy snapshot yields
unknown comparison results, not an invented match or a new signature.

For a future explicitly bound decision, `python3 scripts/uiux_preservation_signoff.py
--json` exposes `approval_binding.current_inputs`. An owner may review the exact
snapshot and decide whether and how to record it with `binding_schema` set to
`m5-uiux-inputs-v1`, `approved_inputs` set to that complete digest map and a
`binding_reference` identifying the owner's explicit decision on that digest set.
An agent must not add or change the owner's approval. This is a proposed format,
not a migration or approval of the existing signature.

The binding hashes the actual frontend, the manifest, inventory, route map,
authorization trace, screenshot manifest/index and the two generator scripts.
It hashes a canonical UTF-8 JSON form of the complete ledger with only
`coordination.preservation_signoff` removed (sorted keys, compact separators),
avoiding a self-referential signature hash. Changes to any other ledger content
produce drift; formatting alone does not. A missing screenshot index is represented
by `null`. Images themselves are not checked by this binding. Digest comparison
cannot authenticate a signer or prove what an external approval reference covers;
review the record and history independently.

An unbound or drifted record does not invalidate the historical owner decision,
but does not establish that the current snapshot is approved. Regenerating output
can make `--check` pass while the comparison still says drifted. The owner must
decide whether to retain the historical record unbound, explicitly bind a
reviewed historical snapshot, or issue a new approval; do not infer that decision.
The separate screenshot provenance hold in [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122)
remains open.
