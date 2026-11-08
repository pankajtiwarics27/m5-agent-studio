---
id: ADR-0031
title: shared-domain-model-lifecycle-authority
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0031: One lifecycle authority per shared memory domain

## Context

The owner requires broad native model choice through oMLX, Ollama and LM Studio,
with safe switching on a constrained shared-memory machine, not a replacement
inference engine. Existing model admission primarily limits active requests;
it does not prove global physical residency, native load completion, sufficient
context, or memory fit alongside tools. Native tokenization can load an engine;
unload acknowledgments can be deferred; client cancellation does not prove an
engine operation stopped. A terminal-launched server may also serve other clients.

The [audit](../model-lifecycle-research-and-proposal.md),
[plan](../cross-runtime-model-lifecycle-plan.md) and
[LC-00 capability investigation](../model-lifecycle-capability-feasibility.md)
record source evidence, existing seams, unresolved runtime capabilities and
scope. A long-lived authority/ownership decision is needed before implementation.

## Decision

**Propose** one Studio lifecycle authority per explicitly identified shared
physical-memory domain, independent of session/workspace/worker/provider wire
format. Retain native inference engine internals. A first-release local domain
allows one independently resident model instance and one active generation;
required architectural components belong in that instance's memory profile.
Independently loaded helpers are not exemptions. Qualified concurrency is future
measured policy, not permission to disable native single-request optimization.

Keep durable residency reservations separate from request/execution leases.
A completed turn leaves the resident warm. Proposed durable journal records,
generation/owner identity and a machine-scoped coordinator lock allow crash
reconciliation and prevent supported Studio processes from creating independent
workspace authorities. Reuse SQLite transaction/migration and task-event seams;
do not claim the current schema or event stream already implements this design.
Exact record schema, protocol/error codes and public configuration are later
reviewed, versioned changes, not new surfaces introduced by this ADR proposal.

Every load-capable operation, including compatible inference autoload, tokenizers,
probes, explicit controls and helper workflows, must enter that authority. A
source must drain and have verified release before target loading. Unresolved
physical operations retain their hold even after timeout, cancellation or a
stale-owner fence. A failed switch pauses recoverably; rollback is another
sequential verified transition. Do not auto-replay ambiguous lifecycle effects.

Distinguish attached/shared observation, owner-authorized coordinated use and
managed isolation. A cooperative-use agreement is not enforced exclusivity.
Unknown/unowned residents are conflicts, not permission to evict. First rollout
may honestly expose coordinated-use limitations; strict exclusivity is not
certified until supervision/access isolation is proven. Do not change an owner's
server launch, ports, authentication or other client access implicitly.

Admission must establish both single residency and a shared model/context/tool/
application/headroom memory envelope. Validate metadata/approved profiles before
disrupting the source; exact rendering/counting may occur only after the target
is safely admitted. Missing native context/counting/operation capabilities are
explicit unsupported/unknown states, not guesses, character-based proof or
unconditional success. Persist grants, budgets, evidence, tool/action identity
and pause state through handoffs. Do not silently truncate, enlarge context,
download, change models, relax reasoning or substitute cloud inference.

No new dependency, public API, runtime configuration, persistence migration or
runtime behavior is authorized or shipped by this proposal. Native Ollama dialect
and optional LM Studio SDK work require subsequent qualified implementation and,
for a dependency/security-boundary change, a focused decision/review.

## Alternatives

- Keep process-local request counters and fixed sleeps: insufficient for warm
  residency, competing runtimes, late effects or recovery across processes.
- Trust each engine's eviction policy: engine-local policies cannot coordinate
  independently running runtimes or Studio's session/execution authority.
- Mandate llama-swap or another process proxy: useful scheduling patterns, but
  not a replacement for attached native runtime management, permissions, full
  context admission and durable uncertain-action recovery. Optional supervised
  integration can be investigated separately if direct adapters cannot qualify.
- Replace every backend with oMLX or vLLM: violates the multi-runtime/model-choice
  requirement and does not itself solve application handoff or external clients.
- Treat one resident as complete memory safety: omits context/KV, required model
  components, engine workspace and concurrent tool/system memory demand.
- Require managed isolation immediately: stronger possible guarantee, but needs
  platform/runtime-specific supervision and access-control proof plus explicit
  operational approval; never claim it exists on a shared terminal server.

## Tradeoffs

Benefits are one auditable admission authority, retained warm/native performance,
explicit failure states and preserved model choice/session semantics. Costs are
cold-switch/re-prefill latency, bounded queue waits, persistent coordination,
version/backend qualification and potential operator recovery after uncertain
loads. Native KV state is not transferable across different model weights.

This cannot guarantee an external engine never crashes or an unrelated client
never allocates memory. Advisory locks cannot fence arbitrary clients or older
uncoordinated binaries. Memory observations have provenance/sampling limits.
Exact preflight is unresolved for some runtime/model/media paths, particularly
Ollama full requests and LM Studio inference/template parity. Source/fixture
results are not installed qualification. Activation remains gated until all
required capabilities for a specific approved profile are evidenced.

The failure mode is visible safe pause, not a second speculative allocation or
hidden cloud fallback. A rollback cannot discard the journal, free unknown holds
or silently resume pending tasks under an older unmanaged code path.

## Review Status

Not reviewed. Status is proposed, not accepted. The owner's instruction to begin
LC-00 authorizes investigation, not technical/security signoff of this new ADR.
Independent security review, approved installed-runtime qualification and owner
acceptance remain separate. No runtime/UI ledger or AS-031 review state changes.
