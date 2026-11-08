---
id: ADR-0038
title: coordinated-dispatch-integration
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0038: Coordinated dispatch integration and legacy escape-hatch fencing

## Context

LC-02--07 supply dormant coordination, memory, native adapter and fit contracts.
Served workflows still enter legacy paths with character trimming, output caps,
blind cleanup and compatible-wire local providers. Adding just an alternate
stream function would leave control, fallback, probe and direct-helper bypasses.
No installed qualification/counter/transport/permit producer exists to activate
those contracts safely; terminal Ollama remains user-owned.

## Decision

Propose internal immutable coordinated composition at common application dispatch
and control seams, before legacy sanitization/trimming, demo/online probes or
output/reasoning changes. Preserve the existing legacy rollout default; expose
no new setting, endpoint or automatic backend registration. Native runtime aliases
must be explicitly composed, never inferred as qualified from a port/model name.

Fence direct legacy provider helpers and generic pinned-HTTP runtime effects in
coordinated mode, even when a backend exists. Route explicit controls and workflow
cleanup through the same boundary; pending/refused/uncertain cleanup must not be
reported as completed. Refuse unowned automatic eviction and unsupported combos
rather than silently falling back to another model or cloud. Keep existing remote
provider authorization, pinning, consent and rate/auth/origin protections.

Inline the dependency-free fence in standalone and test outside the checkout.
Keep a generated static entry inventory and reject unmapped direct wire owners.
The backend protocol is trusted composition, **not native-operation authority**.
A fake backend proves routing only. Native execution still requires actual
LC-02 domain operations, LC-03 memory, LC-04--06 qualifications and LC-07 exact
final-wire fit plus independent terminality. No backend is installed by default.

## Alternatives

- Replace served routing immediately: would activate unqualified transports and
  change user-owned native server/settings without permission.
- Gate only `stream_any_model`: misses direct control/provider/probe paths.
- Treat local-compatible routing or `True` callback as permission: neither proves
  an admitted domain operation, native identity or full request fit.
- Reuse old trimming/output recovery after refusal: mutates quality/context and
  can replay an uncertain operation or substitute an unapproved target.

## Tradeoffs

This first integration checkpoint is incomplete LC-08: native executor/canonical
consumer, complete final-wire request-fit coupling and concurrent cold-control /
inference races still need implementation and proof before story closure.
The backend interface is not itself a production executor. Coordinated combos,
unqualified private/proxy providers and unowned evictions currently refuse.
Legacy behavior remains served by default, so no cross-runtime guarantee is
claimed for that default. Static inventory cannot prove dynamic/native reachability.
Standalone fencing works without importing dormant native modules; that is not
standalone native executor qualification. No configuration/schema/dependency,
native activation or security/architecture acceptance is implied.

## Review Status

Proposed, not reviewed. Author-run fake tests, CI or merging a checkpoint cannot
accept architecture/security or qualify installed native behavior. LC-08 remains
in progress and test8 paused/unpassed. See the
[integration record](../runtime-dispatch-integration.md) and generated
[entry inventory](../runtime-dispatch-entry-points.md). ADR-0031--0037 and acceptance
ledgers are unchanged.
