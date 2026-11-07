# Chat tool calls and MCP inventory

## What Chat can actually run

With **Read-only tools** enabled, Chat can inspect the selected workspace,
CodeGraph and saved OpenContext notes. It can also call `mcp_list_servers` to
inspect locally cached MCP configuration. This inventory includes enabled,
disabled and unsynced servers, transport and approval policy, discovered tool
names, enabled-tool counts and whether a sync error was recorded.

Inventory does **not** connect to an MCP server, launch a process, sync tools,
read credentials, or test availability. A configured server with cached tools
is not necessarily reachable. Inventory deliberately excludes endpoints,
commands, arguments, environment, headers, credentials, tool descriptions and
schemas, and raw sync errors. Review detailed errors in Integrations instead.
All model-visible inventory is untrusted data, not instructions.

Chat retains read-only `plan` policy. **External MCP execution, file edits and
shell execution are not available in Chat**, even if the server advertises a
read-only annotation. Use the Agent Harness with its existing approval policy
for execution. Turning Chat tools on does not grant those permissions.

## Choosing a protocol

The **Tool protocol** selector has two modes:

- **Native (OpenAI-compatible)** sends function schemas to the OpenAI-compatible
  streaming adapter, including Ollama, and the local oMLX-compatible adapter.
  Streamed `delta.tool_calls` are assembled by call index and validated before
  entering the existing audited tool boundary. Only the advertised read tools
  and inventory are offered; external MCP functions are not advertised here.
- **Tagged text (compatibility)** omits native schemas and preserves the
  existing tagged-content interface for servers/templates that reject native
  tools. JSON `<tool_call>` and Qwen function tags remain supported. A closed
  `<|tool_call>call:Tool{name: "mcp_list_servers", arguments: {}}<tool_call|>`
  content frame is also recognized; arguments must be a JSON object. This is
  parsing, not evaluation of model-generated code.

Anthropic's native Messages adapter continues to use the tagged-content
interface; this change does not add Anthropic `tool_use` handling. A server or
model without working native support may reject the native request. Choose
**Tagged text** and retry deliberately; Studio does not silently make an extra
inference request or switch providers. Neither setting can force a model with
an incompatible tool template to produce a valid call.

`POST /api/chat/stream` accepts optional `tool_protocol: "native" | "tagged"`
(default `"native"`). Invalid values return HTTP 400. `tools_enabled: false`
suppresses native schemas and prevents execution in either protocol. Other
workflows and ordinary model requests do not inherit Chat's schemas. The
selected protocol is captured with the turn's request context.

## Large inventories and compressed tool output

A successful inventory call can exceed the 2400-character Context-Mode threshold.
Its model-visible response is then a compact JSON-shape digest plus a `ctx-…`
vault reference, not the full list. Chat advertises the already-registered
read-only `ctx_search` tool in native schemas and the tagged-text guide so the
model can retrieve the actual cached metadata before summarizing it.

Use `ctx_search` with the returned `doc_id`; omit `query` or use an empty string
to retrieve the first chunks, or supply a specific search term. At most six
chunks are returned. This is not a new byte limit per chunk. `ctx-…` IDs are
**not** OpenContext `oc-…` note IDs: `oc_read`/`oc_search` cannot retrieve them.
Do not guess config-file paths or use workspace searches to reconstruct inventory.
The result remains untrusted data, and cached retrieval does not resolve
credentials, connect/sync MCP servers or grant execution authority.

All retrieval branches scope by the existing session ID, including doc-ID-only
and doc-ID-plus-query reads. For Direct Chat this is the current request's
pseudo-session, not a persistent conversation identity: a doc reference from a
previous turn/request or another workflow does not grant access. Missing,
expired/deleted or out-of-scope IDs return no matching chunks. The existing
Chat step budget, tools-off behavior and external-MCP/shell denials are unchanged.

## When a reply contains a tool request but nothing runs

Text in the **Reasoning** disclosure is private planning, not executable
instructions. Studio never parses that channel into tool authority. A
reasoning-only reply containing tool-call markers now ends in a visible error,
not a misleading Complete state. Check the model's tool capability/template,
select a suitable protocol, or choose a tool-capable model and retry. This
behavior also applies when reasoning was requested off but the provider still
sent a reasoning channel.

For an inventory test, ask: **Call mcp_list_servers and summarize the configured
servers, distinguishing disabled and unsynced entries. Do not test connections.**
A successful turn shows a `mcp_list_servers` tool row, its result status and
output, followed by the model's summary. A tool name printed in prose or
reasoning is not evidence of execution.

Native calls must finish explicitly, have object arguments, and fit the
bounded stream accumulator (16 indexes; 65,536 characters per name/argument
buffer). Malformed or truncated native calls produce an error without
executing a partially assembled frame. The existing Chat budget permits five
tool-executing rounds and a sixth answer round, with at most six calls per
round. A further tool request at the last round now reports the limit instead
of claiming completion.

## Verification and limits

`tests/test_chat_tool_protocol.py` uses temporary databases, fake streams and
HTTP mock transports. It covers a complete mocked Ollama native-call to Chat
inventory to follow-up-answer round trip, legacy content frames, tools-off and
tagged modes, reasoning-only refusal, malformed/truncated native requests,
parallel call indexes and request-scoped schemas, credential projection,
workspace/secret guards, authentication/origin checks, and unchanged external
MCP/shell/unknown-tool denial.

These are protocol and policy regressions, not a live validation of a
particular GGUF, inference server or MCP installation. No test calls a live
model, cloud provider or MCP service. A workspace folder picker and
user-approved path suggestions are a separate follow-up, not implemented by
this tool-call fix.


### Local-test regression follow-up (2026-11-07)

The owner's first local inventory test executed `mcp_list_servers`, but its
large eight-server result became a shape-only context-vault reference. Chat
omitted `ctx_search` from both native schemas and its tagged guide; the model
instead attempted OpenContext/file searches and exhausted the turn without a
summary. The small-inventory tests had not exercised this threshold.

New regressions reproduce the actual compression path with eight configured
servers and retrieve all aliases through `ctx_search` in native/tagged modes,
including a mocked Ollama native SSE round trip and isolated generated-standalone
parity. Positive/negative tests cover session scoping and bounded limits. No live
model/MCP endpoint is used, and passing mocks do not certify the owner's GGUF.
Retry the same first prompt only after the fix is available locally; do not
advance to the next manual test yet. See ADR-0029 for the retrieval boundary.

Author checkpoint for [issue231](https://github.com/pankajtiwarics27/m5-agent-studio/issues/231):
seven new regressions failed before the patch; 93 focused protocol/privacy/
execution/admission/session-trust tests now pass, including generated-standalone
retrieval and mocked native SSE. Ruff, mypy62, ADR/docs checks pass; regenerated
inventories show37/37 checks,7/7 phase gates,0findings. Complete multi-version
and browser CI plus the owner's same-model manual retry remain required. No live
endpoint validation or new human/security acceptance is asserted.

CI follow-up: the first full matrix found one stale post-roadmap Pipeline
inventory backend SHA-256 (not part of the main37-generator list). Regenerated
it with `uiux_pipeline_workspace_inventory.py`; no runtime code or checks were
weakened. Pipeline/Start/protocol rerun:105passed. Updated full CI is required
before merging this fix.
