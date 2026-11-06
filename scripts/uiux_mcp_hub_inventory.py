#!/usr/bin/env python3
"""Inventory the MCP hub's catalog, connected-server and activity contract (UIV2-21).

    python3 scripts/uiux_mcp_hub_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_mcp_hub_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_mcp_hub_inventory.py --json     # print the inventory

The MCP surface could already connect a remote HTTP server, a local stdio command and a pasted
`mcpServers` JSON block. What it could not do was let anyone *look something up*: presets were two
rows of chips that gave a name and nothing else, and presets, the whole add/edit form, the importer,
every configured server with its discovered tools and a playground all sat in one column. The first
thing on screen was configuration; the tools you had connected were below the fold, and a preset
chip told you neither whether it was local or remote, nor whether it needed a key, nor which
approval policy it would run under.

What this inventory checks: that the dialog is a hub with a three-tab tablist and exactly one
section on screen; that a preset card states its transport, its trust level, its key requirement,
its approval policy and the endpoint or command it would connect to, and that the trust level is
derived from the stored record rather than invented; that search narrows the cards, the filter chips
count what they would show and a search with no hit offers to clear itself; that the editor is a
closed disclosure which a card opens for you; that the connected rows and the Activity pane report
status and sync errors, that a test call is recorded with its outcome and latency, that long output
scrolls in its own box, and that a stored secret is only ever rendered as the mask the server sends.

It also checks the things this story may not break: the six `/api/mcp/*` routes, the stdio launch
gate, all three approval policies, every control the old dialog had, and the six existing tabs.

It fails when a card can describe a server without saying how it is reached, when the configuration
form and the catalog can be on screen at once, when an error stops being visible, when a secret
becomes renderable, or when the story changes an authorization boundary to make its own evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK_PATH = PROJECT_ROOT / "scripts" / "mcp_hub_walk.mjs"
FIXTURE_PATH = PROJECT_ROOT / "scripts" / "mcp_hub_fixture_server.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_MCP_Hub.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-1-mcp-hub.md"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "mcp-hub"

STORY = "UIV2-21"
TITLE = "MCP hub: searchable catalog and connected-server dashboard"

#: The sections the ledger's visual matrix names.
STATES = ("catalog", "search", "connected", "secrets", "activity", "loading", "long", "error")

#: Controls the old single-scroll dialog had. The hub may move them, never drop them.
PRESERVED_CONTROLS = (
    "mcpAlias",
    "mcpName",
    "mcpTransport",
    "mcpApprovalPolicy",
    "mcpAuthHeader",
    "mcpUrl",
    "mcpToken",
    "mcpHeadersJson",
    "mcpCommand",
    "mcpArgs",
    "mcpEnvJson",
    "mcpPresetNote",
    "mcpSaveBtn",
    "mcpImportJsonInput",
    "configuredMcpServersList",
    "mcpPlayServer",
    "mcpPlayTool",
    "mcpPlayArgs",
    "mcpPlayBtn",
    "mcpPlayOutput",
)

#: The API surface the hub talks to; this story adds no endpoint.
MCP_ROUTES = (
    '@app.get("/api/mcp/servers")',
    '@app.post("/api/mcp/servers")',
    '@app.post("/api/mcp/import-json")',
    '@app.delete("/api/mcp/servers/{alias}")',
    '@app.post("/api/mcp/servers/{alias}/sync")',
    '@app.post("/api/mcp/servers/{alias}/call")',
)

#: The five workspace panes that must survive the change (Compare rides inside Council).
WORKSPACE_TABS = ("start", "harness", "hall", "council", "debate", "chat")


def function_body(source: str, name: str) -> str:
    """The body of a top-level `function name(...) { … }`, braces balanced."""
    start = source.find(f"function {name}(")
    if start < 0:
        return ""
    paren = source.index("(", start)
    depth_params = 0
    for position in range(paren, len(source)):
        if source[position] == "(":
            depth_params += 1
        elif source[position] == ")":
            depth_params -= 1
            if depth_params == 0:
                paren = position
                break
    index = source.index("{", paren)
    depth = 0
    for position in range(index, len(source)):
        char = source[position]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index : position + 1]
    return ""


def block(source: str, start_marker: str, end_marker: str) -> str:
    """The slice between two markers, or an empty string."""
    start = source.find(start_marker)
    if start < 0:
        return ""
    end = source.find(end_marker, start)
    return source[start : end if end > 0 else len(source)]


def build_inventory() -> dict[str, Any]:
    html = FRONTEND.read_text(encoding="utf-8")
    script = html[html.index("<script>") :]
    app = APP.read_text(encoding="utf-8")

    modal = block(html, '<div id="mcpModal"', "<!-- ================= EXTERNAL HARNESSES")
    card = function_body(script, "mcpCatalogCardHtml")
    trust = function_body(script, "mcpPresetTrust")
    matches = function_body(script, "mcpPresetMatches")
    render_hub = function_body(script, "renderMcpHub")
    render_activity = function_body(script, "renderMcpActivity")
    apply_search = function_body(script, "applyMcpHubSearch")
    switch_tab = function_body(script, "switchMcpTab")
    keydown = function_body(script, "mcpTabKeydown")
    bind_tabs = function_body(script, "bindMcpTabs")
    record_activity = function_body(script, "recordMcpActivity")
    render_ui = function_body(script, "renderMcpUI")
    playground = function_body(script, "runMcpPlayground")
    select_preset = function_body(script, "selectMcpPreset")
    editor_open = function_body(script, "openMcpEditor")
    save = function_body(script, "saveMcpServer")
    editor_state = function_body(script, "syncMcpEditorState")

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # ── the hub shell ────────────────────────────────────────────────────────
    tab_ids = re.findall(r'id="mcpTab(\w+)" role="tab"', modal)
    panel_ids = re.findall(r'id="mcpPanel(\w+)"', modal)
    check(
        "the dialog is a hub: one tablist with three tabs",
        'role="tablist"' in modal
        and tab_ids == ["Catalog", "Connected", "Activity"]
        and modal.count('role="tab"') == 3,
        f"tabs: {', '.join(tab_ids) or 'none'}",
    )
    check(
        "each tab controls its own panel, and the panels are tabpanels",
        panel_ids == ["Catalog", "Connected", "Activity"]
        and modal.count('role="tabpanel"') == 3
        and all(f'aria-controls="mcpPanel{name}"' in modal for name in tab_ids)
        and all(f'aria-labelledby="mcpTab{name}"' in modal for name in panel_ids),
        f"panels: {', '.join(panel_ids) or 'none'} · aria-controls and aria-labelledby wired both ways",
    )
    check(
        "the landing tab is the catalog, not the configuration form",
        'id="mcpTabCatalog" role="tab" aria-selected="true"' in modal
        and 'id="mcpPanelConnected" class="mcp-hub-panel" role="tabpanel" aria-labelledby="mcpTabConnected" tabindex="0" hidden' in modal,
        "Catalog is selected and Connected starts hidden",
    )
    check(
        "switching a tab shows exactly one panel and moves the roving tabindex",
        "panel.hidden = panel.id !==" in switch_tab
        and "btn.tabIndex = on ? 0 : -1" in switch_tab
        and 'btn.setAttribute("aria-selected"' in switch_tab,
        "switchMcpTab() hides every other panel, sets aria-selected and a single tab stop",
    )
    check(
        "the tab strip answers the arrow keys, Home and End",
        all(key in keydown for key in ("ArrowLeft", "ArrowRight", "Home", "End"))
        and "switchMcpTab(target.dataset.mcpTab)" in keydown
        and "bindMcpTabs()" in function_body(script, "openMcpModal")
        and 'addEventListener("keydown"' in bind_tabs
        and 'onkeydown="' not in modal,
        "one delegated listener bound when the hub opens; no inline key handler in the markup",
    )
    check(
        "the dialog is a labelled modal",
        'class="modal mcp-hub" role="dialog" aria-modal="true" aria-labelledby="mcpHubTitle"' in modal,
        "role=dialog, aria-modal, labelled by the hub title",
    )

    # ── search and filter ────────────────────────────────────────────────────
    check(
        "the hub has one search box that names what it searches",
        'id="mcpHubSearch"' in modal
        and 'aria-label="Search MCP presets, connected servers and tools"' in modal
        and 'oninput="renderMcpHub()"' in modal,
        "type=search, labelled, re-renders the hub on input",
    )
    check(
        "the catalog filters count what they would show",
        'const count = f.id === "all" ? entries.length : entries.filter' in render_hub
        and 'aria-pressed="${on}"' in render_hub
        and "Needs a key" in script,
        "each chip is a toggle with a count and aria-pressed",
    )
    check(
        "search narrows the cards and hides the rows that do not match",
        "haystack.includes(term)" in matches
        and "row.hidden = !hit" in apply_search
        and 'document.querySelectorAll("#mcpModal .mcp-hub-panel [data-mcp-search]")' in apply_search,
        "applyMcpHubSearch() hides every row whose haystack misses the term",
    )
    check(
        "a search with no hit says so and offers to clear itself",
        "No preset matches" in render_hub and "resetMcpHubSearch()" in render_hub,
        "the empty state names the term and offers a Clear search action",
    )

    # ── the catalog card ─────────────────────────────────────────────────────
    check(
        "a preset is a card, not a chip with a name",
        '<article class="mcp-card' in card and "mcp-card-name" in card and "mcp-card-target" in card,
        "mcpCatalogCardHtml() renders a named card with its target",
    )
    check(
        "every card states its transport, trust, key requirement and policy",
        'mcp-pill ${remote ? "remote" : "local"}' in card
        and "${escapeHtml(trust.label)}" in card
        and "${escapeHtml(auth)}" in card
        and "${escapeHtml(mcpPolicyLabel(preset.approval_policy))}" in card,
        "remote/local · trust · key requirement · approval policy on every card",
    )
    check(
        "the transport says remote HTTP or local stdio in words",
        '"Remote · HTTP" : "Local · stdio"' in card,
        "a pill, not a colour or an icon alone",
    )
    check(
        "the key requirement names the variable or says no key is needed",
        '"No key needed"' in card and "Key: ${preset.env_key" in card,
        "either `No key needed` or the exact environment variable",
    )
    check(
        "trust is derived from the stored preset, never invented",
        "preset.requires_token" in trust
        and "preset.approval_policy" in trust
        and "return { cls: \"trusted\"" in trust
        and "preset.trust" not in trust,
        "mcpPresetTrust() reads requires_token and approval_policy only",
    )
    check(
        "the card says whether it is connected and what its button will do",
        "configured ? \"Connected\" : \"Not connected\"" in card
        and "configured ? \"Review\" : \"Use preset\"" in card,
        "state line plus a Use preset / Review action",
    )
    check(
        "cards show the endpoint or the command they would run",
        "preset.url" in card and "preset.command" in card,
        "remote cards print the URL, stdio cards print command and arguments",
    )

    # ── the connected section ────────────────────────────────────────────────
    check(
        "the configuration form is a shut disclosure, not the first thing shown",
        'id="mcpEditor"' in modal
        and modal.count('id="mcpEditor"') == 1
        and 'class="mcp-hub-card mcp-editor"' in modal
        and "<details" in modal
        and 'id="mcpEditor" open' not in modal,
        "the editor is a closed <details> with a summary that says what it is",
    )
    check(
        "a card opens the editor for you and lands on the Connected tab",
        "openMcpEditor()" in select_preset and "openMcpEditor()" in function_body(script, "editMcpServer")
        and 'editor.open = true' in editor_open
        and 'switchMcpTab("connected")' in editor_open,
        "selectMcpPreset() and editMcpServer() both open the editor on Connected",
    )
    check(
        "the editor describes what is actually in it, not the card that filled it",
        "function syncMcpEditorState(" in script
        and 'oninput="syncMcpEditorState()"' in modal
        and "Prefilled from the ${preset.name} card" in editor_state
        and 'New server “${alias}”' in editor_state
        and "Editing ${alias} — the stored secret stays" in editor_state,
        "syncMcpEditorState() reads the alias and picks preset / editing / new",
    )
    check(
        "connected rows state transport, policy and their sync note",
        'data-mcp-search="${escapeHtml(searchKey)}"' in render_ui
        and "Sync note: ${escapeHtml(s.last_error)}" in render_ui
        and "mcp__${escapeHtml(s.alias)}__${escapeHtml(t.name)}" in render_ui,
        "each row carries its search key, its tools and the error it really got",
    )
    check(
        "the connected list explains itself when nothing is connected",
        "No MCP servers connected yet" in render_ui and "Catalog" in render_ui,
        "the empty state points at the Catalog tab",
    )
    check(
        "every control the old dialog had is still on the hub",
        all(f'id="{control}"' in modal for control in PRESERVED_CONTROLS),
        f"{len(PRESERVED_CONTROLS)} controls: "
        + ", ".join(control for control in PRESERVED_CONTROLS if f'id="{control}"' not in modal)
        or "all present",
    )
    check(
        "all three approval policies survive, on the form and on the rows",
        modal.count('value="always_ask"') >= 1
        and modal.count('value="inherit"') >= 1
        and modal.count('value="auto"') >= 1
        and "Always Ask" in render_ui,
        "auto / inherit / always_ask kept in the select and in the per-server row",
    )

    # ── activity ─────────────────────────────────────────────────────────────
    check(
        "Activity reports every server's health, not just the happy ones",
        'id="mcpActivityList"' in modal
        and "Sync note" in render_activity
        and "No token stored" in render_activity
        and "mcpPolicyLabel(s.approval_policy)" in render_activity,
        "state pill, policy, tool counts, token mask and the stored sync note per server",
    )
    check(
        "a test call is recorded with its outcome, its latency and a capped detail",
        playground.count("recordMcpActivity({") >= 2
        and "recordMcpActivity({ alias, tool, ok: !!d.ok, ms: d.elapsed_ms" in playground
        and "recordMcpActivity({ alias, tool, ok: false, ms: 0, detail: e.message })" in playground
        and "MCP_ACTIVITY_CAP" in script
        and ".slice(0, 160)" in playground,
        "both the success and the failure path record, and the detail is capped at 160 characters",
    )
    check(
        "the activity log is bounded and newest first",
        "mcpActivity.unshift(" in record_activity and "mcpActivity.length > MCP_ACTIVITY_CAP" in record_activity,
        f"unshift with a {12}-entry cap",
    )
    check(
        "the tab counts are computed from the state, not typed in",
        'catalogCount.textContent = entries.length' in render_hub
        and "connectedCount.textContent = mcpServers.length" in render_hub
        and "activityCount.textContent = mcpServers.length + mcpActivity.length" in render_hub,
        "catalog / connected / activity counts read the live arrays",
    )

    # ── secrets ──────────────────────────────────────────────────────────────
    secret_interpolation = [
        name
        for name, body in (
            ("mcpCatalogCardHtml", card),
            ("renderMcpUI", render_ui),
            ("renderMcpActivity", render_activity),
        )
        if re.search(r"\$\{[^}]*\b(auth_token|secret|api_key)\b[^}]*\}", body)
    ]
    check(
        "no renderer interpolates a secret into the page",
        not secret_interpolation,
        "renderers: " + (", ".join(secret_interpolation) or "only masked values"),
    )
    check(
        "the only credential the hub renders is the server's mask",
        "masked_token" in render_ui and "masked_token" in render_activity,
        "masked_token in the connected row and in Activity",
    )
    check(
        "saving empties the token field",
        'document.getElementById("mcpToken").value = ""' in save,
        "the typed secret does not stay in the DOM after a save",
    )
    check(
        "the served payload is what masks the secret, not the page",
        "def _mask_secret" in app and "masked_token" in app,
        "app.py sends masked_token; the hub never sees the raw value",
    )

    # ── what this story may not change ───────────────────────────────────────
    check(
        "the six MCP routes are unchanged and none were added",
        all(route in app for route in MCP_ROUTES)
        and len(re.findall(r'@app\.(?:get|post|delete)\("/api/mcp', app)) == len(MCP_ROUTES),
        f"{len(MCP_ROUTES)} routes, no additions",
    )
    check(
        "spawning a stdio command is still gated on the operator's opt-in",
        app.count("STUDIO_ALLOW_MCP_STDIO") >= 2
        and "MCP stdio process launch is disabled by default" in app,
        "STUDIO_ALLOW_MCP_STDIO is still required before a command is launched",
    )
    check(
        "the workspace still has all six tabs",
        all(f'id="pane-{tab}"' in html for tab in WORKSPACE_TABS),
        ", ".join(WORKSPACE_TABS),
    )
    check(
        "the top strip still shows the MCP pill row and its tool count",
        'id="activeMcpPills"' in html and 'id="mcpToolsCountBadge"' in html,
        "the strip is outside the dialog and untouched",
    )

    # ── the walk, the fixture and the evidence ───────────────────────────────
    walk = WALK_PATH.read_text(encoding="utf-8") if WALK_PATH.exists() else ""
    fixture = FIXTURE_PATH.read_text(encoding="utf-8") if FIXTURE_PATH.exists() else ""
    recorded = len(re.findall(r"\brecord\(", walk))
    check(
        "a browser walk asserts the hub on a screen",
        recorded >= 30
        and "mcp-hub-activity" in walk
        and "secretInDom" in walk
        and "switchMcpTab" in walk
        and "mcpCatalogGrid .mcp-card" in walk,
        f"{WALK_PATH.name}: {recorded} recorded checks",
    )
    check(
        "the walk covers every state the ledger asks for",
        all(state in walk for state in STATES),
        "STATES = " + ", ".join(STATES),
    )
    check(
        "the walk connects a real stdio server rather than inventing one",
        "mcp_hub_fixture_server.py" in walk
        and "tools/list" in fixture
        and "tools/call" in fixture
        and 'STUDIO_ALLOW_MCP_STDIO: "1"' in walk,
        "the fixture speaks the protocol; the fixture app opts into stdio launch explicitly",
    )
    check(
        "the walk refuses to photograph the hub with a fake secret",
        'const SECRET = "sk-uiv2-21-do-not-render-9f3c"' in walk and "secretInDom" in walk,
        "the secret is typed into the form and then searched for in the document",
    )
    captures = sorted(MEDIA_DIR.glob("*.png")) if MEDIA_DIR.exists() else []
    before = sorted((MEDIA_DIR / "before-uiv2-21").rglob("*.png")) if MEDIA_DIR.exists() else []
    check(
        "captures exist for every viewport and theme, with a before set",
        len(captures) >= 48 and len(before) >= 16,
        f"{len(captures)} after PNG, {len(before)} before PNG",
    )

    findings = [{"check": entry["check"], "detail": entry["detail"]} for entry in checks if not entry["passed"]]
    return {
        "story": STORY,
        "title": TITLE,
        "schema_version": 1,
        "source": str(FRONTEND.relative_to(PROJECT_ROOT)),
        "states": list(STATES),
        "sections": {
            "catalog": {
                "search": "mcpHubSearch",
                "filters": [entry for entry in ("All", "Remote", "Local", "Needs a key", "Connected")],
                "card": "mcpCatalogCardHtml",
                "trust": "mcpPresetTrust",
            },
            "connected": {
                "editor": "mcpEditor",
                "list": "configuredMcpServersList",
                "playground": "mcpPlayBtn",
            },
            "activity": {"health": "mcpActivityList", "calls": "mcpActivityCalls", "recorder": "recordMcpActivity"},
        },
        "controls": list(PRESERVED_CONTROLS),
        "routes": list(MCP_ROUTES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
    }


def render_doc(payload: dict[str, Any]) -> str:
    lines = [
        "# MCP hub: a catalog you can search, a dashboard you can read",
        "",
        f"Story **{payload['story']}** — {payload['title']}.",
        "",
        "| section | what it answers |",
        "| --- | --- |",
        "| Catalog | what could I connect? each card states remote HTTP / local stdio, trust, key",
        "| | requirement, approval policy and the endpoint or command it would use |",
        "| Connected | what is connected? the editor is shut until a card or Edit opens it; rows show",
        "| | transport, policy, tools and any stored sync note; the playground runs one live call |",
        "| Activity | what happened? per-server health with its error text, and every test call with",
        "| | its outcome and latency |",
        "",
        "Search and the filter chips work across all three sections; the configuration form is never",
        "on screen while you browse the catalog.",
        "",
        "## Checks",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} finding(s).",
        "",
        "| check | result | detail |",
        "| --- | --- | --- |",
    ]
    for entry in payload["checks"]:
        lines.append(f"| {entry['check']} | {'pass' if entry['passed'] else '**FAIL**'} | {entry['detail']} |")
    lines += [
        "",
        "## States",
        "",
        ", ".join(f"`{state}`" for state in payload["states"]) + ".",
        "",
        "## Regenerate",
        "",
        "```",
        "python3 scripts/uiux_mcp_hub_inventory.py",
        "python3 scripts/uiux_mcp_hub_inventory.py --check",
        "```",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale or a rule is broken")
    parser.add_argument("--json", action="store_true", help="print the inventory instead of writing it")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    payload = build_inventory()

    if args.json:
        print(dumps(payload), end="")
    else:
        rendered_json = dumps(payload)
        rendered_doc = render_doc(payload)
        stale = (
            not JSON_PATH.exists()
            or JSON_PATH.read_text(encoding="utf-8") != rendered_json
            or not DOC_PATH.exists()
            or DOC_PATH.read_text(encoding="utf-8") != rendered_doc
        )
        if args.check:
            if stale:
                print(
                    "the MCP hub inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    findings = payload["findings"]
    if findings:
        for entry in findings:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
