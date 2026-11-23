# AP-015 — Preserve context, preferences and artifact continuity

**Status:** delivered for the AP-015 scope; **offline and advisory only**. It
**loads no model, starts no runtime, reads no private store and wires no
production caller**. It is **not activated** and **not a production guarantee**.

The story's objective is itself a test: *does the work continue correctly after
compression, retrieval and model handoff?* That question contains a trap, and
the module is built around avoiding it. **A summary can read beautifully and
still lose the one approval that authorised the rest of the work.** So
readability is never used as the criterion.

## Continuity is a checklist, and it can fail (AC1)

`ContinuationState` accounts for six required kinds: the **brief**, **unresolved
failures**, current **artifacts**, **approvals**, **authority** and outstanding
**decisions**. Each element is either `survived` or `reconstructed` — and a
reconstruction must name its source, because saying "recovered" without saying
*from where* is an assertion, not a reconstruction.

Continuity is **not a score**. `require_continuity` raises when a critical
element was lost; it does not return `0.83` for a caller to judge acceptable.
`missing_kinds` catches the other half of the problem: a checklist that forgets
approvals entirely cannot certify that approvals survived, so an unaccounted
kind is a refusal too. `continuity_is_a_score: false` is in every serialised
state.

## Fitting never silently drops required input (AC2)

This module does **not** re-implement context fitting. `context_reassembly`
already fails audibly when mandatory messages cannot fit, so re-stating that
logic here would create a second, competing authority. Instead the audit is
independent of it:

```python
audit_fit(budget_tokens=…, used_tokens=…, dropped_indices=[3, 4],
          required_indices={"brief-1": 0, "appr-1": 4})
# FitAudit(truncated=True, dropped_required=('appr-1',), safe=False)
```

`truncated` and `safe` are deliberately different questions. Dropping optional
turns is fine; dropping something required is not. A fit that *succeeded* can
still be unsafe, and `require_fit_is_safe` refuses it.

## Probes test decisions and trails, not readability (AC3)

Every `ContinuationProbe` declares a kind — `decision`, `artifact`, `approval`,
`authority`, `failure` or `preference` — and lists the specific facts a correct
answer must contain. A probe whose question or expected answer is about how
readable the summary is gets **refused at construction**.

`ProbeBank.coverage()` and `.uncovered()` report what the bank actually tests, so
ten probes that all query the same decision cannot masquerade as thorough. Ten
probes, one kind → three uncovered kinds, stated plainly.

## Retrieved memory keeps its provenance, and cannot be promoted (AC4)

A `RecalledItem` carries its trust class unchanged through recall —
`trust_upgraded_by_recall: false` on every serialisation. An `untrusted` source
stays untrusted after being retrieved.

Instruction text has its own type. `RecalledInstruction` is refused unless the
item is both *trusted* **and** *verified*, so free-text recall cannot smuggle
"delete the production database" in as a remembered preference. Secret-trust
items must be classed secret — downgrading on recall is not recall.

## Recall does not cross project or privacy boundaries (AC5)

`recall_for` applies two independent filters: project scope identity, then a
confidentiality ceiling. A cross-project item is excluded **even when it is
public**, because a private project's existence is itself information. Everything
withheld is reported as `excluded` with a reason — never as a near-miss to be
reviewed. `cross_project_leak` is `false` on every result.

## Handoff assumes no KV-cache portability (AC6)

`PortableState` lists what actually travels: artifact ids, decisions, approvals,
preferences — things a successor can **re-read**. `kv_cache_portable` is `false`,
and `resumes_predecessor_internal_state` is `false`. Assuming otherwise produces
a continuation that looks fine and forgets it was mid-step.

## What this story does not decide

- **It does not compress or reassemble.** `context_reassembly` owns fitting; this
  module audits its result against what the work requires.
- **It does not read memory.** `context_store`, `evidence_lessons` and
  `history_privacy` keep their own storage, promotion and redaction contracts.
  Recall here is a filter over items the caller already holds.
- **It does not execute a handoff.** No model is loaded and no successor is
  called; a `SwitchVerdict` records whether a switch *would* be valid.
- **It does not prove output quality.** `render_continuity` emits a
  `does_not_prove` block: a satisfied checklist is not equal work quality, and
  probes are only as good as the questions asked.

## Reviewability

`render_continuity` is deterministic sorted-key JSON with a trailing newline and
a `continuity_rules` block stating every boundary
(`readability_is_continuity: false`, `silent_truncation_permitted: false`,
`recall_upgrades_trust: false`, `cross_project_recall_permitted: false`,
`kv_cache_portable: false`). Every state has a stable digest.

## Verification

```sh
python -m pytest -q tests/test_continuation_contracts.py
```

59 tests, fully offline — no model loaded, no runtime started, no private store
read, no handoff executed.
`runtime_activation_authorized` remains `false`; ADR-0040–0045 remain
`not-reviewed`.
