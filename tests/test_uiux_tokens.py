"""UIUX-P1-01: the design tokens must be a closed, checked system.

The frontend had a redesign layer with two themes before this story; what it did
not have was any way to distinguish a token from a typo. `var(--nope)` renders as
nothing and says nothing, and a colour that exists in one theme but not the other
fails only for the users who switch. These tests are about those two silences:

* the committed artifacts are the generator's own output, and `--check` agrees
  with the tree;
* each rule is proved by breaking a *temporary copy* of the stylesheet, so the
  tests exercise the real parser rather than a mock of it;
* the contrast numbers are recomputed from the tokens here, so a future edit that
  keeps the JSON in sync but lowers a ratio still fails.

The tests do not assert that the UI looks right. They assert that every name the
stylesheet uses exists, every name it declares is used, both themes define the
same colours, density moves only what it is allowed to move, and the text/background
pairs clear their floors.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "product" / "M5_2_0_UIUX_Tokens.json"
DOC = ROOT / "docs" / "uiux-2-0-tokens.md"
GENERATOR = ROOT / "scripts" / "uiux_tokens.py"
FRONTEND = ROOT / "static" / "index.html"


def _payload() -> dict:
    return json.loads(TOKENS.read_text(encoding="utf-8"))


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_tokens", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old: str, new: str):
    """Point the generator at a modified copy of the stylesheet.

    Each caller passes the exact text it is changing and the tests assert the
    substitution happened, so a later edit to index.html cannot quietly turn a
    "this is refused" test into a test of nothing.
    """
    source = FRONTEND.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"the stylesheet no longer contains the anchor {old[:60]!r}"
    copy = tmp_path / "index.html"
    copy.write_text(source.replace(old, new), encoding="utf-8")
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
    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout


def test_every_token_in_the_artifact_is_declared_in_the_stylesheet() -> None:
    payload = _payload()
    style = FRONTEND.read_text(encoding="utf-8")
    for token in payload["tokens"]:
        assert f"{token['name']}:" in style, f"{token['name']} is in the artifact but not the tree"
        assert token["references"] >= 1, f"{token['name']} has no reader"


def test_the_groups_cover_every_token_exactly_once() -> None:
    payload = _payload()
    grouped = [name for names in payload["groups"].values() for name in names]
    assert len(grouped) == len(set(grouped)), "a token is filed under two groups"
    assert sorted(grouped) == sorted(token["name"].lstrip("-") for token in payload["tokens"])


def test_the_contrast_floors_hold_when_recomputed_here() -> None:
    module = _load_generator()
    payload = _payload()
    floors = payload["contrast_floors"]["text_roles"]
    assert set(floors) == {"text", "text-dim", "text-faint"}
    for row in payload["contrast"]:
        recomputed = module.contrast_ratio(
            _value(row["theme"], row["foreground"]), _value(row["theme"], row["background"])
        )
        assert row["ratio"] == pytest.approx(recomputed, abs=0.02), row
        assert recomputed >= row["floor"], row


def _value(theme: str, token: str) -> str:
    """Resolve one token's value in one theme straight from the stylesheet."""
    payload = _payload()
    for row in payload["tokens"]:
        if row["name"] == token:
            return row["theme_overrides"].get(theme, row["value"])
    raise AssertionError(f"{token} is not a token")


def test_the_contrast_formula_is_the_wcag_one() -> None:
    module = _load_generator()
    assert module.contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0)
    assert module.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert module.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)


def test_an_undefined_reference_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ".brand-chip { width: 28px; height: 28px;",
        ".brand-chip { width: 28px; height: 28px; outline-color: var(--not-a-token);",
    )
    with pytest.raises(module.TokenError, match="not-a-token"):
        module.read_tokens()


def test_a_token_with_no_reader_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        "  --space-2: 6px;",
        "  --space-2: 6px;\n  --space-9: 32px;",
    )
    # --space-9 has no group either; the group check runs first, which is the
    # stronger message, so this proves the classification rule directly.
    with pytest.raises(module.TokenError, match="no declared group"):
        module.read_tokens()


def test_a_token_whose_only_reader_is_removed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan rule, proved on a real token rather than a synthetic one.

    `--tracking-tight` has exactly one reader (`.brand`). Dropping that declaration
    while keeping the token leaves a value nothing reads, which is the state the
    check exists to refuse — and the state a token reaches by accident when the
    rule that used it is refactored away.
    """
    module = _patched(
        monkeypatch,
        tmp_path,
        "letter-spacing: var(--tracking-tight);",
        "",
    )
    with pytest.raises(module.TokenError, match="wishes rather than tokens"):
        module.read_tokens()


def test_a_theme_missing_a_colour_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        "  --primary-text: #ffffff;\n  --bg: #f7f7f5;",
        "  --bg: #f7f7f5;",
    )
    with pytest.raises(module.TokenError, match="primary-text"):
        module.read_tokens()


def test_a_theme_that_repoints_a_non_colour_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        "  --bg: #f7f7f5;",
        "  --bg: #f7f7f5;\n  --radius-md: 4px;",
    )
    with pytest.raises(module.TokenError, match="non-colour"):
        module.read_tokens()


def test_density_must_repoint_exactly_its_own_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        'body[data-density="compact"] {\n  --control-min-h: 32px;',
        'body[data-density="compact"] {\n  --control-min-h: 32px;\n  --space-2: 4px;',
    )
    with pytest.raises(module.TokenError, match="density"):
        module.read_tokens()


def test_a_contrast_regression_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # --text-faint is the tightest pair in the tree (4.69:1 in the light theme).
    # Darkening it just past the floor proves the check measures rather than trusts.
    module = _patched(monkeypatch, tmp_path, "  --text-faint: #969da4;", "  --text-faint: #6a7078;")
    with pytest.raises(module.TokenError, match="contrast"):
        module.read_tokens()


def test_the_page_names_its_limits() -> None:
    page = DOC.read_text(encoding="utf-8")
    assert "never hand-edit" in page
    assert "do not prove a screen looks right" in page
    assert "Adding a token" in page
