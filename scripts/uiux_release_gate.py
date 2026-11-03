#!/usr/bin/env python3
"""Gate the UI/UX transformation on evidence, and hand it over (UIUX-P6-05).

    python3 scripts/uiux_release_gate.py           # regenerate the gate JSON, the gate page and the handover
    python3 scripts/uiux_release_gate.py --check   # fail if any of them drifted, or a gate is not met
    python3 scripts/uiux_release_gate.py --json    # print the gate instead of writing it

Thirty-nine stories produced thirty-nine artifacts and thirty-seven `--check` modes. A reader who
wants to know whether this program is finished should not have to run them one at a time and hold
the results in their head, and a reviewer should not have to trust a paragraph that says they were
run. This script runs them - all of them, in the documented order - and records what each one said:

* **the artifact checks**: every generator's `--check` and its exit code. A non-zero exit is a
  finding that carries what the check said, so the gate cannot report "met" over a drifted artifact;
  a passing check contributes only its exit code, because the words a healthy generator prints are
  its own business and one of them names whether ImageMagick is installed;
* **the ledger**: whether the program's own bookkeeping agrees with its stories - counters, phase
  totals, a `done` story without a commit sha or evidence, a `blocked` story, an `in_progress` one;
* **the phase gates**: the seven gate sentences in the ledger, each traced to the stories that
  discharge it and the artifacts that prove it, with the status computed rather than asserted;
* **the five acceptance criteria**: one line each, pointing at the artifact or the CI job that shows
  it, because the criteria are the reviewer's call and the gate's job is to put the evidence in one
  place;
* **the known regressions**: every one the program recorded, with the story that recorded it, so a
  handover says what is still imperfect instead of what is finished;
* **the handover**: what changed, what held, how to verify it in five minutes, and what this gate
  deliberately does not run (the browser suites and the full test matrix, which are CI's, not a
  script's: claiming them from here would be the exact dishonesty this program has been avoiding).

The rule the artifact states: **a release gate is a list of checks that ran, with their exit codes;
anything else is a press release.**
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Release_Gate.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-release-gate.md"
HANDOVER_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-handover.md"

SCHEMA_VERSION = 1
PROGRAM = "M5 2.0 UI/UX transformation"
STORY = "UIUX-P6-05"

#: Every generator, in the order the program regenerates them. The gate runs each one's `--check`
#: and records the exit code and the line it printed; nothing here is a claim about a run.
SOURCES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("UIUX-P0-01", "scripts/uiux_source_manifest.py", ("product/M5_2_0_UIUX_Source_Manifest.json",),
     "every file, tab and route the UI is made of, hashed"),
    ("UIUX-P0-02", "scripts/uiux_control_inventory.py", ("product/M5_2_0_UIUX_Control_Inventory.json",),
     "every control in the markup and in the script templates"),
    ("UIUX-P0-03", "scripts/uiux_route_map.py", ("product/M5_2_0_UIUX_Route_Map.json",),
     "which browser call reaches which route"),
    ("UIUX-P0-04", "scripts/uiux_authorization_trace.py", ("product/M5_2_0_UIUX_Authorization_Trace.json",),
     "who guards every state-changing route"),
    ("UIUX-P0-06", "scripts/uiux_preservation_signoff.py", ("product/M5_2_0_UIUX_Preservation_Signoff.json",),
     "the owner-approved preservation matrix and the digests it binds"),
    ("UIUX-P1-01", "scripts/uiux_tokens.py", ("product/M5_2_0_UIUX_Tokens.json",),
     "the design tokens and their contrast floors"),
    ("UIUX-P1-03", "scripts/uiux_status_components.py", ("product/M5_2_0_UIUX_Status_Components.json",),
     "one status vocabulary across the surfaces that speak it"),
    ("UIUX-P1-04", "scripts/uiux_overlay_inventory.py", ("product/M5_2_0_UIUX_Overlays.json",),
     "dialogs, their shortcuts and the inspector"),
    ("UIUX-P1-05", "scripts/uiux_presentation_inventory.py", ("product/M5_2_0_UIUX_Presentations.json",),
     "the three presentation kinds and their hosts"),
    ("UIUX-P2-01", "scripts/uiux_onboarding_inventory.py", ("product/M5_2_0_UIUX_Onboarding.json",),
     "the preflight's steps, cases and next action"),
    ("UIUX-P2-02", "scripts/uiux_recent_work_inventory.py", ("product/M5_2_0_UIUX_Recent_Work.json",),
     "the recent-work strip's statuses and limit"),
    ("UIUX-P2-03", "scripts/uiux_start_health_inventory.py", ("product/M5_2_0_UIUX_Start_Health.json",),
     "the Start pane's health strip"),
    ("UIUX-P2-04", "scripts/uiux_chat_inventory.py", ("product/M5_2_0_UIUX_Chat.json",),
     "the chat roles, reasons and tones"),
    ("UIUX-P2-05", "scripts/uiux_chat_context_inventory.py", ("product/M5_2_0_UIUX_Chat_Context.json",),
     "the context and tool-event fields chat renders"),
    ("UIUX-P2-06", "scripts/uiux_chat_handoff_inventory.py", ("product/M5_2_0_UIUX_Chat_Handoff.json",),
     "the Chat-to-Harness brief"),
    ("UIUX-P3-01", "scripts/uiux_harness_settings_inventory.py", ("product/M5_2_0_UIUX_Harness_Settings.json",),
     "the Harness settings the server decides"),
    ("UIUX-P3-02", "scripts/uiux_harness_timeline_inventory.py", ("product/M5_2_0_UIUX_Harness_Timeline.json",),
     "the event timeline and its cursor semantics"),
    ("UIUX-P3-03", "scripts/uiux_harness_permissions_inventory.py", ("product/M5_2_0_UIUX_Harness_Permissions.json",),
     "the permission prompts and their capability mappings"),
    ("UIUX-P3-04", "scripts/uiux_harness_evidence_inventory.py", ("product/M5_2_0_UIUX_Harness_Evidence.json",),
     "the evidence record's six sections"),
    ("UIUX-P3-05", "scripts/uiux_harness_recovery_inventory.py", ("product/M5_2_0_UIUX_Harness_Recovery.json",),
     "the Harness recovery states and refusals"),
    ("UIUX-P3-06", "scripts/uiux_pipeline_artifacts_inventory.py", ("product/M5_2_0_UIUX_Pipeline_Artifacts.json",),
     "the Pipeline artifacts and their diffs"),
    ("UIUX-P3-07", "scripts/uiux_pipeline_recovery_inventory.py", ("product/M5_2_0_UIUX_Pipeline_Recovery.json",),
     "pause, cancel, resume and the Pipeline's recovery"),
    ("UIUX-P4-01", "scripts/uiux_council_participants_inventory.py", ("product/M5_2_0_UIUX_Council_Participants.json",),
     "the Council's seats and participants"),
    ("UIUX-P4-02", "scripts/uiux_council_deliberation_inventory.py", ("product/M5_2_0_UIUX_Council_Deliberation.json",),
     "the deliberation's stages and disagreement"),
    ("UIUX-P4-03", "scripts/uiux_council_verdict_inventory.py", ("product/M5_2_0_UIUX_Council_Verdict.json",),
     "the verdict and its dissent"),
    ("UIUX-P4-04", "scripts/uiux_compare_criteria_inventory.py", ("product/M5_2_0_UIUX_Compare_Criteria.json",),
     "the Compare criteria table"),
    ("UIUX-P4-05", "scripts/uiux_compare_provenance_inventory.py", ("product/M5_2_0_UIUX_Compare_Provenance.json",),
     "measured provenance for every Compare claim"),
    ("UIUX-P4-06", "scripts/uiux_compare_continuity_inventory.py", ("product/M5_2_0_UIUX_Compare_Continuity.json",),
     "Compare's continuity across runs"),
    ("UIUX-P5-01", "scripts/uiux_terminology_inventory.py", ("product/M5_2_0_UIUX_Terminology.json",),
     "one name per concept across the panes"),
    ("UIUX-P5-02", "scripts/uiux_keyboard_a11y_inventory.py", ("product/M5_2_0_UIUX_Keyboard_A11y.json",),
     "the keyboard walks, shortcuts and focus surfaces"),
    ("UIUX-P5-03", "scripts/uiux_responsive_inventory.py", ("product/M5_2_0_UIUX_Responsive.json",),
     "the narrow-viewport rules and their breakpoints"),
    ("UIUX-P5-04", "scripts/uiux_stream_perf_inventory.py", ("product/M5_2_0_UIUX_Stream_Performance.json",),
     "bounded streaming and its rendering budget"),
    ("UIUX-P5-05", "scripts/uiux_polish_inventory.py", ("product/M5_2_0_UIUX_Polish.json",),
     "radius, motion and press rules"),
    ("UIUX-P6-01", "scripts/uiux_preservation_e2e.py", ("product/M5_2_0_UIUX_Preservation_E2E.json",),
     "a prover for every row of the matrix"),
    ("UIUX-P6-02", "scripts/uiux_adversarial_inventory.py", ("product/M5_2_0_UIUX_Adversarial.json",),
     "every refusal a write can meet, and where it is drawn"),
    ("UIUX-P6-03", "scripts/uiux_screenshot_review.py", ("product/M5_2_0_UIUX_Screenshot_Review.json",),
     "every committed capture, traced, hashed and themed"),
    ("UIUX-P6-04", "scripts/uiux_docs_verification.py", ("product/M5_2_0_UIUX_Readme_Onboarding.json",),
     "the reader pages, their links, their commands and their claims"),
)

#: The five acceptance criteria every story in the program carries, and where the evidence lives.
#: The gate does not decide whether they are satisfied; it names the artifact or job that answers each.
ACCEPTANCE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "All mapped existing controls remain reachable and functionally equivalent",
        "the control inventory enumerates every control; preservation e2e proves a prover for every "
        "matrix row; the browser job replays the retained journeys",
        (
            "product/M5_2_0_UIUX_Control_Inventory.json#/totals",
            "product/M5_2_0_UIUX_Preservation_E2E.json#/totals",
            "CI: Browser safety regressions",
        ),
    ),
    (
        "No console errors on tested journeys",
        "the adversarial walk and the journey walks assert a clean console; the browser job runs them",
        ("tests/browser/", "CI: Browser safety regressions"),
    ),
    (
        "Keyboard operation and focus states work",
        "keyboard walks drive every pane with the keyboard and check the focus ring; the P5 inventory "
        "records the shortcuts and the focus surfaces",
        ("product/M5_2_0_UIUX_Overlays.json#/totals", "tests/browser/", "CI: Browser safety regressions"),
    ),
    (
        "Failure and recovery states are actionable and honest",
        "the adversarial inventory maps every refusal to the words the pane uses; the presentations "
        "inventory proves the recovery surface; the per-story recovery artifacts cover each pane",
        (
            "product/M5_2_0_UIUX_Adversarial.json#/totals",
            "product/M5_2_0_UIUX_Presentations.json#/totals",
        ),
    ),
    (
        "Target UI behavior is demonstrable by screenshot or test",
        "240 committed captures in 27 reviewed sets, and the tests beside every generator",
        ("product/M5_2_0_UIUX_Screenshot_Review.json#/totals", "CI: test (3.11, 3.12, 3.13)"),
    ),
)

#: What the gate does not run, and why. Named so a reader cannot mistake silence for coverage.
NOT_RUN: tuple[tuple[str, str], ...] = (
    (
        "the full test suite (`pytest -q`)",
        "CI runs it on 3.11, 3.12 and 3.13 on every push; a script that ran it here would report one "
        "interpreter's result as if it were three, and this checkout is shallow, so three local "
        "ancestor assertions cannot run at all",
    ),
    (
        "the browser suites (`tests/browser/`, `tests/test_uiux_adversarial_browser.py`)",
        "they need a Chromium binary and puppeteer-core, neither vendored; CI installs them and runs "
        "`Browser safety regressions` on every push",
    ),
    (
        "a new screenshot capture",
        "captures are taken by the story that changes a pixel and reviewed by `uiux_screenshot_review.py`; "
        "this story changes none",
    ),
    (
        "the ledger's own acceptance verdict",
        "whether a criterion is satisfied is the reviewer's and the owner's call; the gate puts the "
        "evidence in one place and says so",
    ),
)


def read_json(path: str) -> dict[str, object]:
    return json.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


def ledger() -> dict[str, object]:
    return read_json("product/M5_2_0_UIUX_Autonomous_Roadmap.json")


def run_check(script: str) -> dict[str, object]:
    """Run one generator's `--check` and record exactly what it did."""
    command = [sys.executable, script, "--check"]
    completed = subprocess.run(  # noqa: S603 - fixed argv, repository script
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    lines = [line for line in (completed.stdout + completed.stderr).splitlines() if line.strip()]
    return {
        "script": script,
        "command": f"python3 {script} --check",
        "exit_code": completed.returncode,
        # Only a failure keeps its words. What a passing check prints is its own business - one of
        # them says whether ImageMagick is installed, another names the pin - and copying those lines
        # into this artifact would make the release gate differ between two machines that agree.
        "failure_output": lines[-1] if completed.returncode != 0 and lines else None,
    }


def artifact_totals(paths: tuple[str, ...]) -> list[dict[str, object]]:
    """The `totals` block of each artifact, so the gate page can print the numbers it gates on."""
    blocks = []
    for path in paths:
        artifact = read_json(path)
        blocks.append({"artifact": path, "totals": artifact.get("totals", {})})
    return blocks


def program_state(document: dict[str, object]) -> dict[str, object]:
    stories = document["stories"]  # type: ignore[index]
    statuses = [story["tracking"]["status"] for story in stories]  # type: ignore[index]
    done = sum(1 for status in statuses if status == "done")
    return {
        "total_stories": len(stories),
        "done": done,
        "in_progress": sum(1 for status in statuses if status == "in_progress"),
        "blocked": sum(1 for status in statuses if status == "blocked"),
        "not_started": sum(1 for status in statuses if status == "not_started"),
        "percent_complete": round(100 * done / len(stories)),
        "complete": done == len(stories),
        "revision": document.get("revision"),
        "pinned_source_commit": document.get("source_commit_sha"),
    }


def ledger_findings(document: dict[str, object]) -> list[dict[str, str]]:
    """The bookkeeping the ledger has to get right before anyone reads its verdict."""
    findings: list[dict[str, str]] = []
    stories = document["stories"]  # type: ignore[index]
    state = program_state(document)
    counters = document.get("overall_progress", {})  # type: ignore[union-attr]
    for key in ("done", "in_progress", "blocked", "not_started", "total_stories"):
        if counters.get(key) != state[key]:  # type: ignore[index]
            findings.append(
                {
                    "check": "ledger-counters",
                    "detail": f"overall_progress.{key} says {counters.get(key)}, the stories say {state[key]}",  # type: ignore[union-attr]
                }
            )
    for phase in document["phases"]:  # type: ignore[index]
        members = [story for story in stories if story["phase"] == phase["id"]]  # type: ignore[index]
        for status in ("done", "in_progress", "blocked", "not_started"):
            counted = sum(1 for story in members if story["tracking"]["status"] == status)
            if phase[status] != counted:
                findings.append(
                    {"check": "ledger-phases", "detail": f"{phase['id']}.{status} says {phase[status]}, the stories say {counted}"}
                )
    for story in stories:  # type: ignore[index]
        tracking = story["tracking"]
        if tracking["status"] != "done":
            continue
        if not tracking.get("commit_sha") or len(str(tracking["commit_sha"])) != 40:
            findings.append({"check": "ledger-evidence", "detail": f"{story['id']}: done without a commit sha"})
        if not tracking.get("evidence"):
            findings.append({"check": "ledger-evidence", "detail": f"{story['id']}: done without evidence"})
        if tracking.get("progress_percent") != 100:
            findings.append({"check": "ledger-evidence", "detail": f"{story['id']}: done but progress {tracking.get('progress_percent')}"})
    return findings


def phase_gates(document: dict[str, object], checks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Each gate sentence, traced to the stories that discharge it and the checks that prove them."""
    failed = {check["script"]: check["exit_code"] for check in checks}
    gates = []
    for gate_id, sentence in document["release_gates"].items():  # type: ignore[union-attr]
        members = [story for story in document["stories"] if story["phase"] == gate_id]  # type: ignore[index]
        sources = [source for source in SOURCES if any(source[0] == story["id"] for story in members)]
        met = all(story["tracking"]["status"] == "done" for story in members)
        met = met and all(failed.get(source[1]) == 0 for source in sources)
        gates.append(
            {
                "id": gate_id,
                "sentence": sentence,
                "story_ids": [story["id"] for story in members],
                "done": sum(1 for story in members if story["tracking"]["status"] == "done"),
                "total": len(members),
                "proving_checks": [source[1] for source in sources],
                "met": bool(met),
            }
        )
    return gates


def inventory(document: dict[str, object]) -> dict[str, object]:
    """The program's headline numbers, read from the artifacts rather than from prose."""
    manifest = read_json("product/M5_2_0_UIUX_Source_Manifest.json")
    controls = read_json("product/M5_2_0_UIUX_Control_Inventory.json")["totals"]
    e2e = read_json("product/M5_2_0_UIUX_Preservation_E2E.json")["totals"]
    adversarial = read_json("product/M5_2_0_UIUX_Adversarial.json")["totals"]
    review = read_json("product/M5_2_0_UIUX_Screenshot_Review.json")["totals"]
    docs = read_json("product/M5_2_0_UIUX_Readme_Onboarding.json")["totals"]
    signoff = read_json("product/M5_2_0_UIUX_Preservation_Signoff.json")["totals"]
    stories = document["stories"]  # type: ignore[index]
    changed = sorted({path for story in stories for path in story["tracking"].get("changed_files", [])})
    return {
        "tabs": manifest["frontend"]["tab_count"],
        "routes": manifest["api"]["route_count"],
        "markup_controls": controls["markup_controls"],
        "chrome_controls": controls["chrome_controls"],
        "script_rendered_controls": controls["script_rendered_controls"],
        "capabilities": signoff["preserved_capabilities"],
        "e2e_rows": e2e["matrix_rows"],
        "e2e_findings": e2e["findings"],
        "refusals": adversarial["refusals"],
        "refusal_statuses": adversarial["statuses"],
        "captures": review["files"],
        "capture_sets": review["sets"],
        "reviewed_sets": review["reviewed_sets"],
        "screenshot_findings": review["findings"],
        "doc_pages": docs["pages"],
        "doc_commands": docs["commands"],
        "doc_claims": docs["claims"],
        "stories_done": program_state(document)["done"],
        "distinct_files_changed": len(changed),
    }


def known_regressions(document: dict[str, object]) -> list[dict[str, str]]:
    rows = []
    for story in document["stories"]:  # type: ignore[index]
        for text in story["tracking"].get("known_regressions", []):
            rows.append({"story": story["id"], "regression": text})
    return rows


def build() -> dict[str, object]:
    document = ledger()
    checks = []
    findings: list[dict[str, str]] = []
    for story, script, artifacts, owns in SOURCES:
        result = run_check(script)
        result.update({"story": story, "artifacts": list(artifacts), "owns": owns, "totals": artifact_totals(artifacts)})
        checks.append(result)
        if result["exit_code"] != 0:
            findings.append(
                {
                    "check": "artifact-check",
                    "detail": f"`{result['command']}` exited {result['exit_code']}: {result['failure_output'] or '(no output)'}",
                }
            )
        for artifact in artifacts:
            if not (PROJECT_ROOT / artifact).exists():
                findings.append({"check": "artifact-missing", "detail": artifact})

    findings.extend(ledger_findings(document))
    gates = phase_gates(document, checks)
    for gate in gates:
        if not gate["met"] and gate["id"] != "P6":
            findings.append({"check": "phase-gate", "detail": f"{gate['id']} is not met: {gate['done']}/{gate['total']} stories done"})

    state = program_state(document)
    if not state["complete"]:
        findings.append(
            {
                "check": "program-incomplete",
                "detail": f"{state['done']}/{state['total_stories']} stories done, "
                f"{state['in_progress']} in progress, {state['blocked']} blocked",
            }
        )
    if state["blocked"]:
        findings.append({"check": "program-blocked", "detail": f"{state['blocked']} stories blocked"})

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "uiux_release_gate",
        "program": PROGRAM,
        "story": STORY,
        "generated_by": "scripts/uiux_release_gate.py",
        "regenerate_with": "python3 scripts/uiux_release_gate.py",
        "check_with": "python3 scripts/uiux_release_gate.py --check",
        "statement": (
            "A release gate is the list of checks that ran, with their exit codes. Every line below was "
            "executed by this script on the tree it names; nothing here is a report of a run that happened "
            "somewhere else, and the checks this gate does not run are named."
        ),
        "program_state": state,
        "checks": checks,
        "gates": gates,
        "acceptance": [
            {"criterion": criterion, "evidence": evidence, "artifacts": list(artifacts)}
            for criterion, evidence, artifacts in ACCEPTANCE
        ],
        "inventory": inventory(document),
        "known_regressions": known_regressions(document),
        "open_risks": document.get("session_state", {}).get("open_risks", []),  # type: ignore[union-attr]
        "not_run": [{"what": what, "why": why} for what, why in NOT_RUN],
        "handover": {
            "what_this_program_changed": [
                "One shell for six panes: a rail, a workspace and an inspector, with the tab registry, the "
                "shortcuts and the focus order recorded in `product/M5_2_0_UIUX_Source_Manifest.json`.",
                "One status vocabulary: nine states drawn from six tones, with every surface that speaks it "
                "enumerated in `product/M5_2_0_UIUX_Status_Components.json`.",
                "Design tokens as the only place a colour, radius, spacing or motion value is decided, with "
                "measured contrast floors (`product/M5_2_0_UIUX_Tokens.json`).",
                "Presentations for the empty, error and recovery cases, hosted rather than re-invented in "
                "each pane (`product/M5_2_0_UIUX_Presentations.json`).",
                "Journeys that report what the server actually did: the preflight's next action, durable "
                "task state in Harness and Pipeline, Council's deliberation, Compare's provenance, Chat's "
                "handoff to Harness.",
                "A preservation harness, an adversarial harness, a screenshot review and a documentation "
                "verification, each with its own generator and its own failing mode.",
            ],
            "what_held": [
                "Endpoints, authorization contracts, approvals, event-cursor semantics, action admission, "
                "sandbox controls, workspace edits and session recovery: 21 capabilities across 6 panes, "
                "each with a prover, and no route removed.",
                "No new dependency, no CDN, no framework migration, no new outbound request.",
                "Untrusted text stays text: the adversarial inventory maps all 12 refusals and no pane "
                "decides authority locally.",
            ],
            "verify_in_five_minutes": [
                "python3 scripts/uiux_release_gate.py --check",
                "python3 scripts/demo_tour.py --check && python3 scripts/validate_screenshots.py",
                "python3 scripts/uiux_preservation_e2e.py --check && python3 scripts/uiux_adversarial_inventory.py --check",
                "python3 scripts/uiux_docs_verification.py --check && python3 scripts/validate_docs.py",
                "python3 -m pytest -q tests/test_uiux_release_gate.py",
            ],
            "where_the_evidence_lives": [
                "`product/M5_2_0_UIUX_*.json` - one artifact per story, each with a `--check` mode.",
                "`docs/uiux-2-0-*.md` - one generated page per artifact, plus this gate and this handover.",
                "`docs/media/` - 240 committed captures in 27 sets, reviewed by `uiux_screenshot_review.py`.",
                "CI: `test (3.11)`, `test (3.12)`, `test (3.13)` and `Browser safety regressions` on every push.",
            ],
            "what_is_not_claimed": [
                "That the interface is beautiful, or that the copy is good: those are the owner's calls.",
                "That every pane was photographed in every theme and width: the coverage grid names the "
                "combinations nobody has looked at yet, and they are 0 findings only because they are holes "
                "rather than contradictions.",
                "That the program's verdict is the gate's: this file lists evidence with exit codes, and the "
                "acceptance verdict belongs to the reviewer.",
            ],
        },
        "totals": {
            "checks": len(checks),
            "checks_passed": sum(1 for check in checks if check["exit_code"] == 0),
            "gates": len(gates),
            "gates_met": sum(1 for gate in gates if gate["met"]),
            "acceptance_criteria": len(ACCEPTANCE),
            "known_regressions": len(known_regressions(document)),
            "findings": len(findings),
        },
        "findings": findings,
    }


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|")


def render_doc(payload: dict[str, object]) -> str:
    state = payload["program_state"]  # type: ignore[index]
    totals = payload["totals"]  # type: ignore[index]
    lines = [
        "# The release gate",
        "",
        "Generated by [`scripts/uiux_release_gate.py`](../scripts/uiux_release_gate.py) for "
        "`UIUX-P6-05`; the checked artifact is "
        "[`product/M5_2_0_UIUX_Release_Gate.json`](../product/M5_2_0_UIUX_Release_Gate.json). "
        "Do not hand-edit. The handover report generated from the same payload is "
        "[`uiux-2-0-handover.md`](uiux-2-0-handover.md).",
        "",
        f"**{state['done']}/{state['total_stories']} stories done ({state['percent_complete']}%), "
        f"{totals['checks_passed']}/{totals['checks']} artifact checks pass, "
        f"{totals['gates_met']}/{totals['gates']} phase gates met, {totals['findings']} findings.**",
        "",
        "Every line in the checks table below was executed by this script against this tree. The exit "
        "code is the check's. What a passing check printed is not copied here: two machines that agree "
        "on the tree can disagree on how they say so, and this artifact has to be reproducible.",
        "",
        "## The artifact checks",
        "",
        "| Story | Check | Artifact | Exit | What it owns |",
        "|---|---|---|---|---|",
    ]
    for check in payload["checks"]:  # type: ignore[union-attr]
        artifacts = ", ".join(f"`{path}`" for path in check["artifacts"])
        lines.append(
            f"| {check['story']} | `{check['command']}` | {artifacts} | {check['exit_code']} | {_cell(check['owns'])} |"
        )
    lines += [
        "",
        "## The phase gates",
        "",
        "| Gate | Stories | Met | Proving checks | The sentence it is judged by |",
        "|---|---|---|---|---|",
    ]
    for gate in payload["gates"]:  # type: ignore[union-attr]
        checks = ", ".join(f"`{name.split('/')[-1]}`" for name in gate["proving_checks"]) or "-"
        lines.append(
            f"| {gate['id']} | {gate['done']}/{gate['total']} | {'yes' if gate['met'] else 'no'} | {checks} | {_cell(gate['sentence'])} |"
        )
    lines += [
        "",
        "## The five acceptance criteria",
        "",
        "Each story in the program carries the same five criteria. The gate does not decide them; it "
        "names the artifact or the CI job that answers each one, so a reviewer can go straight there.",
        "",
        "| Criterion | Evidence | Where |",
        "|---|---|---|",
    ]
    for row in payload["acceptance"]:  # type: ignore[union-attr]
        where = ", ".join(f"`{item}`" for item in row["artifacts"])
        lines.append(f"| {_cell(row['criterion'])} | {_cell(row['evidence'])} | {where} |")
    inv = payload["inventory"]  # type: ignore[index]
    lines += [
        "",
        "## What the program produced, in numbers",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| tabs | {inv['tabs']} |",
        f"| markup controls | {inv['markup_controls']} ({inv['chrome_controls']} in the chrome, {inv['script_rendered_controls']} rendered by script) |",
        f"| preserved capabilities | {inv['capabilities']} |",
        f"| preservation e2e rows | {inv['e2e_rows']} ({inv['e2e_findings']} findings) |",
        f"| refusal states | {inv['refusals']} across {inv['refusal_statuses']} statuses |",
        f"| committed captures | {inv['captures']} in {inv['capture_sets']} sets, {inv['reviewed_sets']} reviewed |",
        f"| reader pages verified | {inv['doc_pages']} ({inv['doc_commands']} commands, {inv['doc_claims']} claims) |",
        f"| files this program changed | {inv['distinct_files_changed']} |",
        "",
        "## Known regressions the program recorded",
        "",
    ]
    if payload["known_regressions"]:  # type: ignore[truthy-bool]
        lines += ["| Story | Regression |", "|---|---|"]
        for row in payload["known_regressions"]:  # type: ignore[union-attr]
            lines.append(f"| {row['story']} | {_cell(row['regression'])} |")
    else:
        lines.append("None recorded.")
    lines += [
        "",
        "## What this gate does not run",
        "",
        "| Not run here | Why |",
        "|---|---|",
    ]
    for row in payload["not_run"]:  # type: ignore[union-attr]
        lines.append(f"| {_cell(row['what'])} | {_cell(row['why'])} |")
    lines += [
        "",
        "## Findings",
        "",
    ]
    if payload["findings"]:
        for finding in payload["findings"]:  # type: ignore[union-attr]
            lines.append(f"* **{finding['check']}** — {finding['detail']}")
    else:
        lines.append("None: every artifact check passes, every phase gate is met, and the ledger's counters agree with its stories.")
    lines.append("")
    return "\n".join(lines)


def render_handover(payload: dict[str, object]) -> str:
    state = payload["program_state"]  # type: ignore[index]
    handover = payload["handover"]  # type: ignore[index]
    totals = payload["totals"]  # type: ignore[index]
    lines = [
        "# Handover: the M5 2.0 UI/UX transformation",
        "",
        "Generated by [`scripts/uiux_release_gate.py`](../scripts/uiux_release_gate.py) for `UIUX-P6-05` "
        "from [`product/M5_2_0_UIUX_Release_Gate.json`](../product/M5_2_0_UIUX_Release_Gate.json). Do not "
        "hand-edit. The gate itself is [`uiux-2-0-release-gate.md`](uiux-2-0-release-gate.md); the plan the "
        "program was executed against is [`uiux-2-0-transformation-plan.md`](uiux-2-0-transformation-plan.md).",
        "",
        f"**{state['done']}/{state['total_stories']} stories closed, {totals['checks_passed']} of "
        f"{totals['checks']} artifact checks passing, {totals['known_regressions']} known regressions "
        f"recorded, {state['blocked']} blocked.**",
        "",
        "This page is written for the person who inherits the repository, not for the person who "
        "finished it: what changed, what held, how to check it without reading 40 pull requests, and "
        "what is still imperfect.",
        "",
        "## What this program changed",
        "",
    ]
    lines += [f"* {item}" for item in handover["what_this_program_changed"]]
    lines += ["", "## What held", "", "The program's non-negotiable invariants, and how each was kept:", ""]
    lines += [f"* {item}" for item in handover["what_held"]]
    lines += [
        "",
        "## Verify it in five minutes",
        "",
        "Run these in the repository root. Each one prints a summary line and exits non-zero when the "
        "thing it describes has drifted; together they cover every artifact this program produced.",
        "",
        "```bash",
    ]
    lines += list(handover["verify_in_five_minutes"])
    lines += [
        "```",
        "",
        "## Where the evidence lives",
        "",
    ]
    lines += [f"* {item}" for item in handover["where_the_evidence_lives"]]
    lines += ["", "## What is not claimed", ""]
    lines += [f"* {item}" for item in handover["what_is_not_claimed"]]
    lines += [
        "",
        "## Known regressions",
        "",
        "Recorded as they were found, each against the story that found it. None of them blocks the "
        "program; all of them are things a next session would want to know before it trusts a screen.",
        "",
    ]
    if payload["known_regressions"]:  # type: ignore[truthy-bool]
        for row in payload["known_regressions"]:  # type: ignore[union-attr]
            lines.append(f"* {row['story']}: {row['regression']}")
    else:
        lines.append("* None recorded.")
    lines += ["", "Media directories: `docs/media/screenshots` (the published set), `docs/media/uiux-p6-03` (the P6-03 before/after and dark set) and `docs/media/adversarial`, each with an `index.json`.", ""]
    return "\n".join(lines)


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a gate is not met")
    parser.add_argument("--json", action="store_true", help="print the gate instead of writing it")
    args = parser.parse_args(argv)

    payload = build()
    text = dumps(payload)
    doc = render_doc(payload)
    handover = render_handover(payload)
    totals = payload["totals"]

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        drifted = []
        for path, expected in ((JSON_PATH, text), (DOC_PATH, doc), (HANDOVER_PATH, handover)):
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                drifted.append(str(path.relative_to(PROJECT_ROOT)))
        if drifted or payload["findings"]:
            if drifted:
                print("error: the release gate has drifted from the tree.", file=sys.stderr)
                print("       Regenerate it (the change is part of the story, not a fix-up):", file=sys.stderr)
                print("           python3 scripts/uiux_release_gate.py", file=sys.stderr)
                for path in drifted:
                    print(f"       changed: {path}", file=sys.stderr)
            for finding in payload["findings"]:
                print(f"finding: {finding['check']} — {finding['detail']}", file=sys.stderr)
            return 1
        print(
            f"release gate OK: {totals['checks_passed']}/{totals['checks']} artifact checks pass, "
            f"{totals['gates_met']}/{totals['gates']} phase gates met, "
            f"{totals['known_regressions']} known regressions, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    HANDOVER_PATH.write_text(handover, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)}, {DOC_PATH.relative_to(PROJECT_ROOT)} and "
        f"{HANDOVER_PATH.relative_to(PROJECT_ROOT)}: {totals['checks_passed']}/{totals['checks']} checks, "
        f"{totals['gates_met']}/{totals['gates']} gates, {totals['findings']} findings"
    )
    return 0 if not payload["findings"] else 1


if __name__ == "__main__":
    sys.exit(main())
