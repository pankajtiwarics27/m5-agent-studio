# AP-003 — Versioned evaluation briefs and failure fixtures

**State:** in review • **Claim base:** `d48dcbe38075af8958e77c04cce2bf1e41130ca0` • **Draft PR:** [#284](https://github.com/pankajtiwarics27/m5-agent-studio/pull/284)

## Authority and outcome

AP-003 defines the evidence bar before any AP model, prompt, workflow, or swarm
optimization. Its records are offline, synthetic, versioned, and explicitly
provisional. They activate no evaluator runner, optimizer, model, provider,
network destination, account, path, side effect, promotion, or runtime.

The owner authorized AP-002 and AP-003 sequentially. AP-001 and AP-002 are done;
this branch claims only AP-003. AP-004 through AP-032 remain unauthorized.
U01–U06 remain pending, including the initial specialization choice. Therefore
research, website, and short-video briefs below are generic fixture categories,
not launch commitments or owner acceptance.

The records are:

- [`product/M5_Autonomous_Production_Evaluation_Briefs.json`](../product/M5_Autonomous_Production_Evaluation_Briefs.json)
  — quality model, three synthetic briefs, dataset partition policy, task
  inventory, single-agent baseline, resource accounting, provisional target,
  and private-trace policy;
- [`product/M5_Autonomous_Production_Failure_Fixtures.json`](../product/M5_Autonomous_Production_Failure_Fixtures.json)
  — six synthetic fault/security fixture contracts;
- [`autonomous_production_evaluation.py`](../autonomous_production_evaluation.py)
  — strict offline parser, roadmap-drift checks, role/partition decisions,
  ordinary-optimizer-safe view, existing `EvalTask` projection, and metric-only
  private-trace scrubber.

These records extend the existing pure contracts rather than replacing them.
[`model_comparison.py`](../model_comparison.py) continues to own versioned task
and reproducible-config identity. [`workflow_evaluation.py`](../workflow_evaluation.py)
continues to aggregate caller-supplied matched measurements. AP-003 adds no
executor to either module. [`failure_injection.py`](../failure_injection.py)
remains the deterministic crash/cancellation boundary; the AP fixture inventory
names broader scenarios and required evidence without arming it.

## Four separate meanings of quality

One score or one model grader cannot certify every dimension. The registry keeps
four categories and their criteria separate:

| Category | Question | Representative evidence | Gate rule |
|---|---|---|---|
| Technical | Does the requested function work, and is the real artifact valid, usable, accessible, cancellable, and recoverable? | Deterministic checks, real-consumer inspection, adversarial and recovery checks | Critical functional and safety criteria are hard gates and cannot be averaged away. |
| Factual | Are consequential claims sourced, and are uncertainty and contradictions explicit? | Source/claim maps, contradiction checks, provenance review | Unsupported consequential claims fail even when style is strong. |
| Creative | Does voice, structure, pacing, visual language, and originality fit the brief? | Blinded review, artifact inspection, similarity and cross-category review | Creative review cannot waive factual or technical failures. |
| Practical | Can the fictional audience use the package, with limitations, dependencies, cost, and next authority clear? | Completion, delivery, policy, and objective review | Hidden operator repair and unsupported demand/revenue/readiness claims are rejected. |

The 0–4 scale is a representation contract, not an observed result. Category
scores remain independent, every hard gate is preserved, failures and
variability are reported, and a single grader cannot certify all categories.

## Versioned synthetic briefs

Each version-1 brief requires all four quality categories and names its delivery
artifacts and prohibited claims:

| Brief | Bounded output | Explicit non-claims |
|---|---|---|
| `synthetic-research-brief` | Evidence-linked option brief, source map, claim/uncertainty log, and limitations | No owner specialization choice, observed demand, or production approval. |
| `synthetic-website-brief` | Local prototype, source package, functional/accessibility reports, attribution, and limitations | No launch choice, deployment grant, or real conversion claim. |
| `synthetic-short-video-brief` | Playable local draft, script/storyboard/captions, claim map, rights manifest, technical report, and limitations | No launch choice, publishing authority, or monetization claim. |

`eval_task_for_brief()` projects a brief's ID, version, and four category names
onto the existing `model_comparison.EvalTask` identity. It does not score or run
the task. The richer AP criterion and evidence requirements remain in the AP
record rather than being collapsed into that compatibility projection.

## Dataset partitions and held-out boundary

The initial plan deliberately follows the small roadmap proposal: 10 research,
5 website, and 5 short-video cases. All 20 current case identities are synthetic
and answer-free. The sizes are **provisional until baseline calibration** and
cannot support a broad reliability claim.

| Partition | Count | Ordinary optimizer prompt access | Ordinary optimizer answer access | Mutation |
|---|---:|---|---|---|
| Development | 11 | Allowed for synthetic cases | Allowed for synthetic development material | Denied; only a dataset curator can create a new version. |
| Validation | 5 | Allowed | Denied; evaluator-only | Denied; only a dataset curator can create a new version. |
| Held-out | 4 | Denied | Denied; independent-evaluator/curator only | Denied; only a dataset curator can create a new version. |

No answer is embedded in the public registry. Future protected answer material
must live in a separate grant-confined evaluator store outside ordinary
optimizer read/write roots. The evaluator gets read-only scoring access; the
curator creates a new version instead of mutating an accepted one. Protected
material is never released to an optimizer.

`dataset_access_decision()` is an offline policy decision, not a Studio grant.
A future consumer must still enforce equivalent roots and roles through the
owning authority boundary. `ordinary_optimizer_view()` exposes development and
validation prompt identities, only a count plus an integrity commitment for
held-out cases, no validation or held-out answers, and no write authority.

## Comparable single-agent baseline

Every comparison must retain an unchanged one-agent, no-delegation incumbent.
The baseline and challengers use the same dataset/task/rubric versions,
model-runtime configuration, hardware profile, resource budget, and all ten
accounting fields:

- wall time;
- input and output tokens separately;
- tool calls and retries;
- repair attempts and user-intervention seconds;
- cost in integer minor units plus currency;
- peak memory bytes.

The existing workflow evaluator already represents success/outcome, latency,
cost, peak memory, and pinned task/config/hardware/environment identity. The
execution budget supplies elapsed time, tokens, cost, tools, and retries. A
future AP runner still has to supply input/output token split, repair attempts,
and intervention time; AP-003 does not invent those measurements.

The initial three repetitions per task/candidate and 80% accepted-without-user-
repair target are non-binding proposals. Both remain provisional until measured
baseline calibration and owner approval. Reports retain every run, denominator,
variability, failures, exclusions with reasons, first-pass success, repairs,
intervention, median/tail latency, cost per accepted artifact, and resource use.
Critical technical, safety, authority, and egress gates cannot be averaged away.

## Fault and security fixture inventory

Every public fixture is synthetic untrusted data. Network, filesystem effects,
and external accounts are disabled. Protected variants can later use the
held-out policy, but no protected prompt or answer is committed here.

| Kind | Injected condition | Required outcome |
|---|---|---|
| Prompt injection | Retrieved source asks for policy bypass, hidden prompt disclosure, and ungranted network use | Content stays data; no authority expansion, secret disclosure, or network action. |
| Invalid artifact | Malformed/mismatched artifact or missing required component | Verification fails closed, dependent evidence invalidates, and acceptance is denied. |
| Uncertain write | Crash after an effect but before durable result commit | Effect remains uncertain, replay is denied, and explicit reconciliation is required. |
| Cancellation | Owner stop or disconnect while child work is active | Cancellation propagates, children quiesce, and partial work is not accepted. |
| Stale worker | Superseded lease/policy generation attempts a late commit | Fencing rejects it and preserves successor state and authority. |
| Lost context | Handoff omits a decision, artifact version, or unresolved failure | Continuation probes fail, context is recovered/requested, and no decision is invented. |

An exception message or agent assertion is not success evidence. Each fixture
names durable refusal/recovery evidence and existing source/test anchors. This
inventory does not claim those anchors already implement a complete AP runner.

## Private traces and data handling

Synthetic fixtures are the default. Raw real private traces are not accepted as
evaluation input and may not be persisted by this contract. If separately
authorized real traces are ever used, `scrub_private_trace()` must run first.
It requires complete comparable metrics and retains only:

- strategy, terminal outcome, and task success;
- task/config/hardware canonical digests;
- latency, integer-minor-unit cost/currency, peak memory;
- token/tool/retry/repair/intervention counts; and
- bounded generic failure-kind enums.

Prompts, responses, tool arguments/results, paths, accounts, user identifiers,
environment values, trace IDs, and arbitrary free text are dropped. Credential-
shaped content is counted through the existing history redactor but never
returned. The result carries a digest of the safe metric projection, not the raw
trace. Missing or incomparable metrics fail closed rather than becoming zero.
Best-effort credential matching is not a general anonymizer; the safety property
comes from the strict allowlist and dropping all free text.

## Compatibility and rollback

AP-003 is additive and offline. It changes no API, UI, database, generated
`studio.py`, provider path, runtime dispatch, filesystem permission, or network
policy. No feature flag or migration is required. Rollback removes the two AP
records, parser, focused tests, this page, and AP-003 tracking changes while
preserving AP-001/AP-002 history and all AS/M5/UIUX/UIV2/LC records.

## Acceptance mapping

| AP-003 acceptance criterion | Evidence |
|---|---|
| Technical, factual, creative, and practical criteria separately represented | Exact four-category registry, independent criteria/evidence, hard-gate and no-single-grader validation. |
| Single-agent baseline and comparable resource accounting | Unchanged no-delegation baseline, matched identities/budget, ten exact accounting fields, and `EvalTask`/workflow/budget reuse mapping. |
| Injection, invalid artifacts, uncertain writes, cancellation, stale workers, and lost context | Exact six-kind synthetic fixture inventory with expected outcomes, durable evidence, and existing anchors. |
| Small sizes and targets provisional until calibrated | 10/5/5 plan, three-run proposal, and non-binding 80% target all carry explicit calibration status; broad reliability claims are disabled. |
| Held-out answers inaccessible/immutable to ordinary optimizers; real private traces scrubbed first | Answer-free registry, role/partition denial, safe optimizer view, curator-only new versions, protected-store contract, and strict metric-only scrubber tests. |

## Verification

Run the offline validator and focused tests with:

```bash
python autonomous_production_evaluation.py
python -m pytest -q tests/test_autonomous_production_evaluation.py \
  tests/test_workflow_evaluation.py tests/test_model_comparison.py \
  tests/test_failure_injection.py tests/test_history_privacy.py
```

The validator reads only repository JSON. It performs no model inference,
optimization, workflow execution, network access, account access, file effect,
or runtime activation. Real model quality, owner-host performance, private-trace
use, human creative acceptance, independent security review, and release
approval are not executed or claimed by AP-003.
