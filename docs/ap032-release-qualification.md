# AP-032 — release qualification and the independent-agent handoff

A release is not a date; it is a set of claims, each with the evidence that supports it and the scope
that evidence covers. `release_qualification.py` builds that pack as a value and refuses to produce one
that claims more than was tested. It renders nothing, writes nothing and reads no clock.

## 1. Scope is an intersection, not a wish

A `Claim` names the scope it asserts and the evidence that supports it. The constructor refuses a claim
whose evidence is narrower than the claim (`"a claim may not be wider than what was tested"`), a claim
with no evidence, and a claim citing evidence the pack does not contain.

The pack then separates what survives from what does not:

- `published_claims` — evidence passed **and** ran at the head being shipped **and** covers the claim;
- `uncovered_claims` — everything else, listed by id and counted in the blocking reasons.

Nothing is trimmed silently. `to_mapping()` reports `claimed_count` against `published_count` so a
reader can see the gap without reading the table.

## 2. A deferred specialisation is a ledger fact, not a hole

`DeferredSpecialisation` records a pilot that is not shipping as an authorised revision: a human
`owner:`/`user:` granter (a `reviewer:` cannot decide what ships), a reason and a timestamp. The pack
refuses to defer the same specialisation twice or to select and defer one at the same time.

`unaccounted_specialisations` names any specialisation that is **neither** selected nor deferred, and a
non-empty set blocks the release with `"leaving it unmentioned is not the same as deferring it"`. This
is the story's own acceptance criterion, enforced rather than described.

## 3. Preservation is verified, never assumed

Every preserved surface carries an explicit `verdict` — `verified`, `partially-verified`, `missing` or
`not-applicable`. There is no default. A `verified` or `partially-verified` surface needs an evidence
id (a verdict without an observation is an opinion), `not-applicable` needs a reason, and reporting **no
preserved surfaces at all** blocks: unverified is not preserved.

## 4. Held-out cases were not tuned here

A `CaseSuite` carries a version, a kind (`held-out`, `hostile`, `failure`, `restart`, `resource`), and
`tuned_against_version`. A suite that says it shaped the implementation cannot also be held-out
evidence for the same version — re-labelling it is the failure the field refuses. A workflow that mixes
case versions, files a suite under the wrong workflow, or claims more passes than cases ran is refused.
Missing case families are named per workflow and block.

## 5. An approval is real, current and not the author's

`Approval` refuses the author as approver, an agent-looking approver name, a non-human approver, and an
approval outside its TTL (or dated after `now`). Security and screenshot are **separate** kinds; a
second signature of one kind does not replace the other. Author evidence is never substituted for
independent acceptance — the report's `handoff.author_can_approve` is always `false`.

## 6. CI green means green at the head being shipped

`CiVerdict` records the SHA it ran against. `qualifies()` requires both `success` **and** that SHA.
A green run elsewhere is a different build, and the blocking reason says so.

## 7. Drills exercised, artifacts clean

`rollback`, `migration` and `restore` must be recorded as exercised. A restore that was not compared
against the source digests proves only that a command ran, so `proves_restore` requires both digests to
match. Artifacts need a digest, a real licence (an unlicensed output cannot ship) and a zero secret-scan
count.

## What this story does not claim

This is the **machinery** for qualification, not a qualification of the current build. Being explicit:

- **No release is qualified by this story.** Nothing here ran an end-to-end pilot, a held-out suite, a
  video/website artifact review, or a rollback/migration/restore drill. The fixtures in the tests are
  constructed values; a real pack must be assembled by a later release run that actually performs them.
- **No security or screenshot approval exists.** Per owner policy U03 this agent performs public,
  read-only research only and cannot sign, substitute or imply an independent approval. `Approval`
  exists so that an omitted signature *blocks* — it cannot create one.
- **The pack is not wired into `security_release_gate.py`.** The existing gate already parses
  `docs/security-review-signoff.md` and its own prerequisites; this module is a separate, additive
  artifact and does not weaken, bypass or replace that gate. Where they overlap (sign-off, CI at head,
  preservation), the stricter reading applies in each.
- **CI is recorded, not consulted.** `CiVerdict` is a value a caller supplies after a run; no GitHub API
  call happens here.
- **One specialisation shipping is a ledger decision, not this module's.** The deferral record is
  authorised by the owner in `product/M5_Autonomous_Production_Roadmap.json`; this module only refuses
  to let an unmentioned pilot pass as complete.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_release_qualification.py` | 59 passed |
| mypy (102 modules), `ruff check .` | clean |
| shard/scopes registries | 20 passed |
| AST discipline test | imports limited to `__future__, hashlib, json, re, dataclasses, typing`; no `time` |
