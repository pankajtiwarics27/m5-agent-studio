"""Compare's question-first composition and matching columns (UIV2-16).

The pane put a four-row criteria table above the question, gave the answers whatever columns
`auto-fit` produced for the window, drew one model exactly like a comparison whose second column
never filled, and mixed three different claims - what the run settles, what nobody settles, what
the reader judges - into one table.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, and check the captures are the ones
the walk wrote.
"""

from __future__ import annotations

import importlib.util
import json
import re
import struct
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "compare_balanced_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_compare_balanced_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Balanced.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-balanced.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-balanced"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")
STATES = ("compare-collapsed", "compare-criteria-open", "compare-one-model", "compare-two-models")


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_compare_balanced_inventory", INVENTORY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module():
    return _load_module()


@pytest.fixture(scope="module")
def payload(module):
    return module.build_inventory()


@pytest.fixture
def sources():
    """`build_inventory` over a copy of the frontend, with edits made to the copy."""
    module = _load_module()
    original = FRONTEND.read_text(encoding="utf-8")

    class Sources:
        def findings(self, *replacements):
            edited = original
            for old, new in replacements:
                assert old in edited, f"the source no longer contains {old[:60]!r}"
                edited = edited.replace(old, new, 1)
            FRONTEND.write_text(edited, encoding="utf-8")
            try:
                return {entry["check"] for entry in module.build_inventory()["findings"]}
            finally:
                FRONTEND.write_text(original, encoding="utf-8")

    return Sources()


# ── the artifact ─────────────────────────────────────────────────────────────


def test_the_inventory_matches_the_source(payload):
    assert JSON_PATH.read_text(encoding="utf-8") == json.dumps(payload, indent=2, sort_keys=False) + "\n"
    assert DOC_PATH.read_text(encoding="utf-8") == _load_module().render_doc(payload)


def test_the_inventory_is_clean_and_complete(payload):
    assert payload["findings"] == []
    assert payload["status"] == "clean"
    assert payload["check_count"] == len(payload["checks"]) >= 20
    assert all(entry["ok"] for entry in payload["checks"])


def test_the_doc_states_the_order_and_the_groups(payload):
    doc = DOC_PATH.read_text(encoding="utf-8")
    for row in payload["order"]:
        assert row["id"] in doc
    for group in payload["groups"]:
        assert group["label"] in doc
    assert "never hand-edit" in doc.lower() or "do not hand-edit" in doc.lower()


def test_the_ledger_records_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-16")
    assert story["tracking"]["status"] in {"done", "in_progress"}
    assert story["tracking"]["attempt_count"] >= 1
    assert story["tracking"]["changed_files"]
    assert story["tracking"]["tests_executed"]


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_a_pane_missing_one_of_its_regions_is_a_finding(sources):
    findings = sources.findings(
        ('<div id="debateSummary" class="debate-summary"', '<div id="debateSummaryLegacy" class="debate-summary"'),
    )
    assert "the pane reads question, models, criteria, results, answers - in that order" in findings


def test_a_composer_that_is_not_named_is_a_finding(sources):
    findings = sources.findings(
        ('role="group" aria-label="The comparison to run"', 'aria-label="The comparison to run"'),
    )
    assert "the question and the picks are one composer, and it is the pane's first region" in findings


def test_criteria_open_by_default_are_a_finding(sources):
    findings = sources.findings(
        ('<details id="debateCriteriaDetails" class="debate-criteria-details">',
         '<details id="debateCriteriaDetails" class="debate-criteria-details" open>'),
    )
    assert "the criteria are folded in a details element that starts closed" in findings


def test_a_fold_that_stops_counting_is_a_finding(sources):
    findings = sources.findings(
        ("counts.textContent = counted.join", "void counted.join"),
    )
    assert "the folded summary states how many criteria each group holds" in findings


def test_a_group_losing_its_rail_is_a_finding(sources):
    findings = sources.findings(
        ('.debate-criteria-group[data-kind="judged"] { border-left-color: var(--cyan); }', ""),
    )
    assert "each group carries its own rail, so the three are told apart before they are read" in findings


