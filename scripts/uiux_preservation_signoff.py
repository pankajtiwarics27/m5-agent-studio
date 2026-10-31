#!/usr/bin/env python3
"""Freeze the preservation surface and the rollback path for the owner's P0-06 sign-off.

    python3 scripts/uiux_preservation_signoff.py            # regenerate JSON + page
    python3 scripts/uiux_preservation_signoff.py --check    # fail if either drifted
    python3 scripts/uiux_preservation_signoff.py --json     # print the package

`UIUX-P0-06` asks for an owner's approval of the preservation baseline and the
rollback path. An agent may prepare that package; it may not sign it. This script
prepares it from the artifacts the earlier P0 stories produced, so the owner is
approving measured state rather than an agent's summary:

* the capabilities that must survive come from the ledger's preservation matrix,
  carried row for row, with no row merged, dropped or reworded;
* the controls that implement them come from the P0-02 inventory, with their
  ids, labels and inline handlers, attributed to the pane that contains them;
* the routes a pane's buttons can reach come from the P0-03 map, at one call
  depth: the handler named in a control's `onclick` is matched against the
  functions that hold a `fetch` call. A control whose handler sits behind another
  helper is not counted, and the page says so;
* what can refuse those state-changing routes comes from the P0-04 trace, so the
  approval covers the boundary, not just the widgets.

The package is always published as **unsigned** unless the ledger itself carries
an owner approval record. A generated page cannot approve itself.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MANIFEST = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"
INVENTORY = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Control_Inventory.json"
ROUTE_MAP = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Route_Map.json"
TRACE = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Authorization_Trace.json"
SCREENSHOT_INDEX = PROJECT_ROOT / "docs" / "media" / "screenshots" / "index.json"
SIGNOFF_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Preservation_Signoff.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-p0-signoff.md"

SCHEMA_VERSION = "1.0.0"
STORY = "UIUX-P0-06"
FRONTEND = PROJECT_ROOT / "static" / "index.html"
ROUTE_MAP_SCRIPT = PROJECT_ROOT / "scripts" / "uiux_route_map.py"
# How far the pane -> route walk follows calls inside the inline script. Two hops
# catches the common shape (button -> sendChat -> consumeSSE -> fetch) without
# pretending to be a call graph; the depth is published in the package.
MAX_CALL_DEPTH = 2
CALL_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*\(")


class SignoffError(RuntimeError):
    """An input artifact is missing or disagrees with the others, so the package cannot be trusted."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        raise SignoffError(f"{label} is missing: {path.relative_to(PROJECT_ROOT)}")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SignoffError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SignoffError(f"{label} must be a JSON object")
    return loaded


def _handler_of(onclick: str | None) -> str | None:
    """The function a control calls directly, when its handler is a plain call."""
    if not onclick:
        return None
    match = CALL_RE.match(onclick)
    return match.group(1) if match else None


def _approval(ledger: dict[str, object]) -> dict[str, object]:
    """The owner approval, if the ledger records one. Absent means unsigned."""
    coordination = ledger.get("coordination")
    record = coordination.get("preservation_signoff") if isinstance(coordination, dict) else None
    if not isinstance(record, dict):
        return {"approved": False, "approved_by": None, "approved_at": None, "reference": None}
    approved = bool(record.get("approved"))
    if approved and not (record.get("approved_by") and record.get("approved_at") and record.get("reference")):
        # Fail closed: an approval without a signer, a date and a place to read it
        # is not an approval, and publishing it as one would forge the owner's name.
        raise SignoffError(
            "the ledger records an owner approval without approved_by, approved_at and reference; "
            "a partial signature is not a signature"
        )
    return {
        "approved": approved,
        "approved_by": record.get("approved_by") if approved else None,
        "approved_at": record.get("approved_at") if approved else None,
        "reference": record.get("reference") if approved else None,
    }


