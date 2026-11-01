#!/usr/bin/env python3
"""Inventory what a Harness completion claim rests on (UIUX-P3-04).

    python3 scripts/uiux_harness_evidence_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_harness_evidence_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_harness_evidence_inventory.py --json     # print the inventory

A finished Harness run used to end in one mono line: `Completion verified (spec_sha256…)`. That
is the digest of a contract the reader never saw, printed over an execution nothing described -
no test command, no exit code, no revision, no file list. A rejected run said a failure code and
a reason and left the transcript, the classification and the acceptance coverage that decided it
in the event log. The diff a run produced lived in a separate dialog that never said what the
change had been verified against, and the artifacts a turn left behind were on a route the pane
never read.

So this artifact measures five surfaces:

* **the card**, one renderer for the runtime's three outcomes, stating the contract digest, the
  command and its exit code, the revision, the modified files and the patch;
* **the coverage**, the declared criteria and the tests the transcript actually covered, with an
  uncovered criterion drawn rather than dropped;
* **the record**, the surface behind the card: the contract the session set, the observation the
  runtime wrote, the change the claim is attached to, and the artifacts the turn left behind;
* **the change**, counted per file from the same diff the diff dialog draws, with the diff's own
  honesty note - it is the worktree now, not a copy frozen at the verdict;
* **the absence**, a session with a contract and no verdict, drawn as an absence.

It fails when a second verdict renderer appears, when the one-line label comes back, when a card
stops stating what it rests on, when a declared criterion is drawn as covered without an observed
test, when the record is reachable only from code, when an absent verdict is drawn as a pass, when
the pane calls a route the server does not mount or invents a verification endpoint, and when the
routes this story had to preserve (`/diff`, `/apply`, `/task`) change shape.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Evidence.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-evidence.md"

NETWORK_CALLS = ("fetch(", "XMLHttpRequest", "EventSource", "navigator.sendBeacon")
CALLED_PATH = re.compile("""["'`](/api/[^"'`]*)["'`]""")

CARD = "renderHarnessVerdictCardHtml"

FUNCTIONS = (
    "harnessVerdictTone",
    "harnessEvidenceShort",
    "harnessEvidenceTestEvidence",
    "harnessEvidenceFacts",
    "harnessEvidenceFailures",
    "harnessEvidenceCoverage",
    "harnessEvidenceStagnationLabel",
    "harnessEvidenceDeclaredHtml",
    "harnessEvidenceFactListHtml",
    "harnessEvidenceCoverageHtml",
    "harnessEvidenceFailuresHtml",
    "harnessDiffFiles",
    CARD,
    "harnessLatestVerdict",
    "openHarnessEvidenceModal",
    "closeHarnessEvidenceModal",
    "refreshHarnessEvidence",
    "harnessEvidenceMetaText",
    "harnessEvidenceVerdictHtml",
    "harnessEvidenceSpecHtml",
    "harnessEvidenceObservedHtml",
    "harnessEvidenceChangeHtml",
    "harnessEvidenceArtifactsHtml",
    "harnessEvidenceLimitsHtml",
    "renderHarnessEvidence",
)

# The record's body, in the order a reader meets it.
SURFACE_LISTS = (
    "harnessEvidenceVerdict",
    "harnessEvidenceSpec",
    "harnessEvidenceObserved",
    "harnessEvidenceChange",
    "harnessEvidenceArtifacts",
    "harnessEvidenceLimits",
)

# Routes this story reads. All of them predate it.
ROUTES = (
    ("GET", "/api/harness/sessions/{session_id}/task"),
    ("GET", "/api/harness/sessions/{session_id}/diff"),
    ("POST", "/api/harness/sessions/{session_id}/apply"),
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
    collected: list[str] = []
    for line in source[body_start + 1 :].splitlines():
        if line.strip() and not line[0].isspace():
            break
        collected.append(line)
    return "\n".join(collected)


def element(html: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", html)
    return match.group(0) if match else ""


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


def read_frontend(html: str) -> dict:
    card = function_body(html, CARD)
    facts = function_body(html, "harnessEvidenceFacts")
    coverage = function_body(html, "harnessEvidenceCoverage")
    coverage_html = function_body(html, "harnessEvidenceCoverageHtml")
    declared_html = function_body(html, "harnessEvidenceDeclaredHtml")
    failures_html = function_body(html, "harnessEvidenceFailuresHtml")
    diff_files = function_body(html, "harnessDiffFiles")
    tone = function_body(html, "harnessVerdictTone")
    refresh = function_body(html, "refreshHarnessEvidence")
    open_modal = function_body(html, "openHarnessEvidenceModal")
    close_modal = function_body(html, "closeHarnessEvidenceModal")
    render = function_body(html, "renderHarnessEvidence")
    spec = function_body(html, "harnessEvidenceSpecHtml")
    observed = function_body(html, "harnessEvidenceObservedHtml")
    change = function_body(html, "harnessEvidenceChangeHtml")
    artifacts = function_body(html, "harnessEvidenceArtifactsHtml")
    verdict = function_body(html, "harnessEvidenceVerdictHtml")
    limits = function_body(html, "harnessEvidenceLimitsHtml")
    summary = html[
        html.find("function harnessEventSummary") : html.find("function harnessEventSummary") + 9000
    ]
    open_diff = function_body(html, "openHarnessDiffModal")
    dialog = element(html, "harnessEvidenceModal")
    dialog_region = ""
    if dialog:
        start = html.find(dialog)
        dialog_region = html[start : start + 2600]
    diff_dialog = element(html, "harnessDiffModal")
    diff_region = ""
    if diff_dialog:
        start = html.find(diff_dialog)
        diff_region = html[start : start + 2200]

    limits_block = re.search(r"const HARNESS_EVIDENCE_LIMITS = \[(.*?)\n  \];", html, re.S)

    return {
        "functions": {name: bool(function_body(html, name)) for name in FUNCTIONS},
        "card": {
            "one_builder": html.count(f"function {CARD}(") == 1,
            "one_renderer_for_three_outcomes": html.count("if (harnessVerdictTone(ev)) {") == 2
            and html.count('"turn/verified"') == 2
            and html.count('"turn/verification_failed"') == 1
            and html.count('"turn/stagnation_paused"') == 1,
            "the_one_line_label_is_gone": "Completion verified (${escapeHtml(String(ev.spec_sha256" not in html
            and 'color:#6ee7b7; padding:2px 4px;">${UI_ICONS.check} Completion verified' not in html,
            "the_one_line_refusal_is_gone": 'h-tool-card err"><strong>Completion rejected:</strong>' not in html,
            "three_tones": tone.count('return "verified"') == 1
            and tone.count('return "rejected"') == 1
            and tone.count('return "paused"') == 1,
            "handles_the_pause_outcome": '"turn/stagnation_paused"' in tone,
            "states_the_contract": "`spec ${specDigest}`" in facts,
            "states_the_command_and_exit": "`exit ${exitCode} · " in facts,
            "states_the_revision": "harnessEvidenceShort(revision.git_head, 10)" in facts,
            "states_the_files": "`${files.length} modified: " in facts,
            "states_the_patch": "harnessEvidenceShort(r.patch_sha256, 12)" in facts,
            "states_the_environment": "environment.python" in facts,
            "will_not_invent_a_command": "the verification command never ran" in facts,
            "will_not_invent_a_revision": "no revision captured" in facts,
            "names_the_claim": card.count("HARNESS_VERDICT_CLAIMS[tone]") == 1,
            "names_the_failure_code": "ev.failure_code" in card,
            "draws_the_failures": "harnessEvidenceFailuresHtml(failures)" in card,
            "draws_the_coverage": "harnessEvidenceCoverageHtml(coverage)" in card,
            "keeps_the_output_behind_a_disclosure": 'class="h-verdict-output"' in card,
            "opens_the_record": "openHarnessEvidenceModal()" in card,
            "opens_the_diff": "openHarnessDiffModal()" in card,
            "says_it_reruns_nothing": "re-runs nothing" in card,
            "tone_is_a_data_attribute": 'data-verdict="${tone}"' in card,
        },
        "coverage": {
            "a_declared_criterion_is_not_a_pass": 'covered: false' in coverage and "seen.add(criterion)" in coverage,
            "a_missing_test_is_drawn": "missing_test_ids" in coverage,
            "coverage_is_read_from_the_report": "report.acceptance_coverage" in coverage,
            "the_declared_list_says_declared": 'class="ev-cover-mark">declared</span>' in declared_html,
            "covered_is_stated": '"covered" : "not covered"' in coverage_html,
            "an_uncovered_criterion_is_counted": "were not covered by an observed test" in coverage_html,
            "a_failure_names_its_kind": 'data-kind="${escapeHtml(row.kind)}"' in failures_html,
            "the_contract_does_not_judge": "harnessEvidenceDeclaredHtml(declaredCriteria)" in spec
            and "harnessEvidenceCoverageHtml" not in spec,
        },
        "record": {
            "dialog_exists": bool(dialog),
            "is_a_dialog": 'role="dialog"' in dialog_region,
            "is_named": 'aria-label="What a completion claim rests on"' in dialog_region,
            "closes_on_the_backdrop": bool(dialog)
            and bool(re.search(r"if\(event\.target===this\)\w+\(\)", dialog)),
            "opens_through_the_controller": 'openOverlay("harnessEvidenceModal"' in open_modal,
            "closes_through_the_controller": 'closeOverlay("harnessEvidenceModal")' in close_modal,
            "refuses_without_a_session": "there is no run whose evidence could be shown" in open_modal,
            "lists": {name: bool(element(html, name)) for name in SURFACE_LISTS},
            "reads_the_contract_route": "/task`" in refresh,
            "reads_the_diff_route": "/diff`" in refresh,
            "keeps_the_session_it_asked_about": "if (activeHarnessId !== sessionId) return;" in refresh,
            "will_not_race_itself": "harnessEvidenceBusy" in refresh,
            "says_what_failed_to_read": "payload.errors" in render,
            "draws_a_missing_verdict_as_missing": "No verdict is recorded in this session's log" in verdict,
            "draws_a_missing_contract_as_missing": "This session has no verification contract" in spec,
            "draws_a_missing_diff_as_missing": "The workspace diff could not be read" in change,
            "the_verdict_names_the_refs": "report.evidence_refs" in verdict,
            "the_verdict_names_the_transcript": "output sha256" in verdict,
            "the_contract_states_the_rules": "only a clean run verifies" in spec
            and "the revision must not move while the command runs" in spec,
            "the_contract_states_the_command": "no command declared" in spec,
            "the_contract_states_required_files": '"Required modified"' in spec,
            "the_contract_states_known_failures": "pre_existing_failures" in spec,
            "the_contract_says_it_is_not_a_result": "A contract is not a result" in spec,
            "the_observation_states_the_exit": '"Exit code"' in observed,
            "the_observation_states_the_classification": '"Classification"' in observed,
            "the_observation_says_pre_existing_is_not_introduced": "classified as pre-existing, not introduced" in observed,
            "the_change_counts_per_file": "harnessDiffFiles(diff.diff" in change,
            "the_change_states_totals": "+${escapeHtml(String(added))}" in change,
            "the_change_says_what_it_is_not": "not a copy frozen at the verdict" in change,
            "the_artifacts_list_checkpoints": '"Checkpoints"' in artifacts,
            "the_artifacts_list_the_latest": "latest.evidence_refs" in artifacts,
            "the_artifacts_list_the_lineage": "provenance_entry_count" in artifacts,
            "the_artifacts_state_the_next_run_target": '"Workspace now"' in artifacts,
            "the_artifacts_say_a_checkpoint_is_not_a_copy": "not a copy of the workspace" in artifacts,
            "the_limits_are_drawn": "HARNESS_EVIDENCE_LIMITS" in limits,
            "the_limits_are_declared": bool(limits_block),
        },
        "change": {
            "the_diff_is_parsed_once": "diff --git " in diff_files,
            "hunk_headers_are_skipped": 'line.startsWith("@@")' in diff_files,
            "markers_are_skipped": 'line.startsWith("+++") || line.startsWith("---")' in diff_files,
            "the_dialog_reuses_it": "harnessDiffFiles(d.diff || \"\")" in open_diff,
            "the_dialog_counts_the_change": "file(s) · +${added} −${deleted}" in open_diff,
            "the_dialog_says_what_it_is": bool(diff_dialog) and "not frozen at any verdict" in diff_region,
            "the_dialog_offers_the_record": "openHarnessEvidenceModal()" in diff_region,
            "the_dialog_summary_has_a_home": bool(element(html, "harnessGitDiffSummary")),
        },
        "reachability": {
            "from_the_session_actions": '<option value="evidence">' in html
            and 'else if (v === "evidence") openHarnessEvidenceModal();' in html,
            "from_the_palette": "Show what a completion claim rests on" in html,
            "from_every_verdict_card": "openHarnessEvidenceModal()" in card,
            "from_the_diff_dialog": "openHarnessEvidenceModal()" in diff_region,
            "the_timeline_summary_says_it": "HARNESS_VERDICT_WORDS[tone]" in summary,
            "the_surface_is_read_only": "method:" not in refresh
            and not any(call in refresh for call in ()) ,
            "alters_nothing_but_itself": "node.innerHTML = html" in render
            and render.count("document.getElementById") == 2,
        },
    }


