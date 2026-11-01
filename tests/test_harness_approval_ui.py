"""Fail-closed approval preflight follows the existing Harness state contract."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SIGNOFF = json.loads((ROOT / "product" / "M5_2_0_UIUX_Preservation_Signoff.json").read_text(encoding="utf-8"))


def test_decision_rechecks_the_matching_live_server_slot_before_posting() -> None:
    helper_start = HTML.index("async function confirmLiveHarnessApproval(")
    handler_start = HTML.index("async function resolveHarnessApproval(")
    handler_end = HTML.index("async function abortHarnessRun(", handler_start)
    helper = HTML[helper_start:handler_start]
    handler = HTML[handler_start:handler_end]

    assert 'fetch(`/api/harness/sessions/${encodeURIComponent(sessionId)}/state`)' in helper
    assert "stateSessionId !== sessionId" in helper
    assert 'String(approval.approval_id || "") === approvalId' in helper
    assert "approval.live !== false" in helper
    assert "harnessSnapshot = state" in helper
    assert handler.index("confirmLiveHarnessApproval(session, approvalId)") < handler.index('fetch("/api/harness/approve"')
    assert 'const note = noteEl ? noteEl.value.trim() : "";' in handler
    assert "JSON.stringify({ approval_id: approvalId, decision, note, session_id: session || \"\" })" in handler
    assert HTML.count('fetch("/api/harness/approve"') == 1


def test_failed_or_stale_decisions_are_not_reenabled_from_cached_state() -> None:
    handler_start = HTML.index("async function resolveHarnessApproval(")
    handler_end = HTML.index("async function abortHarnessRun(", handler_start)
    handler = HTML[handler_start:handler_end]

    assert 'button.disabled = true' in handler
    assert "This approval is no longer pending; no decision was sent." in HTML
    assert "approval_not_pending" in handler
    assert "approval_rejected" in handler
    assert "authoritative snapshot alone may render controls again" in handler
    assert "button.disabled = false" not in handler


def test_route_inventory_anchor_is_inert_and_never_cloned() -> None:
    start = HTML.index('<template id="harnessApprovalRouteAttributionTemplate">')
    end = HTML.index("</template>", start) + len("</template>")
    anchor = HTML[start:end]
    renderer_start = HTML.index("function renderHarnessApprovalCardHtml(")
    renderer_end = HTML.index("let harnessGrantSnapshot", renderer_start)
    renderer = HTML[renderer_start:renderer_end]

    assert "disabled" in anchor
    assert 'onclick="resolveHarnessApproval(' in anchor
    assert "harnessApprovalRouteAttributionTemplate" not in renderer
    assert "cloneNode" not in renderer


def test_preservation_signoff_maps_both_approval_journey_routes() -> None:
    harness = next(pane for pane in SIGNOFF["panes"] if pane["label"] == "Harness")
    routes = {(route["method"], route["path"]) for route in harness["routes_reached"]}

    assert ("GET", "/api/harness/sessions/{session_id}/state") in routes
    assert ("POST", "/api/harness/approve") in routes