def _screenshot_count() -> tuple[int, int]:
    """How many baseline screenshots exist, out of how many the manifest plans."""
    manifest = PROJECT_ROOT / "docs" / "media" / "screenshots" / "manifest.json"
    plan = _load(manifest, "the screenshot manifest")
    shots = plan.get("shots") if isinstance(plan.get("shots"), list) else []
    if not SCREENSHOT_INDEX.is_file():
        return 0, len(shots)
    index = _load(SCREENSHOT_INDEX, "the screenshot index")
    captured = index.get("shots") if isinstance(index.get("shots"), list) else []
    return len(captured), len(shots)


def _route_map_helpers() -> tuple[object, object]:
    """The route map's own script scan, so both artifacts read the file the same way."""
    spec = importlib.util.spec_from_file_location("uiux_route_map_for_signoff", ROUTE_MAP_SCRIPT)
    if spec is None or spec.loader is None:
        raise SignoffError("cannot load scripts/uiux_route_map.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.inline_script, module.callable_functions


def _callee_map(script: str, functions: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    """Names each function calls, restricted to functions this script defines."""
    known = set(functions) | {"fetch"}
    callees: dict[str, list[str]] = {}
    for name, info in functions.items():
        body = script[int(info["body_start"]) : int(info["end"])]
        found: list[str] = []
        for match in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", body):
            called = match.group(1)
            if called in known and called != name and called not in found:
                found.append(called)
        callees[name] = found
    return callees


def _routes_by_handler(
    calls: list[dict[str, object]], trace_rows: dict[tuple[str, str], dict[str, object]]
) -> dict[str, list[dict[str, object]]]:
    """Every route a fetch-holding function can reach, with its entry point.

    `consumeSSE` fetches whatever URL its caller handed it, so its call sites map
    to eight routes. Blaming all eight on every pane that reaches `consumeSSE`
    would turn a shared helper into a false claim, so each route keeps the
    function that supplied the literal (`entry_point`): the walk only attributes
    a route to a pane when it passes through that function.
    """
    table: dict[str, list[dict[str, object]]] = {}
    for call in calls:
        handler = str(call["handler"])
        bucket = table.setdefault(handler, [])
        for item in call["urls"]:  # type: ignore[union-attr]
            kind = str(item["kind"])
            chain = item.get("forwarded_via") or []
            entry_point = str(chain[-1]) if chain else handler
            if kind == "exact":
                keys = [(str(call["method"]), str(item["route"]), "certain")]
            elif kind == "dynamic":
                keys = [
                    (str(candidate["method"]), str(candidate["path"]), "possible")
                    for candidate in item["candidates"]  # type: ignore[union-attr]
                ]
            else:
                continue
            for method, path, certainty in keys:
                row = trace_rows.get((method, path))
                entry = {
                    "method": method,
                    "path": path,
                    "certainty": certainty,
                    "state_changing": row is not None,
                    "gates": [gate["gate"] for gate in row["shared_gates"]] if row is not None else [],  # type: ignore[index]
                    "entry_point": entry_point,
                }
                if entry not in bucket:
                    bucket.append(entry)
    return table


def _pane_routes(
    handlers: set[str],
    routes_by_handler: dict[str, list[dict[str, object]]],
    callees: dict[str, list[str]],
) -> tuple[list[dict[str, object]], int]:
    """Routes reachable from a pane's controls, following calls inside the script.

    Breadth-first from the handlers the controls name, `MAX_CALL_DEPTH` hops into
    the inline script, keeping only routes whose literal was supplied by a
    function the walk passed through. Returns the routes and how many were
    excluded for failing that test, so the page can show the filter working.
    """
    seen: set[str] = set()
    queue: list[tuple[str, list[str]]] = [(name, [name]) for name in sorted(handlers)]
    found: dict[tuple[str, str], dict[str, object]] = {}
    depth_of: dict[str, int] = {name: 0 for name in handlers}
    excluded = 0

    while queue:
        name, chain = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        chain_set = set(chain)
        for route in routes_by_handler.get(name, []):
            key = (str(route["method"]), str(route["path"]))
            if str(route["entry_point"]) not in chain_set:
                if key not in found:
                    excluded += 1
                continue
            if key not in found:
                found[key] = {k: v for k, v in route.items() if k != "entry_point"} | {"handler": name, "via": chain}
        depth = depth_of.get(name, 0)
        if depth >= MAX_CALL_DEPTH:
            continue
        for callee in callees.get(name, []):
            if callee == "fetch" or callee in seen:
                continue
            depth_of.setdefault(callee, depth + 1)
            queue.append((callee, [*chain, callee]))
    return [found[key] for key in sorted(found)], excluded


def build_signoff() -> dict[str, object]:
    ledger = _load(LEDGER, "the UI/UX ledger")
    inventory = _load(INVENTORY, "the control inventory")
    route_map = _load(ROUTE_MAP, "the route map")
    trace = _load(TRACE, "the authorization trace")
    manifest = _load(MANIFEST, "the source manifest")

    pin = str(ledger.get("source_commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise SignoffError("the ledger's source_commit_sha is not a full 40-char sha; re-pin it first")
    for label, artifact, key in (
        ("the control inventory", inventory, "pinned_source_commit"),
        ("the route map", route_map, "pinned_source_commit"),
        ("the authorization trace", trace, "pinned_source_commit"),
    ):
        if str(artifact.get(key)) != pin:
            raise SignoffError(f"{label} is pinned to {artifact.get(key)!r}, not to the ledger's {pin}; regenerate it")

    registry = ledger.get("tab_registry")
    matrix = ledger.get("feature_preservation_matrix")
    if not isinstance(registry, list) or not registry:
        raise SignoffError("the ledger has no tab registry")
    if not isinstance(matrix, list) or not matrix:
        raise SignoffError("the ledger has no preservation matrix")

    panes_by_id = {str(pane["dom_id"]): pane for pane in inventory.get("panes", [])}  # type: ignore[union-attr]
    calls = route_map.get("calls")
    if not isinstance(calls, list):
        raise SignoffError("the route map has no calls")
    trace_rows = {(str(row["method"]), str(row["path"])): row for row in trace.get("routes", [])}  # type: ignore[union-attr]

    inline_script, callable_functions = _route_map_helpers()
    script, _offset = inline_script(FRONTEND.read_text(encoding="utf-8"))  # type: ignore[operator]
    callees = _callee_map(str(script), callable_functions(str(script)))  # type: ignore[operator]
    routes_by_handler = _routes_by_handler(calls, trace_rows)

    panes: list[dict[str, object]] = []
    for entry in registry:
        dom_id = str(entry["dom_id"])
        if dom_id not in panes_by_id:
            raise SignoffError(f"the ledger names a pane {dom_id!r} that the inventory does not contain")
        source = panes_by_id[dom_id]
        controls = source.get("controls") or []
        handlers = sorted({name for name in (_handler_of(c.get("onclick")) for c in controls) if name})
        capabilities = [
            {
                "capability": str(row["existing"]),
                "target": str(row["target"]),
                "must_preserve": str(row["must_preserve"]),
            }
            for row in matrix
            if str(row["tab"]) == str(entry["label"])
        ]
        if not capabilities:
            raise SignoffError(f"pane {entry['label']!r} has no preservation row; the matrix cannot be the baseline")
        pane_routes, pane_excluded = _pane_routes(set(handlers), routes_by_handler, callees)
        panes.append(
            {
                "label": str(entry["label"]),
                "dom_id": dom_id,
                "keyboard_shortcut": str(entry.get("keyboard_shortcut") or ""),
                "purpose": str(entry.get("purpose") or ""),
                "screenshot_id": str(entry.get("screenshot_id") or ""),
                "preserved_capabilities": capabilities,
                "control_count": int(source.get("control_count") or 0),
                "counts": source.get("counts") or {},
                "controls": [
                    {
                        "tag": control.get("tag"),
                        "id": control.get("id"),
                        "text": control.get("text"),
                        "onclick": control.get("onclick"),
                        "handler": _handler_of(control.get("onclick")),
                    }
                    for control in controls
                ],
                "declared_handlers": handlers,
                "routes_reached": pane_routes,
                "routes_excluded_by_entry_point": pane_excluded,
            }
        )

    matrix_rows = sum(len(pane["preserved_capabilities"]) for pane in panes)  # type: ignore[arg-type]
    if matrix_rows != len(matrix):
        raise SignoffError(
            f"the package carries {matrix_rows} of {len(matrix)} preservation rows: a row is unattributed "
            "to any pane in the tab registry"
        )

    totals = inventory.get("totals") if isinstance(inventory.get("totals"), dict) else {}
    map_totals = route_map.get("totals") if isinstance(route_map.get("totals"), dict) else {}
    trace_totals = trace.get("totals") if isinstance(trace.get("totals"), dict) else {}
    captured, planned = _screenshot_count()
    approval = _approval(ledger)

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiux_preservation_signoff",
        "program": "M5_2_0_UIUX",
        "story": STORY,
        "generator": "scripts/uiux_preservation_signoff.py",
        "regenerate_with": "python3 scripts/uiux_preservation_signoff.py",
        "check_with": "python3 scripts/uiux_preservation_signoff.py --check",
        "statement": (
            "The preservation surface and the rollback path, frozen at one commit for the owner's P0-06 "
            "approval: the capabilities the program must keep, the controls that implement them, the routes "
            "those controls reach, the boundary that can refuse those routes, and what reverting a story "
            "does and does not touch."
        ),
        "prepared_not_signed": (
            "Prepared by an agent. An agent may prepare this package; it may not approve it. The approval "
            "lives in the ledger's coordination.preservation_signoff record, and this page reports it."
        ),
        "pinned_source_commit": pin,
        "status": "approved" if approval["approved"] else "unsigned",
        "signature": approval | {
            "signed": approval["approved"],
            "statement": (
                "Signed in the ledger; this page only reports the record."
                if approval["approved"]
                else "No signature. This is material for the owner to approve, not an approved baseline."
            ),
        },
        "inputs": {
            "ledger_sha256": _sha256(LEDGER),
            "source_manifest_sha256": _sha256(MANIFEST),
            "control_inventory_sha256": _sha256(INVENTORY),
            "route_map_sha256": _sha256(ROUTE_MAP),
            "authorization_trace_sha256": _sha256(TRACE),
            "frontend_sha256": str(manifest.get("frontend", {}).get("sha256", "")) if isinstance(manifest.get("frontend"), dict) else "",
        },
        "approval_request": [
            {
                "id": 1,
                "ask": "Accept the preservation matrix as the baseline P1 must preserve",
                "evidence": f"{matrix_rows} capability rows across {len(panes)} panes, carried row for row from "
                f"product/M5_2_0_UIUX_Autonomous_Roadmap.json",
            },
            {
                "id": 2,
                "ask": "Accept the control inventory as the list of controls that must stay reachable",
                "evidence": f"{totals.get('markup_controls')} markup controls "
                f"({totals.get('chrome_controls')} chrome, {totals.get('pane_controls')} panes) plus "
                f"{totals.get('script_rendered_controls')} rendered by script",
            },
            {
                "id": 3,
                "ask": "Accept that every browser-reachable route stays mounted, and that state-changing ones stay traced",
                "evidence": f"{map_totals.get('routes')} routes exist; {map_totals.get('routes_used_by_frontend')} "
                f"have a browser caller; {trace_totals.get('traced_routes')} state-changing routes carry their "
                f"gates, refusals and state",
            },
            {
                "id": 4,
                "ask": "Accept that P1 may not weaken the shared boundary",
                "evidence": "host allowlist (400), cross-origin rejection (403), bearer token (401, 503 when "
                "unconfigured), rate limit (429) and body-size limit (413), read from the middleware itself",
            },
            {
                "id": 5,
                "ask": "Accept the rollback path: one merge commit per story, no schema change, artifacts regenerate",
                "evidence": "no artifact in this program adds a table, a migration or a privilege; each story is "
                "reverted with `git revert -m 1 <merge sha>` and re-verified with the commands listed below",
            },
        ],
        "totals": {
            "panes": len(panes),
            "preserved_capabilities": matrix_rows,
            "markup_controls": int(totals.get("markup_controls") or 0),
            "chrome_controls": int(totals.get("chrome_controls") or 0),
            "pane_controls": int(totals.get("pane_controls") or 0),
            "script_rendered_controls": int(totals.get("script_rendered_controls") or 0),
            "routes": int(map_totals.get("routes") or 0),
            "routes_used_by_frontend": int(map_totals.get("routes_used_by_frontend") or 0),
            "state_changing_routes_traced": int(trace_totals.get("traced_routes") or 0),
            "pane_routes_reached": sum(len(pane["routes_reached"]) for pane in panes),  # type: ignore[arg-type]
            "pane_routes_excluded": sum(int(pane["routes_excluded_by_entry_point"]) for pane in panes),  # type: ignore[arg-type]
            "route_walk_depth": MAX_CALL_DEPTH,
            "screenshots_captured": captured,
            "screenshots_planned": planned,
        },
        "panes": panes,
        "chrome": {
            "control_count": int((inventory.get("chrome") or {}).get("control_count") or 0),  # type: ignore[union-attr]
            "counts": (inventory.get("chrome") or {}).get("counts") or {},  # type: ignore[union-attr]
            "note": "Tabs, command palette, modals and the shared toolbar. A pane story may restyle chrome, "
            "but every control here stays reachable: the inventory test fails if one disappears.",
        },
        "rollback": {
            "strategy": "One story, one merge commit. Revert the merge, not the individual commits, so the "
            "reverted tree is the tree that ran green before the story.",
            "steps": [
                "Identify the story's merge commit: `git log --merges --oneline --grep 'UIUX-<story>' origin/main`.",
                "Revert it: `git revert -m 1 <merge-sha>` on a branch, then open a pull request that names the story.",
                "Regenerate the derived artifacts if the revert changed any pinned file: "
                "`python3 scripts/uiux_source_manifest.py`, `scripts/uiux_control_inventory.py`, "
                "`scripts/uiux_route_map.py`, `scripts/uiux_authorization_trace.py`, `scripts/uiux_preservation_signoff.py`.",
                "Re-run the verification commands below and record their exit codes in the ledger.",
            ],
            "not_touched": [
                "No database migration, table or column is added by any P0 or P1 story: durable task state, "
                "approvals, event cursors and workspace files are read, never restructured.",
                "No new privilege: the bearer token, host allowlist, origin check, rate limit and body-size "
                "limit are read by the UI, not changed by it.",
                "User workspace data under the configured workspace directory is never rewritten by a revert.",
            ],
            "verify_after_revert": [
                ".venv/bin/python -m pytest -q",
                ".venv/bin/python -m ruff check .",
                ".venv/bin/python -m mypy",
                "python3 scripts/check_frontend_syntax.py",
                "python3 scripts/demo_tour.py --check",
                "python3 scripts/uiux_source_manifest.py --check",
                "python3 scripts/uiux_control_inventory.py --check",
                "python3 scripts/uiux_route_map.py --check",
                "python3 scripts/uiux_authorization_trace.py --check",
                "python3 scripts/uiux_preservation_signoff.py --check",
                "python3 scripts/validate_docs.py",
            ],
        },
        "not_verified": [
            "Nothing here was rendered or clicked. A control counted in the markup is present in the file, "
            "not proven to work, and no keyboard, contrast or screen-reader check has been run.",
            f"Pane-to-route attribution follows calls inside the inline script at most {MAX_CALL_DEPTH} hops from "
            "the handler a control names, and keeps a route only when the walk passes the function that supplied "
            "its literal. A deeper chain is invisible, so a pane with no routes below is not a pane that calls "
            "nothing; the excluded count on each pane shows the filter refusing a shared helper's other callers.",
            "The preservation matrix is a list of capabilities in words. This package carries it row for row "
            "and does not decide which control implements which row; that mapping is what the owner is "
            "being asked to confirm.",
            f"{captured} of {planned} baseline screenshots exist, so the approval cannot rest on visual "
            "evidence: it rests on the inventory, the map, the trace and the test suite.",
            "Approval is an owner act recorded in the ledger. A green test suite proves the package matches "
            "the tree; it does not prove the tree is acceptable.",
        ],
    }


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|") if text else "—"


def render_doc(package: dict[str, object]) -> str:
    totals = package["totals"]
    signature = package["signature"]
    approval = package["approval_request"]
    rollback = package["rollback"]

    lines = [
        "# M5 2.0 P0 preservation and rollback sign-off",
        "",
        "Generated by `python3 scripts/uiux_preservation_signoff.py` — **never hand-edit this page**.",
        "It is the `UIUX-P0-06` package: the capabilities the program must keep, the controls that",
        "implement them, the routes those controls reach, the boundary that can refuse those routes,",
        "and what a revert does and does not touch.",
        "",
        f"* Status: **{package['status']}** — {signature['statement']}",
        f"* Pinned source commit: `{package['pinned_source_commit']}`.",
        f"* {totals['panes']} panes · {totals['preserved_capabilities']} preserved capabilities · "
        f"{totals['markup_controls']} markup controls ({totals['chrome_controls']} chrome, "
        f"{totals['pane_controls']} panes) · {totals['script_rendered_controls']} rendered by script.",
        f"* {totals['routes']} routes exist, {totals['routes_used_by_frontend']} with a browser caller, "
        f"{totals['state_changing_routes_traced']} state-changing routes traced.",
        f"* Baseline screenshots: {totals['screenshots_captured']} of {totals['screenshots_planned']}.",
        "",
        "## What this page is, and is not",
        "",
        str(package["prepared_not_signed"]),
        "",
        "It is frozen at one commit on purpose: an approval of a moving surface approves nothing. If any",
        "input artifact changes, `--check` fails and the package has to be regenerated — which is the point",
        "at which the owner sees what changed.",
        "",
        "## What the owner is being asked to approve",
        "",
        "| # | Ask | Evidence in this package |",
        "|---|---|---|",
    ]
    for item in approval:
        lines.append(f"| {item['id']} | {item['ask']} | {_cell(item['evidence'])} |")

    lines += [
        "",
        "## The preservation surface, pane by pane",
        "",
        "Each pane carries every matrix row that names it, every control the inventory attributes to it,",
        "and the routes its own controls reach at one call depth.",
        "",
    ]
    for pane in package["panes"]:
        reached = pane["routes_reached"]
        lines += [
            f"### {pane['label']} (`data-tab=\"{pane['dom_id']}\"`, {pane['keyboard_shortcut'] or 'no shortcut'})",
            "",
            str(pane["purpose"]),
            "",
            f"**Must preserve — {len(pane['preserved_capabilities'])} capability row(s):**",
            "",
            "| Capability | Target | What must not change |",
            "|---|---|---|",
        ]
        for capability in pane["preserved_capabilities"]:
            lines.append(
                f"| {_cell(capability['capability'])} | {_cell(capability['target'])} "
                f"| {_cell(capability['must_preserve'])} |"
            )
        lines += [
            "",
            f"**Controls — {pane['control_count']} in the markup:**",
            "",
            "| Tag | Id | Label | Handler |",
            "|---|---|---|---|",
        ]
        for control in pane["controls"]:
            lines.append(
                f"| {control['tag']} | {_cell(control['id'])} | {_cell(control['text'])} | {_cell(control['onclick'])} |"
            )
        depth = package["totals"]["route_walk_depth"]
        excluded = pane.get("routes_excluded_by_entry_point", 0)
        lines += [
            "",
            f"**Routes reached from this pane's controls — {len(reached)}, walking at most {depth} calls; "
            f"{excluded} route(s) reached through the same shared helper were excluded because this pane's walk "
            f"never passes the function that supplied their URL:**",
            "",
        ]
        if not reached:
            lines.append(
                f"None within {depth} calls. That is a limit of the walk, not proof that the pane calls "
                "nothing: a handler that reaches its fetch through a longer chain is invisible here."
            )
        else:
            lines += [
                "| Method | Route | Certainty | State-changing | Walk | Gates that can refuse it |",
                "|---|---|---|---|---|---|",
            ]
            for route in reached:
                gates = ", ".join(f"`{gate}`" for gate in route["gates"]) or "—"
                walk = " → ".join(f"`{name}`" for name in route["via"])
                lines.append(
                    f"| {route['method']} | `{route['path']}` | {route['certainty']} "
                    f"| {'yes' if route['state_changing'] else 'no'} | {walk} | {gates} |"
                )
        lines.append("")

    chrome = package["chrome"]
    lines += [
        "## Shared chrome",
        "",
        f"{chrome['control_count']} controls: " + ", ".join(f"{k} {v}" for k, v in sorted(chrome["counts"].items())) + ".",
        "",
        str(chrome["note"]),
        "",
        "## The rollback path",
        "",
        str(rollback["strategy"]),
        "",
        "**Steps:**",
        "",
    ]
    lines += [f"{index}. {step}" for index, step in enumerate(rollback["steps"], start=1)]
    lines += ["", "**What a revert does not touch:**", ""]
    lines += [f"* {item}" for item in rollback["not_touched"]]
    lines += ["", "**Verification after a revert:**", ""]
    lines += [f"* `{command}`" for command in rollback["verify_after_revert"]]

    lines += [
        "",
        "## Approval record",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Status | **{package['status']}** |",
        f"| Approved by | {_cell(signature['approved_by'])} |",
        f"| Approved at | {_cell(signature['approved_at'])} |",
        f"| Reference | {_cell(signature['reference'])} |",
        "",
        "An approval is recorded in `product/M5_2_0_UIUX_Autonomous_Roadmap.json` under",
        "`coordination.preservation_signoff` as `approved_by`, `approved_at` and `reference`, and this page",
        "regenerates to report it. A record missing any of the three fails the generator rather than",
        "publishing a half-signature as an approval.",
        "",
        "## Not verified",
        "",
    ]
    lines += [f"* {item}" for item in package["not_verified"]]
    lines.append("")
    return "\n".join(lines)


def _rendered() -> tuple[str, str]:
    package = build_signoff()
    return json.dumps(package, indent=2, ensure_ascii=False) + "\n", render_doc(package)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if either artifact has drifted")
    parser.add_argument("--json", action="store_true", help="print the package and write nothing")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args(argv)

    try:
        payload, page = _rendered()
    except (SignoffError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        sys.stdout.write(payload)
        return 0

    if args.check:
        drifted = []
        for path, expected in ((SIGNOFF_PATH, payload), (DOC_PATH, page)):
            if not path.is_file():
                print(f"error: {path.name} is missing", file=sys.stderr)
                return 1
            if path.read_text(encoding="utf-8") != expected:
                drifted.append(path.name)
        if drifted:
            print(
                "error: the P0-06 sign-off package has drifted from the ledger or the P0 artifacts: "
                + ", ".join(drifted)
                + "\n       Regenerate it (the change belongs to the story):"
                + "\n           python3 scripts/uiux_preservation_signoff.py",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            package = json.loads(payload)
            totals = package["totals"]
            print(
                "UI preservation sign-off is up to date: "
                f"{package['status']}, {totals['preserved_capabilities']} capabilities across "
                f"{totals['panes']} panes, {totals['markup_controls']} markup controls, "
                f"{totals['state_changing_routes_traced']} state-changing routes traced"
            )
        return 0

    SIGNOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNOFF_PATH.write_text(payload, encoding="utf-8")
    DOC_PATH.write_text(page, encoding="utf-8")
    if not args.quiet:
        package = json.loads(payload)
        totals = package["totals"]
        print(
            f"Wrote {SIGNOFF_PATH.name} and {DOC_PATH.name}: {package['status']}, "
            f"{totals['preserved_capabilities']} capabilities, {totals['panes']} panes"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
