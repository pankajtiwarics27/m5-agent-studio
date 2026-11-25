# AP-021 — a complete website draft workflow

AP-021 exists because "generate a website" is the easiest thing in this programme to do
dishonestly. Every failure mode below produces something that *looks* finished, and each one has
already shipped somewhere: a testimonial from a customer who does not exist, a footer row of
logos for integrations nobody connected, a hamburger menu with no handler behind it, a hero
image whose licence was never checked, and a "we deployed it" claim covering a draft that was
never served.

The module is the refusal list. Producing the page is the executor's job.

## The rule the story applies

**A draft is only complete when the checks ran, the asked-for interactions have honest verdicts,
and nothing in it is an unsourced claim about the world.**

`WebsiteDraft.status()` is derived, never assigned. It is `complete` only when all four required
checks passed, every requested interaction is `supported` with evidence, and no asset is still
`unknown`. Everything else is `partial` and carries the reasons.

That matters more than it sounds: the tempting implementation is a boolean the caller sets. A
derived status cannot be set, so a run cannot decide it is finished.

## Scope before work, and the two things it must name

`DraftScope` requires an audience, at least one page, and — the part people skip — at least one
thing that is **out of scope**. A brief that excludes nothing has not been narrowed, and an
unnarrowed brief is how a draft becomes a different product halfway through.

`DraftScope.no_layout_benchmark` records whether an owner-approved reference existed when the
brief was taken. It does not *decide* anything: the early-sample gate is AP-018's, and re-deciding
it here would duplicate the gate that already exists. What AP-021 owes is the record.

## Interactions: unsupported is an answer, silence is not

Every interaction the brief asked for must come back with a verdict. A missing verdict is
refused by name — `the brief asked for interactions this attempt gave no verdict on: …` —
because a missing verdict and a missing feature are indistinguishable to a reader.

| state | meaning | counts as done |
| --- | --- | --- |
| `supported` | it works, and something exercised it | yes |
| `supported_untested` | the handler exists, nothing proved it | **no** |
| `unsupported` | it is not implemented, and the record says why | **no** |

`supported_untested` is the one that earns its place. It is the state a run falls into when it
writes a click handler and stops, and without it that run would have to choose between lying
(`supported`) and pretending the feature was never asked for. Both are worse than an honest
"written, untested".

A `supported` verdict with no evidence is refused, and an `unsupported` verdict with no
explanation is refused. Honesty is required; it is also not the same as being done.

## Invented content is refused at record time

`ContentClaim` covers the claim kinds that turn a draft into a false public statement:
`testimonial`, `customer_logo`, `integration_badge`, `certification`, `security_claim`, `award`,
`case_study`.

Three of them — testimonial, case study, customer logo — plus **integration badges** cannot exist
without a `source_ref` naming something the owner actually holds. That last one was found by a
test written to assert the opposite: "Works with Stripe" is a factual claim about a connection,
and a badge for a service nobody connected is the same lie as a fabricated testimonial, aimed at a
different audience.

`fabricated=True` is recordable, because an honest placeholder is a fact about the draft. It is
never *admissible*, so a placeholder cannot be counted toward a complete draft. The difference
between a real testimonial and an invented one is the source, and the caller either has it or
does not.

## Assets carry provenance, and "unknown" has an owner

An asset that came from anywhere other than this run must name its source. `licensed` without a
licence id is refused: "licensed" is not a rights statement.

`basis="unknown"` is allowed but blocks delivery, and it must name who resolves the question in
`license_id` (`pending:owner`). This is a small rule with a real effect. An asset recorded as
unknown-without-an-owner is a permanent, anonymous block — the kind that survives three handoffs
and gets shipped by the fourth person. Naming the owner turns it into a task.

## The repair loop is bounded, and exhaustion is a result

`draft_website()` runs at most `attempt_budget` attempts, 1 to 5, default 2. Each retry is told
which checks failed rather than guessing.

When the budget runs out the function **returns a `partial` draft**. It does not raise. A raise
would let a caller catch the exception and report success, which is exactly the behaviour the
bound exists to prevent. The `RepairAttempt` records are kept, so the report shows every turn of
the loop and what was still wrong on the last one.

## Deployment is a constant, not a flag

`DeploymentGrant` has `deployment`, `dns_change` and `production_credentials` as `init=False`
constants pinned to `False`. There is no constructor argument, no setter and no mapping that makes
them true. Asking it to deploy raises, in words, that those are separate grants.

There is no code path here that deploys, resolves DNS or reads a production credential, so the
honest representation of that fact is a constant. A boolean a caller could flip would describe an
authority this story does not have.

## Surface

| artefact | what it is |
| --- | --- |
| `website_draft.py` | the contract: scope, provenance, interaction verdicts, checks, repair bound |
| `WebsiteDraft.to_mapping()` | the report a reader and an evaluator both consume |
| `DraftExecutor` | the injected port a real implementation provides; the module executes nothing |

