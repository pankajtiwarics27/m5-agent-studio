# AP-024 — qualifying video technical and editorial quality

AP-023 answers *"did the workflow produce a sourced, rights-cleared, playable draft?"* This story
answers the question after it, which is much harder to automate: **is the video any good, and did
anyone actually watch it?**

A file can decode, carry the right duration, have the right aspect and still be worthless: black
for four seconds, silent through the sentence that matters, narrated with a mangled product name,
captioned under the wrong scene, and paced so nothing happens until the last three seconds. None of
that fails a decode check. The module is the rubric and the record; the measuring is the probe's
job and the watching is a person's.

## The rule the story applies

**Tiny sample approval is not substituted for the final review, and a beautiful video with
uncleared footage is not ready.**

`PilotReview.ready()` is derived and returns reasons in the order a reviewer should fix them. It is
true only when the media is clean, every audio check ran, a person watched the **whole** timeline
and scored every editorial axis at or above the floor, no asset has unresolved rights, and no
consequential claim is unsupported. Nothing on the record can set it.

## The file: five separate answers, not one verdict

`FrameScan` counts **black**, **frozen** and **missing** frames separately, because they have
different causes (a lost picture, a repeated frame, a timestamp jump) and different fixes. A count
with no position is refused: "three black frames, somewhere" gives an editor nothing to open.

`MediaReport` also refuses to let an omission pass. Every check in `MEDIA_CHECKS` and
`AUDIO_CHECKS` must be present in the probe's `checks` mapping; a missing one is reported as
**"media check never ran: \<name\>"** and blocks readiness, and an invented check name is refused
outright. That is the same rule AP-022 applies to its rubric, in the place where the temptation to
write `checks.get(name, True)` is strongest.

Dimensions are cross-checked against the claimed aspect: a 1920×1080 file that says `9:16` in the
record is refused as a contradiction rather than trusted.

## Audio: measured bands, plus two honest judgements

`AudioLevels` carries integrated loudness, true peak, clipping events and silence. The band is
`-16 … -12 LUFS` with a `-1.0 dBTP` true-peak ceiling, named once as `LOUDNESS_TARGET_LUFS` and
`TRUE_PEAK_CEILING_DBTP` so a report can say which side of the line it fell on. Clipping is
reported as its own count rather than absorbed into the peak: a file can sit under the ceiling and
still crackle.

Two audio checks — `pronunciation_reviewed` and `narration_matches_script` — are recorded as
**human checks, not measurements**, because no probe hears a mispronounced brand name or notices
that the narration drifted from the approved script. They still have to be present and true, so
"nobody listened to it" cannot masquerade as a pass.

## Editorial: four axes, four scores, four anchors

There is deliberately **no composite quality score**. Averaging four judgements into one number is
how an unusable video earns a passing grade:

| axis | what it asks |
| --- | --- |
| `scene_relevance` | does each scene serve a claim or a direction element? |
| `pacing` | is anything happening, and at the right rate? |
| `originality` | is this the draft's own angle, or a generic template? |
| `audience_fit` | would the intended viewer recognise themselves? |

Each `EditorialAxisScore` carries a 1–5 integer, its `EDITORIAL_ANCHORS` text and the reviewer's
evidence. A score below `EDITORIAL_FLOOR` (3) blocks readiness; a review missing an axis is refused
with the list of what it skipped; a score given with `watched_before_scoring=False` is refused
because it describes an expectation, not the video.

`EditorialReview.actor` must be namespaced (`owner:`, `user:`, `reviewer:`) with a name after the
prefix, so an agent cannot review its own video and cannot sign as `reviewer:` with nobody behind
it.

## Coverage is ranges on the timeline, not a flag

`WatchedCoverage` merges the ranges a person actually watched and computes the gaps.
`is_complete()` is arithmetic, not a boolean someone passes in. A range extending past the end of
the file is refused — nobody watched it.

A **`final`** editorial review on incomplete coverage raises `CoverageRefused` rather than being
softened to `partial`, because the detail is not a detail: nobody has seen the end of the video.
A **`sample`** review is recordable and visibly insufficient — `watched_sample_only()` is true and
the readiness list says a sample cannot stand in for the final review. The vocabulary exists so a
reviewer can honestly record "I looked at the first thirty seconds" without either overclaiming it
or hiding it.

## Rights and consequential claims block readiness at the last gate

`PilotReview` re-checks rights and evidence even though AP-023 already did: this is the last gate
before a video is called ready, and a claim that cited a source which never resolved is
unsupported. The check uses **AP-009's research bundle** — `NarrationClaim.is_supported(bundle)` —
so "I checked the sources" without the sources is not accepted. AP-023 still refuses a
consequential claim with no source at all at construction time.

## Platform constraints are recorded, never guaranteed

`PlatformConstraint` names a platform's rule, whether the draft meets it, and — when unmet — the
gap that remains. This is a record of a constraint, not a claim of compliance: naming a rule is not
evidence that a published video satisfies it, and the report says so.

`FORBIDDEN_GUARANTEES` names `monetisation`, `ad_revenue`, `rpm`, `cpm`, `virality`,
`algorithm_boost` and `platform_compliance`. `refuse_guarantees()` refuses each by name, and every
report states `guarantees_made: []` beside `guarantees_refused`. A platform can change a video's
distribution overnight; no programme can promise otherwise, so the record does not.

## What this story does not claim

- **No route, pane or orchestrator consumes `video_quality.py` yet.** Contract with one port and no
  surface, like `video_draft.py` and `website_quality.py` before their first callers.
- **No real ffprobe, frame-scan or loudness implementation ships here.** The suite uses a recording
  probe; a real `MediaProbe` is the next concrete step and none of these rules has been exercised
  against a real file.
- **No real pilot was watched, and no editorial score was invented for evidence.** The scores in
  the tests are fixtures that exercise the record's rules, not judgements about a video. Owner and
  independent editorial acceptance remain human acts, and the production security release remains
  blocked pending independent AS-031 sign-off.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_video_quality.py` | 46 passed |
| `tests/test_ci_test_shard.py`, `tests/test_scoped_tests.py` | 20 passed |
| mypy (95 modules), `ruff check .` | clean |
| `scripts/validate_docs.py` | passed, 323 files |
| `scripts/uiux_release_gate.py --check` | 37/37 checks, 7/7 gates, 0 findings |
