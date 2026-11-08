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

Compose a warm concrete executor from actual native bridge/fit services and
already-admitted leases. Reconstruct final bytes from complete application input,
refuse profile/output/reasoning changes and require the matching fit-service
callback. Tools/usage are withheld through independent native terminality.
Coordinated native gateway consumption bypasses legacy XML normalization and
preserves native IDs and LM argument strings; unknown options refuse explicitly.
Other legacy text tool consumers refuse instead of losing structural authority.

Embed lifecycle/fit/protocol/executor modules as a private standalone package,
not flat globals. Rewrite sibling imports to that package and reuse the existing
application persistence and dispatch objects as shims, preserving transaction
ContextVar and exception identities. Register modules before execution; do not
replace public module names. Isolated artifact tests block sibling fallbacks and
exercise both warm kernels with fake facts, not installed native qualification.

## Alternatives

- Replace served routing immediately: would activate unqualified transports and
  change user-owned native server/settings without permission.
- Gate only `stream_any_model`: misses direct control/provider/probe paths.
- Treat local-compatible routing or `True` callback as permission: neither proves
  an admitted domain operation, native identity or full request fit.
- Reuse old trimming/output recovery after refusal: mutates quality/context and
  can replay an uncertain operation or substitute an unapproved target.

## Tradeoffs

LC-08 remains incomplete: cold acquisition/control orchestration, pre-disruption
metadata gates, remaining all-path canonical consumption and complete cross-runtime
cold-control races still need implementation/proof. The warm executor is not a
production producer or cold-load controller. Current races cover duplicate count
and queued-control blocking while an admitted native stream is active only.
Coordinated combos, unqualified private/proxy providers and unowned evictions
currently refuse. Legacy behavior remains served by default, so no cross-runtime
guarantee is claimed for it. Static inventory cannot prove dynamic/native
reachability. Namespaced warm standalone tests do not qualify installed native
behavior. No configuration/schema/dependency, native activation or
security/architecture acceptance is implied.

## Review Status

Proposed, not reviewed. Author-run fake tests, CI or merging a checkpoint cannot
accept architecture/security or qualify installed native behavior. LC-08 remains
in progress and test8 paused/unpassed. See the
[integration record](../runtime-dispatch-integration.md) and generated
[entry inventory](../runtime-dispatch-entry-points.md). ADR-0031--0037 and acceptance
ledgers are unchanged.
