from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WALK = (ROOT / "scripts" / "uiux_interaction_polish_walk.mjs").read_text(encoding="utf-8")
MANIFEST = json.loads((ROOT / "docs" / "media" / "screenshots" / "manifest.json").read_text(encoding="utf-8"))


def test_shared_toolbar_keyboard_focus_matches_pointer_hover_surface() -> None:
    pattern = re.compile(
        r"\.btn:hover:not\(:disabled\),\s*"
        r"\.top-actions \.btn:focus-visible:not\(:disabled\)\s*"
        r"\{(?P<declarations>[^}]*)\}",
        re.DOTALL,
    )
    match = pattern.search(HTML)
    assert match, "the shared toolbar needs the same hover and keyboard-focus rule"
    declarations = match.group("declarations")
    assert "background: var(--bg-hover)" in declarations
    assert "border-color: var(--border-hi)" in declarations


def test_shared_toolbar_focus_retains_the_global_visible_ring() -> None:
    assert re.search(
        r"button:focus-visible[^\{]*\{[^}]*outline:\s*2px solid var\(--ring\)",
        HTML,
        re.DOTALL,
    )
    assert "outlineWidth: style.outlineWidth" in WALK
    assert "outlineColor: style.outlineColor" in WALK


def test_interaction_walk_visits_every_manifest_pane_and_checks_exact_state_parity() -> None:
    match = re.search(r"const TABS = \[(?P<tabs>[^\]]+)\]", WALK)
    assert match
    walked = re.findall(r'"([a-z]+)"', match.group("tabs"))
    assert walked == [shot["id"] for shot in MANIFEST["shots"]]
    assert "focused.background !== hovered.background" in WALK
    assert "focused.border !== hovered.border" in WALK
    assert "button.matches(\":focus-visible\")" in WALK
    assert "page.screenshot({ type: \"png\", fullPage: false })" in WALK


def test_reduced_motion_contract_remains_in_the_stylesheet() -> None:
    assert "@media (prefers-reduced-motion: reduce)" in HTML
    assert re.search(r"transition-duration:\s*\.01ms\s*!important", HTML)


def test_committed_before_after_screenshot_sets_match_their_indexes() -> None:
    validator = ROOT / "scripts" / "validate_uiux_interaction_polish_screenshots.py"
    result = subprocess.run(
        [sys.executable, str(validator), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "72 PNGs in 12 before/after sets" in result.stdout
