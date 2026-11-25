# AP-022 — qualifying website output quality and delivery

AP-021 answers "was the workflow's part done?" This story answers the harder question: **is the
site any good, and has anyone actually looked at it?**

Those are different, and conflating them is how "the tests passed" becomes "ship it". A site can
satisfy every AP-021 obligation and still be unfit to deliver: a form that posts nowhere, a phone
layout that clips, a stray API key in the bundle, or — most commonly — copy nobody would ever
publish because it still says "Lorem ipsum".

The module is the rubric and the record. The looking is the probe's job.

## The rule the story applies

**A check that did not run is not a pass, and a report nobody signed is not a delivery.**

`PilotReport.deliverable()` is derived and deliberately strict. It is true only when every
automated criterion ran and passed, every layout cell passed, no blocking finding exists, **and a
named person said the site is useful**. No field on the record can set it.

## A missing outcome is refused, not defaulted

Every criterion in `CRITERIA` must come back with an explicit boolean. A probe that omits one is
refused with the list of what it skipped.

This is the least dramatic rule here and the one that matters most. The natural way to write this
code is `outcomes.get("console_clean", True)` — and that single default turns "we never opened the
console" into "the console was clean" for every site ever qualified. To the person receiving the
site, "we didn't check that" and "that failed" are the same thing, so the record refuses to tell
them apart by omission.

An outcome of `"mostly"` is also refused. A boolean that accepts "mostly" is a boolean that will
be given "mostly".

## Phone and zoom are their own cells

The layout matrix is six cells: `phone 390x844`, `tablet 834x1112` and `desktop 1440x900`, each at
**100% and 200%** zoom. Every cell needs its own measurement, and the report names the cell that
failed.

A single `responsive: true` would let a broken 390px layout pass behind a passing 1440px one, which
is exactly the failure the story names. `clipped` and `overlapping` are separate booleans because
they have different causes and different fixes — and a cell that reports horizontal overflow while
claiming nothing clipped is refused, because those are two descriptions of one measurement and they
disagree.

The viewport vocabulary is closed. `Viewport(name="watch")` is refused: a layout judged only at the
sizes someone remembered is not a layout judgement.

## The numbers are quoted from the Studio's own gate

`CONTRAST_FLOORS` is `{normal_text: 4.5, large_text: 3.0}`, `TARGET_MIN_PX` is `24` and
`ZOOM_FACTOR` is `2` — identical to the constants in
`scripts/uiv2_accessibility_adversarial_walk.mjs`. They are named once here and asserted against
those values in `test_the_contrast_floors_are_the_same_numbers_the_studio_gate_uses`.

They are constants rather than parameters on purpose: a caller that can pass its own contrast floor
can pass `1.0`.

## Secrets and placeholders are scanned by the module, not trusted to the probe

`scan_for_secrets()` and `scan_for_placeholders()` run over every shipped document the probe
returns, and their findings are merged with whatever the probe reported. A probe that forgot to
scan cannot produce a clean report — `test_scanning_does_not_need_the_probe_to_remember` proves it
by returning a page containing an AWS key with an empty `problems` list.

A secret is **always** blocking. `AssetProblem` raises if a `secret` finding is given
`severity="warning"`, because the exposure does not depend on anyone's judgement. Everything else
may be a warning, and a warning does not block delivery — it is still reported, which is the whole
point of having a severity field rather than dropping non-blocking findings.

`mask_secret()` exists so a finding can be written down without re-leaking what it found.

## Creative usefulness belongs to a person

`OwnerReview` requires an actor namespaced `owner:`, `user:` or `reviewer:`, refuses the machine
names outright, and requires the owner's own words. An automated axis cannot say whether the site
is any use to the audience it was built for, so the report says `no owner review` and stays
undeliverable until one exists.

An accepted reference is recorded as a **versioned id** (`reference/cafe-site@v2`), and recording
it grants nothing: `to_mapping()` still states `is_final_acceptance: false` and
`grants_publish_permission: false`. Turning an owner's opinion into a reusable reference is
AP-020's job, with its own explicit `use_as_reference` grant. This story points at that decision;
it does not take it.

## Which references apply is AP-020's answer, not a second one

`reference_shortlist()` delegates to `reference_benchmarks.rank()`. Two implementations of "which
approved examples apply here" would eventually disagree, and the disagreement would be resolved by
whoever was asked last. References are returned as a reading order with reasons; none of them
changes the rubric, because an approved example settles style and never quality.

## No revenue claims, and the report says so

There is no conversion, engagement or ROI axis in the rubric, because this programme cannot
measure one. `FORBIDDEN_CLAIMS` names them, `refuse_claims()` raises on any attempt to attach one,
and every report carries `claims_made: []` beside `claims_refused: [...]`.

A number that cannot be measured is precisely the kind of thing that gets invented to close a
story. The empty `claims_made` is a deliberate, machine-checkable statement of what this report did
not do.

Every evaluated run is reported, including the ones that went badly: `runs_evaluated` and
`intervention_minutes` are required fields, not optional summary stats.

## Surface

