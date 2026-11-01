"""Harness approval safety and permission UX (UIUX-P3-03).

Approving a Harness tool call records a scoped grant under the approval's own id - one use,
the exact arguments, ten minutes by default, issued by the operator. Before this story the
pane drew approvals with four different cards, three of which printed a hardcoded policy
badge, none of which could say what the click would record, and none of which could show or
take back what an approval had left behind.

The inventory is the artifact; these tests read it, and break each rule in a copy of the
source so a rule that stopped looking fails here rather than in production.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "harness_permissions_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Permissions.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-permissions.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "harness-permissions"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_harness_permissions_inventory",
        PROJECT_ROOT / "scripts" / "uiux_harness_permissions_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the inventory at copies, so a rule can be broken in one of them."""

    def _make(html_transform=None, app_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, (
                "the transform did not change the markup, so the test proves nothing"
            )
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, (
                "the transform did not change the server source, so the test proves nothing"
            )
            app = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ─────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_harness_permissions_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Harness approvals are permissions (UIUX-P3-03)")
    assert "Do not edit by hand" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P3-03"
    assert data["totals"]["findings"] == 0
    # This story added no endpoint: the grants and approve routes already existed.
    assert [(row["method"], row["path"]) for row in data["routes"]] == [
        ("GET", "/api/harness/sessions/{session_id}/grants"),
        ("POST", "/api/harness/sessions/{session_id}/grants"),
        ("POST", "/api/harness/sessions/{session_id}/grants/revoke"),
        ("POST", "/api/harness/approve"),
    ]


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── one card, and it states the decision ────────────────────────────────────


def test_every_path_draws_the_one_approval_card(payload):
    card = payload["frontend"]["card"]
    assert card["one_builder"] is True
    assert card["every_path_draws_with_it"] is True, card["drawing_paths"]
    assert card["one_headline"] is True
    assert card["one_decision_control"] is True
    assert card["arguments_survive_a_decision"] is True
    assert card["a_decided_card_is_redrawn_from_the_request"] is True, (
        "a decided card is redrawn from an event that carries no tool and no arguments"
    )


def test_the_cards_policy_is_the_policy_in_force(payload):
    card = payload["frontend"]["card"]
    permission = payload["frontend"]["permission"]
    assert card["no_hardcoded_policy_badge"] is True, (
        'a card still says "Standard Policy" whatever policy is in force'
    )
    assert permission["names_the_policy"] is True
    assert permission["policy_is_the_selects_own_label"] is True
    assert permission["policy_comes_from_the_session_first"] is True


def test_the_card_states_what_the_click_records(payload):
    permission = payload["frontend"]["permission"]
    assert permission["names_the_tool"] is True
    assert permission["shows_the_arguments"] is True
    assert permission["states_the_capability"] is True
    assert permission["states_the_resource"] is True
    assert permission["states_the_limit"] is True
    assert permission["states_the_expiry_clock"] is True
    assert permission["will_not_invent_a_scope"] is True, (
        "the card states a resource scope the server did not resolve"
    )


def test_the_capability_the_card_shows_is_the_one_the_server_records(payload):
    import scoped_grants

    permission = payload["frontend"]["permission"]
    mapping = permission["capability_map"]
    assert mapping, "the client's capability table is unreadable"
    for tool, caps in mapping.items():
        assert list(caps) == list(scoped_grants.resolve_tool_capabilities(tool, {})), tool
    assert permission["ttl_seconds"] == scoped_grants.SG_DEFAULT_GRANT_TTL_SECONDS
    assert permission["mcp_tools_resolve_first"] is True


# ── a decision belongs to a session ─────────────────────────────────────────


def test_a_decision_carries_the_session_that_owns_the_approval(payload):
    session = payload["frontend"]["session"]
    assert all(session.values()), [name for name, ok in session.items() if not ok]
    assert session["workflow_cards_use_their_own"] is True, (
        "a workflow approval can post a decision for the session on screen instead of its own"
    )


# ── expiry and refusal are stated ───────────────────────────────────────────


def test_an_offer_that_is_not_decided_expires_in_words(payload):
    expiry = payload["frontend"]["expiry"]
    assert all(expiry.values()), [name for name, ok in expiry.items() if not ok]
    assert expiry["countdown_stops"] is True, "a countdown outlives the last live card"


def test_the_card_says_what_refusing_does_instead(payload):
    refusal = payload["frontend"]["refusal"]
    assert refusal["says_what_rejecting_does"] is True
    assert refusal["says_it_is_not_a_standing_allow"] is True, (
        "a reader can still think approving run_bash allows run_bash"
    )
    assert refusal["says_the_turn_waits"] is True


# ── the permission surface ──────────────────────────────────────────────────


def test_the_permissions_surface_is_a_dialog_opened_from_the_pane(payload):
    surface = payload["frontend"]["surface"]
    assert surface["dialog_exists"] is True
    assert surface["is_a_dialog"] is True
    assert surface["is_named"] is True
    assert surface["closes_on_the_backdrop"] is True
    assert surface["opens_through_the_controller"] is True
    assert surface["closes_through_the_controller"] is True
    assert surface["reachable_from_the_badge"] is True
    assert surface["reachable_from_the_session_actions"] is True
    assert surface["refuses_without_a_session"] is True
    assert all(surface["lists"].values()), [
        name for name, ok in surface["lists"].items() if not ok
    ]


def test_the_surface_says_what_is_held_and_when_it_could_not_read(payload):
    surface = payload["frontend"]["surface"]
    for flag in (
        "states_what_is_held",
        "states_the_rules",
        "states_the_history",
        "states_the_approvals",
        "states_the_policy_and_revision",
        "says_when_it_could_not_read",
        "keeps_the_session_it_asked_about",
    ):
        assert surface[flag] is True, flag


def test_the_badge_is_a_summary_and_not_a_surface_of_its_own(payload):
    surface = payload["frontend"]["surface"]
    assert surface["badge_states_a_count"] is True
    assert surface["badge_is_hidden_without_a_session"] is True
    assert surface["badge_says_none_in_words"] is True
    assert surface["badge_does_not_reach_the_network"] is True
    for flag in (
        "refreshed_on_selection",
        "refreshed_after_a_decision",
        "refreshed_on_a_new_draft",
    ):
        assert surface[flag] is True, flag


# ── revocation is two steps and is not an undo ──────────────────────────────


def test_revoking_is_a_confirmed_act_that_names_its_consequence(payload):
    revocation = payload["frontend"]["revocation"]
    assert all(revocation.values()), [
        name for name, ok in revocation.items() if not ok
    ]
    assert revocation["names_the_denial_rule"] is True, (
        "the confirmation does not say that the rule outlives the grant"
    )
    assert revocation["refreshes_both_views"] is True


# ── nothing that worked stopped working ────────────────────────────────────


def test_the_approve_contract_is_unchanged(payload):
    contract = payload["server"]["approve_contract"]
    assert all(contract.values()), [name for name, ok in contract.items() if not ok]


def test_an_approval_still_records_one_use_of_the_exact_call(payload):
    persistence = payload["server"]["grant_persistence"]
    assert all(persistence.values()), [
        name for name, ok in persistence.items() if not ok
    ]


def test_revocation_is_still_enforced(payload):
    enforcement = payload["server"]["enforcement"]
    assert all(enforcement.values()), [name for name, ok in enforcement.items() if not ok]


def test_the_routes_the_surface_uses_are_still_mounted(payload):
    missing = [
        f"{row['method']} {row['path']}" for row in payload["routes"] if not row["mounted"]
    ]
    assert missing == [], f"routes gone: {missing}"


def test_the_styles_the_surfaces_need_exist(payload):
    styles = payload["frontend"]["styles"]
    assert all(styles.values()), [name for name, ok in styles.items() if not ok]


def test_the_surface_elements_and_the_badge_are_still_wired():
    html = FRONTEND.read_text(encoding="utf-8")
    for element_id in inventory.SURFACE_LISTS:
        assert f'id="{element_id}"' in html, element_id
    assert 'id="harnessPermissionBadge"' in html
    assert 'onclick="openHarnessGrants()"' in html
    assert '<option value="permissions">' in html


# ── the behaviour the pane describes, exercised against the real store ──────


def test_an_approval_records_a_permission_that_a_revocation_rule_outlives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card's three claims: one use, the exact call, and a rule a re-issue cannot clear."""
    import app as studio
    import scoped_grants

    store_dir = tmp_path / "store"
    workspace = tmp_path / "workspace"
    store_dir.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    studio._reset_scoped_grant_store(None)
    studio.init_db()
    try:
        args = {"command": "pytest -q tests/test_store.py"}
        studio._persist_approval_grant_outcome(
            "apr-p3-03",
            "approve",
            session_id="sess-p3-03",
            tool_name="run_bash",
            args=args,
            note="looks right",
            ttl_seconds=float(scoped_grants.SG_DEFAULT_GRANT_TTL_SECONDS),
        )
        store = studio.get_scoped_grant_store()
        summary = store.summary(session_id="sess-p3-03", current_policy="standard")
        grants = summary["grants"]
        assert len(grants) == 1, grants
        grant = grants[0]
        assert grant["grant_id"] == "apr-p3-03"
        assert grant["max_uses"] == 1
        assert grant["uses_consumed"] == 0
        assert grant["issued_by"] == "operator_approval"
        assert grant["capabilities"] == ["code_execution", "workspace_read", "workspace_write"]
        assert grant["args_sha256"], "the approval is not bound to the arguments it approved"
        assert grant["expires_at"] - grant["issued_at"] == pytest.approx(
            scoped_grants.SG_DEFAULT_GRANT_TTL_SECONDS
        )

        # One decision, one grant: a repeated persist is a no-op.
        studio._persist_approval_grant_outcome(
            "apr-p3-03",
            "approve",
            session_id="sess-p3-03",
            tool_name="run_bash",
            args=args,
        )
        assert len(store.summary(session_id="sess-p3-03", current_policy="standard")["grants"]) == 1

        scopes = tuple(grant["resource_scopes"])
        store.revoke_grant("apr-p3-03", session_id="sess-p3-03", reason="operator_revoked")
        denial = store.check_revocation_denials(
            session_id="sess-p3-03",
            task_id=None,
            tool_name="run_bash",
            action_capabilities=("code_execution",),
            action_scopes=scopes,
        )
        assert denial is not None and denial.allowed is False
        assert denial.error_code == "capability_revoked"

        # Issuing a fresh grant does not clear the rule - which is what the
        # confirmation tells the reader before they revoke.
        store.issue_grant(
            session_id="sess-p3-03",
            capabilities=("code_execution", "workspace_read", "workspace_write"),
            resource_scopes=scopes,
            tool_names=("run_bash",),
            ttl_seconds=600.0,
            current_policy="standard",
        )
        again = store.check_revocation_denials(
            session_id="sess-p3-03",
            task_id=None,
            tool_name="run_bash",
            action_capabilities=("code_execution",),
            action_scopes=scopes,
        )
        assert again is not None and again.allowed is False, (
            "a revocation rule must outlive a re-issued grant"
        )
    finally:
        studio._reset_scoped_grant_store(None)


# ── the walk covers the same ground ────────────────────────────────────────


def test_the_walk_reads_the_card_and_the_surface():
    assert WALK.exists(), "scripts/harness_permissions_walk.mjs is missing"
    source = WALK.read_text(encoding="utf-8")
    for needle in (
        "harnessPermissionBadge",
        "harnessGrantsModal",
        "harnessGrantsActive",
        "harnessGrantsRules",
        "harnessGrantsApprovals",
        "openHarnessGrants",
        "closeHarnessGrants",
        "h-approval",
        "apr-card-",
        "api/harness/approve",
        "Revoke and block",
        "Keep it",
        "no page error occurred during the walk",
    ):
        assert needle in source, needle
    # No route mints a pending approval, so the walk has to ask the fixture - and the
    # fixture has to say that it staged the arrival rather than pretend a turn produced it.
    assert "harness_permissions_seed.py" in source
    seed = (PROJECT_ROOT / "scripts" / "harness_permissions_seed.py").read_text(encoding="utf-8")
    assert "_PENDING_APPROVALS" in seed
    assert "_create_pending_tool_approval" in seed


def test_the_media_directory_documents_its_captures():
    assert MEDIA.is_dir(), "docs/media/harness-permissions/ is missing"
    readme = MEDIA / "README.md"
    assert readme.exists(), "docs/media/harness-permissions/README.md is missing"
    text = readme.read_text(encoding="utf-8")
    for name in ("approval-card.png", "permissions-open.png", "revoked.png", "expired.png"):
        assert name in text, name
        assert (MEDIA / name).exists(), name
    assert "harness_permissions_walk.mjs" in text


# ── negative: a rule that stops looking ────────────────────────────────────


def test_a_second_approval_renderer_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (ev.type === \"approval/request\") {",
            "    if (ev.type === \"approval/request\") {\n"
            "      return `<div>Approval required: <code>${escapeHtml(ev.tool)}</code></div>`;",
            1,
        )
    )
    assert "card" in _checks(payload)


