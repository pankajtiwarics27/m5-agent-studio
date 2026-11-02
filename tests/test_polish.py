"""UIUX-P5-05: the polish layer must be a rule, not a mood.

The story measured a rendered page - `scripts/polish_walk.mjs` takes focus through CDP,
moves a real pointer, holds the button down for `:active`, blocks clicks so measuring
changes nothing, walks both themes and captures eight states. Those measurements say what
the page did on the day it ran; they cannot stop the next edit from typing `9px` into a
corner or deleting the `:active` rule that answers a press.

These tests are the half that does. Every rule the inventory asserts is proved here by
breaking a *temporary copy* of the stylesheet, so the tests exercise the real parser rather
than a description of it, and the anchors are asserted to exist - a rule that quietly
stopped matching would otherwise turn its test into a test of nothing.

The tests do not claim the page looks right. That claim belongs to the walk's summary and
to the captures it wrote, both of which are evidence of a state, not of a taste.
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
WALK = PROJECT_ROOT / "scripts" / "polish_walk.mjs"
GENERATOR = PROJECT_ROOT / "scripts" / "uiux_polish_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Polish.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-polish.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "polish"


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_polish_inventory", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def payload() -> dict:
    return _load_generator().build_inventory()


def _patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replacements: list[tuple[str, str]]):
    """Point the inventory at a copy of the stylesheet with the anchors changed.

    Each caller passes the exact text it changes and the substitution is asserted to have
    happened exactly once, so a later edit to `index.html` cannot silently disarm a test.
    """
    source = FRONTEND.read_text(encoding="utf-8")
    changed = source
    for old, new in replacements:
        assert source.count(old) == 1, f"the stylesheet no longer contains the anchor {old[:70]!r}"
        changed = changed.replace(old, new, 1)
    copy = tmp_path / "index.html"
    copy.write_text(changed, encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "FRONTEND", copy)
    return module


def _checks(module) -> list[str]:
    return sorted({finding["check"] for finding in module.build_inventory()["findings"]})


def test_the_committed_artifacts_are_the_generators_output() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_layer_holds_no_findings(payload: dict) -> None:
    assert payload["findings"] == []
    assert payload["totals"]["findings"] == 0


def test_every_corner_and_duration_reads_a_token(payload: dict) -> None:
    assert payload["totals"]["radius_rules"] > 50, "the stylesheet stopped being parsed"
    assert payload["totals"]["motion_rules"] > 10
    assert payload["tokens"]["radius"], payload["tokens"]
    assert payload["tokens"]["press"] == ["--bg-press"]


def test_the_captures_are_the_ones_the_acceptance_criteria_ask_for(payload: dict) -> None:
    captures = payload["captures"]
    assert len(captures) == 8, [capture["file"] for capture in captures]
    themes = {capture["theme"] for capture in captures}
    sizes = {(capture["width"], capture["height"]) for capture in captures}
    assert themes == {"dark", "light"}
    assert sizes == {(1440, 900), (360, 740)}
    for capture in captures:
        assert capture["bytes"] > 10_000, capture["file"]
        assert (PROJECT_ROOT / capture["file"]).is_file()
    states = {capture["file"].split("-", 2)[-1] for capture in captures}
    assert states == {"hover-primary.png", "rest-chat.png", "rest-start.png", "narrow-start.png"}


def test_a_hand_typed_corner_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [(".m-card { border-radius: var(--radius-md); padding: var(--card-pad); }", ".m-card { border-radius: 9px; padding: var(--card-pad); }")],
    )
    assert "radius-token" in _checks(module)


def test_a_hand_typed_duration_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [("animation: toastIn var(--dur-fast) var(--ease-out);", "animation: toastIn 200ms var(--ease-out);")],
    )
    assert "motion-token" in _checks(module)


def test_animating_every_property_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [("  transform: translateY(1px);", "  transition: all 200ms;")],
    )
    assert "no-transition-all" in _checks(module)


def test_a_press_that_paints_nothing_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [("  background-color: var(--bg-press);", "  background-color: var(--bg-hover);")],
    )
    assert "press-token" in _checks(module)


def test_a_pressable_kind_with_no_press_rule_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(monkeypatch, tmp_path, [("summary,\n    select):active:", "select):active:")])
    findings = module.build_inventory()["findings"]
    assert [finding["detail"] for finding in findings if finding["check"] == "press-coverage"] == [
        "no `:active` rule answers `summary`"
    ]


def test_a_hover_that_only_moves_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [(".evidence-copy:hover { color: var(--text); }", ".evidence-copy:hover { transform: translateX(1px); }")],
    )
    assert "hover-not-movement" in _checks(module)


def test_a_field_with_no_hover_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [
            (
                ':is(input, textarea):hover:not(:disabled) {\n  border-color: var(--border-hi);\n}',
                ':is(input, textarea):hover-not(:disabled) {\n  border-color: var(--border-hi);\n}',
            ),
            (
                'select:hover, input[type="text"]:hover, input[type="password"]:hover, textarea:hover',
                'select:hover, input[type="text"]:hover-not, input[type="password"]:hover-not, textarea:hover-not',
            ),
        ],
    )
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert "no `:hover` rule answers `input`" in details
    assert "no `:hover` rule answers `textarea`" in details


def test_a_primary_action_whose_hover_changes_nothing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [
            (
                "background: var(--accent-hi); border-color: var(--accent-hi);",
                "background: var(--accent); border-color: var(--accent);",
            )
        ],
    )
    assert "primary-hover" in _checks(module)


def test_a_ring_removed_with_no_box_to_carry_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [(".prompt-box:focus-within { outline: 2px solid var(--ring); outline-offset: 2px; }", ".prompt-box:focus-within { outline: none; }")],
    )
    assert "ring-removal" in _checks(module)


def test_a_disabled_field_that_keeps_the_pointer_cursor_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [("  cursor: not-allowed;\n  opacity: .55;", "  cursor: default;\n  opacity: .55;")],
    )
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert "no `:disabled` rule covering `input` says `cursor: not-allowed`" in details


def test_a_disabled_control_faded_past_reading_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [("  cursor: not-allowed;\n  opacity: .55;", "  cursor: not-allowed;\n  opacity: .18;")],
    )
    assert "disabled-honest" in _checks(module)


def test_reduced_motion_must_still_neutralise_every_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        [
            (
                "animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; scroll-behavior: auto !important;",
                "animation-duration: 0.01ms !important; scroll-behavior: auto !important;",
            ),
            (
                "animation-duration: .01ms !important; transition-duration: .01ms !important; scroll-behavior: auto !important;",
                "animation-duration: .01ms !important; scroll-behavior: auto !important;",
            ),
        ],
    )
    assert "reduced-motion" in _checks(module)


def test_a_walk_that_stops_measuring_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = WALK.read_text(encoding="utf-8")
    assert "forcePseudoState" in source
    copy = tmp_path / "polish_walk.mjs"
    copy.write_text(source.replace("forcePseudoState", "forceUnused"), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "WALK", copy)
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert "`scripts/polish_walk.mjs` no longer measures forced focus" in details


def test_the_page_names_its_own_limits(payload: dict) -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "never hand-edit this page" in doc
    assert "## What this page is, and is not" in doc
    assert "## What this does not prove" in doc
    assert "only `scripts/polish_walk.mjs` renders a page" in doc
    assert payload["captures"], "the page would list no captures at all"
    assert json.loads(JSON_PATH.read_text(encoding="utf-8"))["story"] == "UIUX-P5-05"


def test_the_program_page_carries_the_story() -> None:
    program = (PROJECT_ROOT / "docs" / "uiux-2-0-program.md").read_text(encoding="utf-8")
    assert "uiux-2-0-polish.md" in program
    assert "UIUX-P5-05" in program
