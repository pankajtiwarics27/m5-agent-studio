# AP-025 — extracting evidence-backed reusable skills from work

Every agent framework eventually writes the same sentence into a prompt: *"here is what worked last
time."* It is nearly always produced by taking one successful run and describing it as a technique.
This story exists to make that sentence either **evidence-backed** or **impossible to write**.

## The rule the story applies

**One success justifies task-local use. It never justifies global superiority, and a failure never
becomes an instruction.**

A skill is not a lesson. `evidence_lessons.py` already owns the durable, project-scoped lesson store
with its promotion gates, staleness invalidation and bounded retrieval. This module **reuses that
gate** rather than building a second knowledge base: `promote_to_lesson_store()` converts a promoted
skill into `EvidenceLessonStore.promote_lesson`, under that store's own privacy scan, anchor
digests, token ceiling and trust rules. There is one store and one promotion gate in this
programme, and a test proves the reuse by making the store's own rejection fire.

## What a skill has to say before it can be recorded

`REQUIRED_SKILL_SECTIONS` is concrete, because "we thought about limits" is not checkable and an
empty `limitations` list is:

| section | why it is a field and not prose |
| --- | --- |
| `evidence` | the verified task(s) it came from, with the spec digest and passed checks |
| `prerequisites` | what has to be true before the technique applies |
| `permissions` | capabilities, from `scoped_grants.SG_CAPABILITY_TAXONOMY` |
| `examples` | at least one marked `is_test=True` |
| `limitations` | a non-empty list; a technique with no stated limits claims universality |
| `version` | a positive integer, because a running task has to pin one |

The test example matters: illustrations show what a skill looked like, tests are what catch a
regression. A skill whose only examples are illustrations is a skill nobody can maintain.

## One success is task-local; sharing has three separate bars

`assess_promotion()` returns `task_local` unless **all three** hold, and each failure has its own
reason so a rejection says which bar it missed:

1. **Evidence** — at least `EVIDENCE_FOR_GLOBAL_SUPERIORITY` (2) independent *sessions*. The same
   session twice is one observation, not two.
2. **Privacy** — `public` confidentiality, no credential patterns, no host paths. A shared skill
   leaves the project boundary by definition, so `internal` is the wrong label for something you
   intend to share.
3. **Comparison** — a matched experiment (`ComparisonEvidence`) against the incumbent that favours
   the candidate. Working twice is not evidence of being better than what is already installed, and
   a comparison about a *different* technique does not transfer.

`ComparisonEvidence` refuses a sample size below two: one run against one run is an anecdote with
error bars drawn on it.

## Failures are retained and structurally unrenderable

`FailedApproach` keeps what did not work, why, and the correction — and it has **no `steps` field and
no `guidance` field**. `render()` raises `PromotionRefused`, and `executable` is a constant `False`
with `init=False`, so no constructor argument can set it.

That is the whole design: a library that stores failures as instructions teaches a model to repeat
them with confidence. Retaining the failure as *context* is valuable; rendering it as something to
do is a defect.

## Isolation: what may not travel

- Permissions are validated against the existing grant taxonomy, so a skill cannot ask for a
  capability the grant system does not know how to grant or refuse.
- `evidence_projects()` is reported on the record, so a skill assembled from two projects is
  visible rather than inferred.
- `scope_project` names where the evidence came from; `privacy_findings()` scans the guidance *and*
  every example for credentials and host paths, including examples, because that is where a leaked
  local path actually shows up.

## Versions: quarantine stops new retrieval, never a running task

`SkillLibrary` models the semantics a caller has to get right:

- a version is **immutable** — publishing a different body under `v1` raises; an edit is `v2`;
- `resolve()` is what a *new* task gets, and returns nothing while quarantined or revoked;
- `pin()` fixes one exact version to one task, and **the pin survives a quarantine**: withdrawing a
  version mid-run would change the instructions underneath a task already executing them;
- `revalidate()` releases the quarantine, and new retrievals resume at the **latest** version.

## What this story does not claim

- **No route, pane or orchestrator consumes `skill_extraction.py` yet.** It is a contract with no
  surface; `promote_to_lesson_store` is a real caller of the lesson store, and the store's tests and
  this module's tests both exercise it, but no application path calls this module.
- **No skill was extracted from this session's own work.** The skills in the tests are fixtures
  exercising the record's rules. Claiming otherwise would be exactly the one-good-run-to-global-
  instruction move this story refuses.
- **No shared skill was published.** Nothing leaves this repository; the store used in the tests is
  a temporary directory.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_skill_extraction.py` | 42 passed |
| `tests/test_evidence_lessons.py`, shard/scopes registries | 27 passed together |
| mypy (96 modules), `ruff check .` | clean |
| `scripts/validate_docs.py` | passed |
