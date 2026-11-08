"""LC-05 wire fixtures only; not installed engine/parity/quality qualification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import httpx
import pytest

import ollama_protocol as op
from model_lifecycle_coordinator import ModelTarget
from test_shared_memory_admission import profile

TARGET = ModelTarget("ollama", "tiny:tag", "digest-b", "profile-b")
TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read exact text",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def native_profile(**changes):
    return replace(
        op.OllamaProfile(
            profile(TARGET),
            "llama",
            '{"num_ctx":8192,"num_predict":512,"temperature":0.2}',
            "high",
        ),
        **changes,
    )


def prepared(**kwargs):
    return op.prepare_chat(
        native_profile(),
        [{"role": "user", "content": "Do not trim: ಕರ್ನಾಟಕ 😀"}],
        **kwargs,
    )


def packet(**changes):
    value = dict(
        model=op.local_reference(TARGET.artifact_id),
        done=False,
        message={"role": "assistant", "content": ""},
    )
    value.update(changes)
    return value


def done(**changes):
    return packet(
        done=True,
        done_reason="stop",
        prompt_eval_count=100,
        eval_count=0,
        total_duration=2_000_000_000,
        prompt_eval_duration=1_000_000_000,
        eval_duration=0,
        **changes,
    )


def line(value):
    return op.encode(value) + b"\n"


def test_native_request_keeps_anchors_profile_tools_and_explicit_reasoning():
    p = prepared(tools=[TOOL])
    body = op.parse_json(p.body)
    assert body["messages"][0]["content"] == "Do not trim: ಕರ್ನಾಟಕ 😀"
    assert body["tools"] == [TOOL]
    assert body["options"] == op.parse_json(p.profile.options_json)
    assert body["think"] == "high"
    assert body["model"] == "tiny:tag:local" and body["runner"] == "llama"
    assert body["truncate"] is False and body["shift"] is False
    assert body["keep_alive"] == -1 and body["stream"] is True
    assert p.digest == hashlib.sha256(p.body).hexdigest()
    op.validate_prepared(p, p.profile)


@pytest.mark.parametrize("think", [None, False, True, "low", "medium", "high"])
def test_thinking_is_not_relaxed_or_replaced(think):
    p = native_profile(think=think)
    request = op.prepare_chat(p, [{"role": "user", "content": "x"}])
    body = op.parse_json(request.body)
    assert ("think" in body) == (think is not None)
    if think is not None:
        assert body["think"] == think and type(body["think"]) is type(think)
    load = p.body()
    assert load["options"] == body["options"]
    assert load.get("think") == body.get("think")


@pytest.mark.parametrize("think", ["max", "minimal", "", 1, [], {}])
def test_unknown_or_normalized_reasoning_not_silently_accepted(think):
    with pytest.raises(op.OllamaDenied):
        native_profile(think=think)


@pytest.mark.parametrize(
    "name",
    [
        "tiny",
        "tiny:cloud",
        "tiny:local",
        "tiny:tag-cloud",
        "tiny:a:local",
        "tiny:tag:cloud",
        "tiny:tag ",
        ":tag",
    ],
)
def test_remote_ambiguous_or_implicit_default_names_are_denied(name):
    with pytest.raises(op.OllamaDenied):
        op.local_reference(name)


@pytest.mark.parametrize(
    "options",
    [
        "{}",
        '{"num_ctx":4096,"num_predict":512}',
        '{"num_ctx":true,"num_predict":1}',
        '{"num_ctx":8192,"num_predict":-1}',
        '{"num_ctx":8192,"num_predict":0}',
        '{"num_ctx":8192,"num_predict":8193}',
        '{"num_ctx":8192,"num_predict":512,"temperature":NaN}',
        '{"num_ctx":8192,"num_ctx":4096,"num_predict":512}',
    ],
)
def test_context_output_and_json_unknowns_fail_before_transport(options):
    with pytest.raises(op.OllamaDenied):
        native_profile(options_json=options)


def history():
    return [
        dict(role="user", content="keep first anchor"),
        dict(
            role="assistant",
            content="",
            reasoning_content="original thought",
            tool_calls=[
                dict(
                    id="native-a",
                    type="function",
                    function=dict(name="read", arguments='{"path":"one"}'),
                ),
                dict(
                    id="native-b",
                    type="function",
                    function=dict(name="read", arguments='{"path":"two"}'),
                ),
            ],
        ),
        dict(role="tool", content="second result first", tool_call_id="native-b"),
        dict(
            role="tool",
            content="first result second",
            tool_call_id="native-a",
            name="read",
        ),
        dict(role="user", content="keep last anchor"),
    ]


def test_parallel_same_name_tool_result_identity_and_reasoning_roundtrip():
    p = op.prepare_chat(native_profile(), history(), tools=[TOOL])
    messages = op.parse_json(p.body)["messages"]
    assert messages[1]["thinking"] == "original thought"
    assert [c["id"] for c in messages[1]["tool_calls"]] == ["native-a", "native-b"]
    assert messages[1]["tool_calls"][1]["function"]["arguments"] == {"path": "two"}
    assert [
        (m["tool_call_id"], m["tool_name"], m["content"]) for m in messages[2:4]
    ] == [
        ("native-b", "read", "second result first"),
        ("native-a", "read", "first result second"),
    ]
    op.validate_prepared(p, p.profile)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "wrong_id",
        "wrong_name",
        "duplicate_id",
        "arguments_fragment",
        "duplicate_argument",
        "image",
        "extra_reasoning",
        "strict_schema",
        "root_keyword",
        "nested_keyword",
    ],
)
def test_unsupported_or_lossy_semantics_refused(mutation):
    messages, tools = history(), json.loads(json.dumps([TOOL]))
    if mutation == "missing":
        messages.pop(2)
    elif mutation == "wrong_id":
        messages[2]["tool_call_id"] = "other"
    elif mutation == "wrong_name":
        messages[2]["name"] = "other"
    elif mutation == "duplicate_id":
        messages[1]["tool_calls"][1]["id"] = "native-a"
    elif mutation == "arguments_fragment":
        messages[1]["tool_calls"][0]["function"]["arguments"] = '{"path":'
    elif mutation == "duplicate_argument":
        messages[1]["tool_calls"][0]["function"]["arguments"] = (
            '{"path":"one","path":"two"}'
        )
    elif mutation == "image":
        messages[0]["content"] = [{"type": "image_url", "image_url": {"url": "x"}}]
    elif mutation == "extra_reasoning":
        messages[0]["reasoning_content"] = "must not disappear"
    elif mutation == "strict_schema":
        tools[0]["function"]["strict"] = True
    elif mutation == "root_keyword":
        tools[0]["function"]["parameters"]["additionalProperties"] = False
    else:
        tools[0]["function"]["parameters"]["properties"]["path"]["minLength"] = 1
    with pytest.raises(op.OllamaDenied):
        op.prepare_chat(native_profile(), messages, tools=tools)


def test_preparation_owns_bytes_not_mutable_input_and_refuses_forged_flags():
    messages = history()
    tools = json.loads(json.dumps([TOOL]))
    p = op.prepare_chat(native_profile(), messages, tools=tools)
    messages[0]["content"] = "changed"
    tools[0]["function"]["name"] = "changed"
    op.validate_prepared(p, p.profile)
    body = op.parse_json(p.body)
    body["truncate"] = True
    raw = op.encode(body)
    with pytest.raises(op.OllamaDenied):
        op.validate_prepared(
            replace(p, body=raw, digest=hashlib.sha256(raw).hexdigest()), p.profile
        )


def test_ndjson_utf8_arbitrary_boundaries_zero_usage_and_unknown_not_fabricated():
    p = prepared()
    data = line(
        packet(message=dict(role="assistant", thinking="ಆಲೋಚನೆ", content="😀"))
    ) + op.encode(done())
    decoder = op.NativeChatDecoder(p)
    events = []
    for byte in data:
        events.extend(decoder.feed(bytes([byte])))
    events.extend(decoder.finish())
    assert events[:2] == [
        dict(type="reasoning", text="ಆಲೋಚನೆ"),
        dict(type="content", text="😀"),
    ]
    usage = events[-1]
    assert usage["completion_tokens"] == 0 and usage["prompt_tokens"] == 100
    assert (
        usage["reasoning_tokens"] is None
        and usage["cached_tokens"] is None
        and usage["ttft"] is None
    )
    assert usage["prefill_tps"] == 100 and usage["decode_tps"] is None
    assert usage["elapsed_s"] == 2 and usage["finish_reason"] == "stop"


def tool_packet(call_id="native-a", **function):
    fn = dict(name="read", arguments={"path": "😀"}, index=0)
    fn.update(function)
    return packet(
        message=dict(
            role="assistant", content="", tool_calls=[dict(id=call_id, function=fn)]
        )
    )


def test_native_complete_objects_not_openai_fragments_and_ids_not_glued_by_index():
    decoder = op.NativeChatDecoder(prepared(tools=[TOOL]))
    assert (
        decoder.feed(line(tool_packet("native-a")) + line(tool_packet("native-b")))
        == []
    )
    assert decoder.feed(line(done())) == []
    events = decoder.finish()
    assert [event["id"] for event in events[:-1]] == ["native-a", "native-b"]
    assert [event["name"] for event in events[:-1]] == ["read", "read"]
    assert events[0]["arguments"] == {"path": "😀"} and events[1]["index"] == 0
    assert (
        events[-1]["finish_reason"] == "tool_calls"
        and events[-1]["native_finish_reason"] == "stop"
    )


@pytest.mark.parametrize(
    "bad",
    [
        b"data: [DONE]\n",
        b"not JSON\n",
        b'{"model":"x","model":"y"}\n',
        b'{"x":"\xff"}\n',
        line(packet(error="secret body")),
        line(packet(remote_host="cloud")),
        line(packet(model="wrong:tag:local")),
        line(packet(done="false")),
        line(packet(message={"role": "tool", "content": "x"})),
        line(packet(message={"role": "assistant", "images": ["x"]})),
        line(tool_packet("", arguments={})),
        line(tool_packet(arguments='{"path":')),
        line(tool_packet(name="unadvertised")),
        line(tool_packet(index=True)),
        line(tool_packet()) + line(tool_packet()),
        line(done()) + line(packet()),
        line(
            packet(done=True, done_reason="unknown", prompt_eval_count=1, eval_count=1)
        ),
        line(
            packet(done=True, done_reason="stop", prompt_eval_count=True, eval_count=1)
        ),
        line(packet(done=True, done_reason="stop", prompt_eval_count=1, eval_count=-1)),
    ],
)
def test_stream_errors_never_emit_usable_tool_or_usage_events(bad):
    decoder = op.NativeChatDecoder(prepared(tools=[TOOL]))
    with pytest.raises(op.OllamaDenied):
        decoder.feed(bad)
        decoder.finish()


def test_truncated_stream_and_truncated_tool_generation_are_not_success():
    decoder = op.NativeChatDecoder(prepared(tools=[TOOL]))
    decoder.feed(line(tool_packet()))
    with pytest.raises(op.OllamaDenied, match="incomplete"):
        decoder.finish()
    decoder = op.NativeChatDecoder(prepared(tools=[TOOL]))
    decoder.feed(line(tool_packet()))
    with pytest.raises(op.OllamaDenied, match="incomplete"):
        decoder.feed(
            line(
                packet(
                    done=True, done_reason="length", prompt_eval_count=1, eval_count=2
                )
            )
        )
    decoder = op.NativeChatDecoder(prepared())
    decoder.feed(
        line(packet(done=True, done_reason="length", prompt_eval_count=1, eval_count=2))
    )
    assert decoder.finish()[-1]["finish_reason"] == "length"


def test_stream_line_and_total_size_bounds_are_errors_not_truncation(monkeypatch):
    monkeypatch.setattr(op, "MAX_LINE_BYTES", 8)
    with pytest.raises(op.OllamaDenied, match="line_too_large"):
        op.NativeChatDecoder(prepared()).feed(b"123456789")
    monkeypatch.setattr(op, "MAX_STREAM_BYTES", 8)
    with pytest.raises(op.OllamaDenied, match="stream_too_large"):
        op.NativeChatDecoder(prepared()).feed(b"123456789")


def test_current_compatible_wire_is_unchanged_and_not_native_parity(monkeypatch):
    import app as studio

    requests = []

    async def allow(*args, **kwargs):
        pass

    def handler(request):
        requests.append(request)
        data = {
            "choices": [{"delta": {"content": "READY"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1},
        }
        return httpx.Response(
            200, content=("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode()
        )

    monkeypatch.setattr(studio, "_validate_outbound_url", allow)

    async def run():
        provider = studio.ollama_virtual_provider()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                event
                async for event in studio._stream_cloud_openai(
                    client,
                    provider,
                    TARGET.artifact_id,
                    [{"role": "user", "content": "READY"}],
                    0.2,
                    512,
                )
            ]

    events = asyncio.run(run())
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert body["max_tokens"] == 512 and body["temperature"] == 0.2
    assert (
        "options" not in body
        and "truncate" not in body
        and "shift" not in body
        and "think" not in body
    )
    native = op.parse_json(prepared().body)
    assert native["options"]["num_ctx"] == 8192 and native["think"] == "high"
    assert native["truncate"] is False and native["shift"] is False
    assert events[0] == {"type": "content", "text": "READY"}


def test_overflowing_json_float_and_nonstring_keys_not_silently_coerced():
    with pytest.raises(op.OllamaDenied):
        op.parse_json('{"x":1e10000}')
    with pytest.raises(op.OllamaDenied):
        op.encode({"nested": {1: "not a JSON string key"}})


def test_native_thinking_false_is_not_silently_ignored():
    p = op.prepare_chat(native_profile(think=False), [dict(role="user", content="x")])
    decoder = op.NativeChatDecoder(p)
    with pytest.raises(op.OllamaDenied, match="thinking_profile_mismatch"):
        decoder.feed(
            line(
                packet(
                    message=dict(role="assistant", thinking="unexpected", content="")
                )
            )
        )


def test_terminal_events_cannot_be_emitted_twice():
    decoder = op.NativeChatDecoder(prepared())
    decoder.feed(line(done()))
    decoder.finish()
    with pytest.raises(op.OllamaDenied, match="already_finished"):
        decoder.finish()
    with pytest.raises(op.OllamaDenied, match="already_finished"):
        decoder.feed(b"\n")


@pytest.mark.parametrize(
    "extra",
    [
        {"unknown_ignored_upstream": True},
        {"temperature": None},
        {"top_k": 0.5},
        {"num_gpu": True},
        {"temperature": "0.2"},
        {"stop": "x"},
        {"use_mmap": 1},
        {"temperature": 1e100},
    ],
)
def test_options_that_upstream_would_ignore_coerce_or_overflow_are_refused(extra):
    options = dict(num_ctx=8192, num_predict=512)
    options.update(extra)
    with pytest.raises(op.OllamaDenied):
        native_profile(options_json=json.dumps(options))
