"""Compare answers for its results, its verdict and its evidence rail (UIV2-17).

The pane drew two tables and a rail. A panel the provider failed on was given a size of `0 ch` - a
zero standing in for a reply that does not exist. The arbiter card named a model and said nothing
about what it judged, that the replies were its evidence, or where its own figures came from. And
the rail counted criteria and then pointed at the pane for the evidence, against a target that
calls the inspector the home of it.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, and check that the captures are the
ones the walk wrote.
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
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "compare_evidence_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_compare_evidence_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Evidence.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-evidence.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-evidence"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")
#: The states the ledger's visual matrix names, in the names the captures use.
STATES = ("empty", "populated", "verdict", "error", "loading", "stopped", "long-content")


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_compare_evidence_inventory", INVENTORY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
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
    """`build_inventory` over a copy of the sources, with edits made to the copies."""
    module = _load_module()
    originals = {
        FRONTEND: FRONTEND.read_text(encoding="utf-8"),
        SEED: SEED.read_text(encoding="utf-8"),
        WALK: WALK.read_text(encoding="utf-8"),
    }

    class Sources:
        def findings(self, *replacements):
            for path, original in originals.items():
                path.write_text(original, encoding="utf-8")
            for path, old, new in replacements:
                edited = path.read_text(encoding="utf-8")
                assert old in edited, f"{path.name} no longer contains {old[:70]!r}"
                path.write_text(edited.replace(old, new, 1), encoding="utf-8")
            try:
                return {entry["check"] for entry in module.build_inventory()["findings"]}
            finally:
                for path, original in originals.items():
                    path.write_text(original, encoding="utf-8")

    return Sources()


# ── the artifact ─────────────────────────────────────────────────────────────


def test_the_inventory_matches_the_source(payload):
    assert JSON_PATH.read_text(encoding="utf-8") == _load_module().dumps(payload)
    assert DOC_PATH.read_text(encoding="utf-8") == _load_module().render_doc(payload)


def test_the_inventory_is_clean_and_complete(payload):
    assert payload["findings"] == []
    assert payload["status"] == "clean"
    assert payload["check_count"] == len(payload["checks"]) >= 25
    assert all(entry["ok"] for entry in payload["checks"])


def test_the_doc_states_the_columns_and_the_states(payload):
    doc = DOC_PATH.read_text(encoding="utf-8")
    for column in payload["columns"]["results"] + payload["columns"]["provenance"]:
        assert column in doc
    for row in payload["states"]:
        assert row["state"] in doc
    assert "do not hand-edit" in doc


def test_the_ledger_records_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-17")
    assert story["tracking"]["status"] in {"done", "in_progress"}
    assert story["tracking"]["attempt_count"] >= 1
    assert story["tracking"]["changed_files"]
    assert story["tracking"]["tests_executed"]


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_a_zero_for_a_missing_reply_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         'const size = text.length ? String(text.length) + " ch" : "—";',
         'const size = String(String(slot.c || "").length) + " ch";'),
    )
    assert "a reply with no text has no size: the results table draws a dash, never 0 ch" in findings


def test_an_unlabelled_cell_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '</td><td data-label="Criteria it met">', "</td><td>"),
    )
    assert "every cell of the results table carries the label its column head gives it" in findings


def test_a_table_that_does_not_stack_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         ".debate-results tbody td, .debate-provenance tbody td {",
         ".debate-results tbody td {"),
    )
    assert "below a phone width both tables stack instead of scrolling sideways" in findings


def test_a_verdict_without_its_role_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'class="stage-output-card debate-verdict" data-role="verdict"', 'class="stage-output-card"'),
    )
    assert "the arbiter's answer is drawn as a verdict, and the card says so in its markup" in findings


def test_a_verdict_that_does_not_name_its_evidence_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "The replies above are its evidence.", "The replies are above."),
    )
    assert "the verdict says the replies are its evidence" in findings


def test_a_demo_verdict_with_figures_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "The server wrote this verdict, so it carries no model figures.",
         "The server wrote this verdict."),
    )
    assert "a demo verdict says the server wrote it, rather than carrying figures it never had" in findings


def test_a_rail_with_its_own_arithmetic_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "const reading = debateProvenanceOf(slot);", 'const reading = { elapsed: "n/a" };'),
    )
    assert "the rail's Evaluation view reads the same usage the pane's table reads" in findings


def test_a_rail_that_drops_the_reason_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'const why = reading.note\n          ? reading.note + (slot.error ? " — " + slot.error : "")',
         'const why = reading.note\n          ? "" + (slot.error ? " — " + slot.error : "")'),
    )
    assert "a reply with no figures is given the reason in the rail, including the panel's own error" in findings


def test_a_rail_that_cannot_reach_the_rows_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "function focusDebateProvenance(button) {", "function focusDebateProvenance(unused) { return; }"),
    )
    assert "the rail can reach the rows it summarises" in findings


def test_a_rail_that_points_at_the_pane_again_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "No comparison yet. Run two models on one question",
         "Scores and the arbiter verdict render in the Compare pane. Run two models on one question"),
    )
    assert "with no run the rail says so instead of counting nothing" in findings


def test_a_rail_that_only_redraws_on_a_tab_switch_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'if (inspectorView === "evaluation") refreshInspectorPanel();', "void 0;"),
    )
    assert "the rail redraws with the run, not only when the tab changes" in findings


def test_a_dropped_provenance_column_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '<th scope="col">First token</th>', ""),
    )
    assert "the provenance table still has its six columns" in findings


def test_a_dropped_result_column_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '<th scope="col">Criteria it met</th>', ""),
    )
    assert "the results table still has its four columns" in findings


def test_a_second_debate_call_site_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'consumeSSE("/api/debate/stream"', 'consumeSSE("/api/debate/stream", {}); consumeSSE("/api/debate/stream"'),
    )
    assert "one summon call site: the pane still posts only the debate stream" in findings


def test_a_fixture_without_a_long_reply_is_a_finding(sources):
    findings = sources.findings(
        (SEED, '"--long-model",', '"--no-such-model",'),
    )
    assert "the fixture can answer at length, so the long-content state is real" in findings


def test_a_walk_that_stops_asserting_a_state_is_a_finding(sources):
    findings = sources.findings(
        (WALK, "a panel with no reply shows a dash, never 0 ch", "a panel with no reply shows a dash"),
    )
    assert "the walk asserts every state the captures show" in findings


def test_a_walk_that_hard_codes_a_path_is_a_finding(sources):
    findings = sources.findings(
        (WALK, 'process.env.STUDIO_PYTHON || "python3"', 'process.env.STUDIO_PYTHON || "/tmp/python"'),
    )
    assert "the walk needs no environment of its own" in findings


# ── the captures ────────────────────────────────────────────────────────────


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:33]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("theme", THEMES)
def test_the_captures_exist_for_every_state_and_viewport(viewport, theme):
    width, height = (int(part) for part in viewport.split("x"))
    for state in STATES:
        path = MEDIA / f"evidence-{state}-{viewport}-{theme}.png"
        assert path.exists(), path.name
        assert _png_size(path) == (width, height), path.name


@pytest.mark.parametrize("theme", THEMES)
def test_the_narrow_state_is_captured_at_a_phone_width(theme):
    path = MEDIA / f"evidence-narrow-390x844-{theme}.png"
    assert path.exists(), path.name
    assert _png_size(path) == (390, 844)


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("theme", THEMES)
def test_the_rail_is_captured_on_its_own(viewport, theme):
    width, height = (int(part) for part in viewport.split("x"))
    path = MEDIA / f"evidence-inspector-{viewport}-{theme}.png"
    assert path.exists(), path.name
    assert _png_size(path) == (width, height), path.name


def test_the_before_set_is_the_pane_from_main():
    before = MEDIA / "before-uiv2-17"
    assert before.is_dir()
    populated = sorted(before.glob("*/*evidence-populated-*.png"))
    assert len(populated) >= 8, len(populated)
    for path in populated:
        assert _png_size(path)[0] >= 390


def test_the_readme_names_the_producer_and_the_fixture():
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    assert "scripts/compare_evidence_walk.mjs" in readme
    assert "council_readiness_seed.py" in readme
    assert "--long-model" in readme


def test_the_walk_refuses_a_failed_transport_as_a_comparison():
    walk = WALK.read_text(encoding="utf-8")
    assert "1 answered" in walk or "answered" in walk
    assert "data-evidence-status" in FRONTEND.read_text(encoding="utf-8")


def test_the_walk_needs_no_private_environment():
    walk = WALK.read_text(encoding="utf-8")
    assert "STUDIO_SCREENSHOT_CHROME" in walk
    assert "STUDIO_PUPPETEER_CORE" in walk
    assert not re.search(r"/tmp/|/Users/", walk), "a walk that hard-codes a path is not reproducible"
