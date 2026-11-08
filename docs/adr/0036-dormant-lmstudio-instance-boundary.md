---
id: ADR-0036
title: dormant-lmstudio-instance-boundary
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0036: Dormant LM Studio instance lifecycle and inference boundary

## Context

LM Studio native v1 distinguishes artifact keys, loaded instances and allocated
config; its list does not prove digest, stream/ownership, JIT settings or physical
terminality. Auto-Evict excludes manual models. Native chat lacks custom-tool and
assistant-history parity. A generic compatible model identifier is not evidence
of exact-instance routing without JIT. SDK adoption would not by itself close those
gaps and must not introduce implicit discovery or get-or-load bypasses.

## Decision

Propose a dormant injected REST boundary with native v1 lifecycle and separately
installed-qualified, exact-instance compatible-wire inference. Do not register a
client, environment/qualification/proof producer, live authorizer or app consumer.
Default-deny effects without current auth/local-only ownership, explicit automatic
load/eviction/expiry exclusion, digest/config provenance and native terminality.

Persist atomic memory/send ownership, complete approved profile and returned native
instance binding before allowing inference. Require exact load echo/list config
and native instance identity, not catalog size/architectural max. Unload ACK never
releases a hold; post-send all-runtime physical confirmation and memory settlement
remain distinct. Ambiguous effects cannot be replayed or cancelled into success.

Preserve prepared request bytes and native JSON tool/ID fragments; withhold tool/
terminal packets until independent exact-instance execution/terminal confirmation.
A packet is not executable tool authority: canonical consumer, full request-fit,
all-path and recovery integration remain separate gates. Do not use native-v1
server MCP/stateful chat as a lossy tool/history replacement or add an SDK here.

## Alternatives

- Generic compatible-wire artifact routing: may trigger JIT or select a different
  instance/config; a response model echo is not execution proof.
- Native chat for all requests: drops unsupported custom tools/assistant history
  or delegates tool execution outside Studio's authority boundary.
- Auto-Evict/unload-all/manual-instance takeover: does not preserve ownership or
  establish cross-runtime physical release.
- Treat timeout/cancel/empty list as completion: late load can still allocate.
- Adopt SDK/private RPC now: introduces dependencies and transport/security/parity
  work without establishing the missing qualification facts.

## Tradeoffs

Conservative refusals leave versions/models/settings unqualified until actual
producers and supported instance-routing semantics are established. The pinned
source is documentation, not executable identity. Trusted attestations are not
native proof mechanisms; GUI/native clients, reused instance IDs, other owner
roots and malicious same-UID changes are not isolated by a coordinator lock.
Format is not backend qualification. No native engine internals or quality/speed
settings are changed. Actual provenance, token counting, packet consumers, journal
retention, populated migration and standalone integration remain release gates.

## Review Status

Proposed, not reviewed. Author-run source/fake tests, CI or merging dormant code do
not accept architecture/security, qualify installed native routing, resume test8
or activate the adapter. See the [contract](../lmstudio-native-bridge.md) for the
exact source limitations and recorded validation.
