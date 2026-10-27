from __future__ import annotations

import pytest

from provider_switching import ProviderCapabilities, RequestSemantics, switch_provider


def test_compatible_provider_switch_preserves_all_request_semantics() -> None:
    source = ProviderCapabilities("source", streaming=True, tools=True, structured_output=True, reasoning=True)
    target = ProviderCapabilities("target", streaming=True, tools=True, structured_output=True, reasoning=True)
    request = RequestSemantics(streaming=True, tools=True, structured_output=True, reasoning=True)
    decision = switch_provider(source, target, request)
    assert decision.supported is True
    assert decision.unsupported_features == ()
    assert decision.preserved == request


def test_unsupported_features_are_reported_instead_of_dropped() -> None:
    source = ProviderCapabilities("source", streaming=True, tools=True, reasoning=True)
    target = ProviderCapabilities("target", streaming=True)
    request = RequestSemantics(streaming=True, tools=True, structured_output=True, reasoning=True)
    decision = switch_provider(source, target, request)
    assert decision.supported is False
    assert decision.unsupported_features == ("tools", "structured_output", "reasoning")
    assert "explicit caller decision" in decision.reason
    assert decision.preserved == request


def test_invalid_switch_inputs_fail_closed() -> None:
    with pytest.raises(TypeError, match="source"):
        switch_provider("source", ProviderCapabilities("target"), RequestSemantics())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="request"):
        switch_provider(ProviderCapabilities("source"), ProviderCapabilities("target"), "request")  # type: ignore[arg-type]
