# AP-017 — Bounded specialists and versioned handoffs

**Status:** delivered and merged (PR #351, `02c53e6`; story recorded `done` by
its completion record); **offline and advisory only**. It
**runs no model, starts no runtime, calls no provider or network** and wires no
production caller. It is **not activated** and **not a production guarantee**.

The objective is the constraint: *enable useful collaboration without a second
coordinator or uncontrolled fan-out.* `child_delegation` already derives child
authority and budget, persists delegation state, records joins and propagates
cancellation; `artifact_lineage` already versions artifact identity;
`execution_budget` owns the budget arithmetic; `challenger_evaluation` owns
comparable-budget reporting. Re-deriving any of that would be the second control
plane the story forbids, so this module **requires their values and refuses
disagreement** instead of recomputing them.

## Authority and budget are strict parent subsets (AC1)

`ParentBounds` is supplied by the caller — this module does not invent a
parent's remaining budget. Against it, `SpecialistTask` refuses:

- a policy ranked above the parent's;
- a tool the parent does not hold;
- a write scope not inside a parent scope;
- a budget that is **not strictly smaller** than what the parent has left.

Strictness matters: a child that claims the parent's entire remaining budget
leaves the parent nothing to join under. And because per-task checks are not
enough, `assert_reservations_fit_parent` ensures the *team's combined*
reservation also leaves the parent something.

## Versioned handoffs and no conflicting writes (AC2)

Every task names at least one `HandoffArtifact` carrying a version digest, so
"the worker read brief v2" and "the brief is now v3" are different facts rather
than an assumption. Scope containment and overlap are delegated to the
repository's own grant matcher (`scoped_grants.scope_subsumes` and
`child_delegation.child_scopes_overlap`) so there is one definition of
"conflicting scope", not two.

Two workers may not hold overlapping write scopes. A task declaring write scopes
without a claim is refused, because an unclaimed scope can be overwritten by
anyone.

## Joins are durable and bounded (AC3)

`JoinLedger` records transitions and refuses illegal ones: no join without a
dispatch, no join before a settled outcome, no re-dispatch after joining, no
duplicate dispatch, and no `failed`/`cancelled` entry without a stated reason. A
silent failure is not a join.

The ledger is capped at `MAX_LEDGER_ENTRIES`; a join needing more bookkeeping
than that is not bounded. `after_restart` fails open tasks *closed*: a process
that died with work in flight must not leave that work looking alive, so each
open task becomes a recorded `cancelled` with reason `restart_reconciliation`,
visible as `reconciled_after_restart` on the rebuilt join.

The join carries the settled result forward: an output recorded on the
`completed` entry stays attached to the terminal `joined` entry rather than
being lost when the disposition is appended.

## The reviewer gets a brief, not a request to agree (AC4)

`ReviewerBrief` has **no verdict field**. It carries the work, the evidence and
the open questions, and refuses to construct when:

- it has no evidence at all (a bare instruction is not a brief);
- the reviewer is the builder it reviews;
- the text asks for endorsement — "approve this", "sign off", "just confirm",
  "rubber stamp", "endorse" and similar phrases are refused in the summary,
  evidence or questions.

A reviewer also cannot hold a write scope or a mutating tool, drawn from
`child_delegation`'s own classification so a newly mutating tool cannot
silently become available to a reviewer.

## Logical parallelism is not concurrent inference (AC5)

`ParallelismPlan` states the logical slot count and separately states
`concurrent_local_inference`. Claiming concurrent local inference is refused:
how many models decode at once belongs to the runtime's scheduler and admission
contracts, not to the number of open specialist tasks. A plan that declares
fewer slots than the team contains is also refused, so the plan describes the
team it dispatches.

## Retain a team only where measurement says it wins (AC6)

`decide_topology` defaults to `single_agent` — the baseline — and only replaces
it when all of the following hold:

1. the comparison is **trustworthy** (no scorer failure, no unjustified score,
   comparable budgets) — otherwise the reason names the blockers;
2. matched `single_agent` and `specialists` results exist;
3. verified quality measurably improves;
4. cost, latency **and** interventions were measured and none has a worse
   median.

Anything unmeasured keeps the simpler execution, and a `specialists` decision
cannot even be constructed without measurement. The operational numbers come
from `challenger_evaluation.measure_metrics`; this module does not fill a gap
with an invented figure.

## What this story does not decide

- **It does not coordinate.** There is no scheduler, no queue, no fan-out limit
  and no federation of agents here — those remain the runtime's contracts.
- **It does not execute specialists.** Every task, output and join is a value
  the caller supplies.
- **It does not persist.** Durability belongs to
  `child_delegation.ChildDelegationStore`; the ledger here is a value type that
  can be rebuilt from loaded entries.
- **It does not prove a team is better.** `render_team` emits a `does_not_prove`
  block: a validated team is not evidence that the team is faster, cheaper or
  better than one agent.

## Reviewability

`render_team` is deterministic sorted-key JSON with a trailing newline and a
`specialist_rules` block stating every boundary (strict subsets, versioned
handoffs, no overlapping writes, no unbounded join, no endorsement request, no
implied concurrent inference, no retention without measurement).

## Verification

```sh
python -m pytest -q tests/test_bounded_specialists.py
```

83 tests, fully offline. Every task, artifact, join and score is synthetic; none
is presented as evidence about a running team. `runtime_activation_authorized`
remains `false`; ADR-0040–0045 remain `not-reviewed`.
