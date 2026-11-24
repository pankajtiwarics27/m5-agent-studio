# AP-019 — lightweight scoped review and preference corrections

AP-019 exists because of a specific failure mode: an owner says something small about one
thing, and the system quietly turns it into a rule about everything. This note records what
was built, what it refuses, and where each acceptance criterion is proved.

## The rule the story applies

**A correction keeps the scope it was given.** Three scopes exist, narrow to wide:

| scope | how it is reached | what it may be read as |
| --- | --- | --- |
| `element` | the comment carries a selection (a region, a passage, a timecode) | a statement about that region of that version |
| `artifact` | words with no selection | a statement about that artifact, at that version |
| `preference` | **only** `promote(basis=…, promoted_by=…)` | a standing preference, with a stated reason |

Two things are deliberately impossible:

- a comment about one artifact **cannot** be captured at `preference` scope. `capture()`
  refuses it and names the promote path instead;
- a preference **cannot** carry a selection. Accepting one would let a note about one heading
  become a rule about every heading, wearing the narrow scope as a disguise.

`assert_not_generalised(record, applying_scope=…)` raises `PreferenceDrift` at read time, so a
consumer that tries to apply a narrow correction wider is stopped where it happens, not asked
politely earlier.

## Free text first, precision optional

The composer is a plain `<textarea>`. The quick actions (`shorter`, `longer`, `louder`,
`quieter`, `plain`, `keep`, `undo`) are shortcuts for the same record, not a required vocabulary.
A region can be selected from the real diff drawn beside the composer, and the pane then says in
words what the comment will apply to — but selecting nothing is a first-class outcome, not a
degraded one: `Scope follows what the owner pointed at, not what the agent hopes to change.`

Nothing here asks for a score, a rating or a number.

## Interpretation is confirmed; it is never assumed

Words make a record `material`, which makes it need confirmation, which makes
`plan_revision().ready` false until the owner says which reading they meant. A bare quick action
names its own change, so it is not material and does not stall a revision.

Confirming the reading is **not** accepting the artifact. The record, the route response and the
pane all say so, and `Feedback.from_mapping()` refuses a stored payload that claims
`is_final_acceptance` or `grants_publish_permission`.

Two live corrections on one region that cannot both be acted on are reported as a
`conflicts` pair. They are shown, never averaged: an average of "keep" and "shorten" is a change
nobody asked for.

## A version, and only the version it was given on

Every record pins `artifact_sha256`. `plan_revision()` includes only feedback whose digest
equals the base, and `stale_for()` returns the rest. The pane draws those under a heading that
says the comment `no longer describes what is on screen`, with the words it was said in.

`affected_evidence(references, invalidated_version=…)` returns only the references naming that
digest, so a revision invalidates what it affects and nothing else.

## Which run it belongs to

The store originally listed by artifact id alone. Two runs can each produce a `hero.html` at the
same version, so one run's corrections were shown to the other run's reader — at the same
digest, looking entirely legitimate. A record now names its run, reading is scoped to it, and
`confirm`, `promote` and `withdraw` answer **404** for another run's id. Databases created before
this are migrated: the column is added, old rows are kept, and they belong to no run rather than
to whichever run happens to ask.

This was found by the browser walk, not by a unit test. It is the kind of defect that only shows
up when two sessions are actually open next to each other.

## Surface

| route | behaviour |
| --- | --- |
| `GET …/{sid}/feedback?artifact_id=&artifact_sha256=` | items, live, revision, awaiting, conflicts, stale |
| `POST …/{sid}/feedback` | `capture()`; requires both the artifact id and its digest |
| `POST …/{sid}/feedback/{id}/confirm` | settles the reading; emits nothing |
| `POST …/{sid}/feedback/{id}/promote` | the only path that widens; 400 without a basis |
| `DELETE …/{sid}/feedback/{id}` | tombstone, not a row delete |

`scoped_feedback.py` is pure: no clock, no environment, no network, no file. A test enforces it,
and another enforces that it shares no top-level name with `app.py` (a collision in the
single-file build once replaced the app's `SCHEMA_VERSION`).

## Acceptance criteria → evidence

1. **Free text and optional region/passage/timecode/quick actions, no mandatory scoring.** The
   composer is a textarea with quick actions; `HARNESS_FEEDBACK_SCOPES` states the reach of each
   scope; the walk types words, presses a quick action and selects a region, and checks the
   pane's own statement of what the comment applies to (walk checks 1, 2, 7).
2. **Feedback pins the artifact version and keeps the original wording.** `capture()` requires
   the digest; `verbatim` is never rewritten; the stale section shows the original words of an
   older comment. Tests: `test_a_comment_is_recorded_against_exactly_one_version`,
   `test_a_size_that_is_not_a_digest_is_refused`; walk checks 1, 6.
3. **Material interpretation confirmed; ambiguity and conflicts not silently generalised.** The
   confirmation gate, the conflict pair, and the refusal of `preference` scope in `capture()`.
   Tests: `test_an_ambiguous_correction_waits_for_the_owner_reading`,
   `test_two_opposing_corrections_are_reported_rather_than_merged`,
   `test_a_preference_cannot_be_written_through_the_capture_route`; walk checks 3, 4, 5.
4. **A revision invalidates only affected downstream evidence/approvals.** `plan_revision()`
   filters to the base digest, `stale_for()` reports the rest, `affected_evidence()` narrows the
   invalidation. Tests: `test_feedback_on_an_older_version_is_reported_stale_not_applied`; walk
   check 6.
5. **The user can correct or delete a retained preference and inspect its scope.** Per item, the
   pane offers "This is what I meant" and "Withdraw"; the reach is printed beside each item; a
   withdrawal leaves a tombstone so the history is not rewritten. Tests:
   `test_a_retained_preference_can_be_inspected_and_removed`,
   `test_withdrawing_feedback_keeps_the_record_and_clears_the_live_set`; walk checks 4, 8.
6. **Keyboard, mobile and zoom stay usable; paused work resumes.** The composer is a real
   textarea reachable by keyboard, and the region rows activate through the app's one delegated
   `data-key-activate` handler (the eighth such control, pinned by the keyboard contract). The
   pane is drawn at 390×844 and at 200% zoom with no horizontal overflow, and survives a reload
   from the record rather than from pane state. A paused-for-direction task is untouched by any
   of this: feedback writes no task state at all. Walk check 10.
7. **The direction rules in `docs/autonomous-production-ux-direction.md`.** The pane keeps
   direction approval, final acceptance and publish permission distinct in its own words, and
   refuses to record a comment it could not later check for staleness.

## What is not claimed

No runtime activation, no acceptance, no publish permission, no benchmark adoption. Every record
and every revision emits `is_final_acceptance: false` and `grants_publish_permission: false`.
Confirming a reading settles what words meant; it settles nothing about the work.
