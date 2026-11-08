"""LC-07 pure fixtures: counts/qualified media bounds are assumptions, not measurements."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

import request_fit as rf
from model_lifecycle_coordinator import ModelTarget, RequestLease
from test_shared_memory_admission import profile


def prepared(
    *,
    wire="omlx_openai",
    context=8192,
    output=512,
    reasoning=128,
    accounting="included",
    origin="chat",
    media=False,
):
    target = ModelTarget(
        {
            "omlx_openai": "omlx",
            "ollama_native": "ollama",
            "lmstudio_openai": "lmstudio",
        }[wire],
        "model:tag",
        "digest",
        "profile",
    )
    lease = RequestLease("lease", "request", "shared", 1, 1, target, "exact-instance")
    fp = rf.FitProfile(
        replace(profile(target), context_tokens=context),
        wire,
        "a" * 64,
        "tokenizer-qualified",
        "renderer-qualified",
        accounting,
    )
    args = {"path": "file"} if wire == "ollama_native" else '{"path":"file"}'
    messages = [
        dict(role="system", content="SECURITY and skills-memory anchor"),
        dict(role="user", content="original objective"),
        dict(
            role="assistant",
            content="",
            tool_calls=[
                dict(
                    id="call-a",
                    type="function",
                    function=dict(name="read", arguments=args),
                ),
                dict(
                    id="call-b",
                    type="function",
                    function=dict(name="read", arguments=args),
                ),
            ],
        ),
        dict(role="tool", content="second result", tool_call_id="call-b"),
        dict(role="tool", content="first result", tool_call_id="call-a"),
        dict(role="user", content="Latest ಕರ್ನಾಟಕ 😀"),
    ]
    if media:
        messages[-1]["content"] = [
            {"type": "text", "text": "Latest ಕರ್ನಾಟಕ 😀"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    body = dict(
        model=lease.instance_id
        if wire == "lmstudio_openai"
        else target.artifact_id + ":local"
        if wire == "ollama_native"
        else target.artifact_id,
        messages=messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }
        ],
        stream=True,
    )
    if wire == "ollama_native":
        body.update(
            options={"num_ctx": context, "num_predict": output},
            truncate=False,
            shift=False,
            think=True,
        )
    else:
        body["max_tokens"] = output
    if accounting == "additional":
        body["thinking_budget"] = reasoning
    return rf.prepare_request(
        lease,
        fp,
        rf.OutputBudget(output, reasoning, accounting),
        rf.encode(body),
        origin=origin,
        notes=(
            rf.AssemblyNote("skills_memory", 0),
            rf.AssemblyNote("objective", 1),
            rf.AssemblyNote("handoff", 1),
        ),
    )


@pytest.mark.parametrize("wire", ["omlx_openai", "ollama_native", "lmstudio_openai"])
@pytest.mark.parametrize(
    "origin",
    [
        "chat",
        "harness",
        "council",
        "debate",
        "gsd",
        "sdlc",
        "critic",
        "child",
        "handoff",
        "tool_followup",
    ],
)
def test_one_final_body_contract_for_workflows_without_claiming_served_integration(
    wire, origin
):
    p = prepared(wire=wire, origin=origin)
    before = p.body
    rf.verify_prepared(p)
    result = rf.assess_fit(
        p, effective_context=8192, input_tokens=7000, confidence="exact"
    )
    assert p.body == before and p.body_digest == hashlib.sha256(before).hexdigest()
    assert "SECURITY" in before.decode() and "ಕರ್ನಾಟಕ 😀" in before.decode()
    assert result.input_tokens == 7000 and result.generation_reserve_tokens == 512
    assert p.components >= {
        "system_security",
        "history",
        "latest_user",
        "skills_memory",
        "handoff",
        "objective",
        "tool_definitions",
        "tool_exchange",
        "template",
    }
    assert all(count is None for _, count in result.component_tokens)
    assert "SECURITY" not in repr(p) and "SECURITY" not in str(result.to_mapping())


def test_exact_7326_over_4096_failure_is_not_resize_or_truncation():
    p = prepared(context=4096)
    before = p.body
    with pytest.raises(rf.RequestFitDenied, match="context_exceeded") as error:
        rf.assess_fit(p, effective_context=4096, input_tokens=7326, confidence="exact")
    diagnostic = error.value.to_mapping()
    assert (
        diagnostic["fit"]["input_tokens"] == 7326
        and diagnostic["fit"]["effective_context_tokens"] == 4096
    )
    assert diagnostic["retryable"] is False and p.body == before
    assert all(
        x not in str(diagnostic)
        for x in ("SECURITY", "file", "model:tag", "native body")
    )


def test_inclusive_template_and_reasoning_subcounts_not_added_twice_at_boundary():
    p = prepared(output=1192, reasoning=512)
    result = rf.assess_fit(
        p,
        effective_context=8192,
        input_tokens=7000,
        confidence="exact",
        component_tokens=(("template", 1000), ("tool_definitions", 500)),
    )
    assert result.generation_reserve_tokens == 1192
    assert result.to_mapping()["component_tokens"]["template"] == 1000
    with pytest.raises(rf.RequestFitDenied, match="context_exceeded"):
        rf.assess_fit(
            prepared(output=1193, reasoning=512),
            effective_context=8192,
            input_tokens=7000,
            confidence="exact",
        )


def test_additional_reasoning_limit_reserves_both_without_changing_either():
    p = prepared(output=1000, reasoning=192, accounting="additional")
    assert (
        rf.assess_fit(
            p,
            effective_context=8192,
            input_tokens=7000,
            confidence="qualified_upper_bound",
        ).generation_reserve_tokens
        == 1192
    )
    with pytest.raises(rf.RequestFitDenied, match="context_exceeded"):
        p = prepared(output=1000, reasoning=193, accounting="additional")
        rf.assess_fit(p, effective_context=8192, input_tokens=7000, confidence="exact")


@pytest.mark.parametrize("bad", [True, False, -1, 1.5, "7326", None, 2**63])
def test_counts_are_not_char_estimates_bool_coercions_or_unknown_success(bad):
    with pytest.raises(rf.RequestFitDenied):
        rf.assess_fit(
            prepared(), effective_context=8192, input_tokens=bad, confidence="exact"
        )


@pytest.mark.parametrize("kind", ["estimated", "unknown", "source", None])
def test_unqualified_confidence_never_becomes_a_fit(kind):
    with pytest.raises(rf.RequestFitDenied):
        rf.assess_fit(
            prepared(), effective_context=8192, input_tokens=1, confidence=kind
        )


@pytest.mark.parametrize(
    "change",
    [
        "tool_id",
        "tool_result",
        "missing_result",
        "incomplete_call",
        "output",
        "context",
        "digest",
        "anchor",
        "profile",
    ],
)
def test_any_post_preparation_change_or_incomplete_exchange_invalidates_owned_request(
    change,
):
    p = prepared(wire="ollama_native")
    body = rf.decode(p.body)
    if change == "tool_id":
        body["messages"][2]["tool_calls"][0]["id"] = "other"
    elif change == "tool_result":
        body["messages"][3]["content"] = "changed result"
    elif change == "missing_result":
        body["messages"].pop(3)
    elif change == "incomplete_call":
        body["messages"][2]["tool_calls"][0]["function"]["arguments"] = "{"
    elif change == "output":
        body["options"]["num_predict"] = -1
    elif change == "context":
        body["options"]["num_ctx"] = 16384
    elif change == "anchor":
        body["messages"][0]["content"] = "silently trimmed"
    if change == "digest":
        forged = replace(p, body_digest="b" * 64)
    elif change == "profile":
        forged = replace(
            p,
            profile=replace(
                p.profile, memory=replace(p.profile.memory, context_tokens=4096)
            ),
        )
    else:
        forged = replace(p, body=rf.encode(body))
    with pytest.raises(rf.RequestFitDenied):
        rf.verify_prepared(forged)


def test_inline_media_requires_full_expansion_coverage_not_text_length():
    p = prepared(media=True)
    assert "media" in p.features and "media" in p.components
    result = rf.assess_fit(
        p, effective_context=8192, input_tokens=5000, confidence="qualified_upper_bound"
    )
    assert result.confidence == "qualified_upper_bound"
    body = rf.decode(p.body)
    body["messages"][-1]["content"][-1]["image_url"]["url"] = (
        "https://do-not-fetch.invalid/secret"
    )
    with pytest.raises(rf.RequestFitDenied, match="unsupported_media"):
        rf.prepare_request(
            p.lease, p.profile, p.budget, rf.encode(body), origin=p.origin
        )


@pytest.mark.parametrize(
    "counts",
    [
        (("template", 8000),),
        (("template", 1), ("template", 2)),
        (("unknown", 1),),
        (("history", True),),
    ],
)
def test_component_diagnostics_cannot_claim_inconsistent_totals(counts):
    with pytest.raises(rf.RequestFitDenied):
        rf.assess_fit(
            prepared(),
            effective_context=8192,
            input_tokens=7000,
            confidence="exact",
            component_tokens=counts,
        )


@pytest.mark.parametrize(
    "raw",
    [b'{"messages":[],"messages":[]}', b'{"x":NaN}', b'{"x":1e10000}', b"\xff", b"[]"],
)
def test_invalid_final_json_cannot_be_admitted(raw):
    p = prepared()
    with pytest.raises(rf.RequestFitDenied):
        rf.prepare_request(p.lease, p.profile, p.budget, raw, origin="chat")


@pytest.mark.parametrize(
    "field,value",
    [
        ("previous_response_id", "hidden-history"),
        ("integrations", ["server-mcp"]),
        ("n", 2),
        ("truncate", True),
        ("shift", True),
        ("max_completion_tokens", 4096),
    ],
)
def test_hidden_history_tools_alternate_output_limits_and_permissive_input_not_a_full_request(
    field, value
):
    p = prepared()
    body = rf.decode(p.body)
    body[field] = value
    with pytest.raises(rf.RequestFitDenied):
        rf.prepare_request(p.lease, p.profile, p.budget, rf.encode(body), origin="chat")
