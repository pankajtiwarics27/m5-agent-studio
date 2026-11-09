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

Compose an optional native explicit-load plan on the inference actor, sharing its
lock but retaining coordinator/memory authority across independent actors. Use
pure native load configuration readiness (no fake inference input/count) and real
load-purpose queue/stages/admission. Extend native permits explicitly to LoadLease
for read-only model confirmation, never inference/send. Reuse native observation /
proof validation, actual native load-profile records, and fresh all-runtime owned
instance/context evidence. Check current dispatched model memory/budget/sample
revision in the same final write as control-slot completion. Canonical confirmed
requires the actual completed uncancelled/unexpired load row. Keep all producers
uninstalled; oMLX targets and unload/all/cache commands still refuse. Do not
redirect the existing oMLX HTTP model-control route or infer future request fit
from model residency. This is partial LC08 integration, not activation/review.

Add a distinct release-only foundation before any native unload command consumer.
Do not invent a replacement target or widen existing transition/permit authority.
Persist private schema v4 acquire/release goals and source request/model-hold
references; permit NULL target only for release. Preserve/refuse v1/v2/v3 without
migration/reset. Physical unload uses existing operation-scoped full-runtime
absence/terminal proof, but retains the switching/operation barrier until the
actual bound source reservation has independent durable LC03 zero settlement.
Retire the barrier only with current operation/epoch/generation/request/hold checks
in one final write; cancelled/expired requests cannot become completed controls.
Keep physically complete-but-unsettled release markable uncertain and restart
reconciliation-only. Existing native permits reject this new type; miswired
existing actors fence it without native calls. This foundation is not an unload
consumer, already-absent success policy, unload-all/cache, LC09 recovery, default
producer or production activation.

Extend only the Ollama/LM Studio native permit unions explicitly for source-only
ReleaseStep, with a separate durable release gate and disjoint receipt prefixes.
Retain all native qualification, loaded-profile, ownership/activity and observed
version/config checks. Add pure UnloadAdmission and source-bound queued intent,
plus distinct concrete release jobs consumed by a bounded step driver. Physical
proof must precede independently sampled real source-hold zero settlement and
final barrier retirement. After send, cancelled/expired cleanup may retire resources
but cannot complete a command. A stage/barrier ReleaseResult is not canonical
confirmation. Keep the public/actor unload command, oMLX unload-only permits,
already-absent policy, unload-all/cache, native producers and activation unavailable.
No schema change or implicit recovery/migration is introduced by this continuation.

Compose an optional NativeUnloadPlan on the existing native load/inference actor,
without default installation or HTTP redirection. Capture the actual source after
shared lock acquisition and consume only its two FIFO-fenced release stages.
Canonical confirmed requires exact durable live completed request, release goal/
source/instance/epoch/generation, released source-model hold and final retirement
receipt; a returned retired flag or physical absence alone is insufficient. Do not
require the slot to remain idle after historical completion, or poison successor
work on cancelled/expired cleanup. Preserve unknown holds/barriers on interrupted
work and never unlock another lock owner. Keep oMLX unload-only, already-absent
success, unload-all/cache, production composition and LC09 recovery unavailable.

Add distinct OmlxUnloadAdmission with LC04 source qualification and the actual
separately bound model-memory profile, never a fabricated native fit/load profile.
Bind clock/observation policy/runtime to the actual owner before queue/probe.
Explicitly extend only the oMLX release intent role and compose concrete OmlxRelease
on the optional actor. Record source profile digest with actual load send; require
that record and exact hold before release drain/mutation. Missing records refuse,
not migrate/reset. Keep disjoint release send/ACK receipts, mandatory current ACK
before confirmation, all native source/visibility gates, full-runtime physical
proof, independent real zero settlement and durable canonical retirement checks.
A pending200/202 ACK remains insufficient. Preserve cancelled/expired cleanup,
uncertainty/no-replay and later target budget fencing. Do not qualify oMLX target/
inference, install producers, redirect HTTP, activate or complete LC09 recovery.

Harden explicit-load confirmation symmetrically with unload: record a native
provider-specific verified control/memory identity receipt atomically with actual
terminal slot completion, after native profile/instance/physical/memory checks.
Require the actor's own request/domain/target/epoch before producer execution and
exact completed uncancelled/unexpired request/deadline, released control slot,
actual model binding and unique native receipt before canonical success. A generic
kernel finish, boolean or request status cannot manufacture native qualification.
Roll back slot completion on receipt-write failure. Preserve historical completion
after independent successors without a new probe or poisoning their ownership or
memory. Keep pure identity hashing distinct from proof/authority; no schema,
backfill, migration/reset, activation or new native target support is implied.

Fence claimed request/control leases to the actor's exact queued request, domain,
target and owner epoch before producer execution or cleanup assignment. Foreign
wrong-purpose slots must not be marked uncertain or cancelled by this actor;
owned wrong-purpose slots retain conservative holding semantics. Require warm
fit services to share the scheduler's actual coordinator and memory-owner objects
before counter/native execution. Treat this consumer fence as identity only, not
native/fit/physical/recovery authority, and preserve existing generation/instance/
profile/current-state gates. Do not infer general operation ownership or enable
new native targets, producers, routes or migration/recovery through this check.

