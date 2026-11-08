---
id: ADR-0034
title: unactivated-omlx-lifecycle-bridge
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0034: Unactivated oMLX native lifecycle bridge

## Context

LC-02 provides durable intents, not native effects; LC-03 reserves combined
memory, not execution authority. oMLX unload may return deferred 202 and pool
status cannot establish instance identity, active context or physical release.
The served helper's sleeps/suppressed errors are not a qualified release boundary.
Native activation before all runtime/request-fit/session paths are ready would
create a partial guarantee with load-capable bypasses.

## Decision

Propose a separate, unregistered adapter for the pinned oMLX public route shape.
Require an explicit live server authorizer and installed-scope qualification,
never infer support from source review/version strings. Inject authenticated,
pinned, bounded, no-redirect/no-retry transport; supply no production client or
native proof producer in this story. Keep served helpers unchanged until LC-08.

Persist one operation/stage send marker before each native mutation, composing
load's LC-03 dispatch in that same fenced transaction. Reuse metadata-only
journal entries in private schema v2, not a new database/long transaction. Never
auto-replay an ambiguous send, even if a process died before the request left.
Reject stream/instance uncertainty, pinned/loading/helper/external conflicts,
auth failures and malformed status rather than guessing zero/false.

Unload ACK/202/200 never releases the domain; load's qualified blocking response
can only advance to fresh verification. Completion requires explicit current
multi-runtime, ownership, terminality and physical evidence, not pool metadata.
Keep physical and memory holds through timeout/cancellation; separate settlement
still applies. Do not expose a cold-load-capable tokenizer before LC-07.

## Alternatives

- Replace served helpers now: activates partial coverage before request-fit,
  native qualification and session/all-path enforcement exist.
- Fixed sleeps / accepting all 2xx or 400-as-absent: do not prove release or
  exclude delayed loads; may collide with a target allocation.
- Infer native instance/context from model name or `model_context_length`:
  conflates catalog identity/architectural metadata with current allocations.
- Retry after timeout/connection loss: can duplicate an effect that still runs.
- Commit memory and native-send ownership separately: leaves a crash window
  where one layer can regard the effect as unsent or replayable.
- Tokenize as preflight: reviewed endpoint leases/loads an engine, can reject
  media, and does not alone prove unchanged evidence/template preparation.

## Tradeoffs

Strict unknowns and conservative wire blockers deny profiles whose helper,
stream, ownership or physical visibility is unqualified. Native inference quality
and engine internals are not changed to obtain a pass. The server remains outside
arbitrary-client isolation guarantees. Qualification flags/references are trusted
attestations, not proof mechanisms; their actual producers and provenance remain
release gates. A synchronous live authorizer must be bounded and cannot replace
real grant/session checks. Durable send markers add history to an unbounded
metadata journal; reviewed retention and populated migration remain outstanding.

## Review Status

Proposed, not reviewed. Passing source/fake tests is not installed support,
architectural acceptance, independent security approval, native activation or
hardware test8 acceptance. See the [adapter contract](../omlx-lifecycle-bridge.md)
for the exact trust limits, failure behavior and remaining integration gates.
