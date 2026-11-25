# AP-020 — versioned owner-approved reference benchmarks

AP-020 exists because of one failure mode: work that once went well becomes a standing
permission. A page is approved, and from then on every page "of that kind" is treated as
approved, published and verified. This note records what was built, what it refuses, and where
each acceptance criterion is proved.

## The rule the story applies

**A reference is an owner's decision about one artifact version, and it settles exactly what that
version established.** It is not acceptance of anything, and it is not permission to ship.

Four things are recorded separately, and the code refuses to let them collapse:

| name | what it means | where it is enforced |
| --- | --- | --- |
| `use_as_reference` | this accepted artifact may be reused as an example | `accept()` refuses an approval without it |
| `final_acceptance` | *some other piece of work* is accepted | `Reference.__post_init__` raises if a reference carries it |
| `publish` | work may be shipped or deployed | same, and `from_mapping()` refuses a stored row claiming it |
| `continue_direction` | the current run may keep going | owned by AP-018; a reference never touches task state |

The tempting shortcut — "the owner already said they liked it" — is refused explicitly:
approving an artifact is not adopting it as a reference, and `accept()` says so in the message it
raises rather than silently widening the grant.

## An agent cannot approve anything

`OwnerApproval` requires an actor namespaced `owner:`, `user:` or `reviewer:`, and refuses the
machine names outright (`agent`, `system`, `automation`, `assistant`, `model`, `scheduler`). The
approval must carry the owner's own words, verbatim; an empty statement is refused rather than
defaulted.

This is enforced in three places, because one is not enough:

- at construction, so a fresh record cannot be built around a machine's approval;
- at `from_mapping`, so a hand-edited database row cannot become authority;
- at the route, which offers no field that would let a client supply an actor of its choosing.

The pane goes further than the module requires. There is no stored owner identity in the app, so
the pane asks the owner to type their name and records `owner:<name>`. If the name is missing it
refuses and sends nothing: a guessed approver is a fabricated one.

## Versions, and the acceptance that is never rewritten

`enhance()` requires a strictly greater version and a **fresh** approval. An owner who approved
version 1 did not thereby approve version 2.

`supersede()` writes only the link forward. The earlier version keeps its artifact digest, its
approval, its brief and its checks; only `status` and `superseded_by` change. `revoke()` names
the person who withdrew the reference and keeps the record — revocation is a status, not a
deletion.

The store enforces this too, not just the functions: `ReferenceStore.put()` refuses to replace a
row whose approval or artifact digest differs from what is stored. "Prior acceptance cannot be
silently rewritten" holds at the boundary where a careless script would actually do it.

## Retrieval by audience, language, task and style

`rank()` offers references for a task and explains each choice. A reference for another audience
or another language is **labelled** ("different language (hi)") rather than hidden or silently
offered as an equivalent.

Crucially, `rank()` never scores layout. A layout is an outcome of the work, not a parameter of
it, and scoring it would quietly turn "reuse the tone" into "copy the arrangement". The pane
repeats the point: `cannot_settle` is printed beside every list.

## Coverage comes from the store, never from the request body

The direction route used to build `BenchmarkCoverage` from the caller's JSON. A caller could type
a reference id and a digest it invented and be told the owner had already approved the direction.
That is the hole this story closed.

The route now reads the project's live references from `ReferenceStore` and applies two narrowings
before a stored row counts: the row must belong to the session's own derived project scope, and
the requested checks must have been measured and passed on that exact version. A caller may name
the references it relies on; naming one the project does not hold is a **400**, not a silent
`skipped`.

A project scope is derived from the project root (`derive_project_scope_id`), never accepted as a
nickname. `proj-someone-else` is refused.

## What a reference can never discharge

`verification_still_required(changed=[…])` returns the obligations a style reference cannot meet:

| change | obligations that remain |
| --- | --- |
| code | `changed_code_works`, `security_sign_off` |
| facts | `changed_facts_are_true` |
| assets | `changed_assets_are_licensed` |
| screenshots | `screenshot_approval` |
| *any* | `final_acceptance_of_new_work`, `publish_permission` |

`REFERENCE_CANNOT_SETTLE` names them once, and every route response repeats the list beside the
coverage it reports, so no client can read `covered: true` as "approved to ship".

## Surface

| route | behaviour |
| --- | --- |
| `GET /api/reference-benchmarks?session_id=…&task_kind=…` | catalogue or ranked list with `why_offered` |
| `GET /api/reference-benchmarks/{reference_id}` | every version, oldest first |
| `GET /api/reference-benchmarks/{reference_id}?version=N` | one version |
| `POST /api/reference-benchmarks` | `accept()`; the owner's approval is the only way in |
| `POST /api/reference-benchmarks/{reference_id}/enhance` | a new version with its own approval |
| `POST /api/reference-benchmarks/{reference_id}/revoke` | status change, record kept |
| `POST /api/reference-benchmarks/coverage` | read-only: what is settled and what is not |

