---
id: ADR-0027
title: direct-chat-tool-protocol-and-local-mcp-inventory
status: accepted
date: 2026-11-07
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0027: Explicit Chat tool protocols and local MCP inventory

## Context

An owner-supplied Ollama log contained a request for `mcp_list_servers` inside
reasoning, but no actual execution. That name was unregistered. Direct Chat
advertised active external MCP functions even though its plan policy denies
all external MCP execution. OpenAI-compatible streaming did not send function
schemas or consume native function-call deltas. Reasoning and content were
correctly separate; executing private planning to accommodate the log would
create the wrong authority boundary.

## Decision

Add a local, managed-read `mcp_list_servers` tool to the existing registry,
classifier and session-read capability descriptor. Query a minimal allowlisted
SQLite projection; do not reuse the MCP UI inventory helper that resolves
credentials. Return public configuration metadata and cached tool names,
counts and status flags, not endpoints, arguments, credentials, descriptions,
schemas or raw error strings. Do not connect, sync, launch or probe a server.

Offer native schemas only inside the request-scoped Chat tool stream, and only
for Chat's advertised read functions. Consume OpenAI-compatible indexed native
function deltas in the shared OpenAI adapter and local oMLX adapter; validate
complete object arguments and explicit stream completion before conversion to
the existing audited content-call path. Escape delimiters in the conversion so
an argument string cannot inject another tagged call. Bound call indexes and
fragment sizes. Keep authorization, admission, secret/path guards, audit and
untrusted-result framing in the existing executor, rather than adding a
second execution mechanism.

Provide an explicit tagged-content compatibility selector and optional
`tool_protocol` request enum on the existing Chat route. The default is native;
operators with text-only templates can deliberately choose tagged mode.
Anthropic Messages remains tagged-content, not native `tool_use`. No retry or
provider switch is added. Schema context is reset in a finally block and does
not affect unrelated inference, workflow or model-list requests. Closed
Gemma-style content frames are supported with JSON object arguments, never
`eval` or arbitrary code interpretation.

Do not advertise external MCP execution in Chat. Retain plan's categorical
external-MCP denial, including servers claiming read-only annotations. Correct
the composer copy, and expose reasoning-only tool requests and exhausted tool
rounds as failures, not successful completion. No new route, dependency,
configuration environment variable, credential scope or automatic network
operation is introduced.

## Alternatives

- Parsing reasoning as calls was rejected: planning text is not executable
  authority and cannot authorize filesystem or connector effects.
- Allowing all externally annotated read tools in Chat was rejected: server
  annotations are not sufficient authorization for arbitrary remote effects.
- Hiding a failed native request behind an automatic inference retry was
  rejected: compatibility must not add implicit network or billable requests.
- Returning the full MCP UI payload was rejected: resolving credentials and
  exposing launch/endpoint configuration is unnecessary for model inventory.
- Bundling a new filesystem browsing API was rejected for this scoped fix. The
  owner-requested workspace picker, folder creation and editable, approved path
  suggestions need separate authenticated path-boundary design and tests.

## Tradeoffs

Native function schemas increase prompt size and require a compatible model
and server template. Tagged mode preserves a deliberate escape hatch but does
not make an arbitrary model tool-capable. Native calls are buffered until an
explicit terminal finish; malformed/truncated streams fail closed. Native
Anthropic tool use and native tools in non-Chat workflows are not added.
Inventory reports cached configuration, not liveness, and recommendations
based on it must acknowledge that limitation.

Focused security delta: one registered metadata-only managed read with no
filesystem, process or network capability; no existing execution class is
downgraded. Positive and negative tests exercise the real Chat executor for
inventory, deny external MCP/shell/unregistered operations, retain secret and
workspace-escape guards, and check auth/origin refusal. Mock transports test
native assembly, argument-delimiter isolation, truncation and request-scoped
schemas. The independent security review and live model validation remain
outstanding; automated evidence is not a human sign-off.

## Review Status

Not reviewed. This record documents the implementation decision and its
focused security delta, not an independent accepted security review. Review
metadata remains unsigned; the existing security release gate is unchanged.
