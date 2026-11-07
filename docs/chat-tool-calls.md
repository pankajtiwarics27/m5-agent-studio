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
