---
id: ADR-0035
title: unactivated-ollama-native-bridge
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0035: Unactivated Ollama native lifecycle and inference bridge

## Context

The served Ollama compatible-wire path does not carry an approved native context
profile on each request. Catalog/show/ps have different meanings; unload ACKs and
empty residency polls do not prove release or exclude late loads. Native tool
calls are complete objects with IDs, not OpenAI argument deltas. Native input and
thinking defaults can silently differ from a compatible-looking API surface.

## Decision

Propose two dormant injected modules: a pure strict canonical/native translator
and a coordinator/shared-memory native bridge for the pinned source contract.
Require explicit local references, unchanged approved options/runner/context/
thinking, strict no-truncation/no-shift flags and exact full-payload digest-bound
request authority. Reject unsupported media/schema/choice semantics instead of
silently dropping them. Preserve tool/result IDs and unknown metrics explicitly.
Do not register a client, installed qualifier, proof producer or app consumer.

Commit LC-03 dispatch, complete load-profile binding and one-send metadata before
native effects. Require fresh native observations plus separate exact-instance,
stream, ownership, late-effect and all-runtime physical evidence as appropriate.
An unload ACK never releases a hold; a load ACK only permits verify. Publish tools
and release an inference slot only after validated EOF and independent terminal
confirmation. Keep warm model memory reserved. Cancellation/timeouts do not replay
or imply physical cancellation. Use the current private schema, not a second pool.

## Alternatives

- Activate native inference alone now: creates load-capable bypasses before full
  request-fit, all-path and recovery integration exist.
- Reuse the served OpenAI delta/XML assembler: loses native ID/result identity
  and mistakes whole-object calls for fragments.
- Infer active instances/streams from tags/show/ps or an unload ACK: those APIs
  lack exact native instance, stream and terminal/physical authority.
- Relax thinking, increase context, truncate anchors or retry another protocol:
  changes quality/intent and can duplicate an already-running allocation.
- Accept arbitrary tool schema/options: pinned native typed fields can discard
  constraints or warn/skip unknown options; apparent success is not parity.

## Tradeoffs

Conservative refusals deny models/versions/tools without qualified semantics.
No settings are chosen from a particular machine/RAM/model; the approved profile
and bound must come from a trusted installed producer. Capability references,
activity and grant records remain trusted attestations, not native proof systems.
Artifact-based routing needs qualified exclusive control: locks do not isolate
arbitrary native clients or owner roots. Actual producer provenance, journal
retention, populated migration, standalone consumer integration and quality/speed
benchmarks remain release gates. No SDK or runtime dependency is added.

## Review Status

Proposed, not reviewed. Fake/source evidence and merging dormant code do not
accept architecture, independently review security, qualify installed engines,
activate native routing or pass paused hardware test8. See the
[bridge contract](../ollama-native-bridge.md) for trust boundaries and validation.