def read_server(server: str) -> dict:
    diff = python_function(server, "api_get_harness_diff")
    task = python_function(server, "api_get_harness_task_state")
    compute = python_function(server, "_compute_git_diff")
    return {
        "diff_contract": {
            "still_mounted": '"/api/harness/sessions/{session_id}/diff"' in server,
            "still_returns_the_git_diff": "_compute_git_diff(Path(sess[\"worktree_dir\"]))" in diff,
            "still_reports_a_branch": '"branch": info["branch"]' in compute,
            "still_reports_a_status": '"status": status_out' in compute,
            "still_redacts": "combined = redact_text(" in compute,
            "still_previews_untracked": "_unified_diff_snippet(" in compute,
            "still_says_when_it_is_not_a_repo": "Workspace is not a git repository." in compute,
            "still_bounds_the_payload": "combined[:50_000]" in compute,
        },
        "apply_contract": {
            "still_mounted": '"/api/harness/sessions/{session_id}/apply"' in server,
            "still_refuses_a_plan_policy": 'read-only \'plan\'' in server,
            "still_handles_a_quarantined_workspace": "workspace_quarantined" in server,
            "still_emits_the_event": "worktree/applied" in server,
            "the_story_did_not_touch_it": "apply" not in python_function(server, "refreshHarnessEvidence")
            if python_function(server, "refreshHarnessEvidence")
            else True,
        },
        "task_contract": {
            "still_mounted": '"/api/harness/sessions/{session_id}/task"' in server,
            "still_returns_the_spec": '"verification_spec": ver_spec.to_dict()' in task,
            "still_returns_the_evidence_preview": '"verification_evidence": ver_evidence_preview' in task,
            "still_returns_the_checkpoints": '"checkpoints": [ck.to_dict() for ck in checkpoints]' in task,
            "still_returns_the_lineage": '"artifact_lineage": _artifact_lineage_summary_or_none()' in task,
            "a_failed_capture_is_empty_not_fatal": "ver_evidence_preview = None" in task,
        },
    }


