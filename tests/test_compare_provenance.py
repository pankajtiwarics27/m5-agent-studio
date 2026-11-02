"""Compare measured performance and provenance (UIUX-P4-05).

The pane printed one number — the arbiter's throughput, in a green badge, with nothing saying where
it came from — and a demo verdict carried 29.5 or 42.1 tok/s that no model ever ran for. Latency,
tokens and cost per model were absent, so a side-by-side comparison compared prose.

These tests read the inventory, break each rule in a copy of the source so a rule that stopped
looking fails here rather than in production, check the fixture can produce the states the story is
about, and check the captures are the ones the walk wrote.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "compare_provenance_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_compare_provenance_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Provenance.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-provenance.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-provenance"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"

CAPTURES = (
    "compare-provenance.png",
    "compare-provenance-arbiter.png",
    "compare-provenance-demo.png",
    "compare-provenance-light.png",
    "compare-provenance-narrow.png",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_compare_provenance_inventory", INVENTORY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the inventory at copies, so a rule can be broken in one of them."""

    def _make(html_transform=None, app_transform=None, seed_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        seed = SEED.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, "the transform did not change the server source, so the test proves nothing"
            app = changed
        if seed_transform:
            changed = seed_transform(seed)
            assert changed != seed, "the transform did not change the fixture, so the test proves nothing"
            seed = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        seed_path = tmp_path / "council_readiness_seed.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        seed_path.write_text(seed, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        monkeypatch.setattr(inventory, "SEED", seed_path)
        return inventory.build_inventory()

    return _make


def _checks(broken: dict) -> set[str]:
    return {finding["check"] for finding in broken["findings"]}


# ── the artifact itself ──────────────────────────────────────────────────────


def test_the_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, str(INVENTORY), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_the_inventory_is_clean_and_complete(payload: dict):
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] == len(payload["checks"]) >= 16
    assert payload["findings"] == []


def test_the_fields_are_the_payload_s_and_the_columns_are_the_table_s(payload: dict):
    assert [entry["field"] for entry in payload["fields"]] == [
        "elapsed_s",
        "ttft",
        "completion_tokens",
        "decode_tps",
    ]
    assert payload["columns"] == ["Model", "Elapsed", "First token", "Reply tokens", "Decode", "Cost"]
    assert payload["stream_events"] == ["panel_done", "synthesis_done", "debate_done"]
    assert payload["surface"] == "#debateProvenance"


def test_the_refusals_are_the_point(payload: dict):
    joined = " ".join(payload["refusals"])
    assert "no price is configured for any model" in joined
    assert "it carries no measured figure" in joined
    assert "this page times nothing" in joined


# ── rules that must keep looking ────────────────────────────────────────────


def test_a_client_clock_in_the_pane_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "  function renderDebateProvenance() {",
            "  function renderDebateProvenance() {\n    const startedAt = performance.now();",
            1,
        )
    )
    assert "the pane prints no figure the browser timed" in _checks(broken)


def test_a_zero_instead_of_a_dash_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "if (!Number.isFinite(number) || number <= 0) return null;",
            "if (!Number.isFinite(number)) return null;",
            1,
        )
    )
    assert "a figure the response did not carry is a dash, never a zero" in _checks(broken)


def test_crediting_a_demo_reply_with_figures_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'if (debateIsDemo(usage) || state === "demo") return { note: debateNoFigures("demo") };',
            'if (false) return { note: debateNoFigures("demo") };',
            1,
        )
    )
    assert "a demo reply is given no figure, and the demo path's numbers appear nowhere" in _checks(broken)


def test_printing_the_demo_paths_own_numbers_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '    if (debateIsDemo(usage) || state === "demo") return { note: debateNoFigures("demo") };',
            '    if (state === "demo") return { note: "", decode: "29.5 tok/s" };\n'
            '    if (debateIsDemo(usage)) return { note: debateNoFigures("demo") };',
            1,
        )
    )
    assert "a demo reply is given no figure, and the demo path's numbers appear nowhere" in _checks(broken)


def test_a_field_the_server_does_not_send_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'elapsed: debateFigure(usage.elapsed_s, " s"),',
            'elapsed: debateFigure(usage.elapsed, " s"),',
            1,
        )
    )
    assert "a figure is a figure the response carried: every field printed is one the app yields" in _checks(broken)


