---
id: ADR-0029
title: chat-context-vault-retrieval
status: accepted
date: 2026-11-07
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0029: Reachable, session-scoped Chat context-vault retrieval

## Context

The owner's first local MCP inventory test executed the tool successfully but
failed to produce a summary. Its eight-server result exceeded the existing
Context-Mode compression threshold. The model received a JSON-shape digest and
instructions to use `ctx_search`, but Chat omitted that already-registered read
function from native schemas and the tagged guide. OpenContext tools use a
different store and cannot retrieve `ctx-…` references. Existing small-result
protocol tests did not exercise this handoff.

## Decision

Advertise existing `ctx_search` in the native read-tool schema and tagged guide.
Explain doc-ID/empty-query retrieval versus BM25 terms, distinguish Context-Mode
from OpenContext, and discourage config-file hunting to reconstruct inventory.
Keep existing compression, untrusted-result framing, audited tool dispatch,
plan policy, tools-off behavior and tool-step budget. Add no HTTP route, model
retry, provider switch, MCP connection or execution permission.

Before advertising retrieval, require session-ID filtering in every existing
vault query, including doc-ID-only and doc-ID-plus-query branches; doc IDs must
not be ambient cross-session read capabilities. Clamp retrieval to one through
six chunks, matching the published native schema. Direct Chat owns a fresh
pseudo-session per request, so prior-turn/workflow references do not establish
ownership. These are explicit restrictions on existing helper behavior, not a
new conversation/session identity system or an authentication change.

## Alternatives

- Disabling all compression was rejected: it removes an existing context-budget
  control and may send unbounded tool dumps into inference requests.
- Asking the model to reconstruct inventory through config files or OpenContext
  was rejected: those sources are unrelated and can invite unnecessary reads.
- Increasing the tool-step budget was rejected: it hides the broken retrieval
  handoff behind more inference without making the correct tool available.
- Advertising retrieval with global doc-ID access was rejected: a reference
  must not authorize access to another request's/workflow's cached output.

## Tradeoffs

A compatible model must still choose the correct tool; mock protocol success
cannot prove any particular live model/template will do so. Large inventories
may consume another round within the unchanged budget. A six-chunk limit is
not a new per-chunk byte cap. Previous request references intentionally stop
working because no persistent Chat ownership identity exists at this boundary.

## Review Status

Not reviewed; independent review remains pending. Author tests cover large
native/tagged inventory retrieval, actual mocked native wire handling, session
isolation, retrieval bounds and standalone parity. No human visual/security
acceptance or AS-031 verification is asserted. The owner should retry only the
original first test after the patch is merged and installed locally.
