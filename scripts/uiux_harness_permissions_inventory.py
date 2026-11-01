#!/usr/bin/env python3
"""Inventory the Harness's approval safety and permission UX (UIUX-P3-03).

    python3 scripts/uiux_harness_permissions_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_harness_permissions_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_harness_permissions_inventory.py --json     # print the inventory

Approving a tool call in the Harness is not "run it once": `POST /api/harness/approve`
resolves the gate, and `_persist_approval_grant_outcome()` records a scoped grant under the
approval's own id - one use, the exact arguments, `issued_by="operator_approval"`, ten
minutes by default. That grant is what lets the call execute, and `check_revocation_denials()`
is what refuses it when an operator has revoked the capability.

Before this story the pane said none of that. An approval arrived as one of four different
cards, and three of the four printed a hardcoded "Standard Policy" badge whatever policy was
in force; none stated the capability or resource scope the click would record, none said how
long the record lasted, and expiry was drawn only after the fact. What an approval left
behind was invisible: the grants routes existed, but nothing in the UI could list a session's
permissions or take one back.

So this artifact measures five surfaces:

* **one card**, the single renderer every path now draws with, and the fact that it names the
  tool, the arguments, the capability, the resource, the policy, the limit and the expiry;
* **the session**, that a decision is sent with the session it belongs to rather than the
  one on screen;
* **expiry and refusal**, the countdown, the expired card, and the sentence that says what
  refusing does instead;
* **the surface**, the permissions dialog: what the session holds, the revocation rules that
  refuse matching actions, the history, and the pending or expired approvals;
* **revocation**, the two-step confirmation that names the denial rule it creates, and the
  existing route it goes through.

It fails when a second approval renderer appears, when a policy badge is hardcoded again,
when a card stops stating what the click records or how long it lasts, when the capability
map drifts from `scoped_grants.resolve_tool_capabilities()`, when a decision can be sent
without its session, when a revoke is offered as one click or described as undoable, when the
pane touches a permissions endpoint that does not exist, and when the approve contract,
grant persistence or revocation enforcement this story had to preserve changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Permissions.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-permissions.md"

NETWORK_CALLS = ("fetch(", "XMLHttpRequest", "EventSource", "navigator.sendBeacon")

# Any `/api/...` path the pane builds, in a template literal, a string or a quoted fetch.
CALLED_PATH = re.compile("""["'`](/api/[^"'`]*)["'`]""")

# The one card builder this story left behind, and the four places that used to build their own.
CARD = "renderHarnessApprovalCardHtml"
CARD_PATHS = (
    "renderStoredHarnessEventHtml",
    "appendWorkflowApprovalCard",
    "updateWorkflowApprovalCard",
    "runHarnessTurn",
)

FUNCTIONS = (
    "harnessApprovalPermission",
    "harnessPolicyInForce",
    "harnessPolicyLabel",
    "harnessClockFrom",
    "harnessApprovalGrantExpiry",
    "harnessApprovalRemainingLabel",
    "tickHarnessApprovalCountdowns",
    "startHarnessApprovalCountdown",
    CARD,
    "renderHarnessGrants",
    "refreshHarnessGrants",
    "openHarnessGrants",
    "closeHarnessGrants",
    "requestRevokeHarnessGrant",
    "cancelRevokeHarnessGrant",
    "revokeHarnessGrant",
    "focusHarnessApprovalCard",
    "updateHarnessPermissionBadge",
    "resolveHarnessApproval",
)

# The dialog's body, in the order a reader meets it.
SURFACE_LISTS = (
    "harnessGrantsSummary",
    "harnessGrantsActive",
    "harnessGrantsRules",
    "harnessGrantsHistory",
    "harnessGrantsApprovals",
)

# Routes the surface reads and writes. All of them predate this story.
ROUTES = (
    ("GET", "/api/harness/sessions/{session_id}/grants"),
    ("POST", "/api/harness/sessions/{session_id}/grants"),
    ("POST", "/api/harness/sessions/{session_id}/grants/revoke"),
    ("POST", "/api/harness/approve"),
)


def function_body(html: str, name: str) -> str:
    """The balanced body of ``function name(...)``, skipping the parameter list."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", html)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for offset in range(open_paren, len(html)):
        char = html[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = offset
                break
    if close_paren < 0:
        return ""
    start = html.find("{", close_paren)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(html)):
        char = html[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : offset + 1]
    return html[start:]


def python_function(source: str, name: str) -> str:
    """The indented body of ``def name(...)`` in Python source, by dedent."""
    match = re.search(rf"^(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.M)
    if not match:
        return ""
    # Walk past the signature (which may span lines and close at column 0), then take every
    # indented line that follows it.
    depth = 0
    index = match.start()
    while index < len(source):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    colon = source.find(":", index)
    if colon < 0:
        return ""
    body_start = source.find("\n", colon)
    if body_start < 0:
        return ""
    lines = source[body_start + 1 :].splitlines()
    collected: list[str] = []
    for line in lines:
        if line.strip() and not line[0].isspace():
            break
        collected.append(line)
    return "\n".join(collected)


def element(html: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", html)
    return match.group(0) if match else ""


def enclosing_function(html: str, offset: int) -> str:
    """The name of the function a source offset sits inside."""
    best = ""
    for match in re.finditer(r"(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\(", html):
        body = function_body(html, match.group(1))
        if not body:
            continue
        start = html.find(body, match.end())
        if start <= offset <= start + len(body):
            if not best or len(body) < len(function_body(html, best)):
                best = match.group(1)
    return best


def mounted_routes(server: str) -> set[str]:
    """Every route the server mounts, with each path parameter collapsed to `{}`."""
    found = set()
    for match in re.finditer(r'@app\.(?:get|post|put|patch|delete)\("(/[^"]*)"\)', server):
        found.add(re.sub(r"\{[^}]*\}", "{}", match.group(1)))
    return found


def called_routes(html: str) -> list[str]:
    """Every `/api/...` path literal the pane builds, with each interpolation collapsed to `{}`."""
    found: list[str] = []
    for match in re.finditer(CALLED_PATH, html):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1))
        path = path.split("?")[0]
        if path not in found:
            found.append(path)
    return found


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def route_matches(called: str, mounted: str) -> bool:
    """Whether a path the pane builds can be answered by a mounted route."""
    a, b = _segments(called), _segments(mounted)
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == "{}" or y == "{}":
            continue
        if x != y:
            return False
    return True


def tool_capability_map(html: str) -> dict[str, list[str]]:
    """The client's copy of the tool -> capability mapping, as JSON."""
    match = re.search(r"const HARNESS_TOOL_CAPABILITIES = (\{.*?\n  \});", html, re.S)
    if not match:
        return {}
    try:
        loaded = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def read_frontend(html: str) -> dict:
    builder = function_body(html, CARD)
    surface = function_body(html, "renderHarnessGrants")
    refresh = function_body(html, "refreshHarnessGrants")
    revoke = function_body(html, "revokeHarnessGrant")
    resolve = function_body(html, "resolveHarnessApproval")
    badge = function_body(html, "updateHarnessPermissionBadge")
    policy_in_force = function_body(html, "harnessPolicyInForce")
    policy_label = function_body(html, "harnessPolicyLabel")
    countdown = function_body(html, "tickHarnessApprovalCountdowns")
    expiry = function_body(html, "harnessApprovalGrantExpiry")
    permission = function_body(html, "harnessApprovalPermission")
    cancel = function_body(html, "cancelRevokeHarnessGrant")
    select = function_body(html, "selectHarnessSession")
    dialog = element(html, "harnessGrantsModal")
    dialog_region = ""
    if dialog:
        start = html.find(dialog)
        dialog_region = html[start : start + 2600]
    badge_el = element(html, "harnessPermissionBadge")

    call_sites = [html[m.start() : m.start() + 40] for m in re.finditer(rf"{CARD}\(", html)]
    drawing_paths = {
        name: CARD in function_body(html, name) for name in CARD_PATHS
    }
    ttl_literal = re.search(r"const HARNESS_APPROVAL_GRANT_TTL_SECONDS = (\d+);", html)

    return {
        "functions": {name: bool(function_body(html, name)) for name in FUNCTIONS},
        "card": {
            "one_builder": html.count(f"function {CARD}(") == 1,
            "no_second_headline_existed": "Human Approval Required" not in html,
            "one_headline": html.count("Approval required: <code>") == 1,
            "no_hardcoded_policy_badge": "Standard Policy" not in html,
            "every_path_draws_with_it": all(drawing_paths.values()) and len(call_sites) >= 4,
            "drawing_paths": drawing_paths,
            "one_decision_control": html.count('onclick="resolveHarnessApproval(') == 2,
            "a_decided_card_is_redrawn_from_the_request": (
                "harnessApprovalRequests.get(String(d.approval_id))" in function_body(
                    html, "updateWorkflowApprovalCard"
                )
                and "card.replaceWith(wrapper.firstElementChild)" in function_body(
                    html, "updateWorkflowApprovalCard"
                )
            ),
            "the_request_is_filed_before_it_is_redrawn": (
                "harnessApprovalRequests.set(approvalId" in builder
            ),
            "arguments_survive_a_decision": "formatToolArgsSummary(tool, args)" in builder,
        },
        "permission": {
            "names_the_policy": "harnessPolicyLabel(options.policy || harnessPolicyInForce())" in builder,
            "policy_is_the_selects_own_label": "option.value === value && option.textContent.trim()" in policy_label,
            "policy_comes_from_the_session_first": "session.policy" in policy_in_force
            and policy_in_force.index("session.policy")
            < policy_in_force.index("control.value"),
            "names_the_tool": "Approval required: <code>${escapeHtml(tool)}</code>" in builder,
            "shows_the_arguments": "formatToolArgsSummary(tool, args)" in builder,
            "states_the_capability": "permission.capabilities.join(\", \")" in builder,
            "states_the_resource": "permission.resource" in builder,
            "states_the_limit": "one use · " in builder
            and "HARNESS_APPROVAL_GRANT_TTL_SECONDS / 60" in builder,
            "states_the_expiry_clock": "harnessClockFrom(expiresAt)" in builder,
            "will_not_invent_a_scope": "resolved inside this session's worktree" in permission
            and "workspace:" not in permission,
            "the_mapping_is_read_from_a_table": "HARNESS_TOOL_CAPABILITIES[tool]" in permission,
            "mcp_tools_resolve_first": 'tool.startsWith("mcp__")' in permission,
            "ttl_seconds": int(ttl_literal.group(1)) if ttl_literal else None,
            "capability_map": tool_capability_map(html),
            "fallback": (re.search(r"HARNESS_TOOL_CAPABILITY_FALLBACK = \[(.*?)\];", html) or [None, ""])[1]
            if re.search(r"HARNESS_TOOL_CAPABILITY_FALLBACK = \[(.*?)\];", html)
            else "",
        },
        "session": {
            "accepts_a_session": "function resolveHarnessApproval(approvalId, decision, sessionId)" in html,
            "falls_back_to_the_active_session": 'typeof sessionId === "string" && sessionId' in resolve,
            "sends_it": 'session_id: session || ""' in resolve,
            "the_card_carries_one": "options.sessionId || \"\"" in builder,
            "workflow_cards_use_their_own": html.count(
                'sessionId: d.session_id || activeHarnessId || ""'
            )
            == 2,
            "a_decided_card_keeps_its_session": 'sessionId: d.session_id || known.session_id' in html,
        },
        "expiry": {
            "counts_from_the_request": "const requestedAt = Number(ev && ev.ts)" in builder
            and "HARNESS_APPROVAL_GRANT_TTL_SECONDS" in expiry,
            "states_the_remaining_time": "harnessApprovalRemainingLabel(expiresAt)" in builder,
            "countdown_ticks": "setInterval(tickHarnessApprovalCountdowns, 15000)" in html,
            "countdown_updates_the_lines": "node.dataset.expiresAt" in countdown,
            "countdown_stops": "clearInterval(harnessApprovalCountdown)" in countdown,
            "an_expired_offer_says_so": "this offer has expired" in function_body(
                html, "harnessApprovalRemainingLabel"
            ),
            "the_expired_card_says_what_to_do": "Re-run the turn to reach the gate again" in builder,
            "an_undecided_card_is_not_an_approval": "This request was never decided" in builder,
            "the_state_badge_states_expiry": "Expired — re-run the turn" in builder,
        },
        "refusal": {
            "says_what_rejecting_does": (
                "You refused this call, so it did not run and no permission was recorded." in builder
            ),
            "says_it_is_not_a_standing_allow": "It is not a standing allow for" in builder,
            "says_the_turn_waits": "the turn waits" in builder,
            "a_decided_card_says_the_permission_was_used": "one-use permission that has now been used"
            in builder,
        },
        "surface": {
            "dialog_exists": bool(dialog),
            "is_a_dialog": 'role="dialog"' in dialog_region,
            "is_named": 'aria-label="Harness permissions for this session"' in dialog_region,
            "closes_on_the_backdrop": bool(dialog)
            and bool(re.search(r"if\(event\.target===this\)\w+\(\)", dialog)),
            "opens_through_the_controller": 'openOverlay("harnessGrantsModal"' in function_body(
                html, "openHarnessGrants"
            ),
            "closes_through_the_controller": 'closeOverlay("harnessGrantsModal")' in function_body(
                html, "closeHarnessGrants"
            ),
            "reachable_from_the_badge": 'id="harnessPermissionBadge"' in html
            and 'onclick="openHarnessGrants()"' in html,
            "reachable_from_the_session_actions": '<option value="permissions">' in html
            and 'else if (v === "permissions") openHarnessGrants();' in html,
            "refuses_without_a_session": "Select a Harness session first" in function_body(
                html, "openHarnessGrants"
            ),
            "lists": {name: bool(element(html, name)) for name in SURFACE_LISTS},
            "reads_the_grants_route": "/grants`" in refresh,
            "states_the_policy_and_revision": "revision ${harnessGrantSnapshot.session_policy_revision}"
            in surface,
            "states_what_is_held": "active.length" in surface,
            "states_the_rules": "rule.revocation_id" in surface,
            "states_the_history": 'String(grant.status) !== "active"' in surface,
            "states_the_approvals": "approvals.pending" in surface and "approvals.expired" in surface,
            "says_when_it_could_not_read": "The permissions could not be read" in surface,
            "keeps_the_session_it_asked_about": "if (activeHarnessId !== sessionId) return null;" in refresh,
            "refreshed_on_selection": "await refreshHarnessGrants();" in select,
            "refreshed_after_a_decision": "await refreshHarnessGrants();" in resolve,
            "refreshed_on_a_new_draft": "harnessGrantSnapshot = null;" in function_body(
                html, "startFreshHarnessDraft"
            ),
            "badge_states_a_count": bool(badge_el) and "data-count" in badge_el,
            "badge_is_hidden_without_a_session": "badge.hidden = true;" in badge,
            "badge_says_none_in_words": '"No permissions"' in badge,
            "badge_names_what_it_opens": "aria-label" in badge,
            "badge_does_not_reach_the_network": not any(call in badge for call in NETWORK_CALLS),
            "finding_an_approval_card": "apr-card-${approvalId}" in function_body(
                html, "focusHarnessApprovalCard"
            ),
            "the_permission_is_a_dl": 'class="perm-facts h-approval-facts"' in builder,
        },
        "revocation": {
            "is_two_steps": "requestRevokeHarnessGrant" in html
            and "harnessGrantConfirm = String(grantId || \"\");" in function_body(
                html, "requestRevokeHarnessGrant"
            ),
            "can_be_kept": "harnessGrantConfirm = \"\";" in cancel,
            "confirms_in_place": 'class="perm-confirm" role="alert"' in surface,
            "names_the_denial_rule": "a <strong>revocation rule</strong> refuses" in surface,
            "names_unrecoverability": "Neither can be undone from here." in surface,
            "names_contained_processes": "matching running commands are stopped too" in surface,
            "uses_the_existing_route": "grants/revoke`" in revoke,
            "is_a_post": 'method: "POST"' in revoke,
            "sends_the_grant_and_a_reason": 'grant_id: grantId, reason: "operator_revoked"' in revoke,
            "blocks_the_scope": "block_scope: true" in revoke,
            "reports_what_was_stopped": "contained_processes" in revoke,
            "a_failed_revoke_is_said": "Revocation failed:" in revoke,
            "keeps_the_session_it_asked_about": "if (activeHarnessId !== sessionId) return null;" in revoke,
            "refreshes_both_views": "await refreshHarnessGrants();" in revoke
            and 'harnessResyncFromSnapshot("grant_revoked")' in revoke,
            "disables_the_button_while_it_runs": "harnessGrantBusy === String(grant.grant_id)" in surface,
        },
        "styles": {
            "card": ".h-approval {" in html,
            "card_decided": '.h-approval[data-live="false"]' in html,
            "card_head": ".h-approval-head {" in html,
            "card_consequence": ".h-approval-consequence {" in html,
            "card_actions": ".h-approval-actions {" in html,
            "card_facts": ".h-approval-facts {" in html,
            "countdown": ".h-approval-countdown {" in html,
            "badge": ".harness-perm-badge {" in html,
            "badge_empty": '.harness-perm-badge[data-count="0"]' in html,
            "badge_held": '.harness-perm-badge[data-count]:not([data-count="0"])' in html,
            "summary": ".perm-summary {" in html,
            "card_row": ".perm-card {" in html,
            "card_revoked": '.perm-card[data-status="revoked"]' in html,
            "card_expired": '.perm-card[data-status="expired"]' in html,
            "facts": ".perm-facts {" in html,
            "confirm": ".perm-confirm {" in html,
            "section": ".perm-section {" in html,
            "wraps_long_paths": "overflow-wrap: anywhere" in html,
        },
    }


