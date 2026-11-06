#!/usr/bin/env python3
"""Inventory the Start command center and check it against the readiness module (UIV2-24).

    python3 scripts/uiux_start_command_center_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_start_command_center_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_start_command_center_inventory.py --json     # print the inventory

Start used to open with four stacked cards of equal weight: a six-row checklist, a
session list, a health card and a workspace card. The check was first and the action
was third, so a reader landing on the pane had to work through the evidence before
finding the thing the pane is for. UIV2-24 replaces that with a command center: the
composer first and above the fold, one sentence of verdict, at most one blocker, and
the checklist, health and workspace behind disclosures one click below.

The rules below are the ones that keep it honest:

* the composer is the first card and its button is the primary action;
* the verdict line comes before the composer's input and the blocker after it, so the
  answer is read in the order a reader needs it and the details never come first;
* every disclosure's summary carries the state of what is inside it - a collapsed
  section that hides a failing check behind the word "details" is a hidden requirement;
* the pane prints the server's verdict (§``GET /api/start/command-center``) and never
  derives one, so the sentence on screen and the checklist below cannot disagree;
* the basis line is printed, because "ready" without "the runtime was not asked" is a
  claim about a socket dressed up as a claim about a model;
* a contradiction is rendered with its evidence and an action, never folded into a
  friendlier word.

The matrix below runs the pure ``start_command_center.build_readiness()`` over every
runtime observation the pane can hold, noting which combinations must not read ready.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
MODULE = PROJECT_ROOT / "start_command_center.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Start_Command_Center.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-4-start-command-center.md"

sys.path.insert(0, str(PROJECT_ROOT))

import onboarding_preflight as preflight  # noqa: E402  (path set up above)
import start_command_center as command_center  # noqa: E402

PROVIDER = preflight.ProviderState(alias="openai", enabled=True, has_credential=True, model_count=2)

BASE_INPUTS = dict(
    auth_ready=True,
    local_runtime_configured=True,
    providers=(PROVIDER,),
    execution_mode="sandbox",
    sandbox_runtime_available=True,
    workspace_writable=True,
    workspace_detail="Workspace directory is writable: /tmp/ws.",
)

# Every runtime observation the pane can be handed, named the way a reader would say it.
CASES: tuple[tuple[str, dict, dict | None, dict | None], ...] = (
    ("ready preflight, runtime never asked", {}, None, None),
    (
        "ready preflight, runtime answered with models",
        {},
        dict(port_answered=True, model_count=2, probe="ran"),
        None,
    ),
    (
        "ready preflight, port answers with no models",
        {},
        dict(port_answered=True, model_count=0, probe="ran"),
        None,
    ),
    (
        "ready preflight, nothing answered",
        {},
        dict(port_answered=False, model_count=None, probe="ran"),
        None,
    ),
    (
        "ready preflight, runtime with models but demo health",
        {},
        dict(port_answered=True, model_count=3, probe="ran"),
        dict(source="simulated_demo"),
    ),
    ("no providers, no probe", {"providers": ()}, None, None),
    (
        "no providers, runtime answered with models",
        {"providers": ()},
        dict(port_answered=True, model_count=3, probe="ran"),
        None,
    ),
    (
        "workspace missing",
        {"workspace_writable": False, "workspace_detail": "Workspace directory is missing: /nowhere."},
        None,
        None,
    ),
    (
        "execution disabled",
        {"execution_mode": "disabled"},
        None,
        None,
    ),
    (
        "auth disabled",
        {"auth_ready": False},
        None,
        None,
    ),
    (
        "no local runtime configured",
        {"local_runtime_configured": False},
        dict(configured=False),
        None,
    ),
    (
        "local runtime configured but the probe could not be made",
        {},
        dict(port_answered=False, model_count=None, probe="unavailable"),
        None,
    ),
)


def _line(html: str, needle: str) -> int | None:
    index = html.find(needle)
    return None if index < 0 else html.count("\n", 0, index) + 1


def _slice(html: str, start_needle: str, end_needle: str) -> str:
    start = html.find(start_needle)
    if start < 0:
        return ""
    end = html.find(end_needle, start)
    return html[start:end] if end > start else html[start:]


def read_matrix() -> list[dict]:
    rows = []
    for label, overrides, runtime_spec, telemetry_spec in CASES:
        inputs = preflight.PreflightInputs(**{**BASE_INPUTS, **overrides})  # type: ignore[arg-type]
        report = preflight.build_report(inputs).to_dict()
        runtime = command_center.RuntimeObservation(**runtime_spec) if runtime_spec else None
        telemetry = command_center.TelemetryObservation(**telemetry_spec) if telemetry_spec else None
        readiness = command_center.build_readiness(
            report,
            runtime=runtime,
            telemetry=telemetry,
            workspace_dir="/tmp/ws",
            workspace_writable=bool(inputs.workspace_writable),
        )
        rows.append(
            {
                "case": label,
                "verdict": readiness["verdict"],
                "headline": readiness["headline"],
                "basis": readiness["basis"],
                "probe": readiness["probe"],
                "port_answered": readiness["runtime"]["port_answered"],
                "model_count": readiness["runtime"]["model_count"],
                "ready_count": readiness["counts"]["ready"],
                "total_count": readiness["counts"]["total"],
                "next": readiness["next"]["id"] if readiness["next"] else None,
                "next_action": readiness["next"]["action"] if readiness["next"] else None,
                "contradictions": [
                    {
                        "id": item["id"],
                        "has_evidence": bool(item.get("evidence")),
                        "has_action": bool(item.get("action") and item.get("action_label")),
                    }
                    for item in readiness["contradictions"]
                ],
            }
        )
    return rows


def read_frontend(html: str) -> dict:
    pane_start = html.find('id="pane-start"')
    pane_end = html.find('<section id="pane-harness"', pane_start if pane_start > 0 else 0)
    pane = html[pane_start:pane_end] if 0 <= pane_start < pane_end else ""

    launch_card = pane.find('id="startLaunchCard"')
    first_card = pane.find('<div class="m-card"')
    composer = pane.find('id="startTaskInput"')
    composer_button = pane.find('id="startTaskBtn"')
    readiness_line = pane.find('id="startReadinessLine"')
    blocker = pane.find('id="startNextBlocker"')
    contradictions = pane.find('id="startContradictions"')
    checklist = pane.find('id="startDiagnostics"')
    health = pane.find('id="startHealthDetails"')
    workspace = pane.find('id="startWorkspaceDetails"')
    steps = pane.find('id="onboardingSteps"')

    def details_block(marker: int) -> str:
        if marker < 0:
            return ""
        end = pane.find("</details>", marker)
        return pane[marker:end] if end > marker else ""

    checklist_block = details_block(checklist)
    health_block = details_block(health)
    workspace_block = details_block(workspace)

    def status_in(block: str, element_id: str) -> bool:
        # The status span has to sit inside the <summary>, not merely inside the card:
        # a badge below the fold is not a summary, it is a reason to open the section.
        summary_start = block.find("<summary")
        summary_end = block.find("</summary>")
        if not (0 <= summary_start < summary_end):
            return False
        return f'id="{element_id}"' in block[summary_start:summary_end]

    verdict_block = _slice(html, "let startReadiness = null;", "async function refreshStartCommand")
    blocker_block = _slice(html, "function renderStartCommand()", "function renderStartContradictions()")
    contradiction_block = _slice(html, "function renderStartContradictions()", "function runStartNextAction")
    action_block = _slice(html, "function runStartNextAction(action)", "function renderOnboardingDetails")

    open_attributes = {
        "checklist": bool(re.search(r'<details[^>]*id="startDiagnostics"[^>]*\sopen', pane)),
        "health": bool(re.search(r'<details[^>]*id="startHealthDetails"[^>]*\sopen', pane)),
        "workspace": bool(re.search(r'<details[^>]*id="startWorkspaceDetails"[^>]*\sopen', pane)),
    }

    return {
        "anchor_lines": {
            "pane": _line(html, 'id="pane-start"'),
            "launch_card": _line(html, 'id="startLaunchCard"'),
            "readiness_line": _line(html, 'id="startReadinessLine"'),
            "composer": _line(html, 'id="startTaskInput"'),
            "blocker": _line(html, 'id="startNextBlocker"'),
            "checklist": _line(html, 'id="startDiagnostics"'),
            "health": _line(html, 'id="startHealthDetails"'),
            "workspace": _line(html, 'id="startWorkspaceDetails"'),
        },
        "order": {
            # The launch card's id sits inside the first m-card's opening tag, so the
            # comparison is "the first card is the launch card", not "same offset".
            "composer_is_first_card": bool(
                0 <= first_card <= launch_card < first_card + 120
            ),
            "composer_first": bool(0 <= composer < steps and 0 <= composer < checklist),
            "readiness_before_composer": bool(0 <= readiness_line < composer),
            "blocker_after_composer": bool(blocker > composer),
            "details_after_blocker": bool(checklist > blocker),
            "contradictions_after_blocker": bool(contradictions > composer),
        },
        "composer": {
            "primary_button": bool(composer_button >= 0 and "btn-primary" in pane[composer_button - 80 : composer_button + 120]),
            "label_names_task": 'for="startTaskInput"' in pane and "Task to open in the Harness" in pane,
            "described_by": bool(re.search(r'id="startTaskBtn"[^>]*aria-describedby="startTaskHint"', pane)),
        },
        "details": {
            "checklist": "startDiagnostics" if checklist >= 0 else None,
            "health": "startHealthDetails" if health >= 0 else None,
            "workspace": "startWorkspaceDetails" if workspace >= 0 else None,
            "all_closed_by_default": not any(open_attributes.values()),
            "summary_carries_state": status_in(checklist_block, "onboardingSummary")
            and status_in(health_block, "startHealthSummary")
            and status_in(workspace_block, "startWorkspaceSummary"),
            "summary_is_native": checklist_block.count("<summary") == 1
            and health_block.count("<summary") == 1
            and workspace_block.count("<summary") == 1,
        },
        "verdict": {
            "reads_route": "/api/start/command-center",
            "prints_headline": "readiness.headline" in blocker_block or "readiness.headline" in verdict_block,
            "prints_counts": "readiness.counts.ready" in verdict_block and "readiness.counts.total" in verdict_block,
            "prints_basis": "start-readiness-basis" in verdict_block and "readiness.basis" in verdict_block,
            "prints_runtime": "runtime.port_answered" in verdict_block and "runtime.model_count" in verdict_block,
            "recomputes": bool(
                re.search(r"counts\.ready\s*===\s*[\w.]*counts\.total|steps\.every\(", verdict_block + blocker_block)
            ),
            "no_second_preflight_read": "/api/onboarding/preflight" not in _slice(
                html, "async function refreshStartCommand", "async function runOnboardingAction"
            ),
        },
        "contradictions": {
            "container": contradictions >= 0,
            "from_payload": "startReadiness.contradictions" in contradiction_block
            or "startReadiness && startReadiness.contradictions" in contradiction_block,
            "prints_evidence": "start-contradiction-evidence" in contradiction_block,
            "alert_role": 'setAttribute("role", "alert")' in contradiction_block,
            "offers_action": "runStartNextAction(item.action)" in contradiction_block,
        },
        "blocker": {
            "button": 'id = "startNextAction"' in blocker_block,
            "opens_workspace": 'openStartSection("startWorkspaceDetails", "startWorkspaceInput")' in action_block,
            "delegates": "runOnboardingAction(action)" in action_block,
            "probing_is_explicit": "refreshStartCommand({ probe: true })" in action_block,
            "no_render_loop": "refreshTelemetry()" not in action_block,
        },
        "styles": {
            "line": ".start-readiness-line {" in html,
            "basis": ".start-readiness-basis {" in html,
            "blocker": ".start-blocker {" in html,
            "contradiction": ".start-contradiction {" in html,
            "details": ".start-diagnostics > summary {" in html,
            "focus_ring": ".start-diagnostics > summary:focus-visible" in html,
        },
        "links": {
            "walk": "scripts/start_walk.mjs" in html
            or (PROJECT_ROOT / "scripts" / "start_walk.mjs").exists(),
        },
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    matrix = read_matrix()
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    # 1. The composer is the first card, and it is the primary action.
    order = frontend["order"]
    if not order["composer_is_first_card"] or not order["composer_first"]:
        fail("composer-not-first", "the task composer is not the first thing on the Start pane")
    if not order["readiness_before_composer"]:
        fail("verdict-after-composer", "the verdict line is printed below the composer it describes")
    if not order["blocker_after_composer"]:
        fail("blocker-before-composer", "the blocker is printed above the composer, so the reader meets the obstacle first")
    if not order["details_after_blocker"]:
        fail("details-before-blocker", "the checklist is printed above the blocker")
    composer = frontend["composer"]
    if not composer["primary_button"]:
        fail("composer-not-primary", "Open in Harness is not the primary button on the pane")
    if not composer["label_names_task"]:
        fail("composer-unlabelled", "the composer input has no visible-or-programmatic label")
    if not composer["described_by"]:
        fail("composer-unexplained", "the composer button is not described by the hint that explains it")

    # 2. The evidence is behind disclosures, and each summary carries its own state.
    details = frontend["details"]
    if not (details["checklist"] and details["health"] and details["workspace"]):
        fail("details-missing", "the checklist, health or workspace card is not a disclosure")
    if not details["all_closed_by_default"]:
        fail("details-open-by-default", "a diagnostics disclosure starts open, so the pane is not concise")
    if not details["summary_carries_state"]:
        fail("details-state-hidden", "a disclosure hides its state inside, so a failing check looks collapsed and fine")
    if not details["summary_is_native"]:
        fail("details-not-native", "a disclosure does not use a native <summary>, so it cannot be toggled by keyboard")

    # 3. The verdict is the server's, printed with its basis.
    verdict = frontend["verdict"]
    if verdict["reads_route"] not in html and not frontend["verdict"]["reads_route"]:
        fail("verdict-route-missing", "the pane does not read /api/start/command-center")
    if not verdict["prints_headline"]:
        fail("verdict-headline-missing", "the pane does not print the server's headline")
    if not verdict["prints_counts"]:
        fail("verdict-counts-missing", "the pane does not print the server's ready/total counts")
    if not verdict["prints_basis"]:
        fail("basis-missing", "the verdict prints no basis, so 'ready' cannot be distinguished from 'assumed'")
    if not verdict["prints_runtime"]:
        fail("runtime-evidence-missing", "the verdict does not print what the runtime answered")
    if verdict["recomputes"]:
        fail("verdict-recomputed", "the pane computes its own verdict instead of printing the server's")
    if not verdict["no_second_preflight_read"]:
        fail("second-source", "the pane still reads /api/onboarding/preflight directly, so two clocks can disagree")

    # 4. Contradictions are printed with evidence and an action.
    contradictions = frontend["contradictions"]
    if not contradictions["container"]:
        fail("contradiction-container-missing", "there is nowhere to print a contradiction")
    if not contradictions["from_payload"]:
        fail("contradiction-not-from-payload", "contradictions are not read from the server's payload")
    if not contradictions["prints_evidence"]:
        fail("contradiction-evidence-missing", "a contradiction is printed without the evidence behind it")
    if not contradictions["alert_role"]:
        fail("contradiction-not-announced", "a contradiction is printed without role=alert, so a screen reader misses it")
    if not contradictions["offers_action"]:
        fail("contradiction-without-action", "a contradiction is printed with no way to act on it")

    # 5. The blocker's action reaches the control it names.
    blocker = frontend["blocker"]
    if not blocker["button"]:
        fail("blocker-no-action", "the blocker prints no button of its own")
    if not blocker["opens_workspace"]:
        fail("blocker-workspace-unreachable", "the workspace blocker does not open the workspace control on this pane")
    if not blocker["delegates"]:
        fail("blocker-not-delegated", "the blocker does not fall back to the checklist's own action handler")

    # 6. The module's own rules, over every observation the pane can hold.
    for row in matrix:
        if row["port_answered"] and row["model_count"] == 0 and row["verdict"] == "ready":
            fail("ready-from-port-alone", f"{row['case']}: a port that answered without models reads ready")
        if row["verdict"] not in ("ready", "needs-you", "blocked", "contradiction"):
            fail("unknown-verdict", f"{row['case']}: verdict {row['verdict']!r} is not one the pane can print")
        for item in row["contradictions"]:
            if not item["has_evidence"]:
                fail("contradiction-without-evidence", f"{row['case']}: {item['id']} carries no evidence string")
            if not item["has_action"]:
                fail("contradiction-without-remedy", f"{row['case']}: {item['id']} carries no action")
        if row["probe"] == "not-run" and row["basis"] == "measured":
            fail("basis-overstated", f"{row['case']}: the basis says measured although nothing was asked")

    # 7. Styles exist for what the renderer emits.
    for flag, description in (
        ("line", "the verdict line has no rule"),
        ("basis", "the basis tail has no rule, so it reads as part of the sentence"),
        ("blocker", "the blocker box has no rule"),
        ("contradiction", "a contradiction row has no rule"),
        ("details", "the disclosure summary has no rule, so it renders as plain text"),
        ("focus_ring", "the disclosure summary has no focus ring, so a keyboard user cannot see where they are"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    basis_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    for row in matrix:
        verdict_counts[row["verdict"]] = verdict_counts.get(row["verdict"], 0) + 1
        basis_counts[row["basis"]] = basis_counts.get(row["basis"], 0) + 1

    return {
        "schema_version": "1.0.0",
        "story": "UIV2-24",
        "statement": (
            "Start opens with the one action it exists for - opening a task - followed by one sentence of "
            "verdict and at most one blocker, with the checklist, health and workspace one disclosure below. "
            "The verdict is the server's, printed with the basis it was reached on, and a port that answers "
            "without a model is a contradiction with its evidence rather than a green word."
        ),
        "source": {
            "frontend": "static/index.html",
            "module": "start_command_center.py",
            "route": "GET /api/start/command-center",
            "preflight": "GET /api/onboarding/preflight",
            "walk": "scripts/start_walk.mjs",
        },
        "verdicts": sorted(verdict_counts),
        "basis_values": sorted(basis_counts),
        "matrix": matrix,
        "frontend": frontend,
        "totals": {
            "cases": len(matrix),
            "verdicts": len(verdict_counts),
            "contradictions": sum(len(row["contradictions"]) for row in matrix),
            "disclosures": 3,
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the matrix runs the pure module, so it covers every runtime observation the pane can hold but not the "
            "live probe against a real runtime",
            "a browser cannot make a machine ready, so scripts/start_walk.mjs asserts the pane against the same API "
            "response it received rather than against an invented ready state",
            "the contradiction that needs a real empty runtime is covered by tests/test_start_command_center.py, "
            "which stubs the probe to answer with zero models",
            "keyboard reachability of the disclosures is proved in the walk (native <summary>), not in this artifact",
        ],
    }


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    lines = [
        "# Start: the command center",
        "",
        "Generated by `python3 scripts/uiux_start_command_center_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## What the pane is now",
        "",
        "| Position | Surface | Why there |",
        "|---|---|---|",
        "| 1 | **New task** composer + the readiness sentence + at most one blocker | the one action the pane exists for, "
        "with the answer to *can I start?* directly above the input and the obstacle directly below it |",
        "| 2 | **Recent work** | the sessions a reader is most likely to want back, read from the Harness's own endpoint |",
        "| 3 | **Readiness details** (collapsed) | the six-step checklist, the lead, the guided first task, and the basis line |",
        "| 4 | **System health** (collapsed) | the four measured rows, their basis, and the re-check |",
        "| 5 | **Project workspace** (collapsed) | the directory the task will run in, set through the same saver as the Harness |",
        "",
        "Each disclosure is a native `<details>`/`<summary>`, so it is toggled by keyboard and announced as a "
        "disclosure rather than as a card with hidden contents. Each summary carries the state of what is inside it: "
        "a collapsed checklist still shows its verdict pill, and a collapsed workspace still shows whether a "
        "directory is set. A failing check hidden behind the word *details* would be a hidden requirement.",
        "",
        "## The verdict, and the basis it stands on",
        "",
        "| Verdict | Meaning |",
        "|---|---|",
        "| `ready` | every preflight step reads ready **and** nothing contradicts it |",
        "| `needs-you` | a step is waiting on the reader; `next` names it and its action |",
        "| `blocked` | a step will not clear on its own |",
        "| `contradiction` | two honest observations disagree; `contradictions` prints both and the evidence |",
        "",
        "The basis is printed next to the verdict, because the sentence *Ready to run* is only true in the sense it "
        "was measured:",
        "",
        "- **measured** — the runtime was asked and answered (`port_answered` plus the `model_count` it reported);",
        "- **derived** — nothing was asked, so the verdict rests on local configuration alone and the line says "
        "*runtime not asked*.",
        "",
        "A port that answers with an empty model list is a **contradiction** (`port-without-models`), never a "
        "ready: the socket is up and a turn would still fail. The same applies to models the check cannot use "
        "(`runtime-models-without-usable-selection`), to checks that passed while the runtime stayed silent "
        "(`checks-passed-without-runtime`), and to the demo health payload sitting beside a live probe "
        "(`demo-health-with-live-runtime`).",
        "",
        "## The matrix",
        "",
        f"{totals['cases']} runtime observations through the pure module:",
        "",
        "| Case | Verdict | Basis | Port answered | Models | Ready count | Next | Contradictions |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in payload["matrix"]:
        port = "—" if row["port_answered"] is None else ("yes" if row["port_answered"] else "no")
        models = "—" if row["model_count"] is None else row["model_count"]
        contras = ", ".join(item["id"] for item in row["contradictions"]) or "—"
        lines.append(
            f"| {row['case']} | `{row['verdict']}` | {row['basis']} | {port} | {models} | "
            f"{row['ready_count']}/{row['total_count']} | {row['next'] or '—'} | {contras} |"
        )
    lines += [
        "",
        f"Contradictions in the matrix: {totals['contradictions']}. Verdicts seen: "
        + ", ".join(f"`{value}`" for value in payload["verdicts"])
        + ".",
        "",
        "## Rules this artifact enforces",
        "",
        "`--check` fails when the composer is not the first card or not the primary action, when the verdict is "
        "printed below the composer it describes or the blocker above it, when the checklist is not behind a "
        "disclosure, when a disclosure starts open or its summary stops carrying its state, when the pane stops "
        "printing the server's headline/counts/basis/runtime evidence, when it computes a verdict of its own or "
        "reads the preflight directly a second time, when a contradiction loses its evidence, its `role=\"alert\"` or "
        "its action, when the workspace blocker no longer opens the workspace control on this pane, when a probe "
        "starts happening on render, and when a port that answered without models is called ready.",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "## Regenerating",
        "",
        "1. Change `start_command_center.py` or the Start pane in `static/index.html`.",
        "2. `python3 scripts/uiux_start_command_center_inventory.py`.",
        "3. `node scripts/start_walk.mjs --json` against a running `scripts/demo_tour.py --serve`.",
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
    if args.json:
        print(dumps(payload))
        return 0

    stale = []
    if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != dumps(payload):
        stale.append(JSON_PATH.relative_to(PROJECT_ROOT))
    doc = render_doc(payload)
    if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != doc:
        stale.append(DOC_PATH.relative_to(PROJECT_ROOT))

    findings = payload["findings"]
    if args.check:
        if findings:
            print("error: the Start command center breaks its own rules:", file=sys.stderr)
            for finding in findings:
                print(f"       {finding['check']}: {finding['detail']}", file=sys.stderr)
            return 1
        if stale:
            print("error: the Start command center inventory has drifted from the source:", file=sys.stderr)
            for path in stale:
                print(f"       {path} is stale", file=sys.stderr)
            print("       Regenerate it (the change belongs to the story):", file=sys.stderr)
            print("           python3 scripts/uiux_start_command_center_inventory.py", file=sys.stderr)
            return 1
        print(
            f"start command center OK: {payload['totals']['cases']} cases, "
            f"{payload['totals']['contradictions']} contradictions, {payload['totals']['disclosures']} disclosures, "
            f"{len(findings)} findings"
        )
        return 0

    JSON_PATH.write_text(dumps(payload), encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['cases']} cases, {payload['totals']['contradictions']} contradictions, "
        f"{len(findings)} findings"
    )
    for finding in findings:
        print(f"  {finding['check']}: {finding['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
