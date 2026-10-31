"""UIUX-P1-03: status and evidence must be closed vocabularies.

The shell had six spellings of "what state is this in" and the redesign layer
flattened every `.badge` variant onto the same two tokens, so `.badge.green` and
`.badge.red` painted identically - the meaning survived only in the words, and
nothing stopped a seventh spelling appearing. These tests pin the replacement:

* the committed artifacts are the generator's own output, and `--check` agrees
  with the tree;
* every tone and every basis is declared, used and drawn from tokens - proved by
  breaking a *temporary copy* of the stylesheet, so the real parser is exercised;
* the ten harness states keep their words and gain an honest tone (`failed` is
  danger now, not the amber it shared with `paused`);
* no surface still writes the old way, `className = "badge " + ...`, which is how
  the label and the tone drift apart;
* the floors differ because the tone is redundant: text 4.5:1, the dot 3:1.

The tests do not assert that a chip looks right; that is `scripts/status_walk.mjs`,
which draws every tone and state in a real browser.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "product" / "M5_2_0_UIUX_Status_Components.json"
DOC = ROOT / "docs" / "uiux-2-0-status-and-evidence.md"
GENERATOR = ROOT / "scripts" / "uiux_status_components.py"
WALK = ROOT / "scripts" / "status_walk.mjs"
FRONTEND = ROOT / "static" / "index.html"

TONES = ("ok", "warn", "danger", "info", "busy", "neutral")
BASES = ("measured", "derived", "unverified", "unavailable")

EXPECTED_STATES = {
    "waiting_approval": ("warn", "WAITING APPROVAL"),
    "running": ("busy", "WORKFLOW RUNNING"),
    "paused": ("warn", "PAUSED"),
    "blocked": ("danger", "BLOCKED"),
    "pending": ("info", "READY"),
    "idle": ("info", "READY"),
    "completed": ("ok", "COMPLETED"),
    "cancelled": ("neutral", "CANCELLED"),
    "failed": ("danger", "FAILED"),
    "error": ("danger", "ERROR"),
}


def _payload() -> dict:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_status_components", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *replacements: tuple[str, str]):
    """Point the generator at a modified copy of the stylesheet.

    Each caller passes the exact text it changes and the substitution is asserted,
    so a later edit to index.html cannot quietly turn a "this is refused" test into
    a test of nothing.
    """
    source = FRONTEND.read_text(encoding="utf-8")
    for old, new in replacements:
        assert source.count(old) == 1, f"the stylesheet no longer contains {old[:60]!r}"
        source = source.replace(old, new)
    copy = tmp_path / "index.html"
    copy.write_text(source, encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "FRONTEND", copy)
    return module


def test_the_committed_artifacts_are_the_generators_output() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 findings" in result.stdout


def test_the_generator_reports_no_findings_on_the_tree() -> None:
    assert _load_generator().build_components()["findings"] == []


def test_the_vocabulary_is_closed_and_each_word_means_something() -> None:
    payload = _payload()
    assert payload["vocabulary"]["tones"] == list(TONES)
    assert payload["vocabulary"]["bases"] == list(BASES)
    assert set(payload["vocabulary"]["tone_meaning"]) == set(TONES)
    meanings = payload["vocabulary"]["tone_meaning"]
    assert all(meaning.strip() for meaning in meanings.values())
    assert len(set(meanings.values())) == len(TONES), "two tones mean the same thing"


def test_every_state_keeps_its_words_and_gains_an_honest_tone() -> None:
    payload = _payload()
    seen = {row["state"]: (row["tone"], row["label"]) for row in payload["state_matrix"]}
    assert seen == EXPECTED_STATES
    # The correction this story made: failure used to share amber with paused.
    assert seen["failed"][0] == "danger"
    assert seen["error"][0] == "danger"
    assert seen["blocked"][0] == "danger"
    assert seen["paused"][0] != seen["failed"][0]


def test_a_state_mapped_to_a_made_up_tone_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('      failed: ["danger", "FAILED"],', '      failed: ["red", "FAILED"],'),
    )
    findings = module.build_components()["findings"]
    assert any(finding["check"] == "tone-undeclared" for finding in findings), findings


def test_a_tone_written_with_a_raw_colour_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('.status[data-tone="ok"]::before { background: var(--emerald); }',
         '.status[data-tone="ok"]::before { background: #22c55e; }'),
    )
    findings = module.build_components()["findings"]
    assert any(finding["check"] == "tone-literal" for finding in findings), findings


def test_a_tone_drawn_without_its_dot_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('.status[data-tone="warn"]::before { background: var(--amber); }', ""),
    )
    findings = module.build_components()["findings"]
    assert any(finding["check"] == "tone-incomplete" for finding in findings), findings


def test_a_surface_that_keeps_its_badge_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('<span id="hallStatusBadge" class="status" data-tone="warn" role="status">IDLE</span>',
         '<span id="hallStatusBadge" class="badge amber">IDLE</span>'),
    )
    findings = module.build_components()["findings"]
    assert any(finding["check"] == "surface-unconverted" for finding in findings), findings


def test_the_old_two_line_writer_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('    setStatus("harnessActiveStatusBadge", "busy", "SDLC RUNNING");',
         '    document.getElementById("harnessActiveStatusBadge").className = "badge cyan";'),
    )
    findings = module.build_components()["findings"]
    assert any(finding["check"] == "legacy-writer" for finding in findings), findings


def test_an_evidence_claim_with_no_basis_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('<div class="evidence" id="onboardingEvidence" data-basis="measured">',
         '<div class="evidence" id="onboardingEvidence" data-basis="trust-me">'),
    )
    findings = module.build_components()["findings"]
    assert any(finding["check"] == "basis-undeclared" for finding in findings), findings


def test_no_status_is_written_by_setting_a_class_and_its_text_separately() -> None:
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'className = "badge' not in html
    assert "setStatus(" in html
    payload = _payload()
    assert payload["writers"]["legacy_badge_writers"] == 0
    assert payload["writers"]["set_status_calls"] >= 8


def test_the_contrast_floors_hold_when_recomputed_here() -> None:
    payload = _payload()
    module = _load_generator()
    tokens = module._token_values()
    for row in payload["contrast"]["tone_graphics"]:
        recomputed = module._tokens_module().contrast_ratio(
            tokens[row["theme"]][row["token"].lstrip("-")], tokens[row["theme"]]["bg-card"]
        )
        assert row["ratio"] == pytest.approx(round(recomputed, 2), abs=0.02), row
        assert recomputed >= payload["contrast"]["graphic_floor"], row
    for row in payload["contrast"]["labels"]:
        recomputed = module._tokens_module().contrast_ratio(tokens[row["theme"]]["text"], tokens[row["theme"]]["bg-card"])
        assert recomputed >= payload["contrast"]["text_floor"], row


def test_the_dark_accent_is_a_graphic_and_never_a_label() -> None:
    """The reason the label is `--text` rather than the tone.

    `--accent` measures about 3.3:1 on `--bg-card` in the dark theme: fine for a
    dot that repeats the words, below the 4.5:1 a label needs. If someone later
    paints the label with the tone, this test explains why it breaks.
    """
    payload = _payload()
    rows = {(row["theme"], row["tone"]): row["ratio"] for row in payload["contrast"]["tone_graphics"]}
    assert rows[("dark", "busy")] < payload["contrast"]["text_floor"]
    assert rows[("dark", "busy")] >= payload["contrast"]["graphic_floor"]
    style = FRONTEND.read_text(encoding="utf-8")
    assert "  border: 1px solid var(--border-hi); background: var(--bg-card); color: var(--text);" in style


def test_every_converted_surface_is_still_in_the_markup() -> None:
    payload = _payload()
    html = FRONTEND.read_text(encoding="utf-8")
    for row in payload["surfaces"]:
        if row["id"]:
            assert f'id="{row["id"]}"' in html, f'#{row["id"]} left the markup'
            assert row["tone"] in TONES
    assert payload["totals"]["status_surfaces"] >= 6
    assert payload["totals"]["live_regions"] >= 1


def test_the_doc_is_generated_and_says_what_it_cannot_prove() -> None:
    assert "never hand-edit" in DOC.read_text(encoding="utf-8")
    limits = _payload()["limits"]
    assert any("rendering" in limit or "looks wrong" in limit for limit in limits)
    assert any("token values" in limit for limit in limits)


def test_the_browser_walk_fails_closed_without_a_browser() -> None:
    """A skipped walk that reports a pass is worse than no walk."""
    assert WALK.exists()
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here; the walk cannot be invoked at all")
    env = {
        "PATH": str(Path(node).parent),
        "HOME": "/tmp",
        "STUDIO_SCREENSHOT_CHROME": "/nonexistent/chrome",
    }
    result = subprocess.run(
        [node, str(WALK), "--base-url", "http://127.0.0.1:9", "--token", "x"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    # Whichever prerequisite is checked first, the message must name it. Both are
    # honest refusals; neither is a pass.
    assert any(
        phrase in combined
        for phrase in ("does not exist", "No Chrome", "ENOENT", "not importable")
    ), combined


def test_the_walk_covers_every_state_and_checks_the_transition_rule() -> None:
    source = WALK.read_text(encoding="utf-8")
    assert "window.M5Status" in source, "the walk must exercise the real component"
    assert "writing the same state twice is silent" in source
    assert "changing the state speaks once" in source
    assert "a tone-only change repaints without speaking" in source
    assert "no page errors during the walk" in source
