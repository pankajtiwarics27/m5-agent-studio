"""The empty, error and recovery presentations (UIUX-P1-05).

Three failure surfaces, one shape. These tests pin the parts a runtime assertion
cannot see across a 5,000-line file: that every kind is styled, that each container
which can be empty or broken is actually presented into, that a presentation cannot
be rendered without saying why and what survived, and that the handlers converted by
this story do not slide back to `"Error: " + message`.

The negative cases matter as much as the positive ones. A check that passes because
it stopped looking is the same defect it was written to catch, so each rule is broken
in a copy of the markup and the rule's own name has to appear in the findings.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "presentation_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Presentations.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-presentations.md"

# The raw `"Error: " + message` writers left outside the converted handlers. This is a
# ceiling, not a target: it may go down as panes are rebuilt, never up.
RAW_WRITER_CEILING = 3
RAW_WRITER_HANDLERS = {"installSkill", "openHarnessDiffModal", "runMcpPlayground"}


def _load_inventory_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_presentation_inventory",
        PROJECT_ROOT / "scripts" / "uiux_presentation_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_inventory_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def markup_copy(tmp_path, monkeypatch):
    """Point the inventory at a copy of the markup so a rule can be broken in it."""

    def _make(transform) -> dict:
        text = FRONTEND.read_text(encoding="utf-8")
        changed = transform(text)
        assert changed != text, (
            "the transform did not change the markup, so the test proves nothing"
        )
        path = tmp_path / "index.html"
        path.write_text(changed, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ──────────────────────────────────────────────────


def test_inventory_matches_the_markup():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_presentation_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_are_generated():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Empty, error and recovery presentations")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P1-05"
    assert data["totals"]["kinds"] == 3


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── the three kinds share one shape ──────────────────────────────────────────


def test_each_kind_is_declared_and_styled(payload):
    kinds = inventory.read_kinds(FRONTEND.read_text(encoding="utf-8"))
    for kind in inventory.KINDS:
        assert kinds["declared"][kind], f"{kind} is not declared"
        assert kinds["styled"][kind], f"{kind} has no style of its own"


def test_the_stylesheet_prints_the_cause_and_kept_labels(payload):
    kinds = inventory.read_kinds(FRONTEND.read_text(encoding="utf-8"))
    assert kinds["labels"] == {"cause": True, "context": True}
    html = FRONTEND.read_text(encoding="utf-8")
    # The label belongs to the stylesheet precisely so a caller cannot forget it.
    assert ".presentation-cause::before" in html
    assert ".presentation-context::before" in html
    assert 'content: "Cause: "' in html
    assert 'content: "Kept: "' in html


def test_every_surface_says_why_what_survives_and_what_to_do(payload):
    assert len(payload["surfaces"]) == len(inventory.CONTAINERS) == 5
    for surface in payload["surfaces"]:
        assert surface["kind"] in inventory.KINDS, surface
        assert surface["has_cause"] and surface["has_context"], surface
        assert surface["actions"], f"{surface['container']} offers no action"
        assert all(label.strip() for label in surface["actions"]), surface


def test_every_container_that_can_be_empty_or_broken_is_wired(payload):
    wired = {surface["container"] for surface in payload["surfaces"]}
    assert wired == set(inventory.CONTAINERS)
    assert payload["totals"]["wired_containers"] == payload["totals"]["containers"]


def test_the_builder_validates_before_it_renders():
    html = FRONTEND.read_text(encoding="utf-8")
    builder = html[
        html.find("function presentationProblem") : html.find("function presentInto")
    ]
    assert builder, "the builder is gone"
    for field in ("kind", "title", "cause", "context", "actions"):
        assert field in builder, f"the builder stopped validating {field!r}"
    assert (
        "throw new Error(`presentation refused" in html
        or "presentation refused" in html
    )
    assert "window.M5Presentations" in html


def test_the_empty_panes_are_drawn_at_startup():
    html = FRONTEND.read_text(encoding="utf-8")
    startup = html[
        html.find("applyShortcutHints();") : html.find("applyShortcutHints();") + 400
    ]
    assert "presentDebateEmpty()" in startup
    assert "presentHallEmpty()" in startup


# ── the converted handlers stay converted ────────────────────────────────────


def test_converted_handlers_do_not_write_raw_errors(payload):
    for writer in payload["remaining_raw_writers"]:
        assert writer["handler"] not in inventory.WIRED_HANDLERS, writer


def test_raw_writer_debt_does_not_grow(payload):
    writers = payload["remaining_raw_writers"]
    assert len(writers) <= RAW_WRITER_CEILING, [w["line"] for w in writers]
    assert {writer["handler"] for writer in writers} <= RAW_WRITER_HANDLERS


# ── the walk exists and knows what to break ──────────────────────────────────


def test_the_walk_breaks_the_network_on_purpose():
    source = WALK.read_text(encoding="utf-8")
    for route in ("/api/onboarding/preflight", "/api/chat/stream", "/events/stream"):
        assert route in source, f"the walk does not starve {route}"
    assert "request.abort" in source
    for state in ("compare-empty", "start-error", "chat-error", "harness-recovery"):
        assert f'"{state}"' in source, f"the walk cannot capture {state}"


def test_the_evidence_is_described_where_it_lives():
    media = PROJECT_ROOT / "docs" / "media" / "presentations"
    described = media / "README.md"
    assert described.exists(), "the captures need a note saying how they were taken"
    assert "presentation_walk.mjs" in described.read_text(encoding="utf-8")


# ── negative cases: each rule must be able to fail ───────────────────────────


def test_removing_a_cause_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace(
            "cause: error && error.message", "reason: error && error.message", 1
        )
    )
    assert "surface-no-cause" in _checks(payload)


def _rename_first(text: str, call: str, old: str, new: str, window: int = 1200) -> str:
    """Rename a field inside one presentInto() call without touching its neighbours."""
    start = text.find(call)
    assert start > 0, f"{call} is gone"
    end = start + window
    block = text[start:end]
    assert old in block, f"{call} no longer passes {old!r}"
    return text[:start] + block.replace(old, new, 1) + text[end:]


def test_removing_the_kept_line_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: _rename_first(
            text, 'presentInto("harnessStreamState"', "context:", "kept_line:"
        )
    )
    assert "surface-no-context" in _checks(payload)


def test_removing_every_action_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: _rename_first(
            text, 'presentInto("debateSynthesisArea"', "actions: [", "offered: ["
        )
    )
    assert "surface-no-actions" in _checks(payload)


def test_an_unstyled_kind_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace(
            '.presentation[data-kind="recovery"]', ".presentation-recovery", 1
        )
    )
    assert "kind-unstyled" in _checks(payload)


def test_a_kind_removed_from_the_list_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace(
            'const PRESENTATION_KINDS = ["empty", "error", "recovery"];',
            'const PRESENTATION_KINDS = ["empty", "error"];',
            1,
        )
    )
    assert "kind-undeclared" in _checks(payload)


def test_a_missing_label_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace('content: "Kept: "', 'content: ""', 1)
    )
    assert "label-missing" in _checks(payload)


def test_an_unwired_container_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace(
            'presentInto("chatState"', 'presentInto("chatStateOld"', 1
        )
    )
    assert "container-unwired" in _checks(payload)


def test_a_raw_writer_in_a_converted_handler_is_caught(markup_copy):
    def regress(text: str) -> str:
        marker = '    clearPresentation("chatState");'
        assert marker in text
        injected = (
            marker
            + '\n    if (!text) document.getElementById("chatState").textContent = "Error: " + "empty";'
        )
        return text.replace(marker, injected, 1)

    payload = markup_copy(regress)
    assert "raw-writer-in-wired-path" in _checks(payload)


def test_an_empty_pane_missing_from_startup_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace("\n  presentHallEmpty();\n", "\n", 1)
    )
    assert "startup-missing" in _checks(payload)