def read_server(server: str) -> dict:
    approve = python_function(server, "api_approve_harness_tool")
    persist = python_function(server, "_persist_approval_grant_outcome")
    return {
        "approve_contract": {
            "requires_a_session": "Approval is scoped to its originating session" in approve,
            "anything_else_is_a_refusal": 'final_decision = "approve" if decision == "approve" else "reject"'
            in approve,
            "replays_fail_closed": "re-submission (replay) rejected" in approve,
            "durable_decisions_fail_closed": "durable decision record" in approve,
            "unknown_is_404": "Approval request not found or already resolved." in approve,
            "ttl_is_bounded": "SG_MAX_GRANT_TTL_SECONDS" in approve,
            "returns_the_session": '"session_id": request_session' in approve,
        },
        "grant_persistence": {
            "records_a_grant_under_the_approval": "grant_id=approval_id," in persist,
            "one_use": "max_uses=1," in persist,
            "names_the_operator": 'issued_by="operator_approval"' in persist,
            "capabilities_from_the_tool": "scoped_grants.resolve_tool_capabilities(tool_name, args)" in persist,
            "scopes_from_the_tool": "scoped_grants.resolve_tool_resource_scopes(" in persist,
            "exact_arguments": "exact_args=args if isinstance(args, Mapping) else {}," in persist,
            "a_refusal_is_recorded_too": "store.record_rejected_approval(" in persist,
            "is_idempotent": "store.has_grant_or_tombstone(approval_id)" in persist,
            "defaults_to_the_store_ttl": "scrolled_grants" not in persist
            and "scoped_grants.SG_DEFAULT_GRANT_TTL_SECONDS" in persist,
        },
        "enforcement": {
            "denials_are_checked": server.count("check_revocation_denials(") >= 2,
            "a_denial_names_the_rule": "capability_revoked" in server,
            "grants_are_issued_and_revoked": "issue_grant(" in server and "revoke_grant(" in server,
        },
    }


