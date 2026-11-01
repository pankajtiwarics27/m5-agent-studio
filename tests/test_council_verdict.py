"""Council consensus, disagreement and results in the Hall pane (UIUX-P4-03).

The Hall could draw a transcript and, if a seat had been asked for its verdict, a banner whose only
claim was `Pinned Council Conclusion (author)`. It could not say what that decision had to do with
the four replies above it, whether four seats had produced four readings or one reading four times,
or that a session ending with nothing pinned had settled nothing.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, drive the fixture for the replies it
can produce, and check the captures are the ones the walk writes - because a state a fixture cannot
produce is a state no screenshot can show, and a screenshot nobody took is not evidence.
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
WALK = PROJECT_ROOT / "scripts" / "council_verdict_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_council_verdict_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Council_Verdict.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-council-verdict.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "council-verdict"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"

CAPTURES = (
    "council-verdict-unpinned.png",
    "council-verdict-pinned.png",
    "council-verdict-decision.png",
    "council-verdict-narrow.png",
    "council-verdict-light.png",
    "council-verdict-offline.png",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_council_verdict_inventory", INVENTORY)
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


def test_the_decision_is_a_stored_reply_with_an_author_and_a_time(payload: dict):
    decision = payload["decision"]
    assert decision["stored_in"] == ["conclusion_author", "conclusion_text", "conclusion_at"]
    assert decision["is_a_seat_reply"] is True
    assert "pin_as_conclusion" in " ".join(decision["writers"])
    assert decision["matched_on"] == "the author the server recorded and the words it kept"


def test_the_measure_is_named_and_sized(payload: dict):
    measure = payload["measure"]
    assert measure["name"] == "shared wording"
    assert measure["n"] == 4
    assert measure["thresholds"] == {"same": 0.85, "mostly": 0.70, "partly": 0.25}
    assert measure["claims"] == "text, not intent"
    assert "single words" in measure["short_text"]


def test_the_readings_are_the_seven_the_artifact_lists(payload: dict):
    assert payload["readings"] == [
        "is the decision",
        "mostly in the decision",
        "partly in the decision",
        "not in the decision",
        "the same words as",
        "echoes",
        "its own words",
    ]
    assert payload["columns"] == ["Seat", "Model", "Size", "Shared wording", "Reading"]
    assert payload["surfaces"] == {
        "transcript": "#hallStream",
        "decision": "#hallConclusionBanner",
        "verdict": "#hallVerdict",
    }


# ── rules that must keep looking ────────────────────────────────────────────


def test_a_decision_credited_by_author_alone_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'reply.author === author && reply.content.trim() === text.trim()',
            "reply.author === author",
            1,
        )
    )
    assert "the decision's provenance is a reply the server recorded, matched on author and words" in _checks(broken)


def test_a_decision_measured_against_itself_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'return { reply, share: 1, against: "", reading: "decision", text: "is the decision" };',
            'return { reply, share: 1, against: "", reading: "mostly", text: "mostly in the decision" };',
            1,
        )
    )
    assert "every other reply is measured against the decision, and the pinned seat is not" in _checks(broken)


def test_shared_wording_called_agreement_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "Replies are never merged here.",
            "The seats agreed.",
            1,
        )
    )
    assert "the pane never calls shared wording agreement or consensus" in _checks(broken)


def test_a_verdict_that_rewrites_the_transcript_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "box.dataset.signature = signature;",
            'box.dataset.signature = signature;\n    document.getElementById("hallStream").textContent = "";',
            1,
        )
    )
    assert "dissent is named, counted, and left where it is" in _checks(broken)


def test_a_block_redrawn_on_every_token_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "if (box.dataset.signature === signature) return;",
            "if (false) return;",
            1,
        )
    )
    assert "the block is not rewritten while nothing in it has moved" in _checks(broken)


def test_a_pane_that_counts_replies_instead_of_voices_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'label: role ? "Seat " + role.seat + " · " + role.label : (m.author || "A voice"),',
            'label: role ? "Seat " + role.seat + " · " + role.label : (m.author || "A voice"), voices: 1,',
            1,
        ).replace("return { session: activeHallData || {}, replies, decision, groups, rows, dissent, repeated, voices: groups.length, state };", "return { session: activeHallData || {}, replies, decision, groups, rows, dissent, repeated, voices: replies.length, state };", 1)
    )
    assert "the pane counts voices, not replies" in _checks(broken)


def test_a_reply_whose_seat_answered_twice_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'reply.display = reply.label + (reply.ofSeat > 1 ? " (reply " + reply.occurrence + ")" : "");',
            "reply.display = reply.label;",
            1,
        )
    )
    assert "a seat that answered more than once is told apart" in _checks(broken)


def test_a_replaced_conclusion_banner_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "Pinned Council Conclusion (${escapeHtml(s.conclusion_author",
            "Conclusion (${escapeHtml(s.conclusion_author",
            1,
        )
    )
    assert "the pinned conclusion banner still draws the pinned text" in _checks(broken)


def test_an_unescaped_value_in_the_table_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '\'<td class="mono">\' + share + "</td>"',
            "'<td class=\"mono\">${share}</td>'",
            1,
        )
    )
    assert "every value the verdict draws is escaped" in _checks(broken)


def test_a_fixture_that_answers_every_model_the_same_way_is_a_finding(sources):
    broken = sources(
        seed_transform=lambda seed: seed.replace(
            'words = _reply_words(model).split(" ")',
            'words = f"reply from {model}".split(" ")',
            1,
        )
    )
    assert (
        "the fixture answers each catalogue model with its own paragraph, and a stranger with a line"
        in _checks(broken)
    )


def test_a_missing_capture_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "MEDIA", tmp_path)
    broken = inventory.build_inventory()
    assert "the captures the doc lists exist and are the pane's own" in _checks(broken)


# ── the fixture can produce the states, and the walk saw them ───────────────


def test_the_fixture_answers_each_catalogue_model_with_its_own_paragraph():
    spec = importlib.util.spec_from_file_location("council_readiness_seed_verdict", SEED)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    replies = [
        module._reply_words(model)
        for model in (module.LOADED_MODEL, module.OVER_BUDGET_MODEL, module.FITS_MODEL)
    ]
    assert len(set(replies)) == 3, "three catalogue models must be able to say three different things"
    assert all(len(reply) > 120 for reply in replies)
    # A model the fixture does not know still gets an answer, and it names itself.
    stranger = module._reply_words("some-other-model")
    assert "some-other-model" in stranger


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    source = WALK.read_text(encoding="utf-8")
    assert "This script refuses to report a walk it did not perform." in source
    for token in (
        "M5CouncilVerdict.markdown()",
        "no decision pinned",
        "Dissent:",
        "One voice, not 4:",
        "hits a 480 px viewport",
        "docs/media/council-verdict",
    ):
        assert token in source or token.replace("hits", "").strip() in source, token
    assert "readVerdict" in source and "startSession" in source and "conclude" in source


def test_the_run_is_measured_not_guessed():
    """The measure the pane draws is the one described, at the size described."""
    html = FRONTEND.read_text(encoding="utf-8")
    assert "const VERDICT_SHINGLE_SIZE = 4;" in html
    assert "const VERDICT_SAME = 0.85;" in html
    assert "const VERDICT_MOSTLY = 0.70;" in html
    assert "const VERDICT_PARTLY = 0.25;" in html
    assert "words.length >= VERDICT_SHINGLE_SIZE ? VERDICT_SHINGLE_SIZE : 1" in html
    assert "shared / left.size" in html


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
        elif "offline" in name:
            # The whole pane, at the width the app draws it.
            assert 400 < width <= 1440, name
        else:
            # An element capture of the block at a 1440 px viewport, not a hand-made crop.
            assert 400 < width <= 1440, name
        assert name in readme, name
    assert "nothing here was drawn by hand" in readme.lower() or "Nothing here was drawn by hand" in readme


def test_the_doc_states_the_measure_and_what_it_refuses_to_invent():
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "4-word sequences" in doc
    assert "text measure" in doc
    for refusal in ("**consensus**", "**agreement**", "**a synthesis**", "**a summary**"):
        assert refusal in doc, refusal
    assert "council_verdict_walk.mjs" in doc
    assert "council-verdict-offline.png" in doc


def test_the_ledger_marks_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(item for item in ledger["stories"] if item["id"] == "UIUX-P4-03")
    tracking = story["tracking"]
    assert tracking["status"] == "done"
    assert tracking["evidence"] and tracking["test_results"]
    assert tracking["commit_sha"]
    assert ledger["session_state"]["next_recommended_story"] != "UIUX-P4-03"