def test_a_hardcoded_policy_badge_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '<span class="badge amber">${escapeHtml(policyLabel)}</span>',
            '<span class="badge amber">Standard Policy</span>',
            1,
        )
    )
    assert "card" in _checks(payload)


def test_a_card_that_stops_stating_the_limit_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "one use · ${escapeHtml",
            "${escapeHtml",
            1,
        )
    )
    assert "permission" in _checks(payload)


def test_a_capability_map_that_drifts_from_the_server_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '"run_bash": ["code_execution", "workspace_read", "workspace_write"],',
            '"run_bash": ["code_execution"],',
            1,
        )
    )
    assert "capability-map" in _checks(payload)


def test_a_decision_that_carries_no_session_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'const body = JSON.stringify({ approval_id: approvalId, decision, note, session_id: session || "" });',
            "const body = JSON.stringify({ approval_id: approvalId, decision, note });",
            1,
        )
    )
    assert "session" in _checks(payload)


def test_a_countdown_that_never_stops_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      clearInterval(harnessApprovalCountdown);\n",
            "",
            1,
        )
    )
    assert "expiry" in _checks(payload)


def test_a_one_click_revoke_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "harnessGrantConfirm = String(grantId || \"\");",
            "void grantId;",
            1,
        )
    )
    assert "revocation" in _checks(payload)