Extend consumer ownership checks to claimed lifecycle stages: verify exact current
queued FIFO head and durable operation/domain/source/target/stage/epoch/generation
identity before actor producers, wrong-purpose mutation or cleanup ownership.
Release additionally binds its durable request and source-memory ID; acquire's
shared target alone is not ownership. Repeat the same read-only check in each
concrete driver's current-gate transaction, before native/current checks. Refuse
foreign claims without mutation, pause, cancellation or expiry of foreign work.
This is not a new persistent acquire-owner/grant, native authorization, coalescing
or recovery protocol. Preserve separate purpose/cancellation/deadline/native/fit/
physical/memory gates and owned failure holding; no schema or activation change.

Couple current-intent and model-memory interruption holds in one fenced ledger
transaction. A stale/completed request or advanced acquire phase must not pause a
successor's model after a failed ownership mark. Preserve genuine cancelled/
expired uncertainty, exact current release-barrier ownership across its phases,
and the physically completed acquire unload's exact unpaid source settlement
until independent zero. Bind that exception to current operation/source/epoch/
generation/target and durable physical-completion receipt; never a released hold
or successor. Validate actual model identity before writes and roll back both
halves on journal failure. Reuse this accounting-only seam in actors/drivers and
native failure paths; do not mint native/dispatch/recovery authority or alter
schema, qualification, proof, settlement, production defaults or parked review.

The full-request fit-counter's post-dispatch cleanup uses that same atomic
current-request/model hold, not a second split uncertainty path. Late counter,
receipt or post-count observation failure after independently proved terminal
scope cannot pause a successor. Current cancelled/expired counts remain held and
charged, and ledger failure rolls back both halves. Count facts do not become
inference completion, physical release, retry permission or fit approval.

Preserve provider-neutral legacy reasoning recovery by default, but let the
application adapter refuse changed-budget reasoning retries in coordinated mode.
Own and close iterators at each Council/Debate route/orchestrator/dispatch forwarding
layer; tolerating iterator-only providers is not permission to invent native
completion or release. Keep text-tool consumers fail-closed and preserve canonical
cleanup refusal. Record bounded per-owner workflow/input evidence, not a claim
that static entry inventory establishes complete workflow support. See the
[adapter audit](../lc08-workflow-adapter-audit.md); no producer or review activation.

Extend that bounded ownership/retry policy to Hall HTTP summon, not MCP summon
by association. Preserve Hall save/pin-before-optional-cleanup ordering: an
unconfirmed cleanup refuses terminal success without rolling back the completed
transcript or marking it abandoned. See the [Hall audit](../lc08-hall-summon-audit.md);
LC09 retention/recovery and review/activation remain separate.

Extend deterministic owned-source closing to the MCP stdio summon helper, with
full matter/chronicle and original options unchanged. Preserve its distinct
cleanup-before-append ordering and existing admission/JSON-RPC refusal; do not
claim HTTP Hall session parity or canonical tool consumption. See the
[MCP audit](../lc08-mcp-summon-audit.md); no producer or review activation.

Extend owned-source closing to the folder-name suggestion consumer, inside its
existing timeout. Preserve explicit model-request consent, accepted full input,
fixed options, tool-context restoration and suggestion-only/no-filesystem-action
semantics. See the [workspace suggestion audit](../lc08-workspace-suggestion-audit.md);
no canonical tool consumer, producer or review activation is introduced.

## Alternatives

- Replace served routing immediately: would activate unqualified transports and
  change user-owned native server/settings without permission.
- Gate only `stream_any_model`: misses direct control/provider/probe paths.
- Treat local-compatible routing or `True` callback as permission: neither proves
  an admitted domain operation, native identity or full request fit.
- Reuse old trimming/output recovery after refusal: mutates quality/context and
  can replay an uncertain operation or substitute an unapproved target.

## Tradeoffs

LC-08 remains incomplete, but the warm-only limitations recorded in earlier
checkpoints are not the current implementation boundary. Dormant bounded
cold-to-warm inference, optional explicit load/unload actors, pre-disruption
readiness, selected cross-runtime/control races, durable native completion receipts
and atomic native/fit-counter holds now exist. They use synthetic qualification
facts and are not production-installed producers or all-workflow enforcement.
Remaining work includes per-owner complete-input/canonical consumption evidence,
production composition, unsupported target/control policies, broader adversarial
coverage and separately gated LC09 recovery. See the
[current status and gap review](../lc08-current-status.md) for source/test anchors
and the recommended bounded workflow-coverage increment. This status clarification
does not change the proposed decision, review metadata or acceptance requirements.
Coordinated combos, unqualified private/proxy providers and unowned evictions
currently refuse. Legacy behavior remains served by default, so no cross-runtime
guarantee is claimed for it. Static inventory cannot prove dynamic/native
reachability. Namespaced warm standalone tests do not qualify installed native
behavior. No public configuration/application-schema/dependency, native activation or
security/architecture acceptance is implied.

## Review Status

Proposed, not reviewed. Author-run fake tests, CI or merging a checkpoint cannot
accept architecture/security or qualify installed native behavior. LC-08 remains
in progress and test8 paused/unpassed. See the
[integration record](../runtime-dispatch-integration.md) and generated
[entry inventory](../runtime-dispatch-entry-points.md). ADR-0031--0037 and acceptance
ledgers are unchanged.