def build_inventory() -> dict:
    import scoped_grants

    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    frontend = read_frontend(html)
    backend = read_server(server)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    for flag, description in (
        ("one_builder", "there is more than one approval card builder again"),
        ("no_second_headline_existed", "the second approval headline is back"),
        ("one_headline", "an approval can arrive under more than one headline"),
        ("no_hardcoded_policy_badge", 'an approval badge says "Standard Policy" again, whatever policy is in force'),
        ("every_path_draws_with_it", "a path that can raise an approval no longer draws it with the one card"),
        ("one_decision_control", "more than one control can decide an approval"),
        (
            "a_decided_card_is_redrawn_from_the_request",
            "a decided card is redrawn from the resolved event, which carries no tool and no arguments",
        ),
        ("the_request_is_filed_before_it_is_redrawn", "an approval is never filed, so its decided card loses what it was about"),
        ("arguments_survive_a_decision", "the arguments are gone from the card"),
    ):
        if not frontend["card"].get(flag):
            fail("card", description)

    permission = frontend["permission"]
    for flag, description in (
        ("names_the_policy", "the card does not name the policy that is asking"),
        ("policy_is_the_selects_own_label", "the card and the policy select can describe one policy two ways"),
        ("policy_comes_from_the_session_first", "the card names the control's policy rather than the session's"),
        ("names_the_tool", "the card does not name the tool"),
        ("shows_the_arguments", "the card does not show the arguments being approved"),
        ("states_the_capability", "the card does not state the capability the approval records"),
        ("states_the_resource", "the card does not state the resource scope the approval records"),
        ("states_the_limit", "the card does not state the one-use limit"),
        ("states_the_expiry_clock", "the card does not state when the record expires"),
        ("will_not_invent_a_scope", "the card invents a resource scope the server did not resolve"),
        ("the_mapping_is_read_from_a_table", "the capability map is not a table the inventory can check"),
        ("mcp_tools_resolve_first", "an MCP tool is shown with every capability instead of `external_mcp`"),
    ):
        if not permission.get(flag):
            fail("permission", description)

    if permission["ttl_seconds"] != scoped_grants.SG_DEFAULT_GRANT_TTL_SECONDS:
        fail(
            "ttl-drift",
            f"the card promises {permission['ttl_seconds']}s but the store issues "
            f"{scoped_grants.SG_DEFAULT_GRANT_TTL_SECONDS}s",
        )

    mapping = permission["capability_map"]
    if not mapping:
        fail("capability-map", "the client's tool -> capability table cannot be read")
    else:
        for tool, caps in sorted(mapping.items()):
            expected = list(scoped_grants.resolve_tool_capabilities(tool, {}))
            if list(caps) != expected:
                fail(
                    "capability-map",
                    f"{tool} is shown as {caps} but scoped_grants resolves {expected}",
                )
        fallback = list(
            json.loads("[" + (frontend["permission"]["fallback"] or "").replace("'", '"') + "]")
        )
        for probe in ("a_tool_the_pane_has_never_seen", "no_such_tool"):
            expected = list(scoped_grants.resolve_tool_capabilities(probe, {}))
            if mapping.get(probe, fallback) != expected:
                fail(
                    "capability-fallback",
                    f"a tool with no entry is shown as {mapping.get(probe, fallback)} but "
                    f"scoped_grants resolves {expected}",
                )
                break


    for flag, description in (
        ("accepts_a_session", "the decision no longer takes the session it belongs to"),
        ("falls_back_to_the_active_session", "a decision with no session falls through instead of using the selected one"),
        ("sends_it", "the decision is posted without a session id"),
        ("the_card_carries_one", "a card does not carry the session its approval belongs to"),
        ("workflow_cards_use_their_own", "a workflow card uses the selected session instead of its own"),
        ("a_decided_card_keeps_its_session", "a redrawn card loses the session it belonged to"),
    ):
        if not frontend["session"].get(flag):
            fail("session", description)

    for flag, description in (
        ("counts_from_the_request", "the expiry is not counted from the request"),
        ("states_the_remaining_time", "the card does not say how long the offer stands"),
        ("countdown_ticks", "the countdown never moves"),
        ("countdown_updates_the_lines", "the countdown does not update the lines it is on"),
        ("countdown_stops", "the countdown runs after the last live card is gone"),
        ("an_expired_offer_says_so", "an expired offer still reads like a live one"),
        ("the_expired_card_says_what_to_do", "an expired card does not say what to do instead"),
        ("an_undecided_card_is_not_an_approval", "an undecided request is presented as if it was refused"),
        ("the_state_badge_states_expiry", "the card's own state no longer distinguishes expiry"),
    ):
        if not frontend["expiry"].get(flag):
            fail("expiry", description)

    for flag, description in (
        ("says_what_rejecting_does", "the card does not say what refusing does instead"),
        ("says_it_is_not_a_standing_allow", "the card leaves a reader thinking the approval allows the tool"),
        ("says_the_turn_waits", "the card does not say the run is waiting on the decision"),
        ("a_decided_card_says_the_permission_was_used", "an approved card does not say the permission was spent"),
    ):
        if not frontend["refusal"].get(flag):
            fail("refusal", description)

    surface = frontend["surface"]
    for flag, description in (
        ("dialog_exists", "the permissions surface is gone"),
        ("is_a_dialog", "the permissions surface is not a dialog"),
        ("is_named", "the permissions dialog has no accessible name"),
        ("closes_on_the_backdrop", "the permissions dialog cannot be dismissed by its backdrop"),
        ("opens_through_the_controller", "the permissions dialog does not go through openOverlay()"),
        ("closes_through_the_controller", "the permissions dialog does not go through closeOverlay()"),
        ("reachable_from_the_badge", "nothing on the pane opens the permissions surface"),
        ("reachable_from_the_session_actions", "the session actions do not offer the permissions surface"),
        ("refuses_without_a_session", "the surface opens with no session to describe"),
        ("reads_the_grants_route", "the surface does not read the session's grants"),
        ("states_the_policy_and_revision", "the surface does not say which policy revision issued what it shows"),
        ("states_what_is_held", "the surface does not say what the session holds"),
        ("states_the_rules", "the surface does not show the revocation rules"),
        ("states_the_history", "the surface does not show what a permission ended as"),
        ("states_the_approvals", "the surface does not show pending or expired approvals"),
        ("says_when_it_could_not_read", "a failed read is presented as an empty session"),
        ("keeps_the_session_it_asked_about", "a slow read can render another session's permissions"),
        ("refreshed_on_selection", "the badge is stale after selecting a session"),
        ("refreshed_after_a_decision", "the badge is stale after a decision"),
        ("refreshed_on_a_new_draft", "a new draft inherits the previous session's permissions"),
        ("badge_states_a_count", "the badge carries no count"),
        ("badge_is_hidden_without_a_session", "the badge is drawn with no session"),
        ("badge_says_none_in_words", "the badge says nothing when the session holds nothing"),
        ("badge_names_what_it_opens", "the badge has no accessible name"),
        ("badge_does_not_reach_the_network", "the badge touches the network"),
        ("finding_an_approval_card", "the surface cannot point at the card for a pending approval"),
        ("the_permission_is_a_dl", "the card's permission is not stated as facts"),
    ):
        if not surface.get(flag):
            fail("surface", description)
    for name, present in surface["lists"].items():
        if not present:
            fail("surface", f"{name} is gone from the permissions dialog")

    for flag, description in (
        ("is_two_steps", "a permission can be revoked in one click"),
        ("can_be_kept", "the revocation confirmation cannot be dismissed"),
        ("confirms_in_place", "the revocation confirmation is not drawn on the card"),
        ("names_the_denial_rule", "the confirmation does not say that a revocation rule outlives the grant"),
        ("names_unrecoverability", "the confirmation does not say the rule cannot be undone from here"),
        ("names_contained_processes", "the confirmation does not say running commands are stopped"),
        ("uses_the_existing_route", "revocation does not go through the grants route"),
        ("is_a_post", "revocation is not a POST"),
        ("sends_the_grant_and_a_reason", "revocation does not name the grant and a reason"),
        ("blocks_the_scope", "revocation does not ask for the scope to be blocked"),
        ("reports_what_was_stopped", "revocation does not report what it stopped"),
        ("a_failed_revoke_is_said", "a failed revocation is silent"),
        ("keeps_the_session_it_asked_about", "a slow revocation can render another session"),
        ("refreshes_both_views", "revocation leaves the timeline and the surface disagreeing"),
        ("disables_the_button_while_it_runs", "the revoke button can be pressed twice"),
    ):
        if not frontend["revocation"].get(flag):
            fail("revocation", description)

    for section in ("approve_contract", "grant_persistence", "enforcement"):
        for flag, description in {
            "requires_a_session": "the approve route no longer requires the originating session",
            "anything_else_is_a_refusal": "the approve route can be talked into something other than a decision",
            "replays_fail_closed": "in-memory replay protection is gone",
            "durable_decisions_fail_closed": "durable replay protection is gone",
            "unknown_is_404": "an unknown approval no longer fails closed",
            "ttl_is_bounded": "the approval ttl is unbounded",
            "returns_the_session": "the approve route does not report the session it decided in",
            "records_a_grant_under_the_approval": "an approval no longer records a grant under its own id",
            "one_use": "an approval now records a standing permission instead of one use",
            "names_the_operator": "an approval grant no longer records who issued it",
            "capabilities_from_the_tool": "an approval grant's capabilities are no longer derived from the tool",
            "scopes_from_the_tool": "an approval grant's resource scopes are no longer derived from the call",
            "exact_arguments": "an approval grant is no longer bound to the exact arguments",
            "a_refusal_is_recorded_too": "a refusal is no longer recorded",
            "is_idempotent": "an approval can record a second grant for one decision",
            "defaults_to_the_store_ttl": "the approval grant's default ttl is no longer the store's",
            "denials_are_checked": "nothing enforces revocation denials any more",
            "a_denial_names_the_rule": "a denial no longer names what refused it",
            "grants_are_issued_and_revoked": "the grant store's issue or revoke path is gone",
        }.items():
            if flag in backend[section] and not backend[section][flag]:
                fail(section, description)

    for flag, description in (
        ("card", "the approval card has no rule"),
        ("card_decided", "a decided card has no rule"),
        ("card_head", "the approval card's head has no rule"),
        ("card_consequence", "the consequence sentence has no rule"),
        ("card_actions", "the approval card's controls have no rule"),
        ("card_facts", "the permission facts have no rule"),
        ("countdown", "the countdown has no rule"),
        ("badge", "the permission badge has no rule"),
        ("badge_empty", "an empty badge has no tone"),
        ("badge_held", "a held permission has no tone"),
        ("summary", "the surface's summary has no rule"),
        ("card_row", "a permission row has no rule"),
        ("card_revoked", "a revocation rule row has no tone"),
        ("card_expired", "an expired row has no tone"),
        ("facts", "a permission's facts have no rule"),
        ("confirm", "the revocation confirmation has no rule"),
        ("section", "the surface's sections have no rule"),
        ("wraps_long_paths", "a long path in a permission has nothing to wrap it"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    routes = []
    for method, path in ROUTES:
        pattern = rf'@app\.{method.lower()}\("\{re.escape(path).replace(chr(92) + "{", chr(92) + "{")}"\)'
        mounted = bool(re.search(pattern, server))
        routes.append({"method": method, "path": path, "mounted": mounted})
        if not mounted:
            fail("route-missing", f"{method} {path} is no longer mounted")

    mounted = mounted_routes(server)
    called = called_routes(html)
    unmounted = [
        path for path in called if not any(route_matches(path, mount) for mount in mounted)
    ]
    for path in unmounted:
        fail("unmounted-call", f"the pane calls {path}, which the server does not mount")

    for invented in ("/api/harness/permissions", "/api/harness/grants", "/api/harness/approvals"):
        if invented in html or invented in server:
            fail("invented-route", f"the story added {invented}; the session-scoped grants routes already existed")

    totals = {
        "checks": sum(
            len(frontend[key])
            for key in ("card", "permission", "session", "expiry", "refusal", "surface", "revocation", "styles")
        )
        + sum(len(backend[key]) for key in ("approve_contract", "grant_persistence", "enforcement")),
        "functions": len(FUNCTIONS),
        "card_paths": len(CARD_PATHS),
        "surface_lists": len(SURFACE_LISTS),
        "capability_map_tools": len(mapping),
        "routes": len(routes),
        "routes_called": len(called),
        "routes_unmounted": len(unmounted),
        "findings": len(findings),
    }

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P3-03",
        "statement": (
            "An approval in the Harness is a permission, and the pane now says so before the click: one card, "
            "drawn by every path that can raise one, naming the policy that is asking, the tool and its exact "
            "arguments, the capability and resource scope the decision records, the one-use limit and the "
            "minute it expires; the decision is sent with the session that owns the approval, not the one on "
            "screen; refusing states what it does instead; an offer that is never decided expires in words and "
            "says to re-run the turn; and the session's permissions are inspectable and revocable from a "
            "surface that lists what is held, the revocation rules that refuse matching actions, the history, "
            "and the approvals still waiting - with a two-step confirmation that says a revocation rule "
            "outlives the grant it came from."
        ),
        "source": {"frontend": "static/index.html", "server": "app.py"},
        "routes": routes,
        "frontend": frontend,
        "server": backend,
        "totals": totals,
        "findings": findings,
        "limits": [
            "the card states the capability and the resource argument it can see; the server canonicalises "
            "the scope (`path:<worktree>:<rel>`) when it issues the grant, so the surface shows the canonical "
            "string and the card shows the argument while saying where it will be resolved - the pane never "
            "invents a scope it did not receive",
            "the capability table is a copy of `scoped_grants.resolve_tool_capabilities()` and this inventory "
            "imports the real function and fails on any disagreement, so the copy is checked rather than trusted",
            "the ten-minute figure is `SG_DEFAULT_GRANT_TTL_SECONDS`, the default the approve route uses when "
            "the request carries no `ttl_seconds`; the pane does not ask for a different one, because a "
            "per-tool TTL chosen in the browser would be a second policy",
            "the countdown is drawn from the browser's clock against the request's timestamp, so a skewed "
            "client shows a skewed countdown; the expiry that matters is the store's `expires_at`, which the "
            "surface prints from the record",
            "revocation is offered per grant. Revoking by capability or scope without a grant is a route the "
            "pane already had and this story does not expose, because a scope-wide rule created by accident "
            "would refuse actions no card on screen named",
            "a revocation rule cannot be undone from the pane: the store keeps rules for the life of the "
            "session, and re-issuing the same capability does not clear the rule - the confirmation says so "
            "rather than offering an undo the server does not have",
            "the surface lists revocations and expired approvals from the session's own grants store and its "
            "approval state; it is not a global audit view, and does not read another session's permissions",
            "the walk's approvals are seeded by `scripts/harness_permissions_seed.py`, not produced by a turn: "
            "the demo has no model runtime, and no route mints a pending approval, so the fixture calls the "
            "app's own `_create_pending_tool_approval()` and lets one slot lapse the way the runtime does. The "
            "approvals, the grants and the events are the server's own records; only the arrival is staged",
        ],
    }


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    frontend = payload["frontend"]
    checks = ", ".join(
        f"{name} {len(frontend[name])}"
        for name in ("card", "permission", "session", "expiry", "refusal", "surface", "revocation", "styles")
    )
    lines = [
        "# Harness approvals are permissions (UIUX-P3-03)",
        "",
        "> Generated by `scripts/uiux_harness_permissions_inventory.py`. Do not edit by hand: change the pane "
        "and regenerate.",
        "",
        payload["statement"],
        "",
        "## What an approval actually is",
        "",
        "`POST /api/harness/approve` resolves a pending gate. When the decision is `approve`,",
        "`_persist_approval_grant_outcome()` records a scoped grant in the same store the grants API reads:",
        "",
        "| Field | Value |",
        "|---|---|",
        "| `grant_id` | the approval id itself |",
        "| `max_uses` | `1` |",
        "| `exact_args` | the arguments that were approved |",
        "| `capabilities` | `scoped_grants.resolve_tool_capabilities(tool, args)` |",
        "| `resource_scopes` | `scoped_grants.resolve_tool_resource_scopes(tool, args, workspace_root=…)` |",
        "| `ttl_seconds` | `SG_DEFAULT_GRANT_TTL_SECONDS` (600) unless the request asked otherwise |",
        "| `issued_by` | `operator_approval` |",
        "",
        "A refusal records the opposite: a tombstone for the same id, so the decision cannot be replayed. This",
        "story changed none of that. What it changed is that the pane says it.",
        "",
        "## One card",
        "",
        "Four paths could raise an approval, and each drew its own card. Three printed the same hardcoded",
        '`Standard Policy` badge whatever policy was in force, and the streamed card sent `activeHarnessId` as',
        "the session while the workflow and GSD cards sent `d.session_id` - so a card left over from another",
        "session could post a decision the server answers with 403.",
        "",
        f"`{CARD}()` is now the only builder, and {totals['card_paths']} paths draw with it. It states, in order:",
        "",
        "```",
        "⚠ Approval required: run_bash                              step 4  [Standard (Ask before Bash)]  [Awaiting decision]",
        "  {\"command\": \"pytest -q tests/test_store.py\"}",
        "  Capability   code_execution, workspace_read, workspace_write",
        "  Resource     this session's worktree, and nothing outside it",
        "  Limit        one use · 10 minutes after you decide",
        "  Approving runs run_bash once and records a permission for this exact call: one use, valid until",
        "  04:58 (expires in 9 min). It is not a standing allow for run_bash. Rejecting refuses this call and",
        "  records the decision, and the turn waits.",
        "  [✓ Approve & run once] [✕ Reject] [ note… ]",
        "```",
        "",
        "The policy name is the option's own label from `#harnessPolicy`, so the card and the select cannot",
        "describe one policy two ways. The capability line is the copy of the server's mapping that this",
        f"inventory checks against `scoped_grants` for all {totals['capability_map_tools']} tools it lists, plus the fallback a tool",
        "with no entry gets. The resource line names the path argument if there is one and says where the",
        "server will resolve it - the pane does not invent a canonical scope it cannot compute.",
        "",
        "A decided card is redrawn from the request the pane filed when the approval first arrived, because the",
        "resolved event carries only `{approval_id, decision, note, step}`: redrawing from that alone would",
        "erase the tool and the arguments from the record of the decision.",
        "",
        "## Expiry",
        "",
        "An approval nobody answers does not stay offered for ever: the slot expires and the run it belonged",
        "to is over. The card counts down from the request (`expires in 9 min`, then `expires in 40 s`), and",
        "when the offer is gone the card says so and says what to do: re-run the turn to reach the gate again.",
        "An undecided request is never drawn as a refusal, because nothing was refused.",
        "",
        "## The permissions surface",
        "",
        "`#harnessPermissionBadge` in the session strip carries the count of permissions the session holds and",
        "opens `#harnessGrantsModal`, which is also the `Permissions & Revocations` session action. It lists:",
        "",
        "| Section | What it says |",
        "|---|---|",
        "| summary | how many permissions are held, with the policy and revision that govern them |",
        "| Active | one row per grant: tool, capability, resource, limit, expiry, `issued_by`, note |",
        "| Revocation rules | what refuses matching actions, for what scope, and why |",
        "| History | grants that were consumed, expired or revoked, and the reason |",
        "| Approvals | approvals still waiting (with a link to the card) and offers that expired |",
        "",
        "A failed read says it failed instead of drawing an empty session, and a read that finishes after the",
        "reader has selected another session is dropped.",
        "",
        "## Revocation is two steps, and it is not an undo",
        "",
        "Revoking a permission does two things: the grant stops, and a revocation rule is created that refuses",
        "matching capabilities on overlapping scopes for the rest of the session. `check_revocation_denials()`",
        "is what enforces it, so **re-issuing the same capability does not clear the rule**. The confirmation",
        "says exactly that, names the capability and scope it will block, says matching running commands are",
        "stopped too, and says neither can be undone from here - then calls the route that already existed:",
        "",
        "```js",
        "POST /api/harness/sessions/<id>/grants/revoke",
        "{ grant_id, reason: \"operator_revoked\", block_scope: true }",
        "```",
        "",
        "It reports how many processes were contained, then refreshes both the surface and the trajectory, so",
        "the `security/grant_revoked` event the server appended is drawn rather than implied.",
        "",
        "## What this story had to preserve",
        "",
        "The approve contract is untouched: a decision other than `approve` is a refusal, a missing or",
        "mismatched `session_id` is a 403, replays are 409 from both the in-memory tombstone and the",
        "durable record, an unknown or settled id is a 404, and `ttl_seconds` is bounded by",
        "`SG_MAX_GRANT_TTL_SECONDS`. Grant persistence keeps its one-use, exact-argument, `operator_approval`",
        "shape, keeps the idempotence check that stops a second grant for one decision, and keeps recording",
        "refusals as tombstones.",
        "",
        "| Preserved rule | Where it still lives |",
        "|---|---|",
        "| a decision is bound to its session | `api_approve_harness_tool` (403) |",
        "| only `approve` approves | `api_approve_harness_tool` (`final_decision`) |",
        "| a replay fails closed | memory tombstone and durable `approval/decision` (409) |",
        "| the ttl is bounded | `SG_MAX_GRANT_TTL_SECONDS` (400) |",
        "| an approval records one use of the exact call | `_persist_approval_grant_outcome` |",
        "| a refusal is recorded | `record_rejected_approval` |",
        "| one decision, one grant | `has_grant_or_tombstone` |",
        "| a revocation rule refuses matching actions | `check_revocation_denials` |",
        "",
        "The four routes the surface uses all predate this story; it added no endpoint, and the inventory fails",
        "if a permissions route appears that the server does not mount.",
        "",
        "## Checks and limits",
        "",
        f"`--check` re-derives {totals['checks']} checks from the source ({checks}, and the server's own contract) and",
        "fails on any of them. `node scripts/harness_permissions_walk.mjs` drives the same surface in a real",
        "browser: no route mints a pending approval, so it starts `scripts/harness_permissions_seed.py`, which",
        "boots the ordinary isolated demo environment and gives it two live gates and one expired offer through",
        "the app's own `_create_pending_tool_approval()`. The walk then reads the card, refuses one call,",
        "approves the other, opens the surface, revokes what the approval recorded, and photographs the four",
        "states in [`media/harness-permissions/`](media/harness-permissions/README.md).",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html`.",
        "2. `python3 scripts/uiux_harness_permissions_inventory.py`.",
        "3. `node scripts/harness_permissions_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a rule broke")
    parser.add_argument("--json", action="store_true", help="print the inventory")
    args = parser.parse_args(argv)

    payload = build_inventory()
    text = dumps(payload)
    doc = render_doc(payload)

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        problems = []
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != text:
            problems.append(f"{JSON_PATH.relative_to(PROJECT_ROOT)} is stale")
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != doc:
            problems.append(f"{DOC_PATH.relative_to(PROJECT_ROOT)} is stale")
        if problems:
            print("error: the Harness permissions inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_harness_permissions_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the Harness's approval safety disagrees with the source:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"harness permissions OK: {payload['totals']['checks']} checks, "
            f"{payload['totals']['capability_map_tools']} capability mappings, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['checks']} checks, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
