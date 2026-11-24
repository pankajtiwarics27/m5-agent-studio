# AP-008: session-scoped artifact versions from the integrated routes

AP-008's first open blocker was that the Harness surface could list the *references* a
checkpoint recorded but could not say which **version of which artifact** a session left
behind. This slice closes that gap at the route the pane actually reads, and is explicit
about the part it cannot close.

## What a checkpoint records

`workflow_checkpoints.EvidenceRef` is `(kind, ref_id, sha256, summary)`. `to_dict()`
truncates the id to 64, the digest to 256 and the summary to 240 characters, and
`save_checkpoint` commits the record with a digest over the payload and the refs.

That is the whole of it. There is **no producer, no producer configuration and no input
version list** in a checkpoint. Those are exactly the axes
`artifact_lineage.artifact_identity_sha256(content_sha256, *, producer, producer_version,
config_sha256, inputs)` folds into a full version identity.

## The refusal that shapes this slice

The obvious implementation is to call the lineage authority and pass something for the
missing axes — the reference path as `producer`, a placeholder `inputs={"task_step":
"checkpoint"}`. That produces a stable, confident-looking digest over invented provenance.
Two artifacts with identical bytes recorded under different paths would then be reported
as different versions; two genuinely different production paths that ended in the same
bytes would be reported as the same one. Neither claim is one the record supports.

So this slice calls the authority with **only the axis that was recorded** and reports the
absence of the rest in the payload itself:

```
identity_basis:          "content_only"
identity_axes_recorded:  ["content_sha256"]
identity_axes_absent:    ["producer", "producer_version", "config", "inputs"]
contributes_a_verified_version:        True
rechecked_against_current_content:     False
is_global_lineage:                     False
is_workspace_snapshot:                 False
```

`rechecked_against_current_content` is `False` because the derivation reads a stored
pointer and never opens the file it names. The identity answers "same recorded bytes",
not "the file on disk still has these bytes".

Three things are therefore **not** available from this surface, and the payload says so
rather than leaving the omission to be inferred:

* a full version identity (the producer and input axes are not recorded),
* proof the content is current,
* any substitution of `artifact_lineage` (global, not session-scoped) or of a workspace
  snapshot (a different fact) for a session artifact version. The pane keeps the global
  lineage in its own, separately labelled row.

## Where it is wired

* `app.py` — `_session_artifact_versions(refs, *, session_id)`, next to the sibling
  `_..._summary_or_none` helpers.
* `GET /api/harness/sessions/{session_id}/task` and the snapshot behind
  `GET /api/harness/sessions/{session_id}/state` both return
  `session_artifact_versions`, derived from `latest_checkpoint.evidence_refs`.
* A checkpoint that fails its own integrity check yields `[]`, not a version list: the
  route reports the failure in `checkpoint_integrity_error` and refuses to let the
  checkpoint define a version.
## Why the pane does not draw it yet

This slice deliberately stops at the route. Two facts decided that:

* Every UIV2 run record pins `static/index.html`'s digest (`source_sha256`), and the
  UIV2 accessibility, performance, cross-tab and release-review records are bound to it.
  Any edit to the markup makes them stale, and making them fresh again requires the
  Chromium walks, which cannot run in this environment.
* The owner's conditional acceptance of the sampled Puppeteer render does not transfer to
  materially changed output, so a new row drawn into that list would need a fresh render
  to be evidence at all.

Shipping markup I cannot render, then regenerating four evidence records around a digest
I did not produce, would trade a real measurement for a green check. So `static/index.html`
is untouched and the pane keeps drawing what it drew before. The data the pane needs is
available and covered by tests; it is simply not drawn, and AP-008 is not done.

`studio.py` is generated from `app.py` + `static/index.html` by
`scripts/build_studio.py`; it was regenerated, not hand-edited, and CI's reproducibility
step confirms it.

## Negative and failure coverage

`tests/test_harness_ap008_recovery.py`:

| test | what it refuses |
| --- | --- |
| `test_session_artifact_versions_are_derived_from_the_checkpoint_not_global_lineage` | non-`artifact` kinds, an absent digest and a malformed digest each yield no version; the basis fields are the honest ones |
| `test_session_with_no_versionable_references_yields_nothing_not_a_guess` | a session with only actions, or nothing, yields `[]` |
| `test_identity_is_not_inflated_with_unrecorded_provenance` | the same bytes under a different path stay one identity (the refusal to pass the path as a producer) |
| `test_identity_ignores_the_reference_path_without_hiding_it` | order is deterministic and both references are still reported |
| `test_both_surfaces_carry_session_artifact_versions` | the integrated routes carry the field, and the pane reads it instead of the global lineage |
| `test_an_integrity_failure_suppresses_versions_instead_of_trusting_them` | a checkpoint whose stored payload was tampered with yields `[]` behind a real integrity error |
| `test_identity_ignores_the_reference_path_without_hiding_it` (second half) | the list is sorted, and both references survive |

## What this does not establish

* No independent security or release review. This is an agent-prepared slice.
* No runtime activation, no live dispatch, no IsolationAcceptance, no installed-runtime
  qualification. Everything here is offline contract plus a test client.
* No render evidence at all. The pane's markup is unchanged by this slice, so nothing new
  was drawn and nothing old was invalidated; the sampled-render approval stands exactly as
  far as it already stood and no further.
* AP-008's second blocker (independent review and final implementation validation) stays
  open, and the story's `acceptance_evidence` remains empty until a review exists.

## The renderer probe, and what it is not

The row this slice wants to add would carry a note line, which means changing
`harnessEvidenceFactListHtml` — the one function every fact row in the verification panel
already goes through. Its escaping is therefore load-bearing beyond this story.

`scripts/ui-probes/ev-facts-note.mjs` extracts the real `escapeHtml` and renderer from
`static/index.html` (or the proposed replacement in
`scripts/ui-probes/ev-facts-note.proposed.js`) and runs them in jsdom against markup,
handler attributes, a 5000-character label, quote-breaking input and non-ASCII text. It
asserts that a `<dt>`/`<dd>` is created with the input as verbatim text, that no element
and no `on*` attribute survives, and that empty notes emit nothing.

```
node scripts/ui-probes/ev-facts-note.mjs             # the shipped renderer
node scripts/ui-probes/ev-facts-note.mjs --proposed  # the replacement
```

`tests/test_harness_ap008_recovery.py::test_the_shipped_fact_row_renderer_turns_data_into_text_never_markup`
runs it, and **skips** when jsdom is absent rather than passing silently — a skipped probe
is not a passing one.

What it does not prove: layout, focus order, theming, 200% zoom, or anything a screenshot
would show. It proves data becomes text. It is not a substitute for the Chromium walks
that produce the source-bound captures, and it does not restore the missing render
evidence described above.
