"""Coordination invariants for the M5 2.0 UI/UX program ledger.

`product/M5_2_0_UIUX_Autonomous_Roadmap.json` is the shared claim ledger for the
UI/UX program, the same way `product/M5_2_0_Autonomous_Roadmap.json` is for the
runtime program. A ledger that drifts from the shipped frontend, or that can be
marked `done` without evidence, coordinates nothing — so the invariants that
matter operationally are asserted here instead of being trusted to review.

The suite is written to fail closed. Every check is written against what the
file claims, so the first session that tries to shortcut the protocol (claim two
stories, mark a story done with no evidence, edit a tab that no longer exists)
gets a red test rather than a green pull request.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
RUNTIME_LEDGER = ROOT / "product" / "M5_2_0_Autonomous_Roadmap.json"
FRONTEND = ROOT / "static" / "index.html"
SCREENSHOT_MANIFEST = ROOT / "docs" / "media" / "screenshots" / "manifest.json"
PROTOCOL = ROOT / "M5_2_0_UIUX_AGENT_PROMPT.md"
PROGRAM_DOC = ROOT / "docs" / "uiux-2-0-program.md"

STORY_ID_RE = re.compile(r"^UIUX-P([0-6])-\d{2}$")
STATUSES = {"not_started", "in_progress", "blocked", "done"}
TRACKING_KEYS = {
    "status",
    "progress_percent",
    "attempts",
    "started_at",
    "updated_at",
    "completed_at",
    "owner_session",
    "commit_sha",
    "changed_files",
    "evidence",
    "test_results",
    "visual_evidence",
    "known_regressions",
    "decisions",
    "blockers",
    "handoff_note",
    "next_action",
    "readme_updated",
}


def _ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _stories() -> list[dict]:
    return _ledger()["stories"]


def _by_id() -> dict[str, dict]:
    return {story["id"]: story for story in _stories()}


# ---------------------------------------------------------------------------
# The ledger must describe the frontend that actually ships
# ---------------------------------------------------------------------------


def test_tab_registry_matches_the_shipped_frontend_and_screenshot_manifest() -> None:
    """Six panes, and the ids they really use.

    The roadmap talks about "Council" and "Pipeline"; the DOM ids are `hall` and
    `council` respectively. A session that greps for a `pipeline` tab finds
    nothing, so the registry is derived from the frontend and the screenshot
    manifest and pinned here.
    """
    registry = _ledger()["tab_registry"]
    assert len(registry) == 6, "the shell has exactly six mode tabs"

    html = FRONTEND.read_text(encoding="utf-8")
    dom_ids = re.findall(r'data-tab="([a-z]+)"', html)
    assert len(dom_ids) == 6, f"expected six data-tab buttons in static/index.html, found {dom_ids}"
    assert len(set(dom_ids)) == 6, f"duplicate data-tab values: {dom_ids}"

    manifest = json.loads(SCREENSHOT_MANIFEST.read_text(encoding="utf-8"))
    shots = manifest["shots"]
    assert [shot["tab"] for shot in shots] == dom_ids, (
        "screenshot manifest tabs must match the DOM tab ids in order: "
        f"{[shot['tab'] for shot in shots]} != {dom_ids}"
    )

    assert [entry["dom_id"] for entry in registry] == dom_ids
    for entry in registry:
        shot = next(s for s in shots if s["tab"] == entry["dom_id"])
        assert entry["screenshot_id"] == shot["id"]
        assert entry["screenshot_file"] == shot["file"]
        assert entry["label"] == shot["title"], (
            f"{entry['dom_id']}: roadmap label {entry['label']!r} != manifest title {shot['title']!r}"
        )
        # The button's `title` attribute is a description ("Guided first run"),
        # not the pane label, so the label is checked against the manifest above
        # and the button only has to describe the pane it opens.
        assert entry["button_title"].strip(), f"{entry['dom_id']}: button has no title attribute"
        assert entry["purpose"].strip(), f"{entry['dom_id']}: screenshot manifest has no purpose"
        assert re.fullmatch(r"Alt\+[1-6]", entry["keyboard_shortcut"] or ""), (
            f"{entry['dom_id']}: missing or malformed keyboard shortcut"
        )


def test_story_tab_labels_are_known_components() -> None:
    """A story may only claim a tab or component that exists in the registry."""
    known = {entry["label"] for entry in _ledger()["tab_registry"]}
    allowed = known | {"Shared"}
    for story in _stories():
        assert story["tab_or_component"] in allowed, (
            f"{story['id']}: unknown tab_or_component {story['tab_or_component']!r}"
        )


def test_feature_preservation_matrix_covers_every_tab() -> None:
    matrix = _ledger()["feature_preservation_matrix"]
    labels = [entry["label"] for entry in _ledger()["tab_registry"]]
    covered = {entry["tab"] for entry in matrix}
    missing = [label for label in labels if label not in covered]
    assert not missing, f"preservation matrix does not cover: {missing}"
    for entry in matrix:
        for field in ("tab", "existing", "target", "must_preserve"):
            assert str(entry.get(field) or "").strip(), f"matrix row {entry.get('tab')!r} missing {field}"


# ---------------------------------------------------------------------------
# Story and tracking schema
# ---------------------------------------------------------------------------


def test_every_story_carries_a_complete_tracking_block() -> None:
    for story in _stories():
        tracking = story["tracking"]
        missing = sorted(TRACKING_KEYS - set(tracking))
        assert not missing, f"{story['id']}: tracking is missing {missing}"
        assert tracking["status"] in STATUSES, f"{story['id']}: bad status {tracking['status']!r}"
        assert isinstance(tracking["progress_percent"], int)
        assert 0 <= tracking["progress_percent"] <= 100
        assert isinstance(tracking["attempts"], int) and tracking["attempts"] >= 0
        assert isinstance(tracking["readme_updated"], bool)
        assert isinstance(tracking["evidence"], list)
        assert isinstance(tracking["test_results"], list)
        if tracking["status"] == "not_started":
            assert tracking["progress_percent"] == 0, f"{story['id']}: not_started with progress"
            assert tracking["owner_session"] is None
        if tracking["status"] == "in_progress":
            assert tracking["owner_session"], f"{story['id']}: in_progress without an owner_session"
            assert 0 < tracking["progress_percent"] < 100


def test_a_story_marked_done_must_carry_evidence() -> None:
    """Fail closed: `done` is a claim, and a claim needs evidence.

    This is the invariant that stops a session from closing the book on a story
    because the code exists. `commit_sha` must be a real 40-hex sha, `evidence`
    must be non-empty, and `completed_at` must be set.
    """
    for story in _stories():
        tracking = story["tracking"]
        if tracking["status"] != "done":
            continue
        assert tracking["progress_percent"] == 100, f"{story['id']}: done but progress != 100"
        assert tracking["evidence"], f"{story['id']}: done with no evidence"
        assert tracking["completed_at"], f"{story['id']}: done with no completed_at"
        assert re.fullmatch(r"[0-9a-f]{40}", str(tracking["commit_sha"] or "")), (
            f"{story['id']}: done without a full commit sha"
        )


def test_story_fields_the_protocol_depends_on_are_present() -> None:
    for story in _stories():
        for field in ("title", "phase", "priority", "acceptance_criteria", "implementation_steps",
                      "required_evidence", "verification_commands", "rollback_strategy"):
            assert story.get(field), f"{story['id']}: missing {field}"
        # An empty dependency list is a real statement ("this one is first").
        assert isinstance(story.get("dependencies"), list), f"{story['id']}: dependencies must be a list"
        for command in story["verification_commands"]:
            assert command.startswith((".venv/bin/python", "node ")), (
                f"{story['id']}: unexpected verification command {command!r}"
            )


# ---------------------------------------------------------------------------
# Graph, counters and readiness
# ---------------------------------------------------------------------------


def test_story_ids_phases_and_dependencies_are_consistent() -> None:
    stories = _stories()
    ids = [s["id"] for s in stories]
    assert len(ids) == len(set(ids)), "duplicate story ids"

    known = set(ids)
    for story in stories:
        match = STORY_ID_RE.match(story["id"])
        assert match, f"{story['id']}: id does not match UIUX-P<phase>-<nn>"
        assert f"P{match.group(1)}" == story["phase"], f"{story['id']}: id phase != story phase"
        for dep in story["dependencies"]:
            assert dep in known, f"{story['id']}: unknown dependency {dep}"
            assert dep != story["id"], f"{story['id']}: depends on itself"

    # No cycles: repeatedly drop stories whose dependencies are already resolved.
    resolved: set[str] = set()
    remaining = {s["id"]: set(s["dependencies"]) for s in stories}
    while True:
        ready = {sid for sid, deps in remaining.items() if deps <= resolved}
        if not ready:
            break
        for sid in ready:
            resolved.add(sid)
            remaining.pop(sid)
    assert not remaining, f"dependency cycle among {sorted(remaining)}"


def test_phase_and_overall_counters_match_the_stories() -> None:
    ledger = _ledger()
    stories = ledger["stories"]
    for phase in ledger["phases"]:
        members = [s for s in stories if s["phase"] == phase["id"]]
        assert phase["story_ids"] == [s["id"] for s in members], f"{phase['id']}: story_ids drifted"
        assert phase["total"] == len(members)
        for status in ("done", "in_progress", "blocked", "not_started"):
            assert phase[status] == sum(1 for s in members if s["tracking"]["status"] == status), (
                f"{phase['id']}: {status} counter drifted"
            )
        assert phase["percent_complete"] == round(100 * phase["done"] / phase["total"]), (
            f"{phase['id']}: percent_complete drifted from the story count"
        )
        expected_phase_status = (
            "done" if phase["done"] == phase["total"]
            else "blocked" if phase["blocked"]
            else "in_progress" if phase["done"] or phase["in_progress"]
            else "not_started"
        )
        assert phase["status"] == expected_phase_status, f"{phase['id']}: status drifted from story tracking"

    overall = ledger["overall_progress"]
    assert overall["total_stories"] == len(stories)
    for status in ("done", "in_progress", "blocked", "not_started"):
        assert overall[status] == sum(1 for s in stories if s["tracking"]["status"] == status), (
            f"overall {status} counter drifted"
        )
    assert sum(overall[k] for k in ("done", "in_progress", "blocked", "not_started")) == len(stories)
    assert overall["percent_complete"] == round(100 * overall["done"] / len(stories))


def test_session_state_names_a_ready_next_story() -> None:
    ledger = _ledger()
    state = ledger["session_state"]
    for field in (
        "active_story",
        "active_stories",
        "last_completed_story",
        "next_recommended_story",
        "last_checkpoint",
        "last_session_id",
        "coordination_protocol",
        "open_risks",
        "last_updated",
    ):
        assert field in state, f"session_state missing {field}"

    assert state["open_risks"], "a program with no recorded risks has not looked"
    assert state["coordination_protocol"] == PROTOCOL.name

    next_id = state["next_recommended_story"]
    stories = _by_id()
    assert next_id in stories, f"next_recommended_story {next_id!r} is not a story"
    next_story = stories[next_id]
    assert next_story["tracking"]["status"] in {"not_started", "in_progress"}
    unmet = [d for d in next_story["dependencies"] if stories[d]["tracking"]["status"] != "done"]
    assert not unmet, f"next_recommended_story {next_id} has unmet dependencies {unmet}"

    # P0 is a hard gate: no later phase may start while a P0 story is open.
    open_p0 = [s["id"] for s in stories.values() if s["phase"] == "P0" and s["tracking"]["status"] != "done"]
    if open_p0:
        assert next_story["phase"] == "P0", (
            f"P0 gate violated: {open_p0} are open but the recommendation is {next_id}"
        )

    assert state["active_story"] is None or state["active_story"] in stories
    for sid in state["active_stories"]:
        assert sid in stories, f"active_stories names unknown story {sid}"


def test_the_source_commit_pin_is_explicit() -> None:
    """A plan written from web extracts must say so, and must be re-pinned."""
    ledger = _ledger()
    sha = ledger["source_commit_sha"]
    status = ledger["source_commit_status"]
    assert re.fullmatch(r"[0-9a-f]{40}", sha), "source_commit_sha must be a full sha"
    assert "re-pin" in status.lower(), (
        "source_commit_status must tell the first session to re-pin HEAD, because main moves"
    )
    unverified = ledger["evidence_basis"]["not_verified"]
    assert any("not started" in item or "not re-run" in item for item in unverified)


# ---------------------------------------------------------------------------
# Coordination artifacts exist and are reachable
# ---------------------------------------------------------------------------


def test_coordination_documents_exist_and_are_linked() -> None:
    assert PROTOCOL.is_file(), "the UI/UX agent prompt is missing"
    assert PROGRAM_DOC.is_file(), "the UI/UX program doc is missing"
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for needle in ("M5_2_0_UIUX_Autonomous_Roadmap.json", "M5_2_0_UIUX_AGENT_PROMPT.md"):
        assert needle in agents, f"AGENTS.md does not point at {needle}"
    assert "uiux-2-0-program.md" in docs_index, "docs/README.md does not index the program doc"
    assert "M5_2_0_UIUX_Autonomous_Roadmap.json" in docs_index


def test_the_program_does_not_edit_runtime_story_tracking() -> None:
    """Two ledgers, two ownership domains; a UI story never closes an M5 story."""
    uiux_ids = set(_by_id())
    runtime = json.loads(RUNTIME_LEDGER.read_text(encoding="utf-8"))
    runtime_ids = {story["id"] for story in runtime["stories"]}
    assert not (uiux_ids & runtime_ids), "story id namespaces must not collide"
    relations = _ledger()["program_relations"]
    assert relations["runtime_program"] == "product/M5_2_0_Autonomous_Roadmap.json"
    assert "never touch UIUX tracking" in relations["coordination_rule"] or (
        "must not touch UIUX tracking" in relations["coordination_rule"]
    )


@pytest.mark.parametrize("phase_doc_key", ["P0", "P1", "P2", "P3", "P4", "P5", "P6"])
def test_each_phase_has_a_release_gate(phase_doc_key: str) -> None:
    gate = _ledger()["release_gates"][phase_doc_key]
    assert isinstance(gate, str) and len(gate) > 40, f"{phase_doc_key}: release gate is not a usable statement"
