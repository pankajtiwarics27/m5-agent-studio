#!/usr/bin/env python3
"""Inventory the MCP tool browser, its risk language and its confirmed test call (UIV2-23).

    python3 scripts/uiux_mcp_tools_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_mcp_tools_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_mcp_tools_inventory.py --json     # print the inventory

UIV2-21 gave the MCP surface a hub and UIV2-22 gave connecting a server a guided path. The tools a
server actually returned were still a collapsed drawer on the Connected row: a checkbox, a name and
a one-line description, with no risk, no policy, no arguments and no way to try one. The playground
below it ran whatever was selected on one click.

What this inventory checks: that the hub's fourth tab says four different things — how many servers
are configured, how many tools were discovered, how many a model can call, and how many the
allowlist refuses; that every row states the risk of a call in words the server can justify (a
read only when the server says `readOnlyHint`, a plain write when it says nothing), what happens when
a model asks, and what arguments the tool takes; that the filter chips count what they would show
and that search reaches the list; that Allow/Deny on a row is the allowlist and not a decoration;
that a test call validates the arguments against the tool's schema before it posts, states the risk,
asks for consent on anything that is not a read, refuses to post without it, and reports the
server's 403 and 428 bodies as refusals with the next step; and that the result inspector says what
authorized the call instead of implying it.

It also checks what the story may not break: the six `/api/mcp/*` routes, the stdio launch gate, the
`auto` approval policy, action admission, and the absence of a React rewrite.

It fails when a tool can become "read-only" without the server saying so, when a write can be posted
without a confirmation for that call, when a refusal is drawn as a success, when the result's
authorization is not stated, or when the evidence the story claims is not on disk.
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
WALK_PATH = PROJECT_ROOT / "scripts" / "mcp_tools_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_MCP_Tools.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-3-mcp-tools.md"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "mcp-tools-uiv2-23"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"

STORY = "UIV2-23"
TITLE = "MCP tool discovery, approval and test-call experience"

#: The four numbers the Tools tab has to keep apart.
COUNTERS = (
    ("configured", "servers the operator added"),
    ("discovered", "tools the servers actually returned"),
    ("available", "tools a model may call right now"),
    ("denied", "tools the allowlist refuses"),
)

#: The four risks, and the only thing that can make a tool a read.
RISKS = (
    {"id": "read", "label": "Read-only", "because": "the server set annotations.readOnlyHint"},
    {"id": "write", "label": "Write", "because": "nothing said otherwise: the fail-closed default"},
    {"id": "write_idempotent", "label": "Write · repeatable", "because": "it carries an idempotency key"},
    {"id": "destructive", "label": "Destructive", "because": "the server set annotations.destructiveHint"},
)

#: The refusals the call surface answers with, and what each lets the page say.
DENIALS = (
    ("server_denied", "the server is switched off"),
    ("tool_denied", "the tool is outside the allowlist"),
    ("unknown_tool", "the server never returned that tool"),
    ("stdio_gate", "the local-command gate is closed"),
    ("approval_required", "the harness policy needs a human approval"),
    ("paused_idempotency", "an earlier write is unresolved"),
)

#: The states the walk photographs, per viewport and theme.
STATES = (
    "empty",
    "list",
    "filter",
    "search",
    "search-empty",
    "consent",
    "error",
    "loading",
    "result",
    "long",
    "denied",
    "refused",
    "recovery",
    "failure",
    "keyboard",
)

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")

#: The controls the old playground had. The new one may move them, never drop them.
PRESERVED_CONTROLS = (
    "mcpPlayServer",
    "mcpPlayTool",
    "mcpPlayArgs",
    "mcpPlayBtn",
    "mcpPlayOutput",
    "configuredMcpServersList",
    "mcpToolsCountBadge",
    "activeMcpPills",
    "mcpImportJsonInput",
    "mcpSaveBtn",
)

#: The controls this story adds.
TOOLS_CONTROLS = (
    "mcpTabTools",
    "mcpTabToolsCount",
    "mcpPanelTools",
    "mcpToolsSummary",
    "mcpToolFilters",
    "mcpToolHint",
    "mcpToolList",
    "mcpToolsEmpty",
    "mcpPlayRisk",
    "mcpPlaySchema",
    "mcpPlayArgsError",
    "mcpPlayConsent",
    "mcpPlayConsentBox",
    "mcpPlayConsentText",
    "mcpPlayResult",
    "mcpPlayMeta",
    "mcpPlayAuth",
    "mcpPlayMore",
    "mcpPlayRaw",
    "mcpPlayRawBody",
)

MCP_ROUTES = (
    '@app.get("/api/mcp/servers")',
    '@app.post("/api/mcp/servers")',
    '@app.post("/api/mcp/import-json")',
    '@app.delete("/api/mcp/servers/{alias}")',
    '@app.post("/api/mcp/servers/{alias}/sync")',
    '@app.post("/api/mcp/servers/{alias}/call")',
)


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


def async_body(source: str, name: str) -> str:
    start = source.find(f"async function {name}(")
    if start < 0:
        return function_body(source, name)
    return function_body(source[start:], name)


def pos(source: str, marker: str) -> int:
    """Where a marker sits, or -1 — a missing marker is a finding, not an exception."""
    return source.find(marker)


def block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    if start < 0:
        return ""
    end = source.find(end_marker, start)
    return source[start : end if end > 0 else len(source)]


def python_function(source: str, name: str) -> str:
    """A top-level `def name(...)` up to the next top-level def/decorator."""
    start = source.find(f"def {name}(")
    if start < 0:
        return ""
    rest = source[start + 4 :]
    ends = [index for index in (rest.find("\ndef "), rest.find("\n@app."), rest.find("\n#: ")) if index > 0]
    return source[start : start + 4 + (min(ends) if ends else len(rest))]


def build_inventory() -> dict[str, Any]:
    html = FRONTEND.read_text(encoding="utf-8")
    script = html[html.index("<script>") :]
    app = APP.read_text(encoding="utf-8")
    modal = block(html, 'id="mcpModal"', "EXTERNAL HARNESSES")
    panel = block(html, 'id="mcpPanelTools"', "TAB 4: ACTIVITY")
    playground = block(html, 'class="mcp-hub-card mcp-playground"', "TAB 3: TOOLS")

    render_tools = function_body(script, "renderMcpTools")
    row_html = function_body(script, "mcpToolRowHtml")
    risk_of = function_body(script, "mcpToolRisk")
    consequence = function_body(script, "mcpToolConsequence")
    policy_promise = function_body(script, "mcpPolicyPromise")
    render_hub = function_body(script, "renderMcpHub")
    switch_tab = function_body(script, "switchMcpTab")
    validate_args = function_body(script, "validateMcpPlayArgs")
    arg_problem = function_body(script, "mcpPlayArgProblem")
    missing_args = function_body(script, "mcpPlayMissingArgs")
    consent = function_body(script, "renderMcpPlayConsent")
    denial_help = function_body(script, "mcpDenialHelp")
    inspector = function_body(script, "renderMcpPlayResult")
    truncate = function_body(script, "mcpPlayTruncate")
    run_play = async_body(script, "runMcpPlayground")
    open_test = function_body(script, "openMcpToolTest")

    block_reason = python_function(app, "mcp_call_block_reason")
    denial_kind = python_function(app, "mcp_denial_kind")
    risk_level = python_function(app, "mcp_tool_risk_level")
    route = app[app.index('@app.post("/api/mcp/servers/{alias}/call")') :]
    route = route[: route.index("# ====") if "# ====" in route else 4000]

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # ── the tab ──────────────────────────────────────────────────────────────
    tab_ids = re.findall(r'id="mcpTab(\w+)" role="tab"', modal)
    check(
        "the hub's tab strip has a fourth tab — Tools — with a count of what it holds",
        tab_ids == ["Catalog", "Connected", "Tools", "Activity"]
        and 'id="mcpTabTools" role="tab"' in modal
        and 'aria-controls="mcpPanelTools"' in modal
        and 'id="mcpTabToolsCount"' in modal,
        f"tabs: {', '.join(tab_ids)}",
    )
    check(
        "the Tools panel is a labelled tabpanel with a summary, filters, a list and a real empty state",
        panel.strip() != ""
        and 'role="tabpanel"' in panel
        and 'aria-labelledby="mcpTabTools"' in panel
        and all(f'id="{name}"' in panel for name in ("mcpToolsSummary", "mcpToolFilters", "mcpToolList", "mcpToolsEmpty"))
        and 'role="status" aria-live="polite"' in panel
        and 'id="mcpToolFilters" role="group" aria-label="Filter tools by what a call can do"' in panel,
        "summary in a live region, a labelled filter group, one list and one empty state",
    )
    check(
        "switching to Tools is a real tab change: one panel shown, the roving tab stop moved",
        '"catalog", "connected", "tools", "activity"' in switch_tab
        and "panel.hidden = panel.id !==" in switch_tab
        and "btn.tabIndex = on ? 0 : -1" in switch_tab
        and 'btn.setAttribute("aria-selected"' in switch_tab,
        "switchMcpTab() whitelists tools exactly like the other three",
    )
    check(
        "the tab and its count are rendered from the same state the rows are",
        "renderMcpTools();" in render_hub
        and "const count = document.getElementById(\"mcpTabToolsCount\")" in render_tools
        and "count.textContent = rows.length" in render_tools,
        "renderMcpHub() calls renderMcpTools(), which sets the tab count from the rows",
    )

    # ── four different numbers ───────────────────────────────────────────────
    check(
        "configured, discovered, available and denied are four numbers, each named",
        all(word in render_tools for word in ("configured server", "discovered tool", "available to call", "denied by the allowlist"))
        and "discovered tool" in render_tools
        and "available to call" in render_tools
        and "denied by the allowlist" in render_tools,
        " · ".join(f"{name}: {label}" for name, label in COUNTERS),
    )
    check(
        "a server with no tools yet is counted separately, not hidden among the zeros",
        "configured without tools yet" in render_tools and "unsynced" in render_tools,
        "`N configured without tools yet` appears only when a server has returned nothing",
    )
    check(
        "an empty Tools tab names the next action instead of showing a blank panel",
        "Sync a server in <strong>Connected</strong>" in render_tools
        and "discovering a server's tools is what fills this tab" in render_tools,
        "the empty state says what to do, not that there is nothing",
    )
    check(
        "the filter chips count what they would show, and one is pressed",
        all(f'id: "{name}"' in script for name in ("all", "read", "write", "denied"))
        and 'aria-pressed="${on}"' in render_tools
        and "setMcpToolFilter(" in render_tools
        and "rows.filter(r => mcpToolMatches(r, f.id, \"\")).length" in render_tools,
        "All / Read-only / Writes / Denied, each with a live count and aria-pressed",
    )
    check(
        "search reaches the tool list, and a search with no hit says so and offers to clear itself",
        "mcpHubTerm()" in render_tools
        and "No tool matches" in render_tools
        and 'onclick="resetMcpHubSearch()"' in render_tools,
        "the hub's one search box narrows the rows, and the empty state clears it",
    )

    # ── the risk a row states ────────────────────────────────────────────────
    check(
        "a tool is read-only only when the server says so, and a silent tool is a write",
        'if (tool.read_only) return "read";' in risk_of
        and 'return "write";' in risk_of
        and pos(risk_of, 'if (tool.destructive) return "destructive";') > pos(risk_of, 'if (tool.read_only) return "read";')
        and pos(risk_of, 'if (tool.idempotency_supported) return "write_idempotent";') > pos(risk_of, 'if (tool.destructive) return "destructive";'),
        "read → destructive → write_idempotent → write, with `write` as the fall-through",
    )
    check(
        "the four risks have labels and a consequence in words",
        all(f'risk-{risk["id"]}' in html for risk in RISKS)
        and all(f'{risk["id"]}: "' in script for risk in RISKS)
        and all(f'risk-{risk["id"]}' in html for risk in RISKS),
        " · ".join(f"{risk['label']} because {risk['because']}" for risk in RISKS),
    )
    check(
        "each row states the risk, the state, what a call does, what happens when a model asks, and its arguments",
        '<span class="mcp-pill risk-${risk}">' in row_html
        and '<span class="mcp-pill state-${denied ? "denied" : "available"}">' in row_html
        and "<strong>What a call does</strong>" in row_html
        and "<strong>When a model asks</strong>" in row_html
        and "<strong>Arguments</strong>" in row_html
        and "data-risk=" in row_html
        and "data-state=" in row_html,
        "five labelled statements per row; the row carries its risk and state as data attributes too",
    )
    check(
        "the row answers the policy question in the policy's own terms",
        "A model's call runs without asking under Auto-Run" in consequence
        and "A model's call waits for your approval in the Harness." in consequence
        and "A model's call follows the Harness policy" in consequence
        and "the server's allowlist refuses a call until you allow it here" in consequence,
        "one sentence per policy, plus one for a denied tool",
    )
    check(
        "the approval policy is explained in words, per server policy",
        "Auto-Run: the Harness sends a call without asking you first." in policy_promise
        and "Always Ask: every call waits for your approval" in policy_promise
        and "Inherit: the Harness policy decides" in policy_promise
        and "mcpPolicyPromise(s)" in function_body(script, "renderMcpUI"),
        "three sentences, one per policy, and the Connected row prints one for each server",
    )
    check(
        "a denied row is drawn as denied and its test call is disabled",
        'data-state="${denied ? "denied" : "available"}"' in row_html
        and '<article class="mcp-tool-row" data-tool-key=' in row_html
        and '${denied ? "disabled" : ""}' in row_html
        and ".mcp-tool-row[data-state=\"denied\"] { border-style: dashed;" in html,
        "dashed row, Denied pill, no Test call",
    )
    check(
        "Allow and Deny on a row are the allowlist itself, not a decoration",
        'onclick="toggleMcpSingleTool(' in row_html
        and '${denied ? "Allow" : "Deny"}' in row_html
        and "function toggleMcpSingleTool(" in script,
        "the row calls the same endpoint the drawer's checkbox does",
    )
    check(
        "Test call opens that exact tool in the playground, on the server that owns it",
        'onclick="openMcpToolTest(' in row_html
        and 'switchMcpTab("connected")' in open_test
        and "populateMcpPlayTools();" in open_test
        and 'document.getElementById("mcpPlayTool").value = toolName;' in open_test
        and "prefillMcpPlayground(alias, toolName);" in open_test,
        "openMcpToolTest() selects the tab, the server and the tool, then pre-fills its arguments",
    )

    # ── the test call ────────────────────────────────────────────────────────
    check(
        "the arguments are validated against the tool's own schema before anything is posted",
        'id="mcpPlayArgs" rows="2"' in playground
        and 'aria-describedby="mcpPlaySchema mcpPlayArgsError"' in playground
        and "JSON.parse(raw)" in arg_problem
        and "one JSON object" in arg_problem
        and 'const required = ((tool.inputSchema || {}).required) || [];' in missing_args
        and "This tool needs" in arg_problem,
        "invalid JSON, a non-object and a missing required argument each get a sentence",
    )
    check(
        "the refusal is announced, and the button stays disabled while it stands",
        'id="mcpPlayArgsError" role="alert"' in playground
        and "box.hidden = !problem;" in validate_args
        and "btn.disabled = Boolean(problem) || (mcpPlayNeedsConsent()" in validate_args
        and 'oninput="validateMcpPlayArgs()"' in playground,
        "role=alert, cleared as soon as the arguments could be accepted; the button follows",
    )
    check(
        "the risk is stated before the call, in the same words as the Tools tab",
        'id="mcpPlayRisk"' in playground
        and "riskBox.innerHTML = `<span class=\"mcp-pill risk-${risk}\">" in consent
        and "MCP_RISK_TEXT[risk]" in consent
        and "The server is switched off, so this will be refused." in consent
        and "This tool is denied by the allowlist, so this will be refused." in consent,
        "a pill and a sentence, plus the reason it will be refused when there already is one",
    )
    check(
        "anything that is not a read needs a consent that names the tool and cannot be pre-ticked",
        'id="mcpPlayConsent" hidden' in playground
        and 'type="checkbox" id="mcpPlayConsentBox"' in playground
        and "consent.hidden = !tool || risk === \"read\";" in consent
        and "I understand this is ${MCP_RISK_NOUNS[risk]} and I want to run ${tool.name} once." in consent
        and ".checked = false" in block(script, "function onMcpPlayToolChange", "function onMcpPlayConsentChange")
        and ".checked = false" in block(script, "function onMcpPlayServerChange", "function prefillMcpPlayground"),
        "hidden for a read, named for a write, and cleared whenever the subject changes",
    )
    check(
        "the request carries confirm_write only because the box was ticked",
        "const confirmWrite = risk !== \"read\" && Boolean(confirmBox && confirmBox.checked);" in run_play
        and "...(confirmWrite ? { confirm_write: true } : {})" in run_play,
        "confirm_write is added to the body only when the consent is given",
    )
    check(
        "a 428 is drawn as a refusal that says nothing ran, not as a result",
        "r.status === 428 && d.confirmation_required" in run_play
        and "renderMcpPlayResult(\"needs-confirmation\"" in run_play
        and "nothing was sent" in run_play
        and 'data-state="needs-confirmation"' in html,
        "the confirmation_required body becomes its own state with the next step",
    )
    check(
        "a 403 is drawn as a denial, with the kind named and the way out given",
        "const kind = (d.denial && d.denial.kind) || \"denied\";" in run_play
        and "MCP_DENIAL_WORDS[kind]" in run_play
        and "mcpDenialHelp(kind, srv, picked)" in run_play
        and "function mcpDenialHelp(" in script
        and "Turn the server on here" in denial_help
        and "Allow it in Tools" not in denial_help
        and "Open the Tools tab and hit Allow" in denial_help
        and all(f'{kind}:' in script for kind, _ in DENIALS),
        " · ".join(f"{kind} → {words}" for kind, words in DENIALS),
    )
    check(
        "the inspector states the outcome, the cost and what authorized the call",
        'id="mcpPlayMeta"' in playground
        and 'id="mcpPlayAuth"' in playground
        and "authorization" in run_play
        and "grant_source" in run_play
        and "harness_would_ask" in run_play
        and "you confirmed this write." in run_play
        and "read-only, so nothing needed confirming." in run_play,
        "outcome · latency · characters · grant source · policy · confirmation · whether a model would be asked",
    )
    check(
        "long output is truncated with a count, and Show all restores every character",
        'id="mcpPlayMore" hidden' in playground
        and "function mcpPlayTruncate(" in script
        and "characters hidden" in truncate
        and "out.textContent = mcpPlayFull;" in function_body(script, "showFullMcpPlayOutput"),
        "head and tail with the hidden count in between; Show all prints the rest",
    )
    check(
        "the raw response is offered behind a disclosure that starts closed",
        '<details class="mcp-play-raw" id="mcpPlayRaw" hidden>' in playground
        and "rawBox.hidden = !raw;" in inspector
        and "JSON.stringify(raw, null, 2)" in inspector,
        "`Raw response`, shut; the inspector fills it and opens nothing",
    )

    # ── the server side ──────────────────────────────────────────────────────
    check(
        "the server classifies a tool from its annotations, and a silent tool is a write",
        'if semantics.read_only:' in risk_level
        and 'return "write"' in risk_level
        and pos(risk_level, 'if semantics.destructive:') > pos(risk_level, 'if semantics.read_only:')
        and pos(risk_level, 'if semantics.idempotency_supported:') > pos(risk_level, 'if semantics.destructive:'),
        "read → destructive → write_idempotent → write, in the same order as the page",
    )
    check(
        "every tool the API returns states its own risk and whether it is allowed",
        't_copy["read_only"] = sem.read_only' in app
        and 't_copy["destructive"] = sem.destructive' in app
        and 't_copy["requires_approval_override"] = sem.requires_approval_override' in app
        and 't_copy["risk"] = mcp_tool_risk_level(sem)' in app
        and 't_copy["denied"] = bool(t_copy.get("name")) and t_copy["name"] not in en_set' in app,
        "get_mcp_server() adds read_only, destructive, requires_approval_override, risk and denied per tool",
    )
    check(
        "a refusal is named before the call is even risked",
        "def mcp_call_block_reason(" in app
        and '"server_denied"' in block_reason
        and '"unknown_tool"' in block_reason
        and '"tool_denied"' in block_reason
        and '"stdio_gate"' in block_reason
        and "STUDIO_ALLOW_MCP_STDIO" in block_reason,
        "disabled server, unknown tool, allowlisted-out tool, closed stdio gate — in that order",
    )
    check(
        "the executor's own refusals can be classified without matching words on the page",
        "MCP_DENIAL_KINDS" in app
        and "def mcp_denial_kind(" in app
        and all(f'"{kind}"' in app for kind, _ in DENIALS)
        and 'return "denied"' in denial_kind,
        " · ".join(kind for kind, _ in DENIALS) + " — and a fallback word for anything unrecognised",
    )
    check(
        "the call route answers each refusal with its own status and a structured body",
        'status_code=404' in route
        and 'status_code=403' in route
        and '"denial": {"kind": kind' in route
        and "status_code=428" in route
        and '"confirmation_required"' in route
        and 'status_code=409' in route,
        "404 unknown server · 403 with denial.kind · 428 confirmation_required · 409 duplicate side effect",
    )
    check(
        "a call that runs says what authorized it, instead of implying it",
        'res["authorization"] = {' in route
        and '"grant_source": "operator_one_call"' in route
        and '"confirmed_write": risk != "read"' in route
        and '"harness_would_ask": mcp_tool_needs_approval(' in route,
        "surface, policy, grant source, approval, confirmation, risk and whether a model would be asked",
    )
    check(
        "the confirmation is required before the executor, not after it",
        pos(route, "if risk != \"read\" and not bool(body.get(\"confirm_write\"))") < pos(route, "res = await execute_mcp_tool_call("),
        "the 428 is returned before any execution is attempted",
    )

    # ── what this story may not change ───────────────────────────────────────
    check(
        "the six MCP routes are still there",
        all(marker in app for marker in MCP_ROUTES),
        " · ".join(marker.split('"')[1] for marker in MCP_ROUTES),
    )
    check(
        "the stdio launch gate is untouched",
        app.count("STUDIO_ALLOW_MCP_STDIO") >= 2
        and "MCP stdio process launch is disabled by default" in app,
        "a local command still needs the operator's own opt-in, whatever the page says",
    )
    check(
        "the auto approval policy still means what it meant",
        "    if ap == \"auto\":\n        return False" in app
        and "requires_approval_override" in python_function(app, "mcp_tool_needs_approval"),
        "auto runs without asking unless the tool asks for an override; this story did not tighten it",
    )
    check(
        "action admission and the workspace panes are still in place",
        "import model_admission" in app
        and "current_admission_scope()" in app
        and all(f'id="pane-{pane}"' in html for pane in ("start", "harness", "hall", "council", "debate", "chat")),
        "the admission gate the harness goes through, and all six panes",
    )
    check(
        "no React rewrite and no new dependency",
        "react" not in (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
        and "react-dom" not in html
        and "import React" not in html,
        "the frontend is still the same single document",
    )
    check(
        "every control the old playground had is still on the page",
        all(f'id="{name}"' in html for name in PRESERVED_CONTROLS),
        ", ".join(PRESERVED_CONTROLS),
    )

    # ── the walk and its evidence ────────────────────────────────────────────
    walk = WALK_PATH.read_text(encoding="utf-8") if WALK_PATH.exists() else ""
    recorded = len(re.findall(r"\brecord\(", walk))
    check(
        "a browser walk drives the tool browser and the test call on a screen",
        recorded >= 40
        and "mcpToolList" in walk
        and "mcpPlayConsentBox" in walk
        and "confirm_write" in walk
        and "mcpDenialHelp" not in walk,
        f"{WALK_PATH.name}: {recorded} recorded checks",
    )
    check(
        "the walk proves a write cannot be posted without consent, and the server refuses it if it is",
        "the request carried confirm_write only because the consent was ticked" in walk
        and "the same tool, called without that confirmation, is refused by the server" in walk
        and "428" in walk,
        "the page's gate and the server's 428 are both asserted",
    )
    check(
        "the walk proves Deny is the allowlist and that Allow recovers the same call",
        "denying a tool turns its row into a denied row with no test call" in walk
        and "the server itself refuses that tool now, with a structured denial" in walk
        and "Allow puts it back" in walk,
        "Deny → 403 tool_denied on the server → Allow → the same call succeeds",
    )
    check(
        "the walk covers every state the ledger asks for, and can photograph the old dialog",
        all(f'"{name}"' in walk for name in ("empty", "list", "filters", "search", "confirm", "error", "loading", "result", "long", "denied", "recovery", "failure", "keyboard"))
        and "--capture-only" in walk,
        "empty · list · filters · search · confirm · error · loading · result · long · denied · recovery · failure · keyboard",
    )
    after = sorted(MEDIA_DIR.glob("*.png")) if MEDIA_DIR.exists() else []
    before = sorted((MEDIA_DIR / "before-uiv2-23").glob("*.png")) if MEDIA_DIR.exists() else []
    combos = {(viewport, theme) for viewport in VIEWPORTS for theme in THEMES}
    expected = {f"mcp-tools-{state}-{viewport}-{theme}.png" for state in STATES for viewport, theme in combos}
    missing = sorted(expected - {path.name for path in after})
    check(
        "captures exist for every state, viewport and theme",
        not missing,
        f"{len(after)} after PNG · missing: {', '.join(missing[:4]) or 'none'}",
    )
    check(
        "the before set is the same instrument against the dialog this story replaced",
        len(before) >= 100
        and all((MEDIA_DIR / "before-uiv2-23" / f"mcp-tools-list-{viewport}-{theme}.png").exists() for viewport, theme in combos),
        f"{len(before)} before PNG across {len(combos)} viewport/theme runs",
    )

    ledger_payload: dict[str, Any] = {}
    if LEDGER.exists():
        ledger_payload = json.loads(LEDGER.read_text(encoding="utf-8"))
    story_entry = next((entry for entry in ledger_payload.get("stories", []) if entry.get("id") == STORY), {})
    tracking = story_entry.get("tracking", {})
    check(
        "the roadmap records this story as done, with its commit and its evidence",
        tracking.get("status") == "done"
        and bool(tracking.get("commit_sha"))
        and bool(tracking.get("visual_evidence"))
        and bool(tracking.get("acceptance_evidence")),
        f"status={tracking.get('status')} commit={tracking.get('commit_sha')} "
        f"visual={len(tracking.get('visual_evidence') or [])} acceptance={len(tracking.get('acceptance_evidence') or [])}",
    )

    findings = [{"check": entry["check"], "detail": entry["detail"]} for entry in checks if not entry["passed"]]
    return {
        "story": STORY,
        "title": TITLE,
        "schema_version": 1,
        "source": str(FRONTEND.relative_to(PROJECT_ROOT)),
        "states": list(STATES),
        "counters": [{"id": name, "meaning": meaning} for name, meaning in COUNTERS],
        "risks": [dict(risk) for risk in RISKS],
        "denials": [{"kind": kind, "speaks": words} for kind, words in DENIALS],
        "controls": {"preserved": list(PRESERVED_CONTROLS), "tools": list(TOOLS_CONTROLS)},
        "routes": list(MCP_ROUTES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
    }


def render_doc(payload: dict[str, Any]) -> str:
    lines = [
        "# MCP tools: what a call can do, and what it needs before it runs",
        "",
        f"Story **{payload['story']}** — {payload['title']}.",
        "",
        "The hub's fourth tab is the tool browser. One row per tool the server actually returned,",
        "each stating the four things that decide whether a model may call it — and the playground",
        "below it is a test call that has to be told, in words, what it is about to do.",
        "",
        "## Four different numbers",
        "",
        "| counter | meaning |",
        "| --- | --- |",
    ]
    for counter in payload["counters"]:
        lines.append(f"| {counter['id']} | {counter['meaning']} |")
    lines += [
        "",
        "## Four risks, and the only thing that makes a tool a read",
        "",
        "| risk | label | why |",
        "| --- | --- | --- |",
    ]
    for risk in payload["risks"]:
        lines.append(f"| `{risk['id']}` | {risk['label']} | {risk['because']} |")
    lines += [
        "",
        "A tool the server says nothing about is a **write**. The order is fixed: `read`, then",
        "`destructive`, then `write_idempotent`, then `write`.",
        "",
        "## Refusals the page can explain",
        "",
        "| kind | the page says |",
        "| --- | --- |",
    ]
    for denial in payload["denials"]:
        lines.append(f"| `{denial['kind']}` | {denial['speaks']} |")
    lines += [
        "",
        "## The test call",
        "",
        "`Test call` on a row lands on the Connected tab with that server and tool selected and the",
        "arguments pre-filled from the tool's schema. Before anything is posted the arguments are",
        "validated against that schema — not JSON, not an object, or missing a required field each get",
        "a sentence in an alert and a disabled button. The risk is stated in the same words the Tools",
        "tab uses, and anything that is not a read needs a consent naming the tool and the risk; the",
        "request carries `confirm_write` only when that box is ticked, and the server refuses the call",
        "with `428 confirmation_required` if it is not. A `403` is drawn as a denial with the kind",
        "named and the way out given. The inspector then states the outcome, the latency, the",
        "character count, the authorization (grant source, policy, whether the write was confirmed and",
        "whether a model would have been asked), the output — truncated with a real `Show all` — and",
        "the raw response behind a disclosure that starts closed.",
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
        "## Captures",
        "",
        "`docs/media/mcp-tools-uiv2-23/` holds "
        + ", ".join(f"`{state}`" for state in payload["states"])
        + " at four viewports (1640×900, 1440×900, 1280×800, 390×844) in light and dark,",
        "beside `before-uiv2-23/`: the same instrument run in `--capture-only` against the dialog this",
        "story replaced (three tabs, the tool list in a collapsed drawer, a playground with no risk,",
        "no consent and a one-line output box).",
        "",
        "## Regenerate",
        "",
        "```",
        "node scripts/mcp_tools_walk.mjs --screenshots docs/media/mcp-tools-uiv2-23",
        "python3 scripts/uiux_mcp_tools_inventory.py",
        "python3 scripts/uiux_mcp_tools_inventory.py --check",
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
                    "the MCP tools inventory has drifted from the source: "
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