def test_a_run_total_from_the_client_clock_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "debate_done(d) { debateRunElapsed = d.total_elapsed_s; renderDebate(); },",
            "debate_done(d) { debateRunElapsed = (Date.now() - 0) / 1000; renderDebate(); },",
            1,
        )
    )
    found = _checks(broken)
    assert "the run's total elapsed time comes from the debate_done event, not from a timer" in found
    assert "the pane prints no figure the browser timed" in found


def test_the_old_unqualified_badge_coming_back_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "${debateArbiterBadge(debateSynth.u)}",
            '<span class="badge">${debateSynth.u?debateSynth.u.decode_tps+\' tok/s\':\'Synthesizing...\'}</span>',
            1,
        )
    )
    assert "the arbiter's card states its figures from its own usage, and never an unqualified tok/s" in _checks(broken)


def test_a_price_in_the_cost_column_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "'<td class=\"mono\" title=\"No price is configured for any model\">—</td></tr>'",
            "'<td class=\"mono\">$0.14</td></tr>'",
            1,
        )
    )
    assert "cost is stated as not known, with the reason, and per row as a dash" in _checks(broken)


def test_a_missing_explanation_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '      failed: "the provider failed before it reported any figure",',
            "",
            1,
        )
    )
    assert "a stage that never reported says why, in its own terms" in _checks(broken)


def test_a_fixture_without_a_second_resident_model_is_a_finding(sources):
    broken = sources(
        seed_transform=lambda seed: seed.replace('"--resident-model"', '"--not-a-flag"', 1)
    )
    assert "the fixture can produce two answering panels, a stopped run and a demo reply" in _checks(broken)


def test_a_missing_capture_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "MEDIA", tmp_path)
    broken = inventory.build_inventory()
    assert "the captures the doc lists exist and were written by the walk" in _checks(broken)


# ── the fixture and the walk ────────────────────────────────────────────────


def test_the_fixture_reports_a_second_resident_model_only_when_asked():
    spec = importlib.util.spec_from_file_location("council_readiness_seed_provenance", SEED)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    server, state, url = module.start_runtime(resident_model=module.FITS_MODEL)
    try:
        loaded = [mid for mid, meta in state.snapshot().items() if meta["loaded"]]
        assert loaded == [module.LOADED_MODEL, module.FITS_MODEL]
        assert state.used_gb() <= module.CEILING_GB, "two resident models still fit the fixture's ceiling"
        assert url.startswith("http://127.0.0.1:")
    finally:
        server.shutdown()


def test_the_default_fixture_still_has_one_resident_model():
    spec = importlib.util.spec_from_file_location("council_readiness_seed_provenance_defaults", SEED)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    server, state, _url = module.start_runtime()
    try:
        assert [mid for mid, meta in state.snapshot().items() if meta["loaded"]] == [module.LOADED_MODEL]
    finally:
        server.shutdown()


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    source = WALK.read_text(encoding="utf-8")
    assert "This script refuses to report a walk it did not perform." in source
    for token in (
        "__debateFrames",
        "response.clone()",
        "panel_done",
        "synthesis_done",
        "debate_done",
        "--resident-model",
        "docs/media/compare-provenance",
    ):
        assert token in source, token
    assert "every figure on screen is the figure the response carried for that reply" in source


# ── the evidence this story ships ───────────────────────────────────────────


def test_the_captures_exist_and_are_the_widths_the_readme_claims():
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    for name in CAPTURES:
        path = MEDIA / name
        assert path.exists(), name
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width = struct.unpack(">I", data[16:20])[0]
        if "narrow" in name:
            assert width == 480, name
        else:
            assert 400 < width <= 1440, name
        assert name in readme, name
    assert "Nothing here was drawn by hand" in readme


def test_the_doc_states_the_fields_and_the_refusals():
    doc = DOC_PATH.read_text(encoding="utf-8")
    for field in ("elapsed_s", "ttft", "completion_tokens", "decode_tps", "total_elapsed_s"):
        assert field in doc, field
    assert "no price is configured for any model" in doc
    assert "29.5 / 42.1" in doc
    assert "compare_provenance_walk.mjs" in doc


def test_the_ledger_marks_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(item for item in ledger["stories"] if item["id"] == "UIUX-P4-05")
    tracking = story["tracking"]
    assert tracking["status"] == "done"
    assert tracking["evidence"] and tracking["test_results"]
    assert tracking["commit_sha"]
    assert ledger["session_state"]["next_recommended_story"] != "UIUX-P4-05"
