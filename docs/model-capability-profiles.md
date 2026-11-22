# AP-013 — Measured model capability and compatibility profiles

**Status:** delivered for the AP-013 scope; **offline and advisory only**. It
**does not load a model, start a runtime, download anything, reserve hardware or
estimate a price**, and no production caller imports it. It is **not
activated**, and it is **not a production guarantee**.

The story's objective is one sentence long, and the module is built around it: *evaluate
exact model configurations instead of routing by brand or parameter count.*

## Why a configuration, not a model

A capability measurement is only meaningful for the thing that was actually
measured. So every record here is pinned to a `ConfigurationIdentity` carrying
the checkpoint digest **and** revision, the quantisation, the runtime **and its
version**, the chat template, the context length and a hardware fingerprint:

```
qwen3-coder-30b@main@7f3a/Q4_K_M/llama.cpp b4010/chatml
```

Change any of those and the digest changes, because a different quantisation or
template is a different behavioural object even when the weights "are the same
model". A `CapabilityProfile` **refuses to hold a measurement taken against a
different configuration**, so a number cannot quietly migrate from a Q4 build on
one host to a Q8 build on another.

## The five things that are kept apart

The story's second criterion exists because these are routinely collapsed into
"is it installed?":

| Facet | Type | Why it is separate |
|---|---|---|
| Capability | `Measurement` + `CapabilityProfile.capability()` | What it does well, measured per task kind |
| Companions | `ModelCompanion` | A vision projector or adapter is a *separate artifact*, and an adapter bound to another base revision is a compatibility failure, not a capability gap |
| Installed disk | `InstalledFootprint` | Weights, companions and KV-cache provision counted separately, so a total can be audited |
| Readiness | `Readiness` | Whether it could run now. `unknown` (installed but runtime support unverified) is **not** `ready` |
| Residency | `ResidencyState` | Whether it *is* resident, timestamped and with resident bytes. A cold model is installed and slow to first token, not incapable |

`assess_readiness` refuses to report `ready` unless runtime support for that
exact runtime version was verified — "installed" is never upgraded to "verified"
by absence of evidence.

## Task kinds are measured separately

`writing`, `coding`, `architecture`, `source_handling` and `schema_tool` each
have their own slot and their own measurement. `summarise_task_probes` takes
**one** kind and raises if given probes of another, because a model that writes
beautifully and cannot emit a valid tool call is not the average of the two, and
pooling them is how a routing decision gets made on a number that describes
nothing.

Failed probes still count as samples: a model that passed two of three attempts
scores the mean including the failure, not 1.0. Confidence requires at least
three probes and a bounded spread; an unstable measurement stays `indicative`
rather than becoming a qualification.

## Claims are recorded and never counted

A model card, an imported leaderboard, or a vendor statement is a `Claim`. It
carries **no value field at all**, so it cannot be read as a number even by
accident, and it always serialises with `qualifies: false` and a reason. The
same holds for a `Measurement` whose provenance is `model_card`,
`imported_benchmark` or `vendor_claim`: it is stored faithfully, `qualifies` is
`False`, and `capability()` reports `unknown` with a reason naming the
provenance. Only `local_probe` — attributed to this exact configuration — counts.

This is the criterion that is easiest to satisfy on paper and easiest to break in
practice, so it is encoded as a property, not a policy: `Measurement.qualifies`
is derived from provenance and cannot be set by a caller.

## Unknown fails explicitly, overrides stay bounded

`require_capability` returns an `allow`, `degrade` or `refuse` boundary:

- **`allow`** only from a confident local measurement of this configuration.
- **`refuse`** when the capability is unknown, when the only value is a claim, or
  when the measurement exists but is too thin or too unstable to trust.
- **`degrade`** only from an active operator `ManualOverride` — never `allow`.

An override names one configuration, one task kind, a rationale, who made it and
an expiry. It cannot contradict a measurement that exists, it does not apply to a
different configuration, it stops applying when it expires, and a caller can
refuse overrides entirely. It is reported as `operator_override` and never as a
measurement. Unknown is reported as **unknown** — not as zero, since "we have
not measured this" is not "it is bad at this".

## Role suggestions stay hypotheses

`default_role_hypotheses()` creates the Mistral, TwIL, Qwen and Gemma
suggestions with status `untested`. `evaluate_hypothesis` can promote one only
from a *confident local measurement of one exact configuration*, and the promoted
hypothesis then names that configuration's digest. A card claiming excellent
coding leaves the hypothesis untested. `RoleHypothesis.is_qualification` is
`False` for every status, so a supported hypothesis is still advice, scoped to a
configuration, and never a claim about the family.

## Dense and mixture-of-experts

`ArchitectureFootprint` records total parameters, active parameters and expert
count, and enforces the arithmetic: a dense model activates all of its
parameters, an MoE activates a strict subset but must name at least two experts,
and a fully-active "MoE" is recorded as dense. The module serialises
`active_parameters_establish_resident_memory: false` on every MoE footprint and
says it in words:

> mixture of experts: 8 experts, 30000000000 total parameters resident,
> 3000000000 active per token (10.0% activation). Low active parameters do not
> establish low RAM use: every expert is resident unless this exact runtime is
> verified to page them.

`CandidateComparison` compares two candidates on resident memory, active
computation, total parameters and *measured* task quality — and states that a
lower active count on the same host is not evidence of lower RAM use.

**MoE experts are not agents.** `ExpertAsAgentClaim` defaults to unsupported and
refuses the idea unless a runtime capability was verified *for that exact
runtime* with named evidence. `experts_are_separately_loadable` is a property
that returns `False`, always: it is not derived from anything, because the module
has no basis to derive it. Turning one checkpoint's experts into a "coding agent"
and a "writing agent" would be an invented capability, and the type says so.

## What this story does not decide

- **It does not measure anything by itself.** Probes are values the caller
  supplies; `summarise_task_probes` aggregates them and records provenance.
  Real capture belongs to `model_calibration`'s injected-probe path.
- **It does not qualify the host or the model.** A profile is evidence about one
  configuration on one host; it says nothing about the family, and it does not
  make a model admissible. `model_admission`, `capability_negotiation` and
  `model_recommendation` keep their own contracts.
- **It does not route, schedule, reserve or price.** No recommendation, no
  budget, no hardware reservation, no download.
- **It does not prove quality.** A task-quality score is a score on whatever
  probes the caller ran; `render_profiles` emits a `does_not_prove` block saying
  exactly that, including that a measurement does not transfer across runtimes or
  quantisations.

## Reviewability

Output is deterministic — sorted keys, sorted companion and capability order,
trailing newline — and every profile has a stable digest, so two runs over the
same evidence are byte-identical and a profile can be cited in a review record.

## Verification

```sh
python -m pytest -q tests/test_model_capability_profiles.py
```

86 tests, offline, covering all eight criteria: configuration pinning and
measurement non-migration, the four separate facets, per-kind measurement and
pooling refusal, claims never qualifying, unknown failing explicitly with
bounded overrides, hypotheses staying untested, dense/MoE memory arithmetic, and
expert-as-agent refusal.

No model was loaded, no runtime was started, no benchmark was downloaded, and no
claim is made about any installed model on the M5 host. `runtime_activation_authorized`
remains `false`; ADR-0040–0045 remain `not-reviewed`.
