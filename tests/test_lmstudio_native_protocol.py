"""Pinned-wire fake cases, never installed LM Studio qualification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

import lmstudio_protocol as p
from model_lifecycle_coordinator import ModelTarget
from test_shared_memory_admission import profile

TARGET = ModelTarget("lmstudio", "publisher/model", "digest-c", "profile-c")
INSTANCE = "native-" + TARGET.artifact_id
CONFIG = dict(
    context_length=8192,
    eval_batch_size=512,
    flash_attention=True,
    offload_kv_cache_to_gpu=True,
    parallel=4,
)


def native_profile(**changes):
    return replace(
        p.LmStudioProfile(profile(TARGET), "gguf", json.dumps(CONFIG)), **changes
    )


def model(**changes):
    row = dict(
        key=TARGET.artifact_id,
        type="llm",
        format="gguf",
        size_bytes=500,
        max_context_length=131072,
        loaded_instances=[dict(id=INSTANCE, config=CONFIG.copy())],
    )
    row.update(changes)
    return row


def request(**changes):
    body = dict(
        model=INSTANCE,
        stream=True,
        messages=[
            dict(role="system", content="keep first anchor"),
            dict(role="user", content="ಕರ್ನಾಟಕ 😀"),
        ],
        max_tokens=512,
        temperature=0.2,
        stream_options={"include_usage": True},
    )
    body.update(changes)
    return body


def packet(**changes):
    row = dict(
        model=INSTANCE,
        choices=[dict(index=0, delta={"content": "READY"}, finish_reason=None)],
    )
    row.update(changes)
    return row


def sse(value):
    return b"data: " + p.encode(value) + b"\n\n"


def final():
    return (
        sse(packet(choices=[dict(index=0, delta={}, finish_reason="stop")]))
        + b"data: [DONE]\n\n"
    )


def test_load_config_distinguishes_unsettable_parallel_and_final_echo():
    q = native_profile()
    body = p.decode(q.load_body())
    assert body == dict(
        model=TARGET.artifact_id,
        echo_load_config=True,
        **{k: v for k, v in CONFIG.items() if k != "parallel"},
    )
    assert "parallel" not in body and q.memory.context_tokens == 8192
    assert "parallel" in p.decode(q.config_json)
    assert (
        q.digest
        == hashlib.sha256(p.encode(__import__("dataclasses").asdict(q))).hexdigest()
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(context_length=4096),
        dict(context_length=True),
        dict(flash_attention=1),
        dict(parallel=0),
        dict(num_experts=1.5),
        dict(unknown_option=True),
    ],
)
def test_unapproved_unknown_or_coerced_load_config_refused(change):
    values = dict(CONFIG, **change)
    with pytest.raises(p.LmStudioDenied):
        native_profile(config_json=json.dumps(values))


def test_catalog_retains_multiple_manual_and_helper_instances_without_promoting_size_or_max_context():
    row = model()
    row["loaded_instances"].append(
        dict(id="manual-second", config={"context_length": 4096})
    )
    helper = model(
        key="embedding-helper",
        type="embedding",
        loaded_instances=[dict(id="embedding-live", config={"context_length": 1024})],
    )
    catalog = p.parse_catalog({"models": [row, helper]})
    assert catalog.keys == (TARGET.artifact_id, "embedding-helper")
    assert [x.instance_id for x in catalog.instances] == [
        INSTANCE,
        "manual-second",
        "embedding-live",
    ]
    assert p.decode(catalog.instances[0].config_json)["context_length"] == 8192
    assert catalog.instances[-1].model_type == "embedding"
    assert p.parse_catalog({"models": [model(loaded_instances=[])]}).instances == ()


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        [model(loaded_instances=None)],
        [model(loaded_instances=[{"id": INSTANCE}])],
        [model(), model(key="other")],
        [model(), model()],
        [model(type="unknown")],
        [model(size_bytes=True)],
        [model(loaded_instances=[dict(id="", config=CONFIG)])],
        [model(loaded_instances=[dict(id=INSTANCE, config={"context_length": None})])],
        [model(variants=["variant-one"])],
        [model(variants=["variant-one"], selected_variant="wrong")],
    ],
)
def test_invalid_ambiguous_or_incomplete_list_is_not_empty_success(rows):
    with pytest.raises(p.LmStudioDenied):
        p.parse_catalog({"models": rows})


def test_variant_and_format_are_not_aliases():
    row = model(variants=["variant-one", "variant-two"], selected_variant="variant-one")
    catalog = p.parse_catalog({"models": [row]})
    assert catalog.artifacts[0].selected_variant == "variant-one"
    assert (
        native_profile(selected_variant="variant-one").digest
        != native_profile(selected_variant="variant-two").digest
    )


def test_inference_owns_bytes_and_keeps_tool_result_ids_schema_and_reasoning_unchanged():
    body = request()
    body["messages"] += [
        dict(
            role="assistant",
            content=None,
            reasoning_content="keep thought",
            tool_calls=[
                dict(
                    id="original-id",
                    type="function",
                    function={"name": "read", "arguments": '{"path":"file"}'},
                )
            ],
        ),
        dict(role="tool", content="keep result", tool_call_id="original-id"),
    ]
    body["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
            },
        }
    ]
    prepared = p.prepare_inference(INSTANCE, native_profile(), body)
    expected = p.encode(body)
    body["messages"][0]["content"] = "changed"
    assert prepared.body == expected
    assert p.decode(prepared.body)["messages"][-1]["tool_call_id"] == "original-id"
    assert prepared.digest == hashlib.sha256(expected).hexdigest()
    # This is a structural owned-payload contract, NOT proof that a real server
    # honors every reasoning/tool/schema field. Installed request_semantics gates it.


@pytest.mark.parametrize(
    "change",
    [
        dict(model=TARGET.artifact_id),
        dict(stream=False),
        dict(max_tokens=-1),
        dict(max_tokens=8193),
        dict(messages=[]),
        dict(messages=[dict(role="user", content=[{"type": "image_url"}])]),
        dict(ttl=1),
        dict(context_length=9000),
        dict(integrations=["server-managed-mcp"]),
        dict(previous_response_id="resp_history"),
        dict(reasoning_effort="max"),
    ],
)
def test_no_jit_artifact_fallback_context_override_mcp_history_or_media_flattening(
    change,
):
    with pytest.raises(p.LmStudioDenied):
        p.prepare_inference(INSTANCE, native_profile(), request(**change))


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e10000}', b"\xff", b"{"]
)
def test_duplicate_nonfinite_and_invalid_json_refused(raw):
    with pytest.raises(p.LmStudioDenied):
        p.decode(raw)


def test_sse_preserves_exact_json_unicode_data_and_withholds_terminal_packets():
    decoder = p.LmSseDecoder(INSTANCE)
    raw = p.encode(
        packet(
            choices=[
                dict(
                    index=0,
                    delta={"reasoning_content": "ಆಲೋಚನೆ 😀", "content": "x"},
                    finish_reason=None,
                )
            ]
        )
    )
    events = []
    for byte in b": keepalive\r\n\r\ndata: " + raw + b"\r\n\r\n" + final():
        events.extend(decoder.feed(bytes([byte])))
    assert events == [raw]
    terminal = decoder.finish()
    assert (
        len(terminal) == 1
        and p.decode(terminal[0])["choices"][0]["finish_reason"] == "stop"
    )
    with pytest.raises(p.LmStudioDenied):
        decoder.finish()


def test_sse_tool_fragments_ids_and_relative_packet_order_are_not_rewritten():
    decoder = p.LmSseDecoder(INSTANCE)
    first = packet(
        choices=[
            dict(
                index=0,
                delta={
                    "tool_calls": [
                        dict(
                            index=0,
                            id="native-tool-id",
                            type="function",
                            function={"name": "read", "arguments": '{"path":'},
                        )
                    ]
                },
                finish_reason=None,
            )
        ]
    )
    second = packet(
        choices=[
            dict(
                index=0,
                delta={"tool_calls": [dict(index=0, function={"arguments": '"one"}'})]},
                finish_reason=None,
            )
        ]
    )
    end = packet(choices=[dict(index=0, delta={}, finish_reason="tool_calls")])
    assert decoder.feed(sse(first) + sse(second) + sse(end) + b"data: [DONE]\n\n") == []
    assert decoder.finish() == tuple(p.encode(x) for x in [first, second, end])


@pytest.mark.parametrize(
    "data",
    [
        b"data: [DONE]\n\n",
        sse({"error": "secret"}),
        sse(packet(model="wrong-instance")),
        sse(packet(choices=None)),
        sse(packet(choices=[dict(index=True, delta={}, finish_reason=None)])),
        sse(packet(choices=[dict(index=1, delta={}, finish_reason=None)])),
        sse(packet(choices=[dict(index=0, delta={}, finish_reason="unknown")])),
        final() + sse(packet()),
        b"event: native-load\n\n",
        b'data: {"x":NaN}\n\n',
        sse(packet()),
    ],
)
def test_wrong_instance_error_malformed_and_incomplete_sse_never_complete(data):
    decoder = p.LmSseDecoder(INSTANCE)
    with pytest.raises(p.LmStudioDenied):
        decoder.feed(data)
        decoder.finish()


def test_sse_partial_frame_and_size_limits_are_errors_not_trimming():
    decoder = p.LmSseDecoder(INSTANCE)
    with pytest.raises(p.LmStudioDenied):
        decoder.feed(b"a" * (1024 * 1024 + 1))
    decoder = p.LmSseDecoder(INSTANCE)
    decoder.feed(sse(packet()) + b"data: {")
    with pytest.raises(p.LmStudioDenied):
        decoder.finish()


def test_mlx_does_not_silently_accept_llama_only_load_options():
    with pytest.raises(p.LmStudioDenied, match="backend_config_unsupported"):
        native_profile(format="mlx")
    mlx = native_profile(format="mlx", config_json='{"context_length":8192}')
    assert p.decode(mlx.load_body()) == dict(
        model=TARGET.artifact_id, context_length=8192, echo_load_config=True
    )


def test_legacy_function_call_dialect_is_not_unbuffered_tool_authority():
    decoder = p.LmSseDecoder(INSTANCE)
    with pytest.raises(p.LmStudioDenied):
        decoder.feed(
            sse(
                packet(
                    choices=[
                        dict(
                            index=0,
                            delta={"function_call": {"name": "read"}},
                            finish_reason=None,
                        )
                    ]
                )
            )
        )
