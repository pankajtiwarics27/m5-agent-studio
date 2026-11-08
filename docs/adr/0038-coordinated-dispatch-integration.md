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

Propose a separate bounded cold-step consumer with readiness before enqueue and
again at every native stage. Share pure native qualification and complete-input
feature classification with the existing send/fit paths; do not serialize a
provisional LM Studio instance or call metadata readiness a token fit. Consume
only real coordinator steps and concrete native bridge permits. Source physical
confirmation and LC03 attributed-zero settlement are separate, both mandatory,
before target load. Return typed stage completion, not a successful whole-control
ACK. Keep this dormant and namespaced in standalone; no actor/producers are
installed and unload-only controls are not fabricated from a fake target.

Compose a bounded inference backend from a concrete cold-step driver and admitted
warm executor, with full immutable call/profile/counter matching at handoff.
Serialize one actor through terminal completion/close; bound in-memory lock waiting
but never add inference output/time caps. Fence coordinator claims optionally to
the FIFO request in the same SQL write, not a racy pre-query. Refuse foreign/pending
work instead of polling or stealing it; cancel only the actor's own unadmitted
waiter. Interrupted stages/requests retain holds for explicit recovery. Keep the
backend dormant and controls unavailable; in-memory waiters are not durable tasks.

Extend concrete step consumption to an owned oMLX **source only**, using its LC04
permit/qualification shape and the actual separately bound model memory profile.
Refuse oMLX target load/verify jobs rather than inventing full-request/native
inference qualification. Normalize only ACK vocabulary; retain independent
physical confirmation and attributed-zero settlement, and bound proof production.
If physical proof advances residency but settlement fails, the paused charged
memory ledger independently blocks any later native target send. No polling or
atomic cross-kernel completion is claimed. Keep this dormant and source-only in
the private standalone bundle as well.

Introduce distinct durable explicit-load intent before implementing its native
command consumer. Keep load slots separate from inference types and guard their
SQL purpose in the native/fit kernels, including rewrapped field-identical leases.
Use the existing single FIFO/exclusive-slot constraint; require load-lease-scoped
all-runtime native terminal proof for slot completion without fabricated inference
or token counts. Cancellation/expiry are not native terminality or a completed
control. Keep model memory held and commands unavailable. Proposed private schema
v3 adds the closed purpose field; preserve/refuse v1/v2, with no automatic
populated migration or namespace clearing. A reviewed backup/migration remains an
activation gate; the active application database is unchanged.

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
production producer or cold scheduler. The continuation step driver adds real
cold source-to-target bridge/memory consumption and both-direction cross-runtime
release/race tests with fake facts; app scheduling, full controls and remaining
workflow consumption are still open.
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
