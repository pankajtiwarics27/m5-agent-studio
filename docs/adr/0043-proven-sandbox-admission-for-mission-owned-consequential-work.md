---
id: ADR-0043
title: proven-sandbox-admission-for-mission-owned-consequential-work
status: accepted
date: 2026-11-16
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0043: Proven sandbox admission for mission-owned consequential work

## Context

[ADR-0041](0041-mission-halt-bounded-stops-and-recovery-reports.md) decided that
"isolation is a requirement the budget can declare". When a budget in the task
chain declares `isolation_required`, claims, resumes and dispatches are refused
unless the caller presents literal proof of an admitted, qualified sandbox
backend.

Three properties of that gate matter here:

- Only server code can declare the requirement, and nothing declares it. A
  mission that declares nothing dispatches consequential actions with no sandbox
  admission.
- The proof is not compared with any recorded backend, and it carries no
  qualification identity or date.
- One kind of proof covers all work.

The owner then answered the six questions of the
[U06 call-path audit](../autonomous-production-u06-call-path-audit.md). The
answers were recorded through PR #309 as owner input, not as a resolution. Four
of them concern this gate:

1. Container backends only, the runtime and image pinned in the M5-P3-01
   evidence, count as backend-qualified for AP. Native lifecycle, browser worker
   and host or disabled execution remain refused for AP.
2. A fresh qualification run on the target host is required before U06 can be
   recorded as satisfied. The 2026-10-31 record alone is not sufficient.
3. Consequential mission actions never dispatch without sandbox admission:
   admission, then dispatch, never a host-execution fallback.
4. The activation order is contained execution, then browser worker, then native
   lifecycle. Each step activates only with its own recorded evidence, and
   everything not yet activated remains refused.

Answer 6 says the AP-007 offline subset is not accepted as the refusal
foundation and that rework is required. The owner chose the scope of that rework
in the AP-007 builder session: full alignment with answers 1 to 4, for
mission-owned work only. The selection is quoted in the AP ledger
(`stories[AP-007].tracking.post_completion_changes`).

U06 stays pending. This record changes one decision of ADR-0041. Every other
decision of ADR-0041 stands, and so does
[ADR-0042](0042-late-result-fence-limited-to-mission-owned-work.md).

## Decision

### Proof is mandatory for mission-owned consequential work

The atomic budget charge
(`DurableTaskBudgetManager.charge_hierarchical_tool_call`) requires isolation
proof when both of these hold:

- the task chain contains a task of a mission plan;
- the action is consequential (`managed_edit`, `consequential` or `external`),
  or it belongs to an activation step other than contained execution.

Whether a budget declares `isolation_required` no longer matters for such an
action. Ownership is the rule of ADR-0042: the task is a task of a mission plan,
or a descendant of one. The task a plan hangs from is not a task of the plan. A
store without mission tables owns no mission work.

The charge is the last admission step before durable intent and dispatch, so the
order is admission, then dispatch. It takes two new keyword arguments. Both
default to the refusing side: `consequential=True` and
`isolation_step="contained_execution"`. The application's mission envelope
coordinator passes the action class the boundary resolved; that class cannot be
downgraded to read-only.

A declared `isolation_required` keeps its meaning and stays the stricter rule.
It binds every action of the chain, read-only ones included, and it binds claims
and resumes. A claim is not a dispatch: for a plan that declares nothing, proof
is demanded at the charge and not at the claim.

### Proof is judged against a recorded acceptance

`IsolationAcceptance` names what a recorded owner decision accepts:

- one container backend: runtime, runtime version and full image digest;
- the earliest qualification time it accepts;
- the activation steps it covers.

The budget manager is composed with the acceptance. It is not supplied per
dispatch, and it is never parsed from a request, from model output or from a
planning document. The default is none, and without one nothing is accepted.
**No acceptance is recorded in this build, so every consequential action of
mission-owned work is refused** with `isolation_unavailable`.

`IsolationEvidence` now also names the backend's runtime, runtime version and
image digest, its qualification with the time of the qualification run, and the
step it proves. `isolation_refusal` accepts evidence only when all of these
hold:

- the step is a known step, the acceptance activates it, and the evidence proves
  that step;
- execution mode is `sandbox`, admission is literally true, and the backend is
  literally qualified and named, as in ADR-0041;
- runtime, runtime version and image digest equal the pin. The comparison is
  exact, except that the runtime name is case-insensitive. A shortened digest is
  not a pin;
