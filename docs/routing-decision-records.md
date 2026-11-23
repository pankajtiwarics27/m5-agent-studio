# AP-014 — Policy-aware routing and resource-safe scheduling

**Status:** delivered for the AP-014 scope; **offline and advisory only**. It
**dispatches nothing**: no model is loaded, no runtime started, no inference
issued, no provider contacted, and no production caller imports it. It is **not
activated** and **not a production guarantee**.

The story's objective is the constraint: *select eligible models per task stage
without bypassing existing lifecycle qualification.* So this module **composes
gates** — it does not call a model. Every decision it makes is a record an
operator can audit.

## Four properties, each structural rather than advisory

### 1. Every gate runs before dispatch, and every gate appears in the record (AC1)

Five gates in a fixed order: **compatibility → measured quality → grants →
residency → resource fit**. The first blocking gate stops evaluation of *further*
gates — but every gate is still present in the result, marked `not_reached`:

```
compatibility  passed
measured_quality  passed
grants         failed     "the candidate holds no grant covering path:/ws:out"
residency      not_reached  "an earlier gate blocked this candidate"
resource_fit   not_reached  "an earlier gate blocked this candidate"
```

That is deliberate. Silence is not a pass, and an operator reading the record
needs to see what was never asked rather than infer it. `unknown` **blocks**:
"we never verified runtime support for vision" is not "vision works".

Gates reuse existing authority instead of restating it — grants through
`scoped_grants.scope_subsumes`, measured quality through the AP-013 profile
contract, resource semantics matching `resource_admission`.

### 2. Local inference stays local; there is no hosted fallback (AC3)

A hosted candidate is **refused at construction** — `LocalCandidate(is_hosted=True)`
raises — rather than quietly excluded at dispatch time. That is stronger than an
eligibility check, and it means no later gate, tie-break or quality rescue can
reach one.

When nothing local qualifies, the outcome is a **`ScopedLimitation`**: the stage,
the local shortfall, and what would have to change ("measure a local
configuration's capability on this host, free the resident model, or relax the
stated requirement"). It carries `hosted_fallback_offered: false`. There is no
branch anywhere in the module that can suggest otherwise, and
`HOSTED_FALLBACK_AVAILABLE` is a constant `False` because no code path could
compute anything else.

### 3. Accounting is not completion (AC4)

`MemoryAccounting` keeps three numbers apart — what is **charged**, what the
runtime **observed**, and what is **free**:

- The charge is raised to the observed figure when the runtime reports more than
  was charged. Assuming less than was charged is never safe.
- The charge is released **only** when the runtime reported the model `released`
  *and* nothing resident. That is the single case where the bytes are evidenced
  free.
- An unknown observation leaves the charge standing. A load or unload the runtime
  reports as finished is an *observation*, not proof that the slot is idle.

`accounting_gap` then reports the difference between what was charged and what
was evidenced, without ever quietly releasing it. A positive difference is
memory charged that nothing has shown to be occupied — reported, not assumed
away. `is_settled` means *the runtime resolved it*, not that the two numbers
happen to be equal.

### 4. Simultaneous residency is never introduced (AC2)

`plan_sequential_schedule` plans one stage at a time, unloading before loading.
`validate_sequential_plan` replays the plan and raises if any step would load
while another model is resident. A `switch` step **must** name the model it
unloads; a step that loads and unloads the same model is refused.

The plan is deliberately the slow one. A faster plan that co-resides two models
is not expressible, because every load is preceded by the unload it needs. No
step carries `activates_native_lifecycle: true` — that flag is a literal `False`
on every step mapping, and dormant native activation stays dormant.

## Explanations and override boundaries (AC5)

`RoutingDecisionRecord` is exhaustive by construction: it refuses to hold a
decision that selected a model without a selected verdict, or one that selected
nothing without a scoped limitation, or one with two selections. Every rejected
candidate carries at least one blocking gate, so "no model was chosen" is always
explainable rather than mysterious.

Preference (`prefer=`) is **not** an override: it only breaks ties between
candidates that already passed every gate, and it never promotes a lower-quality
candidate above a higher one.

A `ManualOverrideRequest` can choose among candidates that already passed —
usefully, the operator picking the model with the better voice when two are
equally capable. It **cannot** select a blocked candidate: accepting a gate
failure is refused at construction (`accepts_gate_failure=True` raises), because
that would bypass lifecycle qualification, which is the one thing the story
forbids. The record keeps who asked and why.

## Comparison against the baseline (AC6)

`BaselineComparison` reports quality, latency, switch count and cost **separately**
with explicit deltas, and refuses to collapse them:

> quality, latency, switch count and cost are compared separately; a faster plan
> that adds switches or loses quality is a trade, not an improvement.

`is_pareto_improvement` requires better quality *and* no worse latency, switches
or cost. A plan that halves latency by quadrupling switches reports
`pareto_improvement: false` — correctly, because that is a different trade.

## What this story does not decide

- **It does not dispatch.** No load, no generate, no runtime call, no provider.
  The scheduler produces a *plan*; executing it is someone else's authority.
- **It does not qualify models or hosts.** Quality gates read AP-013 profiles;
  compatibility reads recorded runtime support. A gate passing here is not a
  benchmark and not an acceptance.
- **It does not reserve memory.** `resource_fit` compares against the
  accounting figures the caller supplies; nothing is locked, and
  `resource_admission` remains the owner of real reservation.
- **It does not prove output quality.** `render_decisions` emits a
  `does_not_prove` block saying so, including that a passed quality gate rests on
  whatever probes the caller measured on one configuration.

## Reviewability

`render_decisions` is deterministic sorted-key JSON with a trailing newline and a
`routing_rules` block stating the boundaries (`hosted_fallback_available: false`,
`simultaneous_residency_permitted: false`, `accounting_implies_completion: false`,
`manual_override_can_bypass_a_gate: false`, `unknown_gate_blocks: true`). Every
decision has a stable digest.

## Verification

```sh
python -m pytest -q tests/test_routing_decision_records.py
```

63 tests, fully offline — no model loaded, no runtime started, no inference
issued. No claim is made about any installed model or host.
`runtime_activation_authorized` remains `false`; ADR-0040–0045 remain
`not-reviewed`.