The module imports only `hashlib`, `json`, `re`, `dataclasses` and `typing`. It reads no clock,
opens no file and touches no network, which is what lets the same logic run against a recording
double in CI.

## Acceptance criteria → evidence

1. **Audience and scope are explicit; sample direction exists when there is no benchmark.** An
   audience that is not a `DraftAudience` is refused; a brief with no exclusions is refused; a
   site with no pages is refused; `no_layout_benchmark` records whether a reference existed.
   Tests: `test_a_site_without_an_audience_cannot_be_described`,
   `test_a_brief_that_excludes_nothing_is_refused`, `test_a_site_with_no_pages_is_refused`,
   `test_an_unbenchmarked_site_is_marked_as_having_no_layout_benchmark`.
2. **Requested interactions work or are honestly marked unsupported; no fake testimonials or
   integrations.** A missing verdict is refused by name; `supported` needs evidence;
   `supported_untested` and `unsupported` need words and do not count as done; sourced claim
   kinds cannot exist without a source; a fabricated placeholder is never admissible. Tests:
   `test_a_requested_interaction_with_no_verdict_is_refused_not_dropped`,
   `test_an_interaction_that_claims_to_work_must_name_its_evidence`,
   `test_written_but_untested_is_not_working`,
   `test_an_honest_unsupported_verdict_still_makes_the_draft_partial`,
   `test_sourced_claim_kinds_cannot_exist_without_a_source`,
   `test_an_integration_badge_needs_a_real_source_too`,
   `test_a_fabricated_placeholder_is_recordable_but_never_admissible`.
3. **Research and assets have provenance and applicable rights.** Non-authored assets must name
   a source; `licensed` requires the licence; `unknown` blocks delivery and must name its owner;
   an unrecognised basis is refused. Tests:
   `test_an_asset_from_elsewhere_must_name_where_it_came_from`,
   `test_a_licence_claim_without_the_licence_is_refused`,
   `test_an_unknown_basis_blocks_delivery_rather_than_passing_quietly`,
   `test_an_unknown_provenance_basis_name_is_refused`.
4. **Code executes in an authorised environment with real browser tests.** The four required
   checks are `document_loads`, `no_console_error`, `no_broken_link` and
   `no_horizontal_overflow`; a draft that omits one is refused, because a report cannot speak to
   a check that never ran, and a duplicate is refused because it would double-count. The browser
   execution itself is injected through `DraftExecutor` — the same port AP-010 uses for its
   browser worker — and the real Chromium evidence for the shipped UI lives in the browser walks,
   not in this module. Tests: `test_a_draft_that_omits_a_required_check_is_refused`,
   `test_a_duplicate_check_would_double_count_and_is_refused`,
   `test_every_required_check_present_and_passing_is_complete`.
5. **The repair loop is bounded and partial failure is visible.** The budget is 1–5 and rejects
   booleans; a retry is told what failed; exhaustion returns `partial` with every attempt
   recorded, never an exception and never a completion. Tests:
   `test_the_loop_retries_within_the_budget_and_reports_each_attempt`,
   `test_an_exhausted_budget_yields_partial_not_an_exception`,
   `test_the_budget_cannot_be_unbounded_or_zero`,
   `test_partial_failure_is_visible_in_the_report_not_rounded_up`.
6. **No deployment, DNS or production credentials without separate grants.**
   `DeploymentGrant`'s three fields cannot be set; asking it to deploy raises; the report states
   `deployment`, `dns_change`, `production_credentials` and `publish_permission` as `False` and
   lists `CANNOT_BE_CLAIMED` beside them. Tests:
   `test_deployment_is_a_constant_that_cannot_be_flipped`,
   `test_asking_the_grant_to_deploy_is_refused_in_words`,
   `test_the_report_never_claims_what_the_draft_cannot_do`.
7. **Direction compliance and no namespace collision.** The module reads no clock, opens no file
   and touches no network, and shares no top-level name with `app.py` — both asserted rather than
   assumed, because a collision in the single-file build has already broken this repository once.
   Tests: `test_the_module_reads_no_clock_and_opens_nothing`,
   `test_the_module_shares_no_top_level_name_with_app`,
   `test_the_executor_protocol_is_the_only_place_work_happens`.

## What this story does not claim

No rendered draft is accepted by an agent. No deployment, DNS change, production credential,
revenue or conversion outcome, copyright clearance, security sign-off, owner acceptance or
publish permission is produced or implied by any record here. Those are listed in
`CANNOT_BE_CLAIMED` and restated `False` on every report.

The evaluation of a draft's *quality and delivery* is AP-022, which depends on this story. A
`complete` draft here means the workflow's own obligations were met; it does not mean the site is
good, and it is not the story that says so.