- the evidence names a qualification, and the qualification run is not older
  than the acceptance.

A qualification is as old as the run that produced it. The adapter
(`IsolationEvidence.from_admission`) takes the earlier of the time the
qualification record was written and the time its report was generated, so
recording an old report again does not make it fresh.

### Steps follow the activation order

The steps are `contained_execution`, `browser_worker` and `native_lifecycle`. An
acceptance can record only a prefix of that order. Contained execution is the
only step this build defines evidence for. Work of the two later steps is
refused even when an acceptance names them. A sandbox admission proves contained
execution and nothing else.

No table, column, store file, schema version, route or configuration surface is
added. Budget documents and their identities are unchanged.

## Alternatives

- Keep the gate opt-in, as ADR-0041 decided: not chosen. It contradicts
  answer 3.
- Have `create_mission_plan` write `isolation_required` into every plan budget:
  rejected. It changes the budget identity of existing plans, it also binds
  read-only actions, and a budget written without the key would again be
  ungated.
- Gate in a mission dispatcher: rejected. None exists, and a second admission
  path would compete with the action boundary.
- Gate every task-bound consequential action: rejected. The owner limited the
  scope to mission-owned work. On a default install it would refuse
  consequential tool calls of the running application.
- Pass the acceptance per dispatch, next to the evidence: rejected. The caller
  that observes the backend would then also decide what is acceptable.
- Read the AP operating-envelope JSON at run time: rejected. A planning document
  would become a runtime dependency.
- Hard-code the M5-P3-01 runtime and image as the pin: rejected. Answer 2 asks
  for a fresh qualification before U06 can be recorded as satisfied, so no value
  is accepted yet. The values belong to the decision that resolves U06.
- Compare digests by content only and ignore the repository prefix: rejected for
  now. An exact comparison cannot accept a string the owner did not record.
- Judge freshness by the time the qualification record was written: rejected.
  The sandbox layer sets that time when it ingests a report, so an old report
  recorded again would look fresh.
- Define evidence for the browser-worker and native-lifecycle steps: not done.
  No such backend exists (AP-010 is unauthorized and the native lifecycle is
  dormant), and acceptance rules for them would be a decision without a subject.

## Tradeoffs

Verified by reading the callers: no route or other production caller creates a
mission plan. A store written through the application has no mission-owned work,
and for that application the charge answers as it did before. Tests pin both
sides, at the budget manager and at the application's tool boundary.

For a consequential task-bound tool call the charge adds one catalogue lookup
and one indexed lookup per task of the chain. Both run inside the transaction
the charge already uses.

In this build a mission cannot dispatch a consequential action at all. That is
the intended state while U06 is pending. The accepting path runs only in tests,
with a fictional acceptance record and fictional evidence. One test adapts the
security store's own decision, backend profile and qualification record; its
store is temporary, and its backend and report are written by the test.

Not delivered, and stated here so that it is not assumed:

- Nothing records an acceptance, builds evidence or presents it in production.
  That wiring is activation and depends on U06.
- The recovery report does not say that consequential actions of a plan are
  refused for lack of isolation. The refusal is reported where it happens, as
  `isolation_unavailable` at the tool boundary. A plan that declares
  `isolation_required` is still reported `blocked`, as before.
- Work of a later step that no mission owns is not judged by this gate. Nothing
  in this build dispatches such work.
- The sandbox layer gives a report that carries no generation time the time of
  ingestion. The gate cannot tell such a report from a fresh one.
  `sandbox_boundary.py` is not changed here.
- The target host is not part of the evidence. The qualification record is local
  to the security store of the host that holds it.

Pinned expectations changed. AP-007 cases that charged a consequential action on
a plan task without proof now present fixture proof, and the proof cases need an
acceptance record. No test was deleted.

Rollback is a source revert, which restores the opt-in gate. No data is migrated
either way.

## Review Status

Not reviewed. Accepted on the owner selection recorded in the AP ledger, without
independent review. For mission-owned work it supersedes the paragraph
"Isolation is a requirement the budget can declare" of ADR-0041; the rest of
ADR-0041 and ADR-0042 stand. U06 remains pending, and whether this gate is
accepted as the refusal foundation is the owner's decision. Recording an
acceptance, any activation, any execution on a qualified backend and any change
to the authority boundary require separate review and owner authorization.