def test_a_surface_that_draws_an_empty_session_on_a_failure_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "The permissions could not be read",
            "Nothing held",
            1,
        )
    )
    assert "surface" in _checks(payload)


def test_an_approve_route_without_session_scoping_is_caught(sources):
    def unscoped(server: str) -> str:
        return server.replace(
            "Approval is scoped to its originating session; session_id must match.",
            "Mismatch.",
            1,
        )

    payload = sources(app_transform=unscoped)
    assert "approve_contract" in _checks(payload)


def test_a_standing_permission_instead_of_one_use_is_caught(sources):
    def standing(server: str) -> str:
        return server.replace("            max_uses=1,", "            max_uses=None,", 1)

    payload = sources(app_transform=standing)
    assert "grant_persistence" in _checks(payload)


def test_an_invented_permissions_route_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "/grants`",
            "/permissions`",
            1,
        )
    )
    assert "unmounted-call" in _checks(payload), "the pane can call a route nothing mounts"


def test_a_permissions_endpoint_the_server_does_not_mount_is_caught(sources):
    def invented(server: str) -> str:
        return server.replace(
            '@app.post("/api/harness/approve")',
            '@app.post("/api/harness/permissions")',
            1,
        ).replace(
            '/api/harness/permissions")',
            '/api/harness/permissions")',
            1,
        )

    payload = sources(app_transform=invented)
    assert "invented-route" in _checks(payload)
