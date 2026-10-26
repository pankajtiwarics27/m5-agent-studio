from __future__ import annotations

import pathlib

import pytest

from scripts.verify_baseline import BaselineError, verify_baseline


def test_pinned_baseline_is_present_and_ancestral() -> None:
    pin = verify_baseline()
    assert len(pin) == 40
    assert pin == "7904892d759bb66b817bb17b6f4f377874bd6e25"


def test_malformed_baseline_pin_fails_closed(tmp_path: pathlib.Path) -> None:
    (tmp_path / "BASELINE_COMMIT").write_text("not-a-commit\n", encoding="ascii")
    with pytest.raises(BaselineError, match="full, lowercase 40-character Git SHA"):
        verify_baseline(tmp_path)


def test_missing_baseline_commit_fails_closed(tmp_path: pathlib.Path) -> None:
    (tmp_path / "BASELINE_COMMIT").write_text("0" * 40 + "\n", encoding="ascii")
    with pytest.raises(BaselineError, match="not present in this checkout"):
        verify_baseline(tmp_path)
