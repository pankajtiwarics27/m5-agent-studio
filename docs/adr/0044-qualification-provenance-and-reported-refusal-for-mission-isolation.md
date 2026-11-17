---
id: ADR-0044
title: qualification-provenance-and-reported-refusal-for-mission-isolation
status: accepted
date: 2026-11-17
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0044: Qualification provenance and reported refusal for mission isolation

## Context

[ADR-0043](0043-proven-sandbox-admission-for-mission-owned-consequential-work.md)
made isolation proof mandatory for consequential actions of mission-owned work
and judged the proof against a recorded acceptance. It listed what it did not
deliver. Three items of that list concern what the proof rests on and what the
reader of a report is told:

- The sandbox layer gives a qualification report that states no generation time
  the time of ingestion, and the qualification record did not say that it had.
  The evidence adapter takes the earlier of the record time and the report
  time. For such a report both are the time of ingestion, so an undated report
  counted as a fresh qualification.
- The acceptance named no host, and the qualification record did not say on
  which machine its report was produced. A report from another machine, or a
  security store copied from another machine, satisfied the gate. The owner's
  U06 answer 2 asks for a fresh qualification run on the target host.
- The recovery report mentioned isolation only when the root budget declares
  `isolation_required`. A plan that declares nothing was reported `in_progress`
  with no word about isolation, although each of its consequential actions is
  refused at the charge.

After the AP-007-F2 completion record landed, the owner selected a follow-up
that closes these three items for mission-owned work, with an additive change to
the sandbox layer that does not change the admission of the running application.
The selection, and every option that was offered, are quoted in the AP ledger
(`stories[AP-007].tracking.post_completion_changes`).

U06 stays pending. This record adds to ADR-0043 and changes none of its
decisions.

## Decision

### A qualification record says what its report stated

`sandbox_boundary.BackendQualificationRecord` gains two fields. Both have
defaults, and the sandbox admission of the application reads neither.

| Field | Meaning | Default |
|---|---|---|
| `report_time_stated` | `true` only when the report carried a usable generation time of its own: a positive, finite time under `generated_at_epoch`, `qualified_at` or `generated_at_utc`. Otherwise `report_generated_at` is the time of ingestion, as before. | `false` |
| `host_fingerprint` | The fingerprint of the machine description in the report (its `fingerprint` block). Empty when the report carries none, or an incomplete one. | empty |

All three writers of a record fill the fields: ingestion, fallback qualification
and requalification before restoration. A record written before the fields
existed reads as not stated and no host. Only the literal `true` and a
well-formed fingerprint are read back from the store.

The sandbox layer accepts the same reports as before. An undated report, or one
without a machine description, is still recorded and still qualifies the backend
for the application.

### The host fingerprint

`sandbox_boundary.qualification_host_fingerprint` reduces a machine description
to `sha256:` and 64 hex digits. The digest is the SHA-256 of the canonical JSON
of five values: operating system, architecture, processor model, processor count
and memory size. `scripts/qualify_environment.py` already writes all five. The
same block also carries the operating-system release and version, the Python
version, the git revision and memory readings. They are left out, because they
change while the host stays the same.

The fingerprint identifies a kind of machine, not one unit. Two machines of the
same model and memory size share it. No new hardware identifier is collected.

### Proof needs a stated time and the recorded target host

`IsolationAcceptance` gains a required field, `target_host`: the fingerprint of
the host the decision accepts. An acceptance without a well-formed one cannot be
built.

`IsolationEvidence` gains two fields:

- `qualified_host`: the host the qualification report was produced on, taken
  from the qualification record;
- `observed_host`: the host the server observed itself to run on for the
  dispatch. Server code supplies it. It is never taken from a request or from
  the record.

`IsolationEvidence.from_admission` takes a qualification time only from a record
whose `report_time_stated` is literally `true`. `isolation_refusal` adds these
refusals after the checks of ADR-0043, in this order:

1. the qualification states no usable time for its run;
2. the qualification predates the acceptance (as before);
3. the qualification names no host;
4. the qualification was not run on the accepted target host;
5. the host of the dispatch was not observed;
6. this host is not the accepted target host.

A time that is zero, negative, not finite or not a number was reported as
"predates the recorded acceptance". It now has the first reason.

Nothing observes the host in this build, and no acceptance is recorded. Every
consequential action of mission-owned work therefore stays refused.

### The recovery report states the refusal

