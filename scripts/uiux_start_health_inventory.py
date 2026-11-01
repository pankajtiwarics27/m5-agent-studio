#!/usr/bin/env python3
"""Inventory Start's system health and workspace selection (UIUX-P2-03).

    python3 scripts/uiux_start_health_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_start_health_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_start_health_inventory.py --json     # print the inventory

Two questions a first-run reader asks after "what next": is this machine actually
working, and where will my task run?

**Health.** The answers already exist. ``GET /api/telemetry`` reports the runtime,
unified memory and containers; the harness session payload reports the workspace and
its git state. What Start lacked was a place to say them together - and, in the demo,
the one thing that matters more than the numbers: when no model runtime answers, the
telemetry route returns a *simulated* payload (``telemetry_source = "simulated_demo"``)
so the monitor rail has something to draw. Rendering that as if it described this
machine would be exactly the failure the evidence component was built to prevent, so
the card's basis is ``unverified`` in that case, the claim says which rows are still
real, and the walk asserts both.

**Workspace.** One setting, two panes. Start reads it from the session payload
(``default_workspace``, ``recent_workspaces``, ``git_info``) and writes it through the
same ``POST /api/workspace/directory`` the Harness uses, then re-reads the preflight -
because a checklist that still says the workspace is unwritable after it was set is
worse than no checklist.

The rules below keep both honest:

* the health card never polls: it renders the payload ``renderTelemetry()`` already
  fetched, so the pane and the monitor rail cannot disagree;
* the basis is chosen from the payload, not from optimism - ``measured`` live,
  ``unverified`` when simulated, ``unavailable`` when there is none;
* every row it draws has a tone from the shared vocabulary;
* the workspace write goes through ``saveWorkspaceDirectory()``, which returns an
  outcome rather than only a toast, and a failure is presented with a cause and with
  what is still in force;
* changing the workspace re-reads the preflight;
* there is one workspace input list, so Start and the Harness cannot point at
  different directories.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Start_Health.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-start-health.md"

TONES = ("ok", "warn", "danger", "info", "busy", "neutral")
BASES = ("measured", "derived", "unverified", "unavailable")
HEALTH_FUNCTIONS = ("startHealthRows", "renderStartHealth")
WORKSPACE_FUNCTIONS = (
    "renderStartWorkspace",
    "pickStartWorkspace",
    "saveStartWorkspace",
)


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


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


def tones_in(body: str) -> list[str]:
    """Every tone a ``tone:`` expression can produce, ternaries included.

    ``tone: "ok"`` is easy to read; ``tone: online ? "ok" : "warn"`` is the common
    case, and a rule that only saw the first kind would pass while the second named a
    tone nobody paints. Each expression is read to the end of its line.
    """
    found: set[str] = set()
    for match in re.finditer(r"tone:\s*([^\n]+)", body):
        found.update(re.findall(r'"([a-z_]+)"', match.group(1)))
    return sorted(found)


def read_frontend(html: str) -> dict:
    health = "\n".join(function_body(html, name) for name in HEALTH_FUNCTIONS)
    workspace = "\n".join(function_body(html, name) for name in WORKSPACE_FUNCTIONS)
    telemetry = function_body(html, "renderTelemetry")
    # The failure path lives where the request is made, not in the renderer, so the
    # rule reads both: feeding the card and clearing it are one contract.
    refresh = function_body(html, "refreshTelemetry")
    recent = function_body(html, "loadStartRecent")
    sync = function_body(html, "syncProjectDirInputs")
    saver = function_body(html, "saveWorkspaceDirectory")
    return {
        "functions": {
            name: bool(function_body(html, name))
            for name in HEALTH_FUNCTIONS + WORKSPACE_FUNCTIONS
        },
        "health": {
            "reads_last_payload": "lastTelemetry" in health,
            "no_poll": "fetch(" not in health,
            "memory_from_payload": "mem.omlx_gb" in health and "mem.total_gb" in health,
            "containers_from_payload": "dk.containers" in health,
            "runtime_from_payload": "d.omlx_online" in health,
            "simulated_branch": '"simulated_demo"' in health,
            "basis_measured": '"measured"' in health,
            "basis_unverified": '"unverified"' in health,
            "basis_unavailable": '"unavailable"' in health,
            "claim_names_real_rows": health.count("still this machine") >= 2,
            "tones": tones_in(health),
            "uses_status_component": "setStatus(pill," in health,
            "uses_evidence_component": "evidenceNote(" in health,
            "workspace_row": "startWorkspace.dir" in health,
            # A card that says "unavailable" and stops still leaves the reader with
            # nothing to press.
            "failure_presented": (
                'presentInto("startHealthState"' in health
                and 'clearPresentation("startHealthState"' in health
            ),
            "failure_names_unaffected": "workspace row is unaffected" in health,
            # The same two buttons inside and below one card is noise, not redundancy.
            "one_recovery_affordance": 'getElementById("startHealthActions")' in health,
            # A timestamp the payload does not carry would be an invented number in a
            # card whose whole job is to name where its numbers came from.
            "real_read_time": "startHealthReadAt" in health and "read_at" not in health,
            # One row that vanishes when the payload does reads as "that part is fine".
            "absent_keeps_shape": health.count("not measured yet") >= 3,
        },
        "telemetry": {
            "feeds_card": "lastTelemetry = d" in telemetry
            and "renderStartHealth()" in telemetry,
            "clears_on_failure": "lastTelemetry = null" in refresh
            and "renderStartHealth()" in refresh,
        },
        "workspace": {
            "reads_server_payload": "default_workspace" in recent
            and "git_info" in recent,
            "writes_through_saver": "saveWorkspaceDirectory(" in workspace,
            "no_direct_fetch": "fetch(" not in workspace,
            "reports_outcome": "result.ok" in workspace or "!result.ok" in workspace,
            "rereads_preflight": "refreshOnboarding()" in workspace,
            "error_presentation": 'presentInto("startWorkspaceState"' in workspace,
            "error_says_what_holds": "is still the project directory" in workspace,
            "error_offers_retry": 'label: "Retry"' in workspace,
            "shared_input_list": "startWorkspaceInput" in sync,
            "recent_picker": "pickStartWorkspace(" in html
            and "startWorkspaceRecent" in html,
        },
        "server": {
            "saver_returns_outcome": "return { ok: true" in saver
            and "return { ok: false" in saver,
        },
        "styles": {
            "row": ".start-health-row {" in html,
            "value": ".start-health-value {" in html,
            "workspace": ".start-workspace {" in html,
            "actions": ".start-workspace-actions {" in html,
        },
        "markup": {
            "health_rows": 'id="startHealthRows"' in html,
            "health_evidence": 'id="startHealthEvidence"' in html,
            "health_state": 'id="startHealthState"' in html,
            "workspace_row": 'id="startWorkspaceRow"' in html,
            "workspace_state": 'id="startWorkspaceState"' in html,
            "described_by": 'aria-describedby="startWorkspaceHint"' in html,
        },
        "startup": "loadStartRecent();\n  renderStartWorkspace();\n  renderStartHealth();"
        in html,
    }


def server_sources(app_source: str) -> dict:
    """The two payloads the card reads, and the states they can be in."""
    simulated = re.findall(r'\["telemetry_source"\]\s*=\s*"([a-z_]+)"', app_source)
    route = re.search(
        r'@app\.post\("/api/workspace/directory"\)\s*\n(?:async )?def (\w+)', app_source
    )
    return {
        "telemetry_route": "/api/telemetry"
        if '@app.get("/api/telemetry")' in app_source
        else None,
        "simulated_sources": sorted(set(simulated)),
        "workspace_route": "/api/workspace/directory" if route else None,
        "workspace_route_line": _line(app_source, route.start()) if route else None,
        "telemetry_fields": sorted(
            {
                field
                for field in (
                    "omlx_online",
                    "memory",
                    "docker",
                    "telemetry_source",
                    "omlx",
                )
                if re.search(rf'"{field}"\s*:', app_source)
            }
        ),
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = server_sources(APP.read_text(encoding="utf-8"))
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    # 1. One payload, no second poll.
    if not frontend["health"]["reads_last_payload"]:
        fail("health-polls", "the health card does not read lastTelemetry")
    if not frontend["health"]["no_poll"]:
        fail(
            "health-polls",
            "the health card makes its own request instead of reading the payload the rail fetched",
        )
    if not frontend["telemetry"]["feeds_card"]:
        fail(
            "health-disconnected",
            "renderTelemetry() does not feed the health card, so it would show a stale or empty state",
        )
    if "fetch(" not in function_body(html, "refreshTelemetry"):
        fail(
            "health-unwired",
            "refreshTelemetry() no longer makes the request, so the card has no live source",
        )
    if not frontend["telemetry"]["clears_on_failure"]:
        fail(
            "health-stale",
            "a failed telemetry read does not clear the card, so it would keep describing a machine it can no longer see",
        )

    # 2. The numbers are the payload's.
    for flag, description in (
        (
            "memory_from_payload",
            "the memory row does not read memory.omlx_gb / total_gb",
        ),
        (
            "containers_from_payload",
            "the container row does not read docker.containers",
        ),
        ("runtime_from_payload", "the runtime row does not read omlx_online"),
        ("workspace_row", "the health card does not show the workspace"),
    ):
        if not frontend["health"].get(flag):
            fail("health-invented", description)

    # 3. Honest basis, including the simulated payload the demo tour produces.
    if server["simulated_sources"] and not frontend["health"]["simulated_branch"]:
        fail(
            "basis-blind",
            f"the telemetry route can return {server['simulated_sources']} but the card has no branch for it",
        )
    for flag, description in (
        ("basis_measured", "a live payload is not reported as measured"),
        ("basis_unverified", "a simulated payload is not reported as unverified"),
        ("basis_unavailable", "an absent payload is not reported as unavailable"),
    ):
        if not frontend["health"].get(flag):
            fail("basis-missing", description)
    for tone in frontend["health"]["tones"]:
        if tone not in TONES:
            fail(
                "tone-unknown",
                f"the health rows use tone {tone!r}, which is not in the status vocabulary",
            )
    if not frontend["health"]["tones"]:
        fail("tone-missing", "the health rows declare no tones")
    if not frontend["health"]["uses_status_component"]:
        fail("status-bypassed", "the health rows do not paint through setStatus()")
    if not frontend["health"]["uses_evidence_component"]:
        fail(
            "evidence-bypassed",
            "the health card carries a claim without the evidence component",
        )
    if not frontend["health"]["failure_presented"]:
        fail(
            "no-way-back",
            "a failed health read presents no cause and no way to re-read",
        )
    if not frontend["health"]["failure_names_unaffected"]:
        fail(
            "failure-overreach",
            "the failure does not say which row is unaffected by it",
        )
    if not frontend["health"]["one_recovery_affordance"]:
        fail(
            "duplicate-recovery",
            "the failure box and the card footer offer the same buttons at the same time",
        )
    if not frontend["health"]["absent_keeps_shape"]:
        fail(
            "shape-collapses",
            "with no payload the card drops rows instead of saying they are unknown",
        )
    if not frontend["health"]["real_read_time"]:
        fail(
            "invented-field",
            "the claim's read time comes from a field the payload does not carry, or from nowhere",
        )
    if not frontend["health"]["claim_names_real_rows"]:
        fail(
            "claim-misleading",
            "the simulated case does not say which rows still describe this machine",
        )

    # 4. The workspace is one setting, written one way.
    for flag, description in (
        (
            "reads_server_payload",
            "the workspace card does not read default_workspace/git_info from the session payload",
        ),
        (
            "writes_through_saver",
            "the pane does not write through saveWorkspaceDirectory()",
        ),
        (
            "no_direct_fetch",
            "the pane makes its own request instead of using the Harness's handler",
        ),
        ("reports_outcome", "the pane ignores whether the write succeeded"),
        (
            "rereads_preflight",
            "a workspace change does not re-read the preflight, so the checklist can contradict it",
        ),
        ("error_presentation", "a failed workspace change is not presented"),
        (
            "error_says_what_holds",
            "the failure does not say which directory is still in force",
        ),
        ("error_offers_retry", "the failure offers no way to try again"),
        (
            "shared_input_list",
            "startWorkspaceInput is not in syncProjectDirInputs(), so the two panes can disagree",
        ),
        ("recent_picker", "there is no recent-folders picker"),
    ):
        if not frontend["workspace"].get(flag):
            fail("workspace-split", description)
    if not frontend["server"]["saver_returns_outcome"]:
        fail(
            "outcome-lost",
            "saveWorkspaceDirectory() does not return what happened, so the pane cannot present a failure",
        )
    if not server["workspace_route"]:
        fail("route-missing", "POST /api/workspace/directory is gone")

    # 5. What the pane emits has rules, and it paints before the first request lands.
    for flag, description in (
        ("row", "a health row has no rule"),
        ("value", "a health value has no rule"),
        ("workspace", "the workspace block has no rule"),
        ("actions", "the workspace actions row has no rule"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)
    if not frontend["startup"]:
        fail(
            "not-painted",
            "the two cards are not painted at startup, so Start opens with empty boxes",
        )
    for flag, description in (
        ("health_rows", "the health row container is gone"),
        ("health_evidence", "the health evidence block is gone"),
        ("health_state", "the health presentation container is gone"),
        ("workspace_row", "the workspace row is gone"),
        ("workspace_state", "the workspace presentation container is gone"),
        ("described_by", "the workspace field is not described by its hint"),
    ):
        if not frontend["markup"].get(flag):
            fail("markup-missing", description)

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P2-03",
        "statement": (
            "Start answers two questions with data the server already produces: whether this machine is working (runtime, "
            "memory, containers, workspace) and where a task will run. The health card renders the telemetry payload the "
            "monitor rail fetched, states whether that payload describes this machine or the demo's, and the workspace is "
            "the same setting the Harness uses - written through the same route and followed by a fresh preflight."
        ),
        "source": {
            "frontend": "static/index.html",
            "server": "app.py",
            "routes": [
                route
                for route in (server["telemetry_route"], server["workspace_route"])
                if route
            ],
        },
        "server": server,
        "health_rows": [
            "Model runtime",
            "Unified memory",
            "Containers",
            "Project workspace",
        ],
        "bases_used": [
            basis for basis in BASES if frontend["health"].get(f"basis_{basis}")
        ],
        "tones_used": frontend["health"]["tones"],
        "frontend": frontend,
        "totals": {
            "health_rows": 4,
            "bases": len(
                [basis for basis in BASES if frontend["health"].get(f"basis_{basis}")]
            ),
            "tones": len(frontend["health"]["tones"]),
            "routes": len(
                [
                    route
                    for route in (server["telemetry_route"], server["workspace_route"])
                    if route
                ]
            ),
            "simulated_sources": len(server["simulated_sources"]),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the memory and container rows are the runtime's own report, including when that report is the demo's; the "
            "artifact and the card both say so rather than smoothing it over",
            "the workspace row is the session payload's default_workspace, so it describes what the Harness will use even "
            "when the runtime is unreachable",
            "the browser walk checks the card against /api/telemetry on the host it runs on, so the simulated and the live "
            "branches are asserted by whichever one that host produces, and the other is asserted structurally",
            "setting a workspace creates the directory if it is missing - that is the server's documented behaviour of "
            "POST /api/workspace/directory, not something the pane adds",
        ],
    }


def render_doc(payload: dict) -> str:
    server = payload["server"]
    lines = [
        "# Start: system health and the project workspace",
        "",
        "Generated by `python3 scripts/uiux_start_health_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## Where the numbers come from",
        "",
        "| Row | Source | When it cannot be read |",
        "|---|---|---|",
        "| Model runtime | `omlx_online` from `GET /api/telemetry` | the card says *not reachable* and the basis drops to unverified |",
        "| Unified memory | `memory.omlx_gb + orbstack_gb + macos_apps_gb` over `total_gb`, plus `omlx.ceiling_gb` | rows say *not measured yet* |",
        "| Containers | `docker.containers` and `docker.orbstack_detected` | *not measured yet* |",
        "| Project workspace | `default_workspace` and `git_info` from the session payload | *not set*, with what the Harness will fall back to |",
        "",
        f"The telemetry route can answer with a simulated payload (`telemetry_source = "
        f"{', '.join(f'`{value}`' for value in server['simulated_sources']) or '—'}`) when no runtime responds, so the "
        "monitor rail still has something to draw. The card does not hide that: when it happens the basis is "
        "*unverified*, the summary pill reads **Demo data**, and the claim says the runtime and memory rows describe no "
        "machine while the workspace row still does.",
        "",
        "That is the whole point of the evidence component: a number without its basis is a claim the reader cannot check.",
        "",
        "## The three bases the card can show",
        "",
        "| Basis | When | What it means |",
        "|---|---|---|",
        "| `measured` | a live telemetry payload | the runtime's own report, read just now |",
        "| `unverified` | the simulated payload | the demo's numbers; the workspace row is still this machine's |",
        "| `unavailable` | no payload at all | nothing here describes this machine |",
        "",
        "## One workspace, two panes",
        "",
        f"Start reads the configured directory from the session payload and writes it through `POST "
        f"{server['workspace_route']}` (app.py:{server['workspace_route_line']}) — the same handler the Harness uses, "
        "which is why the Harness's own input updates when Start's does (`syncProjectDirInputs` owns the list of inputs "
        "that are the same setting).",
        "",
        "After a change the pane re-reads the preflight, because a checklist that keeps saying the workspace is "
        "unwritable after it was set is worse than no checklist. A failure is not a toast: it is the error presentation, "
        "naming the cause and *which directory is still in force*, with Retry and a path to the Harness.",
        "",
        "## Rules this artifact enforces",
        "",
        "`--check` fails when the health card polls instead of reading the rail's payload, when the rail stops feeding it "
        "or keeps a payload after a failure, when a row reads something other than the payload, when the simulated case "
        "has no branch or the claim stops naming which rows remain real, when a tone is outside the shared vocabulary, "
        "when either card bypasses the status or evidence component, when the workspace is written any way other than "
        "the Harness's handler, when a change does not re-read the preflight, when a failure is lost, when either "
        "card is not painted at startup, when the claim's read time comes from a field the payload does not carry, when the "
        "card drops a row because it could not read it, or when a failed read presents no cause, no way to re-read, and "
        "no statement of what it did not affect, or when the failure box and the card footer print the same buttons at once.",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "`node scripts/start_health_walk.mjs` reads `/api/telemetry` itself, holds the card's rows against it, asserts "
        "the basis matches what the host actually produced, sets a workspace through the pane and restores it, and "
        "refuses both routes to check the failure states.",
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html`, or a field in `GET /api/telemetry`.",
        "2. `python3 scripts/uiux_start_health_inventory.py`.",
        "3. `node scripts/start_health_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the artifacts drifted or a rule broke",
    )
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
            print("error: the start-health inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_start_health_inventory.py")
            return 1
        if payload["findings"]:
            print("error: Start and the machine disagree:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"start health OK: {payload['totals']['health_rows']} rows, {payload['totals']['bases']} bases, "
            f"{payload['totals']['tones']} tones, {payload['totals']['routes']} routes, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['bases']} bases, {payload['totals']['routes']} routes, "
        f"{len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
