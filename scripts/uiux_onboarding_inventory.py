#!/usr/bin/env python3
"""Inventory the Start pane's readiness answer and check it against the preflight (UIUX-P2-01).

    python3 scripts/uiux_onboarding_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_onboarding_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_onboarding_inventory.py --json     # print the inventory

`onboarding_preflight.build_report()` is the only thing in the product that decides
what a first-run reader should do next. It returns six ordered steps, a
``completed_count``, a ``total_count`` and exactly one ``next_step`` - and a
``next_step`` is only useful if it carries the action that advances it.

The pane did not use any of that. It rendered six rows of equal weight, showed
``summary`` in a pill, and disabled the Run button with no reason printed anywhere.
A reader had to work out from six paragraphs which one was theirs, and a keyboard
user met a disabled control that explained nothing.

This story is that the pane prints the server's answer once, marks the row the server
named as ``next`` with ``aria-current="step"``, and states why Run is off. The rules
below are the ones that keep it honest:

* the server must never hand the pane a ``next_step`` with no action - a lead with a
  button that does nothing is worse than six plain rows;
* every step the server can emit must carry a title and a detail, because the pane
  prints them verbatim;
* the pane must not compute readiness itself - it renders ``completed_count``,
  ``total_count``, ``ready`` and ``next_step``, and a client-side recount could claim
  a state the preflight never measured;
* a step the server gave no action is printed as following the next one, rather than
  as a dead button;
* Run must be off *with a stated reason*, both visibly and as the button's accessible
  description;
* clearing a presentation must not drop focus on ``<body>``, which is what happened
  when the Re-check button that held it was removed.

The matrix below is derived by calling the pure module, not by reading it, so a new
branch in a step builder that produces an actionless step fails here.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Onboarding.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-start.md"

sys.path.insert(0, str(PROJECT_ROOT))

import onboarding_preflight as preflight  # noqa: E402  (path set up above)


def _provider(
    alias: str, *, enabled: bool = True, credential: bool = True, models: int = 4
):
    return preflight.ProviderState(
        alias=alias, enabled=enabled, has_credential=credential, model_count=models
    )


# Every branch of every step builder, named the way a reader would describe it.
CASES: tuple[tuple[str, dict], ...] = (
    (
        "fresh install, nothing configured",
        dict(
            auth_ready=False, local_runtime_configured=False, workspace_writable=False
        ),
    ),
    (
        "signed in, no model source",
        dict(auth_ready=True, local_runtime_configured=False, workspace_writable=True),
    ),
    (
        "provider saved without a key",
        dict(
            auth_ready=True,
            local_runtime_configured=False,
            workspace_writable=True,
            providers=(_provider("openrouter", credential=False),),
        ),
    ),
    (
        "provider saved and disabled",
        dict(
            auth_ready=True,
            local_runtime_configured=False,
            workspace_writable=True,
            providers=(_provider("openrouter", enabled=False),),
        ),
    ),
    (
        "cloud provider with models",
        dict(
            auth_ready=True,
            local_runtime_configured=False,
            workspace_writable=True,
            providers=(_provider("openrouter"),),
        ),
    ),
    (
        "local runtime configured, never probed",
        dict(auth_ready=True, local_runtime_configured=True, workspace_writable=True),
    ),
    (
        "local runtime probed and empty",
        dict(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=True,
            local_probe="ran",
            local_models_available=0,
        ),
    ),
    (
        "local runtime probed with models",
        dict(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=True,
            local_probe="ran",
            local_models_available=3,
        ),
    ),
    (
        "local runtime unreachable",
        dict(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=True,
            local_probe="unavailable",
        ),
    ),
    (
        "workspace not writable",
        dict(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=False,
            local_probe="ran",
            local_models_available=2,
        ),
    ),
    (
        "sandbox selected with no runtime",
        dict(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=True,
            local_probe="ran",
            local_models_available=2,
            execution_mode="sandbox",
        ),
    ),
    (
        "sandbox selected and available",
        dict(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=True,
            local_probe="ran",
            local_models_available=2,
            execution_mode="sandbox",
            sandbox_runtime_available=True,
        ),
    ),
    (
        "host execution enabled",
        dict(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=True,
            local_probe="ran",
            local_models_available=2,
            execution_mode="host",
        ),
    ),
)


class OnboardingError(Exception):
    """The pane and the preflight disagree."""


def _line(html: str, index: int) -> int:
    return html.count("\n", 0, index) + 1


def read_matrix() -> list[dict]:
    rows: list[dict] = []
    for label, kwargs in CASES:
        report = preflight.build_report(preflight.PreflightInputs(**kwargs))
        as_dict = report.to_dict()
        next_step = as_dict["next_step"]
        rows.append(
            {
                "case": label,
                "ready": as_dict["ready"],
                "blocked": as_dict["blocked"],
                "completed_count": as_dict["completed_count"],
                "total_count": as_dict["total_count"],
                "next_step": next_step["id"] if next_step else None,
                "next_step_title": next_step["title"] if next_step else None,
                "next_step_action": next_step["action"] if next_step else None,
                "next_step_action_label": next_step["action_label"]
                if next_step
                else None,
                "steps": [
                    {
                        "id": step["id"],
                        "status": step["status"],
                        "title": step["title"],
                        "has_action": bool(step["action"] and step["action_label"]),
                    }
                    for step in as_dict["steps"]
                ],
            }
        )
    return rows


def read_frontend(html: str) -> dict:
    lead = html.find('id="onboardingLead"')
    renderer_start = html.find("function renderOnboardingLead")
    renderer = (
        html[renderer_start : html.find("async function refreshOnboarding")]
        if renderer_start > 0
        else ""
    )
    steps_start = html.find("list.innerHTML = onboardingReport.steps.map")
    steps = html[steps_start : steps_start + 2200] if steps_start > 0 else ""
    clear_start = html.find("function clearPresentation")
    clear = html[clear_start : clear_start + 1200] if clear_start > 0 else ""
    refresh_start = html.find("async function refreshOnboarding")
    refresh = html[refresh_start : refresh_start + 6000] if refresh_start > 0 else ""
    return {
        "lead_container": {
            "present": lead > 0,
            "line": _line(html, lead) if lead > 0 else None,
        },
        "renderer": {
            "present": bool(renderer),
            "uses_next_step": "report.next_step" in renderer,
            "uses_counts": "report.completed_count" in renderer
            and "report.total_count" in renderer,
            "uses_ready": "report.ready" in renderer,
            "announces_blocked": "report.blocked" in renderer,
            "action_attached": "runOnboardingAction(next.action)" in renderer,
            "no_action_branch": "has no action" in renderer or "No action" in renderer,
        },
        "steps": {
            "aria_current": 'aria-current="step"' in steps,
            "aria_current_from_next": bool(
                re.search(
                    r"isNext\s*=\s*Boolean\(next\s*&&\s*step\.id\s*===\s*next\.id\)",
                    steps,
                )
            ),
            "waiting_line": "onboarding-step-waiting" in steps,
            "waiting_rule": bool(
                re.search(r"nextIndex\s*>=\s*0\s*&&\s*index\s*>\s*nextIndex", steps)
            ),
            "action_button": "runOnboardingAction(" in steps,
        },
        "run_button": {
            "described_by": 'aria-describedby="onboardingRunHint"' in html,
            "hint_element": 'id="onboardingRunHint"' in html,
            "hint_written": 'getElementById("onboardingRunHint")' in refresh,
            "reason_names_next": "next: ${next.title}" in refresh
            or "next: ${next" in refresh,
        },
        "focus": {
            "clear_restores": "document.activeElement" in clear
            and "options.focus" in clear,
            "refresh_names_target": 'clearPresentation("onboardingState", { focus: "onboardingLead" })'
            in refresh,
            # Ordering is the invariant the walk caught: focus is handed to the lead,
            # so the lead has to exist at that moment. Rendering it afterwards replaces
            # the focused node and drops the reader on <body>.
            "rendered_before_focus": 0
            <= refresh.find("renderOnboardingLead();")
            < refresh.find('clearPresentation("onboardingState", { focus:'),
        },
        "readiness": {
            "uses_server_ready": "onboardingReport.ready" in html,
            "recomputes_ready": bool(
                re.search(
                    r"steps\.every\(|steps\.filter\(\s*step\s*=>\s*step\.status\s*!==",
                    html,
                )
            ),
            "counts_client_side": bool(
                re.search(r"steps\.filter\([^)]*\)\.length", html)
            ),
        },
        "styles": {
            "lead_rule": ".onboarding-lead {" in html,
            "lead_states": all(
                f'.onboarding-lead[data-state="{state}"]' in html
                for state in ("next", "blocked", "ready")
            ),
            "chip": ".onboarding-step-chip {" in html,
            "waiting": ".onboarding-step-waiting {" in html,
            "next_row": ".onboarding-step.next {" in html,
        },
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    matrix = read_matrix()
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    # 1. The server must never name a next step the pane cannot act on.
    for row in matrix:
        if row["next_step"] and not (
            row["next_step_action"] and row["next_step_action_label"]
        ):
            fail(
                "next-step-has-no-action",
                f"{row['case']}: next step {row['next_step']!r} carries no action, so the lead would be a dead end",
            )

    # 2. Every step the pane prints must carry words to print.
    for row in matrix:
        for step in row["steps"]:
            if not step["title"].strip():
                fail(
                    "step-without-title",
                    f"{row['case']}: step {step['id']} has no title",
                )
        for step in row["steps"]:
            if (
                step["status"] not in preflight.READY_STATUSES
                and not step["has_action"]
                and step["id"] == row["next_step"]
            ):
                fail(
                    "next-step-inactionable",
                    f"{row['case']}: next step {step['id']} is not actionable",
                )

    # 3. The pane renders the server's answer and does not re-derive it.
    if not frontend["lead_container"]["present"]:
        fail("lead-missing", "the Start pane has no #onboardingLead region")
    renderer = frontend["renderer"]
    for flag, description in (
        ("present", "renderOnboardingLead() is gone"),
        ("uses_next_step", "the lead does not read the server's next_step"),
        ("uses_counts", "the lead does not print completed_count/total_count"),
        ("uses_ready", "the lead ignores the server's ready flag"),
        (
            "announces_blocked",
            "a blocked preflight is not distinguished from a waiting one",
        ),
        ("action_attached", "the lead does not run the action the server attached"),
        ("no_action_branch", "a next step without an action is not explained"),
    ):
        if not renderer.get(flag):
            fail("lead-incomplete", description)
    if frontend["readiness"]["recomputes_ready"]:
        fail(
            "readiness-recomputed",
            "the pane computes readiness from the steps instead of rendering the server's verdict",
        )
    if frontend["readiness"]["counts_client_side"]:
        fail(
            "count-computed-client-side",
            "the pane counts steps client-side instead of printing completed_count",
        )

    # 4. The next row is marked once, from the server's id, and dead rows say why.
    steps = frontend["steps"]
    if not steps["aria_current"]:
        fail("aria-current-missing", "no step row is marked aria-current=step")
    if not steps["aria_current_from_next"]:
        fail(
            "aria-current-invented",
            "the marked row is not derived from the server's next_step id",
        )
    if not steps["waiting_line"] or not steps["waiting_rule"]:
        fail(
            "waiting-line-missing",
            "a step with no action and no expiry is printed without saying it follows the next one",
        )

    # 5. Run is off with a stated reason, visibly and accessibly.
    run = frontend["run_button"]
    for flag, description in (
        ("described_by", "the Run button has no aria-describedby"),
        ("hint_element", "there is no #onboardingRunHint for it to point at"),
        ("hint_written", "nothing writes the hint, so the description would be empty"),
        ("reason_names_next", "the hint does not name the step that is holding it up"),
    ):
        if not run.get(flag):
            fail("run-reason-missing", description)

    # 6. Clearing a presentation must not drop focus on the body.
    if not frontend["focus"]["clear_restores"]:
        fail(
            "focus-lost-on-clear",
            "clearPresentation() removes the focused button and never restores focus",
        )
    if not frontend["focus"]["refresh_names_target"]:
        fail(
            "focus-target-missing",
            "a successful re-check does not send focus back to the lead",
        )
    if not frontend["focus"]["rendered_before_focus"]:
        fail(
            "focus-target-replaced",
            "the lead is rendered after focus is handed to it, so the focused node is replaced and focus falls to <body>",
        )

    # 7. The styles exist for what the renderer emits.
    for flag, description in (
        ("lead_rule", "the lead has no rule, so it renders as unstyled text"),
        ("lead_states", "a lead state (next, blocked, ready) is not styled"),
        ("chip", "the next step is marked in ARIA only, with no visible chip"),
        ("waiting", "the waiting line has no rule"),
        ("next_row", "the next row is not distinguished visually"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    actions: dict[str, str] = {}
    statuses: dict[str, int] = {}
    for row in matrix:
        for step in row["steps"]:
            statuses[step["status"]] = statuses.get(step["status"], 0) + 1
    for _label, kwargs in CASES:
        report = preflight.build_report(preflight.PreflightInputs(**kwargs))
        for step in report.steps:
            if step.action:
                actions[step.action] = step.action_label

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P2-01",
        "statement": (
            "The preflight decides what a first-run reader should do next and the Start pane prints that answer once: how far "
            "along, which step is next, and the action the server attached to it. The pane never recounts readiness, a step "
            "with no action is shown as following the next one, and Run is disabled only with a stated reason."
        ),
        "source": {
            "frontend": "static/index.html",
            "preflight": "onboarding_preflight.py",
            "route": "GET /api/onboarding/preflight",
        },
        "steps_contract": {
            "order": list(preflight.STEP_ORDER),
            "ready_statuses": sorted(preflight.READY_STATUSES),
            "first_task_prompt": preflight.FIRST_TASK_PROMPT,
        },
        "actions": [
            {"action": action, "label": label}
            for action, label in sorted(actions.items())
        ],
        "statuses_seen": statuses,
        "matrix": matrix,
        "frontend": frontend,
        "totals": {
            "cases": len(matrix),
            "steps_per_case": len(preflight.STEP_ORDER),
            "next_step_is_actionable_always": all(
                row["next_step_action"] and row["next_step_action_label"]
                for row in matrix
                if row["next_step"]
            ),
            "actions": len(actions),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the matrix runs the pure preflight module, so it covers every branch of the six step builders but not the "
            "app adapter that fills PreflightInputs from live state",
            "a browser check cannot make a machine ready, so scripts/start_walk.mjs asserts the lead against the same API "
            "response the pane received rather than against an invented ready state",
            "the readiness of a real install depends on the machine; this artifact says what the pane does with the answer",
        ],
    }


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    lines = [
        "# Start readiness and the next step",
        "",
        "Generated by `python3 scripts/uiux_onboarding_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## What the preflight decides",
        "",
        f"`GET /api/onboarding/preflight` returns six steps in a fixed order "
        f"({', '.join(f'`{step}`' for step in payload['steps_contract']['order'])}), a `completed_count`, a "
        "`total_count`, a `ready` flag, a `blocked` flag and exactly one `next_step` — the first step whose status is "
        "neither `ready` nor `optional`. That is the server's answer to *what should a first-run reader click*.",
        "",
        "The pane prints it once, above the list:",
        "",
        "- **how far along** — `completed_count` of `total_count`, verbatim, never recounted in the browser;",
        '- **which step is next** — its title and its detail, with `aria-current="step"` and a visible chip on that row;',
        "- **what to click** — the action label the server attached to that step, wired to `runOnboardingAction()`;",
        "- **why Run is off** — a visible hint naming the step that is holding it up, and the same text as the button's "
        "`aria-describedby`, because a disabled control with no stated reason is an invisible refusal.",
        "",
        "A step the server gave no action is printed as following the next one rather than as a button that does nothing: "
        "`first-task` is `action-required` with no action whenever no model is selectable, and the thing to click in that "
        "state is the model step above it.",
        "",
        "## The matrix",
        "",
        f"{totals['cases']} input combinations, {totals['steps_per_case']} steps each, run through the pure module:",
        "",
        "| Case | Ready | Blocked | Ready count | Next step | Action |",
        "|---|---|---|---|---|---|",
    ]
    for row in payload["matrix"]:
        action = (
            f"`{row['next_step_action_label']}`" if row["next_step_action"] else "—"
        )
        lines.append(
            f"| {row['case']} | {'yes' if row['ready'] else 'no'} | {'yes' if row['blocked'] else 'no'} | "
            f"{row['completed_count']}/{row['total_count']} | {row['next_step_title'] or '—'} | {action} |"
        )
    lines += [
        "",
        "`next_step` is actionable in every case where it exists, which is the property the lead depends on: "
        f"{'yes' if totals['next_step_is_actionable_always'] else 'NO'}.",
        "",
        "## What changed in the pane",
        "",
        "| Before | After |",
        "|---|---|",
        "| six rows of equal weight; the reader picks | one lead naming the next step, plus `aria-current` on that row |",
        "| `summary` in a pill, nothing else | `completed_count` of `total_count`, from the server |",
        "| Run disabled with no reason | visible hint naming the blocking step and the same text as `aria-describedby` |",
        "| a step with no action rendered as a bare row | printed as following the step marked next |",
        "| Re-check removed the button that had focus, dropping it on `<body>` | focus returns to the lead (and to the "
        "composer when a chat error is dismissed) |",
        "",
        "## Rules this artifact enforces",
        "",
        "`--check` fails when the preflight names a next step with no action, when a step carries no title, when the lead "
        "stops reading `next_step`/`completed_count`/`total_count`/`ready`/`blocked`, when the pane recounts readiness or "
        "counts steps itself, when the `aria-current` row is not the server's next step, when the waiting line disappears, "
        "when Run loses its description or the hint stops naming the blocker, when clearing a presentation drops focus "
        "or hands it to a node that is about to be re-rendered, "
        "or when a state the renderer emits has no rule.",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "The behaviour is measured by `node scripts/start_walk.mjs`, which reads the same response the pane read, asserts "
        "the lead and the marked row against it, checks that Run is off with a stated reason, presses the lead action and "
        "confirms it does what its label says, and then refuses the preflight and asserts that Re-check restores the list "
        "with focus back inside the pane instead of on `<body>`.",
        "",
        "## Regenerating",
        "",
        "1. Change a step builder in `onboarding_preflight.py` or the pane in `static/index.html`.",
        "2. `python3 scripts/uiux_onboarding_inventory.py`.",
        "3. `node scripts/start_walk.mjs --json` against a running `scripts/demo_tour.py --serve`.",
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
            print("error: the onboarding inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_onboarding_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the pane and the preflight disagree:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"onboarding OK: {payload['totals']['cases']} cases, next step actionable in every one, "
            f"{payload['totals']['actions']} actions, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['cases']} cases, {payload['totals']['actions']} actions, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