def test_a_judged_criterion_drawn_as_settled_is_a_finding(sources):
    findings = sources.findings(
        ("no — yours to judge", "yes — settled by the pane"),
    )
    assert "a measured criterion says what settles it, and a judged one says it is the reader's" in findings


def test_a_fold_that_swallows_the_focus_is_a_finding(sources):
    findings = sources.findings(
        ("if (details) details.open = true;", ""),
    )
    assert "adding a criterion opens the panel and keeps the input focused" in findings


def test_a_grid_back_on_auto_fit_is_a_finding(sources):
    findings = sources.findings(
        ("grid-template-columns: repeat(var(--debate-cols, 1), minmax(0, 1fr));",
         "grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));"),
    )
    assert "the grid takes its column count from the run" in findings


def test_one_model_with_no_word_about_it_is_a_finding(sources):
    findings = sources.findings(
        ("is a single answer, not a comparison", "is a comparison"),
    )
    assert "one model is named as a single answer rather than a comparison" in findings


def test_unbounded_card_bodies_are_a_finding(sources):
    findings = sources.findings(
        ("max-height: 46vh; overflow-y: auto;", "max-height: none;"),
    )
    assert "cards in a row are the same height, and their bodies scroll rather than stretch the row" in findings


def test_a_hidden_label_that_draws_again_is_a_finding(sources):
    findings = sources.findings(
        ("  .field-lbl[hidden] { display: none; }", ""),
    )
    assert "a hidden label stays hidden" in findings


def test_an_inspector_left_on_the_old_counts_is_a_finding(sources):
    findings = sources.findings(
        ('            renderInspectorFor("debate");', ""),
    )
    assert "a criterion added or removed re-reads the inspector that counts it" in findings


def test_a_second_debate_call_site_is_a_finding(sources):
    findings = sources.findings(
        ('consumeSSE("/api/debate/stream"', 'consumeSSE("/api/debate/stream", {}); consumeSSE("/api/debate/stream"'),
    )
    assert "one summon call site: the pane still posts only the debate stream" in findings


# ── the walk and its captures ───────────────────────────────────────────────


def test_the_walk_asserts_every_state_the_inventory_claims():
    walk = WALK.read_text(encoding="utf-8")
    module = _load_module()
    for _state, needle in module.WALK_STATES:
        assert needle in walk, needle
    assert "--capture-only" in walk
    assert "council_readiness_seed.py" in walk


def test_the_walk_needs_no_private_environment():
    walk = WALK.read_text(encoding="utf-8")
    assert "STUDIO_SCREENSHOT_CHROME" in walk
    assert "STUDIO_PUPPETEER_CORE" in walk
    assert not re.search(r"/tmp/|/Users/", walk), "a walk that hard-codes a path is not reproducible"


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:33]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("theme", THEMES)
def test_the_captures_exist_at_the_viewports_the_story_requires(viewport, theme):
    width, height = (int(part) for part in viewport.split("x"))
    for state in STATES:
        path = MEDIA / f"{state}-{viewport}-{theme}.png"
        assert path.exists(), path.name
        assert _png_size(path) == (width, height), path.name


@pytest.mark.parametrize("theme", THEMES)
def test_the_narrow_state_is_captured_at_a_phone_width(theme):
    path = MEDIA / f"compare-narrow-390x844-{theme}.png"
    assert path.exists(), path.name
    assert _png_size(path) == (390, 844)


def test_the_before_set_is_the_same_pane_before_the_change():
    # The before set came from `capture_screenshots.mjs`, which keeps the manifest's retina scale,
    # so the files are the CSS viewport at scale 2. What has to hold is the shape, not the scale.
    for viewport in VIEWPORTS:
        for theme in THEMES:
            path = MEDIA / "before-uiv2-16" / f"compare-{viewport}-{theme}.png"
            assert path.exists(), path.name
            width, height = (int(part) for part in viewport.split("x"))
            png_width, png_height = _png_size(path)
            assert png_width in {width, width * 2}, path.name
            assert abs((png_width / png_height) - (width / height)) < 0.01, path.name


def test_the_readme_names_the_producer_and_the_fixture():
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    assert "scripts/compare_balanced_walk.mjs" in readme
    assert "council_readiness_seed.py" in readme