For a live plan that still owes work, `mission_recovery_report` judges the
evidence it was given with `isolation_refusal` and the recorded acceptance. This
is the rule the charge uses for the contained-execution step. If the evidence is
refused, the report carries the limitation `consequential_actions_refused`, with
the reason and a next action.

The limitation does not change the outcome. A plan with unfinished tasks stays
`in_progress`: read-only actions are still charged, and the report cannot know
whether the remaining tasks need a consequential action.

These plans do not get the new limitation:

- a plan whose root budget declares `isolation_required`. It carries
  `isolation_unavailable`, as before;
- a plan whose outcome is `cancelled` or `stopped`, and a halted or superseded
  plan;
- a plan whose join succeeded.

No table, column, schema version or route is added.

## Alternatives

- Reject an undated report when it is ingested: rejected. That changes the
  admission of the running application and the answers of three
  security-incident routes, which the owner's scope excludes.
- Record no time for an undated report: rejected. The sandbox layer compares
  that time in its post-incident freshness check and returns it in the
  qualification object, so the change would alter what the application does and
  shows.
- Compare only the host of the qualification run: rejected. A security store
  copied to another machine would still pass.
- Use a unique hardware identifier: rejected. The qualification report carries
  none. Collecting one changes the M5-P3-01 suite and stores a new identifier,
  and the machine description is enough to refuse a report from a different kind
  of machine.
- Add the operating-system release to the fingerprint: rejected. No rule is
  decided for requalification after an update, and the runtime version and the
  image are pinned separately.
- Add a function that observes the host: not done. Nothing builds evidence in
  production, and the macOS readings cannot be verified on the authoring host.
  It belongs to the activation wiring.
- Report a plan with unproven isolation as `blocked`: rejected. Read-only work
  continues, so `blocked` would overstate.
- Reuse the code `isolation_unavailable`: rejected. Its next action says that the
  mission cannot run.
- State the refusal for cancelled and stopped plans as well: rejected. Those
  reports already say that the plan runs no further.

## Tradeoffs

Verified by tests:

- `SecurityIncidentStore.check_admission` gives the same answer for a dated and
  an undated report, for a report with and without a machine description, and
  for a record without the new fields.
- The security-incident routes accept the same reports as before. One test
  sends an undated report through each of the three routes that record a
  qualification.

Costs:

- A qualification object gains two keys. They appear in the store file and in
  the JSON that `GET /api/security/incidents` and the three `POST` routes under
  `/api/security/incidents` already return for a qualification.
- A record written before this change is not usable as mission proof until its
  report is ingested again. This is the refusing side, and the owner's answer 2
  asks for a fresh run in any case.
- The report hash of a live unfinished plan changes, because the limitation is
  part of the hashed report.
- Pinned expectations changed. The fixture acceptance and the fixture evidence
  carry the host fields. Seven rows that expected "predates the recorded
  acceptance" for an unusable time expect the new reason. One recovery-report
  case expects the new limitation. No test was deleted.

Not delivered, and stated here so that it is not assumed:

- Nothing records an acceptance, builds evidence, observes the host or presents
  proof in production. That wiring is activation and depends on U06. An observer
  must read the same five values that the qualification suite records and
  digest them with `qualification_host_fingerprint`. If it reads them another
  way, its fingerprint does not match and the gate refuses.
- A qualification report is not authenticated. The gate believes what a report
  says about its time and its machine, as the sandbox layer already believes its
  verdict.
- The fingerprint does not tell two machines of the same model and memory size
  apart.
- The report does not say that work of the browser-worker and native-lifecycle
  steps is refused. That refusal is reported where it happens, at the tool
  boundary. Nothing in this build dispatches such work.
- The report reads `isolation_required` from the root budget only. When a task
  budget below the root declares it, the report states the refusal of
  consequential actions and does not say `blocked`. Nothing in this build
  declares the requirement.
- Work of a later step that no mission owns is not judged by the gate. This is
  unchanged from ADR-0043.

Rollback is a source revert. No data is migrated either way. A store written by
this version reads on the earlier one, which ignores the two keys. A store
written earlier reads here with the refusing defaults.

## Review Status

Not reviewed. Accepted on the owner selection recorded in the AP ledger, without
independent review. It adds to ADR-0043 and supersedes none of its decisions.
U06 remains pending, and whether the gate is accepted as the refusal foundation
is the owner's decision. Recording an acceptance, observing the host in
production, any activation, any execution on a qualified backend and any change
to the authority boundary require separate review and owner authorization.
