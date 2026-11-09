# LC08 chat-stream consumer-close audit — bounded follow-up

## Scope and coverage accounting

This increment covers consumer cancellation across three chat streaming
surfaces: `api_chat_stream.event_gen`, the `openai_gateway_chat` route and its
`sse_gen`, and `_coordinated_gateway_chat.events`. The ordinary API chat and
OpenAI SSE wrappers now own their delegated model sources with
`contextlib.aclosing`; the coordinated event path already did so and is checked
as a bounded passing control. Tests inject a synthetic async generator; no
model, provider, HTTP network, daemon, or hardware is used. This does not test
all route semantics or all steps of the chat/tool workflow.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. Eighteen owners have bounded
evidence; **11 remain inventory-only**. `stream_any_model` remains
inventory-only: these checks cover three chat streaming surfaces' cancellation
handoff, not every dispatch mode or workflow that consumes it. No runtime
activation, hardware qualification, release acceptance, test8, or LC09–11
status advances.

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
uses a blocked synthetic model-source iterator. It checks API chat, the
OpenAI-compatible route's ordinary and tool-buffering SSE branches, and the
coordinated gateway events path: one event is consumed, the response iterator
is closed, and the model source's `finally` is observed before close returns.
These are in-process tests and make no outbound request.

The unchanged-source baseline was **2 failed** for the ordinary API-chat and
OpenAI-compatible paths. The tool-buffering and already-owned coordinated
branch were added as bounded follow-up cases; all **4 cases pass**. The test
does not qualify
multi-step tool execution, disconnect behavior under a live ASGI server,
coordinated/native runtime behavior, or the remaining inventory-only owners.
