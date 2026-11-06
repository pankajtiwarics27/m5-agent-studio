---
id: ADR-0026
title: moneyprinter-turbo-mcp-stdio-server
status: accepted
date: 2026-11-07
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0026: MoneyPrinterTurbo MCP stdio server (cross-harness entry point)

## Context

ADR-0025 integrated MoneyPrinterTurbo into this repository as a typed Studio
tool contract (`moneyprinter_tool.py`) and a Council provider profile
(`MoneyPrinterAPI`). Its alternatives section recorded an "MCP server wrapper
instead of a native contract" as **rejected for now**, explicitly leaving the
door open to layer one on later without changing the contract.

Owner follow-up (2026-11-07): "Integrate this as well for MoneyPrinterTurbo"
with a link to `deepseek-ai/deepseek-harness`. DeepSeek Harness (MIT,
actively maintained, default branch `master`) is an agent harness whose
sanctioned seam for external tools is MCP: `@deepseek-ai/dsh-mcp-client`
launches stdio servers and the shipped MCP manager mounts them into
profiles. MoneyPrinterTurbo itself ships no MCP server (verified against
its upstream tree), and the Part 2 `MoneyPrinterAPI` profile does not
solve invocation — it solves Council routing, and only behind an
OpenAI-compatible shim.

## Decision

1. **Add `moneyprinter_mcp.py`**, a standalone stdio MCP server (newline-delimited
   JSON-RPC 2.0) exposing exactly one tool, `moneyprinter_video_generate`,
   delegating execution to `moneyprinter_tool.run_moneyprinter_video` under
   the identical contract (schema validation, ffmpeg preflight, argv exec,
   caps, timeout, lineage). It is a separate, optional entry point:
   `moneyprinter_tool.py`, the orchestration loop, `app.py`, and
   `studio.py` are unchanged.
2. **Trust boundaries stay deny-by-default.** No sockets, no HTTP; stdout
   carries protocol frames only (diagnostics on stderr); 4 MiB cap per input
   line; one generation at a time (a concurrent call fails fast with an
   `isError` `busy` result); a `tools/call` without an `id` (notification)
   is dropped without executing; tool-level failures are reported as
   `isError` results, and JSON-RPC errors are reserved for protocol faults
   (parse error, unknown method, unknown tool, non-object `arguments`).
3. **Configuration is environment-driven and fail-closed.** `MONEYPRINTER_TURBO_DIR`
   (required; whitespace treated as unset), optional `MONEYPRINTER_FFMPEG`,
   `MONEYPRINTER_TIMEOUT_SECONDS` (default 1800, clamped to ≤ 3600, malformed
   values fall back), `MONEYPRINTER_LINEAGE_ROOT`. No credential material is
   read, inherited, or logged; the child still receives only the minimal
   allowlist from Part 1.
4. **No new runtime dependencies**; the server uses the standard library plus
   `moneyprinter_tool`. Protocol revision advertised: `2025-06-18`
   (`initialize`, `ping`, `tools/list`, `tools/call`;
   `notifications/initialized` / `notifications/cancelled` accepted and
   ignored). Cancellation of an in-flight render is explicitly out of scope;
   the client's own tool timeout governs.
5. **Documentation carries the registrations** for DeepSeek Harness
   (`@deepseek-ai/dsh-mcp-client` with an explicit `toolCallTimeoutMs`
   above render duration, default is 60 s) and for M5 Agent Studio's MCP
   settings. The doc states plainly that this path needs **no**
   OpenAI-compatible shim, unlike Council `MoneyPrinterAPI` traffic.

## Alternatives

- **Keep the native contract only (ADR-0025 as written).** Rejected now:
  DeepSeek Harness cannot call in-process Python contracts; without an MCP
  seam the MoneyPrinterTurbo capability would remain Studio-internal.
- **Author a first-party DeepSeek Harness plugin** (`@deepseek-ai/dsh-moneyprinter`,
  Cordis/TypeScript). Rejected for this change: it belongs to the
  deepseek-harness tree (or a fork), cannot be built or tested against the
  real harness here, and would ship unverified code. The MCP seam is the
  documented, supported extension point and keeps this repository self-contained.
- **Ship a new admission-boundary built-in inside the Studio instead.**
  Rejected: same boundary-widening concerns as ADR-0025; an MCP client
  launched by user configuration passes through the existing MCP admission
  policy unchanged.
- **Streamable HTTP transport instead of stdio.** Rejected: adds a listening
  port and auth questions for a single-user local tool; stdio is
  sufficient and keeps the attack surface at "can the user already run
  processes".

## Tradeoffs

- **One process per generation request is avoided but not parallelism:**
  the server is long-lived (harness holds it) yet serializes generations —
  correct for unified-memory hygiene, and a known limit for batch media
  work.
- **No progress/streaming notifications** and no cancellation; long renders
  look opaque to the harness. `toolCallTimeoutMs` must be raised by the
  user or the harness aborts healthy renders.
- **Protocol support is minimal by design** (four methods); servers that
  need resources, prompts, or elicitation can be added later under a new
  decision.
- **No independent review yet.** Same posture as ADR-0025: this entry point
  must not be treated as authorization-widening; a harness launching this
  server already opted into running it with the user's environment.

## Review Status

Not reviewed. Independent review required before unattended or
pipeline-wide execution through this server is enabled.
