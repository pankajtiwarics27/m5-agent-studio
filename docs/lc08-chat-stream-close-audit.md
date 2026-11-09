# LC08 chat-stream consumer-close audit — bounded follow-up

## Scope and coverage accounting

This increment covers consumer cancellation in two existing streaming response
wrappers: `api_chat_stream.event_gen` and `openai_gateway_chat.sse_gen`. Their
model-source iterators are explicitly closed when the response consumer stops
early. Tests inject a synthetic async generator; no model, provider, HTTP
network, daemon, or hardware is used. This does not test all route semantics or
all steps of the chat/tool workflow.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. Sixteen owners have bounded evidence;
**13 remain inventory-only**. `stream_any_model` remains inventory-only: these
checks cover two wrappers' cancellation handoff, not every dispatch mode or
workflow that consumes it. No runtime activation, hardware qualification,
release acceptance, test8, or LC09–11 status advances.

## Reproduced failure and bounded behavior

The unchanged-source tests failed for both wrappers: after receiving one mock
content event, closing the response iterator left the delegated model-source
generator open. The wrappers used `async for` without owning the iterator's
close operation. The API-chat stream now wraps each step's model source in
`contextlib.aclosing`. The OpenAI-compatible SSE wrapper does so in both its
ordinary and tool-buffering streaming branches.

The behavior change is limited to cancellation cleanup. It does not change
output framing, tool parsing, model selection, completion/error handling, or
claims about work cancellation by any external provider.

## Executable evidence and limits

[`test_workflow_chat_stream_cancel.py`](../tests/test_workflow_chat_stream_cancel.py)
uses a blocked synthetic model-source iterator. It checks API chat, the ordinary
OpenAI-compatible SSE path, and its tool-buffering path: one event is consumed,
the response iterator is closed, and the model source's `finally` is observed
before close returns. These are in-process tests and make no outbound request.

The unchanged-source baseline was **2 failed** for the ordinary API-chat and
OpenAI-compatible paths. The tool-buffering branch was then added as additional
post-fix coverage; all **3 cases pass**. The test does not qualify
multi-step tool execution, disconnect behavior under a live ASGI server,
coordinated/native runtime behavior, or the remaining inventory-only owners.