`reference_benchmarks.py` is pure: no clock, no environment, no network, no file at import time.
A test enforces it, and another enforces that it shares no top-level name with `app.py` — a
collision in the single-file build once replaced the app's `SCHEMA_VERSION`.

## Surface, in the interface

The evidence dialog gained a References block under the feedback composer. Each row shows the
reference id and version, the brief, the owner's own approval words, the artifact digest it points
at, and why it was offered. `status` is drawn as a badge, and a superseded row says what replaced
it while still showing the earlier approval as it was given. The block closing every state — empty,
populated, failed — says what a reference never settles.

Keyboard and screen-reader behaviour follows the existing contract: the approver field and the
statement are real labelled inputs, the submit is a native button, and the note beside it is a
`role="status"` live region, so a refusal is announced where the control is.

## Acceptance criteria → evidence

1. **Record the brief, exact artifact, provenance, scope, checks and configuration.**
   `ArtifactVersion` carries `content_sha256` plus optional `identity_sha256` / `identity_basis` /
   `producer` / `producer_version` / `config_sha256`; a `content+provenance` identity without its
   digest is refused rather than downgraded, and `content_only` never presents itself as full
   provenance. Tests: `test_a_reference_records_the_brief_artifact_provenance_scope_and_checks`,
   `test_a_content_only_identity_never_presents_itself_as_a_full_one`,
   `test_a_provenance_identity_without_its_digest_is_refused`; route
   `test_a_reference_needs_the_exact_artifact_and_its_digest`.
2. **Continue-direction, final acceptance, use-as-reference and publish are separate grants.**
   `APPROVAL_KINDS` names all four; a reference raises if it carries `is_final_acceptance` or
   `grants_publish_permission`, and `from_mapping` refuses a stored row that claims either. Tests:
   `test_approving_a_reference_is_not_approving_the_artifact_or_publishing_it`,
   `test_an_approval_that_accepted_the_artifact_but_did_not_adopt_it_is_refused`,
   `test_a_stored_record_claiming_publish_permission_is_refused_at_read_time`.
3. **Enhancements create a new version; prior acceptance is never silently rewritten.**
   `enhance()` refuses a version that does not increase and requires a fresh approval;
   `supersede()` moves only the forward link; `ReferenceStore.put()` refuses a changed approval or
   a redirected artifact. Tests: `test_enhancing_creates_a_new_version_with_its_own_approval`,
   `test_superseding_links_forward_without_altering_the_earlier_acceptance`,
   `test_the_store_refuses_to_rewrite_an_acceptance`,
   `test_the_store_refuses_to_redirect_an_accepted_version`; route
   `test_an_enhancement_adds_a_version_and_leaves_the_first_acceptance_intact`.
4. **Retrieval by audience, language, task and style without forcing identical layouts.**
   `rank()` scores task, audience, language and style tags — and never layout — and labels a
   language mismatch instead of hiding it. Tests:
   `test_retrieval_prefers_a_reference_for_the_same_audience_language_and_task`,
   `test_retrieval_never_scores_layout_because_layout_must_not_be_copied`,
   `test_a_reference_for_another_language_is_labelled_rather_than_silently_offered`; probe check
   that a shared style is shown and layout is not.
5. **The agent cannot synthesize owner approval, security sign-off or screenshot approval.**
   The namespaced-actor rule, the refusal of machine names at construction and at `from_mapping`,
   and the pane's refusal to record without a typed name and statement. Tests:
   `test_a_machine_actor_cannot_approve_anything` (six machine names),
   `test_a_stored_record_with_a_machine_approver_is_refused_at_read_time`,
   `test_an_agent_cannot_record_its_own_approval`; probe check that the pane's refusal path sends
   nothing.
6. **Changed facts, code or assets still require their own verification.** `verification_still_required`
   and `REFERENCE_CANNOT_SETTLE`, returned beside every coverage answer. Tests:
   `test_a_style_reference_does_not_settle_changed_code`,
   `test_changed_facts_and_assets_carry_their_own_obligations`,
   `test_coverage_names_the_obligations_a_changed_artifact_brings`; probe check that the pane
   names what a reference never settles.
7. **`docs/autonomous-production-ux-direction.md` is satisfied, with real implementation review.**
   Direction approval, final acceptance, reference adoption and publish permission stay distinct
   in the record and in the pane's own words. The reference pane never writes task state, so a
   paused-for-direction task is untouched. The reference-pane probe (`scripts/ui-probes/reference-panel.mjs`)
   runs the shipped functions against hostile input in jsdom; the Chromium walks remain the
   evidence for layout and focus.

## The hole that was found while building this

`app.py`'s direction route built `BenchmarkCoverage` from the request body, and silently skipped
any row that was not a dict. A caller could therefore assert coverage — including a digest it made
up — and the task would be told the owner had already approved that direction. Tightening the
validation would not have fixed it: the row had to be looked up in the store. The route now does
that, and refuses an unknown id instead of skipping it.
