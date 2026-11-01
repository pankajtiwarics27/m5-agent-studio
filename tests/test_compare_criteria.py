"""Compare criteria and side-by-side results (UIUX-P4-04).

The Compare pane offered checkboxes, a question and a wall of cards. Nothing said what the
comparison was being judged on; a panel that failed raised a toast and left its card saying
"Streaming"; a comparison the reader stopped left the same word on screen; the panels behind a
failure kept saying "Waiting" as if their turn were still coming; and no row said which models
answered.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, drive the fixture for the states the
story is about, and check the captures are the ones the walk wrote.
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
WALK = PROJECT_ROOT / "scripts" / "compare_criteria_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_compare_criteria_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Criteria.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-criteria.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-criteria"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"

CAPTURES = (
    "compare-criteria.png",
    "compare-partial-failure.png",
    "compare-stopped.png",
    "compare-offline.png",
    "compare-light.png",
    "compare-criteria-narrow.png",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_compare_criteria_inventory", INVENTORY)
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
    assert payload["check_count"] == len(payload["checks"]) >= 22
    assert payload["findings"] == []


def test_the_criteria_say_what_settles_them(payload: dict):
    criteria = {entry["key"]: entry for entry in payload["criteria"]}
    assert list(criteria) == ["answered", "finished", "size", "cost", "judged"]
    assert [entry["kind"] for entry in criteria.values()] == [
        "measured",
        "measured",
        "measured",
        "not-stated",
        "judged",
    ]
    assert criteria["cost"]["why"] == "no price is configured for any model, so this pane claims no money"
    assert criteria["finished"]["settled_by"] == "the response's finish reason"


def test_the_statuses_are_the_ten_the_artifact_lists(payload: dict):
    assert payload["statuses"] == [
        "queued",
        "running",
        "answered",
        "cut",
        "demo",
        "empty",
        "failed",
        "stopped",
        "lost",
        "not-run",
    ]
    assert set(payload["status_tones"].values()) <= {"ok", "warn", "danger", "info", "busy", "neutral"}
    assert payload["status_tones"]["stopped"] == "warn"
    assert payload["status_tones"]["failed"] == "danger"
    assert payload["surfaces"]["results"] == "#debateResults"


def test_the_refusals_are_the_point(payload: dict):
    joined = " ".join(payload["refusals"])
    assert "no price is configured for any model" in joined
    assert "theirs to judge" in joined
    assert "not which reply is better" in joined


# ── rules that must keep looking ────────────────────────────────────────────


def test_cost_given_a_figure_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'kind: "not-stated",',
            'kind: "not-stated", price: "$3 per million tokens",',
            1,
        )
    )
    assert "cost is stated, and stated as unsettleable rather than guessed" in _checks(broken)


def test_a_panel_state_read_from_the_event_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "wrote ? \"answered\" : \"empty\";",
            "\"answered\";",
            1,
        )
    )
    assert "a panel's outcome is read from the response, not from an event having arrived" in _checks(broken)


def test_a_stream_that_leaves_a_panel_running_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'closed() { debateEnd("lost", ""); },',
            "closed() { debateEnd(\"stopped\", \"\"); },",
            1,
        )
    )
    assert "no panel keeps saying it is running after the stream is gone" in _checks(broken)


def test_a_results_table_without_the_refusal_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "not which reply is better.",
            "so pick the top one.",
            1,
        )
    )
    assert "the results refuse to score: cost and judgement are named as not settled here" in _checks(broken)


def test_a_pane_that_ranks_answers_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "<strong>What this run left, read against the criteria</strong>",
            "<strong>The best answer this run left</strong>",
            1,
        )
    )
    assert "the pane claims no ranking or quality anywhere in this feature" in _checks(broken)


def test_a_demo_reply_credited_with_answering_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'if (slot.status === "demo") return text.length ? ["size"] : [];',
            'if (slot.status === "demo") return ["answered", "finished", "size"];',
            1,
        )
    )
    assert "a demo reply is credited with a size and nothing else" in _checks(broken)


def test_a_start_button_without_its_reason_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "button.disabled = !ready;",
            "button.disabled = false;",
            1,
        )
    )
    assert "Start is disabled on the same predicate that writes its hint" in _checks(broken)


def test_the_old_status_words_coming_back_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'body = renderMarkdown(slot.c || (slot.status === "queued" ? "_Waiting for its turn..._" : ""));',
            'body = renderMarkdown(slot.c || (slot.status === "queued" ? "Streaming" : ""));',
            1,
        )
    )
    assert "the old status strings are gone from the pane" in _checks(broken)


def test_an_unescaped_value_in_a_card_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'escapeHtml(slot.error || "the provider failed")',
            "(slot.error || \"the provider failed\")",
            1,
        )
    )
    assert "every value the pane draws from a model or an error is escaped" in _checks(broken)


def test_an_invented_route_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "function renderDebateResults() {",
            'function renderDebateResults() {\n    fetch("/api/debate/scores");',
            1,
        )
    )
    assert "the pane posts only the route it already posted, and invents none" in _checks(broken)


def test_a_fixture_that_cannot_be_stopped_is_a_finding(sources):
    broken = sources(
        seed_transform=lambda seed: seed.replace('"--slow-model"', '"--not-a-flag"', 1)
    )
    assert "the fixture can produce a stopped comparison and a cut panel" in _checks(broken)


def test_a_missing_capture_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "MEDIA", tmp_path)
    broken = inventory.build_inventory()
    assert "the captures the doc lists exist and are the pane's own" in _checks(broken)


# ── the fixture can produce the states, and the walk saw them ───────────────


def test_the_fixture_can_stream_slowly_enough_to_be_stopped():
    spec = importlib.util.spec_from_file_location("council_readiness_seed_compare", SEED)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module._RuntimeHandler.slow_word_seconds >= 0.3
    source = SEED.read_text(encoding="utf-8")
    assert "self.slow_word_seconds if (self.slow_model and model == self.slow_model) else 0.004" in source
    assert '"finish_reason": "length" if capped else "stop"' in source


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    source = WALK.read_text(encoding="utf-8")
    assert "This script refuses to report a walk it did not perform." in source
    for token in (
        "2 panels · 1 answered · 1 failed",
        "1 panel · 1 stopped by you",
        "1 panel · 1 cut at the length limit",
        "--slow-model",
        "--cap-model",
        "docs/media/compare-criteria",
    ):
        assert token in source, token
    assert "readCompare" in source and "runCompare" in source


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
            # A clip or element capture at a 1440 px viewport, not a hand-made crop.
            assert 400 < width <= 1440, name
        assert name in readme, name
    assert "nothing here was drawn by hand" in readme.lower() or "Nothing here was drawn by hand" in readme


def test_the_doc_states_the_statuses_and_the_limit_of_the_stream():
    doc = DOC_PATH.read_text(encoding="utf-8")
    for status in ("answered", "cut", "demo", "failed", "stopped", "lost", "not-run"):
        assert f"`{status}`" in doc, status
    assert "no price is configured for any model" in doc
    assert "run_stages" in doc and "one `error`" in doc
    assert "compare_criteria_walk.mjs" in doc


def test_the_ledger_marks_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(item for item in ledger["stories"] if item["id"] == "UIUX-P4-04")
    tracking = story["tracking"]
    assert tracking["status"] == "done"
    assert tracking["evidence"] and tracking["test_results"]
    assert tracking["commit_sha"]
    assert ledger["session_state"]["next_recommended_story"] != "UIUX-P4-04"
