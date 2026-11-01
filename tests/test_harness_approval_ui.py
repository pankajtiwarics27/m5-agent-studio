"""UI disclosures and fail-closed approval flow stay aligned with server behavior."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")
GRANTS = (ROOT / "scoped_grants.py").read_text(encoding="utf-8")


def test_approval_card_discloses_requester_scope_expiry_and_refusal_consequence() -> None:
    for field in ("Requested by", "What will run", "Permission scope", "Expiry", "If rejected"):
        assert f"<dt>{field}</dt>" in HTML
    assert "One use of this submitted tool invocation in this Harness session" in HTML
    assert "this request times out after" in HTML
    assert "the one-use grant expires after" in HTML
    assert "This invocation is not run" in HTML
    assert "Approval never overrides server policy, action-admission, or sandbox checks" in HTML
    assert "Standard Policy</span>" not in HTML
    assert "Approve &amp; Run" not in HTML


def test_expiry_disclosure_matches_authoritative_server_defaults() -> None:
    wait = re.search(r"async def _await_tool_approval\([^\n]*timeout: float = ([0-9.]+)", APP)
    grant = re.search(r"^SG_DEFAULT_GRANT_TTL_SECONDS = ([0-9.]+)$", GRANTS, re.MULTILINE)
    ui_wait = re.search(r"HARNESS_APPROVAL_WAIT_TIMEOUT_SECONDS_UI = ([0-9.]+)", HTML)
    ui_grant = re.search(r"HARNESS_APPROVAL_GRANT_TTL_SECONDS_UI = ([0-9.]+)", HTML)
    assert wait and grant and ui_wait and ui_grant
    assert float(ui_wait.group(1)) == float(wait.group(1))
    assert float(ui_grant.group(1)) == float(grant.group(1))


def test_approval_action_requires_fresh_server_snapshot_before_existing_endpoint() -> None:
    start = HTML.index("async function resolveHarnessApproval(")
    end = HTML.index("async function abortHarnessRun(", start)
    handler = HTML[start:end]
    assert "confirmLiveHarnessApproval(session, approvalId)" in handler
    assert handler.index("confirmLiveHarnessApproval(session, approvalId)") < handler.index('fetch("/api/harness/approve"')
    assert HTML.count('fetch("/api/harness/approve"') == 1
    assert "may render the request as actionable again" in handler