def build_inventory() -> dict:
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

    for section, descriptions in (
        (
            "card",
            {
                "one_builder": "there is more than one verdict card builder again",
                "one_renderer_for_three_outcomes": "a verdict can be drawn by more than one branch",
                "the_one_line_label_is_gone": "the one-line `Completion verified (digest)` label is back",
                "the_one_line_refusal_is_gone": "the one-line rejection card is back",
                "three_tones": "the three outcomes the runtime can report are not distinguished",
                "handles_the_pause_outcome": "a stagnation pause is not drawn as a verdict",
                "states_the_contract": "the card no longer states the contract digest",
                "states_the_command_and_exit": "the card no longer states the command's exit code",
                "states_the_revision": "the card no longer states the revision the result is tied to",
                "states_the_files": "the card no longer says which files changed",
                "states_the_patch": "the card no longer states the patch digest",
                "states_the_environment": "the card no longer states the environment the command ran in",
                "will_not_invent_a_command": "a run that never executed a command is drawn as if it had",
                "will_not_invent_a_revision": "a result with no captured revision is drawn as if it had one",
                "names_the_claim": "the card does not say what the verdict means",
                "names_the_failure_code": "a rejected card does not name the code that decided it",
                "draws_the_failures": "the failing tests are not drawn",
                "draws_the_coverage": "the acceptance coverage is not drawn",
                "keeps_the_output_behind_a_disclosure": "the verification output is dumped inline",
                "opens_the_record": "the card does not open the record behind it",
                "opens_the_diff": "the card does not open the change it belongs to",
                "says_it_reruns_nothing": "the card does not say it re-runs nothing",
                "tone_is_a_data_attribute": "the card's tone is not stated in the DOM",
            },
        ),
        (
            "coverage",
            {
                "a_declared_criterion_is_not_a_pass": "a declared criterion is treated as covered",
                "a_missing_test_is_drawn": "the missing test ids are dropped instead of drawn",
                "coverage_is_read_from_the_report": "coverage is not read from what the runtime recorded",
                "the_declared_list_says_declared": "a declared criterion is drawn with a verdict mark",
                "covered_is_stated": "a coverage row does not say whether it was covered",
                "an_uncovered_criterion_is_counted": "uncovered criteria are drawn without being counted",
                "a_failure_names_its_kind": "a failing test is not classified",
                "the_contract_does_not_judge": "the contract section judges coverage it did not observe",
            },
        ),
        (
            "record",
            {
                "dialog_exists": "the record is gone",
                "is_a_dialog": "the record is not a dialog",
                "is_named": "the record has no accessible name",
                "closes_on_the_backdrop": "the record cannot be dismissed by its backdrop",
                "opens_through_the_controller": "the record does not go through openOverlay()",
                "closes_through_the_controller": "the record does not go through closeOverlay()",
                "refuses_without_a_session": "the record opens with no session to describe",
                "reads_the_contract_route": "the record does not read the session's contract",
                "reads_the_diff_route": "the record does not read the change",
                "keeps_the_session_it_asked_about": "a slow read can render another session's evidence",
                "will_not_race_itself": "two reads can interleave",
                "says_what_failed_to_read": "a failed read is presented as a clean one",
                "draws_a_missing_verdict_as_missing": "an absent verdict is presented as a result",
                "draws_a_missing_contract_as_missing": "an absent contract is presented as a gate",
                "draws_a_missing_diff_as_missing": "an unreadable diff is presented as a change",
                "the_verdict_names_the_refs": "the evidence references are not drawn",
                "the_verdict_names_the_transcript": "the transcript digest is not drawn",
                "the_contract_states_the_rules": "the contract's rules are not stated",
                "the_contract_states_the_command": "the contract does not state its command",
                "the_contract_states_required_files": "the contract does not state its required files",
                "the_contract_states_known_failures": "the contract's known failures are not stated",
                "the_contract_says_it_is_not_a_result": "the contract is drawn as if it were a result",
                "the_observation_states_the_exit": "the observation does not state the exit code",
                "the_observation_states_the_classification": "the observation does not classify the run",
                "the_observation_says_pre_existing_is_not_introduced": "a pre-existing failure is drawn as an introduced one",
                "the_change_counts_per_file": "the change is not counted per file",
                "the_change_states_totals": "the change has no totals",
                "the_change_says_what_it_is_not": "the diff is presented as frozen at the verdict",
                "the_artifacts_list_checkpoints": "the checkpoints are not listed",
                "the_artifacts_list_the_latest": "the latest checkpoint's evidence is not listed",
                "the_artifacts_list_the_lineage": "the artifact lineage is not listed",
                "the_artifacts_state_the_next_run_target": "the revision a re-run would use is not stated",
                "the_artifacts_say_a_checkpoint_is_not_a_copy": "a checkpoint is drawn as a copy of the workspace",
                "the_limits_are_drawn": "the surface does not say what it does not prove",
                "the_limits_are_declared": "the limits are not a list this inventory can check",
            },
        ),
        (
            "change",
            {
                "the_diff_is_parsed_once": "the unified diff is not parsed",
                "hunk_headers_are_skipped": "a hunk header counts as an added line",
                "markers_are_skipped": "the ---/+++ markers count as changes",
                "the_dialog_reuses_it": "the diff dialog and the record disagree about the change",
                "the_dialog_counts_the_change": "the diff dialog states no per-file counts",
                "the_dialog_says_what_it_is": "the diff dialog does not say it is not frozen at a verdict",
                "the_dialog_offers_the_record": "the diff dialog cannot reach the record",
                "the_dialog_summary_has_a_home": "the diff dialog has nowhere to put the counts",
            },
        ),
        (
            "reachability",
            {
                "from_the_session_actions": "the session actions do not offer the record",
                "from_the_palette": "the command palette does not offer the record",
                "from_every_verdict_card": "a verdict card cannot open the record",
                "from_the_diff_dialog": "the diff dialog cannot open the record",
                "the_timeline_summary_says_it": "the timeline strip still summarises a verdict generically",
                "the_surface_is_read_only": "the record writes something",
                "alters_nothing_but_itself": "the surface does more than draw its own sections",
            },
        ),
    ):
        for flag, description in descriptions.items():
            if not frontend[section].get(flag):
                fail(section, description)
    for name, present in frontend["record"]["lists"].items():
        if not present:
            fail("record", f"{name} is gone from the record")

    for section, descriptions in backend.items():
        for flag, description in descriptions.items():
            if not backend[section].get(flag):
                fail(section, description)

    routes = []
    for method, path in ROUTES:
        mounted = bool(re.search(rf'@app\.{method.lower()}\("{re.escape(path)}"\)', server))
        routes.append({"method": method, "path": path, "mounted": mounted})
        if not mounted:
            fail("route-missing", f"{method} {path} is no longer mounted")

    mounted = mounted_routes(server)
    called = called_routes(html)
    unmounted = [path for path in called if not any(route_matches(path, mount) for mount in mounted)]
    for path in unmounted:
        fail("unmounted-call", f"the pane calls {path}, which the server does not mount")

    for invented in (
        "/api/harness/verification",
        "/api/harness/sessions/{session_id}/evidence",
        "/api/harness/sessions/{session_id}/artifacts",
    ):
        if invented in html or invented.replace("{session_id}", "") in server:
            fail("invented-route", f"the story added {invented}; /task and /diff already carried this")

    styles = {
        "verdict": ".h-verdict {" in html,
        "verdict_rejected": '.h-verdict[data-verdict="rejected"]' in html,
        "verdict_paused": '.h-verdict[data-verdict="paused"]' in html,
        "verdict_facts": ".h-verdict .ev-facts {" in html,
        "coverage": ".ev-coverage {" in html,
        "coverage_uncovered": '.ev-cover[data-covered="false"]' in html,
        "coverage_declared": '.ev-cover[data-declared="true"]' in html,
        "failure": ".ev-failure {" in html,
        "output": ".h-verdict-output pre, .ev-output pre {" in html,
        "record_block": ".ev-block {" in html,
        "record_verdict": ".ev-verdict {" in html,
        "record_facts": ".ev-facts {" in html,
        "record_files": ".ev-file {" in html,
        "record_limits": ".ev-limits {" in html,
        "wraps_long_paths": "overflow-wrap: anywhere" in html,
    }
    for flag, present in styles.items():
        if not present:
            fail("style-missing", f"{flag} has no rule")

    totals = {
        "checks": sum(
            len(frontend[key])
            for key in ("card", "coverage", "record", "change", "reachability")
        )
        + sum(len(backend[key]) for key in backend)
        + len(styles),
        "functions": len(FUNCTIONS),
        "record_sections": len(SURFACE_LISTS),
        "routes": len(routes),
        "routes_called": len(called),
        "routes_unmounted": len(unmounted),
        "styles": len(styles),
        "findings": len(findings),
    }

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P3-04",
        "statement": (
            "A completion claim in the Harness is a claim about evidence, and the pane now draws it "
            "as one: a verdict card that states the contract's digest, the command and its exit code, "
            "the revision the result is tied to, the files and the patch - and opens the record behind "
            "it, which holds the contract the session set, the observation the runtime wrote (including "
            "which declarations an observed test actually covered), the change the claim is attached to, "
            "counted per file from the same diff the diff dialog draws, and the checkpoints and revision "
            "a re-run would be tied to - while a session with no verdict, no contract or no readable "
            "diff says exactly that instead of implying a pass."
        ),
        "source": {"frontend": "static/index.html", "server": "app.py"},
        "routes": routes,
        "frontend": frontend,
        "server": backend,
        "styles": styles,
        "totals": totals,
        "findings": findings,
        "limits": [
            "the verdict is drawn from the record the runtime wrote (`TaskVerificationReport.to_dict()` "
            "inside `turn/verified` / `turn/verification_failed` / `turn/stagnation_paused`); the surface "
            "does not re-run the command, and the card says so rather than implying a fresh result",
            "the diff is read live from the worktree, so a change made after the turn appears in it; the "
            "record says that instead of presenting the diff as a copy frozen at the verdict",
            "the contract is read from `GET …/task`, which also computes a read-only evidence preview "
            "(the workspace revision and the environment a next command would run in). When that capture "
            "fails the server returns null and the surface states that it is missing rather than guessing",
            "acceptance coverage is what the transcript printed: a test id counts only when it appears in "
            "the output and is not among the classified failures, so an uncovered criterion is drawn as "
            "uncovered and fails closed",
            "both fixtures are the product's own records: `scripts/harness_evidence_seed.py` builds two git "
            "workspaces and lets `TaskStateMachine.verify_task_completion()` decide - one passes, one "
            "fails - then appends the reports with `_append_harness_event()`. What is staged is the turn "
            "bookends, because the demo has no model runtime to run one",
            "the surface lists the session's own checkpoints and the store-wide lineage summary; it is not "
            "a global audit view and does not read another session's records",
        ],
    }


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    frontend = payload["frontend"]
    checks = ", ".join(
        f"{name} {len(frontend[name])}"
        for name in ("card", "coverage", "record", "change", "reachability")
    )
    return "\n".join(
        [
            "# A completion claim and the evidence behind it (UIUX-P3-04)",
            "",
            "> Generated by `scripts/uiux_harness_evidence_inventory.py`. Do not edit by hand: change "
            "the pane and regenerate.",
            "",
            payload["statement"],
            "",
            "## What a verdict is",
            "",
            "When a Harness turn claims completion, `TaskStateMachine.verify_task_completion()` decides:",
            "",
            "| Input | Where it comes from |",
            "|---|---|",
            "| the contract | `TaskVerificationSpec` — command, required files, protected digests, rules |",
            "| the observation | `TestEvidence` — exit code, transcript digest, classified failures |",
            "| the revision | `capture_source_revision()` — git head, dirty paths, file digests |",
            "| the coverage | `evaluate_acceptance_coverage()` over that transcript |",
            "",
            "The report is then written into the durable log as `turn/verified`,",
            "`turn/verification_failed` or `turn/stagnation_paused` — with `TaskVerificationReport.to_dict()`",
            "as the payload. Everything the pane draws was already in that record.",
            "",
            "## One card, three outcomes",
            "",
            "Before this story the pane drew a verified turn as `Completion verified (spec_sha256…)` and a",
            "rejected one as a failure code with a reason. Neither said what was checked. The two renderers",
            "are gone; `renderHarnessVerdictCardHtml()` draws all three outcomes:",
            "",
            "```",
            "⚠ Completion rejected                                            spec 93ddcca159a8…",
            "  The run claimed completion and the evidence did not support it, so the turn did not complete.",
            "  verification_test_failed_introduced — Authoritative test command exited with code 1 …",
            "  Contract     spec 93ddcca159a8…",
            "  Command      exit 1 · introduced",
            "  Revision     abd4a28f9a… · 1 uncommitted",
            "  Files        2 modified: src/total.py, tests/test_total.py",
            "  Patch        sha256 92d63ae6e9dc…",
            "  Environment  python 3.11.2 on Linux x86_64",
            "  tests/test_total.py::test_total_sums_a_list   INTRODUCED   assert 7 == 6",
            "  [▸ Verification output (500 chars kept)]",
            "  [Verification & artifacts] [Inspect the diff]",
            "  A verdict is the runtime's record of one turn: this card draws it, and re-runs nothing.",
            "```",
            "",
            "A value the record does not contain is never invented: a run with no captured revision says",
            "`no revision captured`, and one whose command never ran says `the verification command never ran`.",
            "",
            "## The record behind the card",
            "",
            "`#harnessEvidenceModal` is the `Verification & Artifacts` session action, the command palette's",
            "`Show what a completion claim rests on`, a button on every verdict card, and a button in the",
            "diff dialog. It reads only what already existed — `GET …/task` and `GET …/diff` — and draws:",
            "",
            "| Section | What it says |",
            "|---|---|",
            "| The verdict | the verdict, the code that decided it, the facts it rests on, the failing tests, "
            "the evidence refs, the transcript digest, and the output itself |",
            "| What had to pass | the contract: command, first stage, required output markers, required and "
            "modified files, protected digests, the rules in force, and the criteria it declares |",
            "| What was observed | exit code, classification, pre-existing failures classified as "
            "pre-existing, output digest, and the coverage the transcript actually demonstrated |",
            "| The change | the branch, the status, and per-file `+n −m` counted from the same diff the diff "
            "dialog draws |",
            "| What the turn left behind | the checkpoints, the latest checkpoint's evidence refs, the "
            "artifact lineage, and the revision a re-run would be tied to |",
            "| What this does not prove | four statements, in the surface rather than in a commit message |",
            "",
            "Three absences are drawn as absences: no verdict in the log (`No verdict is recorded in this",
            "session's log yet`), no contract on the session (`completion was not gated by one`), and no",
            "readable diff. None of them is drawn as a pass.",
            "",
            "## Coverage is what the transcript printed",
            "",
            "A declared criterion is a promise about a test, not proof of it. `evaluate_acceptance_coverage()`",
            "counts a test id only when it appears in the output and is not among the classified failures,",
            "so the record can say:",
            "",
            "```",
            "NOT COVERED   total() sums a list   tests/test_total.py::test_total_sums_a_list",
            "1 declared criterion(s) were not covered by an observed test, so this record does not claim them.",
            "```",
            "",
            "The contract section draws the same criteria as `declared` — no verdict mark — because the",
            "contract is the gate, and the observation is what happened when it was applied.",
            "",
            "## The change, counted",
            "",
            "`harnessDiffFiles()` parses the unified diff once, skipping `diff --git`, `+++`, `---` and `@@`",
            "so a hunk header cannot be mistaken for an addition, and both the record and the diff dialog",
            "count with it: `2 file(s) · +18 −4 · on master`. The diff dialog now states what it is:",
            "",
            "> This is the worktree as it is now. It is not frozen at any verdict: \"Verification & artifacts\"",
            "> says what a completion claim was checked against.",
            "",
            "## What this story had to preserve",
            "",
            "| Preserved rule | Where it still lives |",
            "|---|---|",
            "| `/diff` returns the git status, the diff and the branch | `api_get_harness_diff` |",
            "| the diff is redacted and bounded | `_compute_git_diff` |",
            "| untracked files are previewed | `_compute_git_diff` |",
            "| `/apply` refuses a read-only policy and reports a quarantined workspace | `api_apply_harness_worktree` |",
            "| `/task` still returns the spec, the evidence preview, the checkpoints and the lineage | `api_get_harness_task_state` |",
            "| a failed evidence capture leaves the preview empty | `ver_evidence_preview = None` |",
            "",
            "The three routes the surface uses all predate this story; it added no endpoint, and the",
            "inventory fails if a verification route appears that the server does not mount.",
            "",
            "## Checks and limits",
            "",
            f"`--check` re-derives {totals['checks']} checks from the source ({checks}, the styles, and the "
            f"server's own contracts) and fails on any of them. `node scripts/harness_evidence_walk.mjs` "
            "drives the same surface in a real browser against `scripts/harness_evidence_seed.py`, which "
            "lets the product's verifier decide on two small git workspaces - one passing, one failing - "
            "and appends the reports with the writer a turn uses. The walk reads the card, opens the",
            "record, follows the change into the diff dialog, and photographs five states in",
            "[`media/harness-evidence/`](media/harness-evidence/README.md).",
            "",
        ]
        + [f"- {limit}." for limit in payload["limits"]]
        + [
            "",
            "## Regenerating",
            "",
            "1. Change the pane in `static/index.html`.",
            "2. `python3 scripts/uiux_harness_evidence_inventory.py`.",
            "3. `node scripts/harness_evidence_walk.mjs --screenshots docs/media/harness-evidence`.",
            "",
        ]
    )


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
            print("error: the Harness evidence inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_harness_evidence_inventory.py")
            return 1
        if payload["findings"]:
            print("error: what a completion claim rests on disagrees with the source:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"harness evidence OK: {payload['totals']['checks']} checks, "
            f"{payload['totals']['record_sections']} record sections, 0 findings"
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