| artefact | what it is |
| --- | --- |
| `website_quality.py` | the rubric, the layout matrix, the scanners, the owner review |
| `PilotReport.to_mapping()` | the report a reader, an evaluator and a ledger all consume |
| `QualityProbe` | the injected port; the module executes nothing |

The module imports only `hashlib`, `json`, `re`, `dataclasses`, `typing` and
`reference_benchmarks`. It reads no clock, opens no file and opens no socket — asserted, not
assumed, because a rubric that cannot be replayed against a recording double cannot be checked
after the fact.

## Acceptance criteria → evidence

1. **Critical forms, links, navigation and input flows pass real tests.** `InteractionCheck`
   requires a passing flow to name what exercised it, and classifies every flow as `form`, `link`,
   `navigation` or `input` so a failure can be triaged. A failed flow makes the report
   undeliverable. Tests: `test_a_passing_flow_must_say_what_exercised_it`,
   `test_a_flow_is_classified_so_it_can_be_triaged`,
   `test_a_failing_flow_makes_the_report_undeliverable`.
2. **Actual phone/tablet/desktop and zoom layouts have no clipped or overlapping content.** Six
   cells, each measured, each named when it fails; an incomplete matrix, an unknown zoom, a
   viewport outside the matrix, a duplicated cell and a self-contradicting cell are all refused.
   Tests: `test_the_zoom_cell_is_judged_separately_from_the_base_cell`,
   `test_a_broken_phone_layout_fails_even_when_desktop_passes`,
   `test_an_unmeasured_cell_is_not_a_passing_cell`, `test_an_unknown_zoom_factor_is_refused_not_rounded`,
   `test_a_viewport_outside_the_matrix_is_refused`,
   `test_overflow_and_no_clipping_cannot_both_be_reported`, `test_a_cell_may_be_recorded_once_only`.
3. **Keyboard, focus, contrast and reduced-motion checks pass applicable criteria.** Four of the
   eleven criteria are these, each individually blocking, at the floors the Studio gate uses.
   Tests: `test_the_contrast_floors_are_the_same_numbers_the_studio_gate_uses`,
   `test_an_accessibility_criterion_that_failed_blocks_delivery` (parametrized over all four).
4. **Console/network/secrets/dependency problems and placeholder content are checked.** Five
   criteria plus the two scanners; a missing outcome, an invented criterion and a non-boolean are
   each refused; a secret can never be downgraded; a dependency warning is reported without
   blocking. Tests: `test_a_missing_criterion_outcome_is_refused_not_assumed`,
   `test_an_invented_criterion_measures_nothing`,
   `test_an_outcome_that_is_mostly_true_is_not_a_boolean`,
   `test_an_api_key_in_the_shipped_page_is_found_and_reported_masked`,
   `test_a_secret_can_never_be_a_warning`, `test_shipped_placeholder_copy_blocks_delivery`,
   `test_scanning_does_not_need_the_probe_to_remember`,
   `test_a_dependency_finding_is_classified_and_reported`.
5. **The owner reviews creative usefulness; accepted references are scoped and versioned.** No
   owner review means undeliverable however green; machine actors are refused; an accepted
   reference must be versioned and grants nothing; the shortlist reuses AP-020's ranking. Tests:
   `test_no_owner_review_means_not_deliverable_however_green`,
   `test_an_agent_cannot_review_its_own_work`, `test_the_review_actor_must_be_namespaced`,
   `test_an_owner_who_says_it_is_not_useful_blocks_delivery`,
   `test_an_accepted_reference_must_be_versioned`,
   `test_an_accepted_reference_is_recorded_but_grants_nothing`,
   `test_the_shortlist_reuses_ap020_ranking_rather_than_reimplementing_it`.
6. **Report all evaluated runs, intervention, cost and time, and limitations; no revenue claims.**
   `runs_evaluated` and `intervention_minutes` are required; limitations are carried; the six
   forbidden claims are refused by name and listed on every report. Tests:
   `test_every_evaluated_run_is_reported_not_just_the_best`,
   `test_runs_evaluated_cannot_be_zero_or_a_boolean`, `test_limitations_are_carried_into_the_report`,
   `test_a_revenue_claim_is_refused_by_name`, `test_every_forbidden_claim_is_refused`,
   `test_the_report_states_which_claims_it_did_not_make`.

## What this story does not claim

No rendered site is accepted by an agent, and no revenue or conversion outcome is asserted by any
record here. The production security release remains blocked pending independent sign-off.

Two limits are named rather than hidden:

- **No route or pane consumes `website_quality.py` yet.** Like `website_draft.py` before its first
  caller, this is a contract with a probe port and no surface. The API wiring is a separate,
  reviewable change and is not claimed by this story.
- **No real-browser probe ships with it.** The module is the rubric; a Chromium implementation of
  `QualityProbe` — carrying the viewport matrix and resolving real links — is the next concrete
  step. The Studio's own browser evidence (the CI browser job, `uiv2_accessibility_adversarial_walk`)
  proves the *Studio*, not a generated site, and this page does not borrow that evidence.
