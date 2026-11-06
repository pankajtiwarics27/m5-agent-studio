#!/usr/bin/env python3
"""Inventory the MCP guided connect wizard's contract (UIV2-22).

    python3 scripts/uiux_mcp_wizard_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_mcp_wizard_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_mcp_wizard_inventory.py --json     # print the inventory

UIV2-21 made the MCP surface a hub you can look things up in, but the editor it opened was still one
form: alias, name, transport, URL, auth header, token, raw headers JSON, command, arguments,
environment JSON and a policy select, all at once, above a single button. Nothing asked whether you
had thought about running a local command on this machine, nothing said what the chosen approval
policy would cost you, and nothing told you whether the server had answered before you committed.

What this inventory checks: that connecting a server is a four-step guide — name it, say how it is
reached, decide what a call costs, review and connect — with exactly one step on screen; that a step
which is not satisfied refuses to advance and says why in words, over a named field; that the
credentials step shows only the chosen transport's fields, keeps the token in a password field with
autocomplete off, and never pre-fills it; that the review never reads the typed token back and shows
a stored one only as the mask the server sent; that a local command needs an explicit, named consent
that the page states it cannot turn into permission; that raw headers and environment JSON are
behind one closed disclosure, off the guided path; and that saving re-checks the earlier steps,
tests the connection in the same request and reports the result — found tools, or the server's own
error — where the button is.

It also checks the things this story may not break: the six `/api/mcp/*` routes, the stdio launch
gate, the mask the server sends, and every control the old form had.

It fails when a step can be skipped, when the guide can save a form it never validated, when a typed
secret becomes renderable or the review echoes it, when consent is implied rather than given, when
the connection test result is not visible, or when the story reaches for a permission the operator
never granted.
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
WALK_PATH = PROJECT_ROOT / "scripts" / "mcp_wizard_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_MCP_Wizard.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-2-mcp-wizard.md"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "mcp-wizard-uiv2-22"

STORY = "UIV2-22"
TITLE = "MCP guided connect wizard and advanced configuration"

#: The four questions the guide asks, in order.
STEPS = (
    {"id": 1, "label": "Server", "heading": "mcpWizardHeading", "question": "what is it called?"},
    {"id": 2, "label": "Transport and credentials", "heading": "mcpWizardHeading2", "question": "how is it reached?"},
    {"id": 3, "label": "Policy and consent", "heading": "mcpWizardHeading3", "question": "what does a call cost?"},
    {"id": 4, "label": "Review and connect", "heading": "mcpWizardHeading4", "question": "is this what I want, and does it answer?"},
)

#: The states the ledger's visual matrix names, as the walk names them.
STATES = ("empty", "populated", "long-content", "error", "loading", "recovery")

#: Controls the old one-shot form had. The guide may move them between steps, never drop them.
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

#: The controls the guide itself adds.
WIZARD_CONTROLS = (
    "mcpWizardSteps",
    "mcpWizardTab1",
    "mcpWizardTab2",
    "mcpWizardTab3",
    "mcpWizardTab4",
    "mcpWizardProgress",
    "mcpWizardBack",
    "mcpWizardNext",
    "mcpWizardHint",
    "mcpTransportEffect",
    "mcpPolicyEffect",
    "mcpConsentRow",
    "mcpStdioConsent",
    "mcpConsentCommand",
    "mcpConsentNote",
    "mcpReviewList",
    "mcpWizardResult",
    "mcpWizardConfirm",
    "mcpAdvancedJson",
)

#: The API surface the guide talks to; this story adds no endpoint.
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


def pos(source: str, marker: str) -> int:
    """Where a marker sits, or -1 — a missing marker is a finding, not an exception."""
    return source.find(marker)


def block(source: str, start_marker: str, end_marker: str) -> str:
    """The slice between two markers, or an empty string."""
    start = source.find(start_marker)
    if start < 0:
        return ""
    end = source.find(end_marker, start)
    return source[start : end if end > 0 else len(source)]


def async_body(source: str, name: str) -> str:
    """The body of `async function name(...) { … }` — `function_body` with the async prefix."""
    start = source.find(f"async function {name}(")
    if start < 0:
        return function_body(source, name)
    return function_body(source[start:], name)


def build_inventory() -> dict[str, Any]:
    html = FRONTEND.read_text(encoding="utf-8")
    script = html[html.index("<script>") :]
    app = APP.read_text(encoding="utf-8")

    editor = block(html, '<details class="mcp-hub-card mcp-editor" id="mcpEditor">', '<details class="mcp-hub-card mcp-import">')

    go = function_body(script, "mcpWizardGo")
    refresh = function_body(script, "mcpWizardRefresh")
    validate = function_body(script, "mcpWizardValidate")
    problem = function_body(script, "mcpWizardProblem")
    nxt = function_body(script, "mcpWizardNext")
    back = function_body(script, "mcpWizardBack")
    review_rows = function_body(script, "mcpReviewRows")
    policy_effect = function_body(script, "mcpPolicyEffect")
    result = function_body(script, "mcpWizardResult")
    confirm = function_body(script, "mcpWizardConfirm")
    done = function_body(script, "mcpWizardDone")
    open_editor = function_body(script, "openMcpEditor")
    save = async_body(script, "saveMcpServer")
    toggle = function_body(script, "toggleMcpTransportFields")
    steps_const = block(script, "const MCP_WIZARD_STEPS = [", "];")

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # ── the shape of the guide ───────────────────────────────────────────────
    rail_ids = re.findall(r'id="mcpWizardTab(\d)"', editor)
    panel_ids = re.findall(r'id="mcpStep(\d)"', editor)
    step_items = re.findall(r'data-wizard-step="(\d)"', editor)
    check(
        "connecting a server is four named steps, not one form",
        rail_ids == ["1", "2", "3", "4"]
        and panel_ids == ["1", "2", "3", "4"]
        and len(step_items) >= 4
        and len(re.findall(r'id="mcpWizardHeading\d?"', editor)) == 4,
        "rail: " + " · ".join(rail_ids) + " · panels: " + ", ".join(panel_ids),
    )
    check(
        "the guide asks four questions in order: what, how, what it costs, and is it right",
        all(f"heading: \"{entry['heading']}\"" in steps_const for entry in STEPS)
        and [entry["label"] for entry in STEPS][0] in steps_const
        and "Review and connect" in steps_const
        and pos(editor, 'id="mcpStep1"') < pos(editor, 'id="mcpStep2"') < pos(editor, 'id="mcpStep3"') < pos(editor, 'id="mcpStep4"'),
        " · ".join(f"{entry['id']} {entry['label']}" for entry in STEPS),
    )
    check(
        "one step is on screen at a time, and starting over goes back to the first",
        "panel.hidden = entry.id !== target" in go
        and "hidden" not in re.search(r'id="mcpStep1"[^>]*>', editor).group(0)
        and all("hidden" in re.search(rf'id="mcpStep{step}"[^>]*>', editor).group(0) for step in (2, 3, 4))
        and "mcpWizardGo(1, { focus: false })" in open_editor,
        "mcpWizardGo() hides every other panel; opening the editor resets to step 1",
    )
    check(
        "the rail says where you are, in a live region, and marks the step it is on",
        'item.setAttribute("aria-current", entry.id === target ? "step" : "false")' in go
        and "item.classList.toggle(\"done\", entry.id < target)" in go
        and 'id="mcpWizardProgress" role="status" aria-live="polite"' in editor
        and "`Step ${target} of ${MCP_WIZARD_STEPS.length} · " in go,
        "aria-current=step on the current item, done on the ones behind, `Step N of 4 · label` in role=status",
    )
    check(
        "each step is a labelled group whose heading names the question",
        len(re.findall(r'class="mcp-wizard-panel" id="mcpStep\d" data-wizard-step="\d" role="group" aria-labelledby=', editor)) == 4
        and all(f'aria-labelledby="{entry["heading"]}"' in editor for entry in STEPS)
        and all(f'id="{entry["heading"]}" tabindex="-1"' in editor for entry in STEPS),
        "role=group, aria-labelledby the step heading, heading focusable for the keyboard",
    )

    # ── a step that is not satisfied ─────────────────────────────────────────
    check(
        "no step can advance until it is satisfied, and the refusal is words, not a shake",
        'Give the server an alias first' in validate
        and "A remote server needs its URL" in validate
        and "must start with https://" in validate
        and "A local server needs the command that starts it" in validate
        and "if (mcpWizardProblem(mcpWizardStep)) return false;" in nxt,
        "mcpWizardValidate() returns the sentence, mcpWizardNext() refuses to move while it does",
    )
    check(
        "a refusal puts the cursor in the field that needs it, and clears when satisfied",
        'hint.dataset.step = String(step)' in problem
        and "field.focus({ preventScroll: true })" in problem
        and 'id="mcpWizardHint" class="mcp-hub-hint" role="status" aria-live="polite"' in editor
        and "!mcpWizardValidate(Number(hint.dataset.step))" in refresh
        and 'hint.textContent = ""' in refresh,
        "mcpWizardProblem() names the reason, focuses the field, and mcpWizardRefresh() clears it",
    )
    check(
        "Back and Continue are real buttons, and Continue leaves the last step",
        'id="mcpWizardBack" onclick="mcpWizardBack()"' in editor
        and 'id="mcpWizardNext" onclick="mcpWizardNext()"' in editor
        and "back.disabled = target === 1" in go
        and "next.hidden = target === MCP_WIZARD_STEPS.length" in go
        and "mcpWizardGo(mcpWizardStep - 1)" in back,
        "Back is disabled on step 1; Continue disappears on step 4, where Save & test connection is",
    )

    # ── transport and credentials ────────────────────────────────────────────
    check(
        "step 2 shows only the fields the chosen transport needs",
        'document.getElementById("mcpHttpFields").style.display = tr === "http" ? "block" : "none"' in toggle
        and 'document.getElementById("mcpStdioFields").style.display = tr === "stdio" ? "block" : "none"' in toggle
        and 'id="mcpHttpFields"' in editor
        and 'id="mcpStdioFields" style="display:none;"' in editor,
        "toggleMcpTransportFields() shows one block and hides the other",
    )
    check(
        "the transport says what will happen, in words",
        "Studio starts this program itself and talks to it over stdin and stdout." in refresh
        and "Studio talks to the server over the URL you give it." in refresh
        and 'id="mcpTransportEffect"' in editor,
        "a sentence per transport, not a badge or a colour",
    )
    check(
        "the token is a password field that is never pre-filled or auto-completed",
        'type="password" id="mcpToken" autocomplete="off"' in editor
        and 'oninput="mcpWizardRefresh()"' in editor
        and "mcpToken" not in block(html, "async function editMcpServer", "async function saveMcpServer"),
        "type=password, autocomplete=off, and the editor never writes a stored secret into it",
    )
    check(
        "raw headers and environment JSON are one closed disclosure, off the guided path",
        '<details class="mcp-advanced" id="mcpAdvancedJson">' in editor
        and 'id="mcpAdvancedJson" open' not in editor
        and pos(editor, 'id="mcpAdvancedJson"') > pos(editor, 'id="mcpStdioFields"')
        and pos(editor, 'id="mcpHeadersJson"') > pos(editor, 'id="mcpAdvancedJson"')
        and pos(editor, 'id="mcpEnvJson"') > pos(editor, 'id="mcpAdvancedJson"'),
        "`Advanced: raw headers and environment JSON`, shut, holding both JSON fields",
    )

    # ── policy and consent ───────────────────────────────────────────────────
    check(
        "the policy step explains what each policy costs",
        all(phrase in policy_effect for phrase in (
            "Every call runs without asking",
            "waits for your approval in the Harness",
            "Harness policy decides",
        ))
        and 'id="mcpPolicyEffect"' in editor
        and 'onchange="mcpWizardRefresh()"' in editor,
        "three sentences, one per policy, rendered next to the select",
    )
    check(
        "a local command needs consent that is given, not implied",
        'id="mcpConsentRow" hidden' in editor
        and 'type="checkbox" id="mcpStdioConsent"' in editor
        and "mcpWizardTransport() === \"stdio\"" in validate
        and "!consent.checked" in validate
        and "item must tick" not in validate
        and "Read the command and tick the consent box" in validate,
        "the consent row only exists for stdio and mcpWizardValidate(3) refuses until it is ticked",
    )
    check(
        "the consent names the command it is about",
        'mcpConsentCommand' in editor
        and 'command.textContent = `${mcpWizardValue("mcpCommand") || "this command"} ${mcpWizardValue("mcpArgs")}`' in refresh
        and "I have reviewed" in editor
        and "run it on this machine" in editor,
        "mcpConsentCommand is re-rendered from the exact command and arguments",
    )
    check(
        "the consent says it cannot grant the operator's permission",
        "STUDIO_ALLOW_MCP_STDIO=1" in editor
        and "it cannot grant that permission" in editor
        and "does not weaken the sandbox" in editor,
        "the note names the server-side opt-in and disclaims it, so the screen is not mistaken for a gate",
    )

    # ── review ───────────────────────────────────────────────────────────────
    terms = re.findall(r'\["([A-Z][^"]+)", ', review_rows)
    check(
        "the review answers what, how, what it costs and whether consent was given",
        terms == ["Alias", "Display name", "Transport", "Target", "Credential", "Approval policy", "Local command consent"]
        and 'id="mcpReviewList"' in editor
        and '<dl class="mcp-review" id="mcpReviewList">' in editor,
        " · ".join(terms),
    )
    check(
        "the review never reads the typed token back out",
        "mcpReviewRows()" in refresh
        and 'Boolean(mcpWizardField("mcpToken") && mcpWizardField("mcpToken").value)' in review_rows
        and "A new token will be stored, and only its mask shown" in review_rows
        and "mcpWizardValue(\"mcpToken\")" not in review_rows
        and not re.search(r"\[\s*\"Credential\",\s*[^\]]*mcpWizardField\(\"mcpToken\"\)\.value[^\]]*\]", review_rows),
        "the presence of a typed token is a boolean; its characters are never interpolated",
    )
    check(
        "a stored token is kept when the field is left blank, and shown only as its mask",
        "The stored token is kept (${stored.masked_token})" in review_rows
        and "masked_token" in review_rows
        and "auth_token" not in review_rows,
        "unchanged-token semantics: blank means keep, and the mask the server sent is what is named",
    )

    # ── save, test, confirm ──────────────────────────────────────────────────
    check(
        "saving re-checks every earlier step, so a field edited after passing it cannot slip through",
        "for (const step of [1, 2, 3])" in save
        and "const problem = mcpWizardValidate(step);" in save
        and "mcpWizardGo(step, { focus: false });" in save
        and "mcpWizardProblem(step);" in save
        and pos(save, "for (const step of [1, 2, 3])") < pos(save, 'fetch("/api/mcp/servers"'),
        "the guard runs before the POST, not only on the way to it",
    )
    check(
        "saving says it is testing, then tests in the same request",
        'btn.textContent = "Saving and testing…"' in save
        and "btn.disabled = true" in save
        and 'mcpWizardResult("testing", "Saving the server and asking it for its tool list…")' in save
        and "btn.disabled = false; btn.textContent = label;" in save,
        "button label and disabled state restored in `finally`; the POST is the connection test",
    )
    check(
        "the result is visible where the button is, and says what was found or what failed",
        'Connected — ${tools} tool${tools === 1 ? "" : "s"} discovered.' in save
        and "Saved, but the connection test failed — ${d.sync_error}" in save
        and '"Not saved — "' in save
        and 'id="mcpWizardResult" class="mcp-wizard-result" role="status" aria-live="polite" hidden' in editor
        and "box.dataset.state = state" in result,
        "ok / failed / testing carried as data-state on a live region inside the last step",
    )
    check(
        "the confirmation names the tool namespace and offers the server list",
        "are now callable as <code>mcp__${escapeHtml(d.server.alias)}__" in save
        and 'id="mcpWizardDoneBtn" onclick="mcpWizardDone()">Open the server list' in save
        and 'editor.open = false' in done
        and 'switchMcpTab("connected")' in done
        and "box.hidden = !html" in confirm,
        "a successful test ends with a way to the list, not a dead end",
    )
    check(
        "the typed secret leaves the field after a save",
        'document.getElementById("mcpToken").value = ""' in save,
        "the field is emptied on success, so the secret does not stay in the page",
    )

    # ── what this story may not change ───────────────────────────────────────
    check(
        "every control the old form had is still in the guide",
        all(f'id="{control}"' in html for control in PRESERVED_CONTROLS),
        f"{len(PRESERVED_CONTROLS)} controls: "
        + (", ".join(control for control in PRESERVED_CONTROLS if f'id=\"{control}\"' not in html) or "all present"),
    )
    check(
        "all three approval policies survive inside the guide",
        all(f'value="{policy}"' in editor for policy in ("auto", "inherit", "always_ask")),
        "auto / inherit / always_ask in the policy select",
    )
    check(
        "the six MCP routes are unchanged and none were added",
        all(route in app for route in MCP_ROUTES)
        and len(re.findall(r'@app\.(?:get|post|delete)\("/api/mcp', app)) == len(MCP_ROUTES),
        f"{len(MCP_ROUTES)} routes, no additions",
    )
    check(
        "consent on the screen does not replace the operator's stdio opt-in",
        app.count("STUDIO_ALLOW_MCP_STDIO") >= 2
        and "MCP stdio process launch is disabled by default" in app
        and "STUDIO_ALLOW_MCP_STDIO" not in block(html, "async function saveMcpServer", "function toggleMcpTransportFields")
        and "STUDIO_ALLOW_MCP_STDIO" not in save,
        "the server still refuses to spawn unless the operator set the variable; the page only records consent",
    )
    check(
        "the served payload is what masks a secret, not the page",
        "def _mask_secret" in app
        and 'd.pop("auth_token"' in app
        and "masked_token" in app,
        "app.py masks on read and keeps a blank token as the stored one",
    )

    # ── the walk and its evidence ────────────────────────────────────────────
    walk = WALK_PATH.read_text(encoding="utf-8") if WALK_PATH.exists() else ""
    recorded = len(re.findall(r"\brecord\(", walk))
    check(
        "a browser walk drives the guide on a screen",
        recorded >= 30
        and "mcpWizardNext" in walk
        and "mcpStdioConsent" in walk
        and "mcpReviewList" in walk
        and "mcpWizardResult" in walk,
        f"{WALK_PATH.name}: {recorded} recorded checks",
    )
    check(
        "the walk covers every state the ledger asks for",
        all(name in walk for name in ("empty", "credentials", "policy", "review", "loading", "success", "error", "recovery", "keyboard", "long")),
        "empty · credentials · policy · review · loading · success · error · recovery · keyboard · long",
    )
    check(
        "the walk proves the secret never reaches the page, the console or another request",
        'const SECRET = "sk-uiv2-22-do-not-render-4b71"' in walk
        and "secretInDom" in walk
        and "secretConsole" in walk
        and "posts.every" in walk,
        "the secret is typed into the form, then searched for in the DOM, the console and every request body",
    )
    captures = sorted(MEDIA_DIR.glob("*.png")) if MEDIA_DIR.exists() else []
    before = sorted((MEDIA_DIR / "before-uiv2-22").rglob("*.png")) if MEDIA_DIR.exists() else []
    check(
        "captures exist for every viewport and theme, with a before set",
        len(captures) >= 48 and len(before) >= 16,
        f"{len(captures)} after PNG, {len(before)} before PNG",
    )
    check(
        "the walk can photograph the old one-shot form too",
        "--capture-only" in walk and "captureOnly" in walk and len(before) >= 16,
        "the same instrument runs against the pre-wizard editor to make the before set",
    )

    findings = [{"check": entry["check"], "detail": entry["detail"]} for entry in checks if not entry["passed"]]
    return {
        "story": STORY,
        "title": TITLE,
        "schema_version": 1,
        "source": str(FRONTEND.relative_to(PROJECT_ROOT)),
        "states": list(STATES),
        "steps": [dict(entry) for entry in STEPS],
        "controls": {"preserved": list(PRESERVED_CONTROLS), "wizard": list(WIZARD_CONTROLS)},
        "routes": list(MCP_ROUTES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
    }


def render_doc(payload: dict[str, Any]) -> str:
    lines = [
        "# Connecting an MCP server is a guide, not a form",
        "",
        f"Story **{payload['story']}** — {payload['title']}.",
        "",
        "The editor the hub opens asks four questions, one screen at a time. Nothing is saved, and no",
        "connection is attempted, until the last of them is answered.",
        "",
        "| step | question | what it holds |",
        "| --- | --- | --- |",
        "| 1 Server | what is it called? | alias and display name |",
        "| 2 Transport & credentials | how is it reached? | remote HTTP (URL, auth header, token) or a local command (command, arguments), plus one closed disclosure for raw headers and environment JSON |",
        "| 3 Policy & consent | what does a call cost? | approval policy with its consequence in words; for a local command, an explicit consent naming the exact command, and a note that the screen cannot grant the server's own `STUDIO_ALLOW_MCP_STDIO` permission |",
        "| 4 Review & connect | is this what I want, and does it answer? | the seven things that will be saved, then `Save & test connection`, a testing state, and the result the server gave |",
        "",
        "A step that is not satisfied does not advance: it says why in a live region, puts the cursor in",
        "the field that needs it, and clears the sentence as soon as the field satisfies it. The review",
        "never reads a typed token back — it says a new token will be stored, and a stored one is named",
        "only by the mask the server already sent.",
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
        "`docs/media/mcp-wizard-uiv2-22/` holds the captures: `empty`, `credentials`, `policy`,",
        "`review`, `loading`, `success`, `error`, `recovery`, `keyboard` and a `long` target, at four",
        "viewports and in both themes, plus `before-uiv2-22/` for the same instrument run against the",
        "one-shot form this story replaced.",
        "",
        "## Regenerate",
        "",
        "```",
        "python3 scripts/uiux_mcp_wizard_inventory.py",
        "python3 scripts/uiux_mcp_wizard_inventory.py --check",
        "node scripts/mcp_wizard_walk.mjs --screenshots docs/media/mcp-wizard-uiv2-22",
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
                    "the MCP wizard inventory has drifted from the source: "
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
