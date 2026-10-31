"""The browser CI job may not silently succeed when tooling is absent."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("required", [False, True])
def test_browser_dependency_absence_is_only_a_local_optional_skip(tmp_path: Path, required: bool) -> None:
    env = os.environ.copy()
    for key in ("STUDIO_SCREENSHOT_CHROME", "STUDIO_PUPPETEER_CORE", "M5_REQUIRE_BROWSER_TESTS", "PYTHONPATH"):
        env.pop(key, None)
    env["HOME"] = str(tmp_path)
    if required:
        env["M5_REQUIRE_BROWSER_TESTS"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
         "tests/test_screenshot_capture_browser.py", "-k", "real_isolated_demo"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    output = result.stdout + result.stderr
    if required:
        assert result.returncode != 0
        assert "browser regressions are required" in output
    else:
        assert result.returncode == 0
        assert "1 skipped" in output


def test_browser_ci_guard_rejects_any_skipped_test(tmp_path: Path) -> None:
    source = tmp_path / "test_guard_example.py"
    source.write_text("import pytest\n@pytest.mark.skip(reason='synthetic skip')\ndef test_demo(): pass\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "scripts.browser_ci_no_skips", str(source)],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode != 0
    assert "Browser CI cannot pass with skipped tests" in result.stdout + result.stderr

    source.write_text("def test_demo(): pass\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "scripts.browser_ci_no_skips", str(source)],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_browser_ci_guard_rejects_collection_time_skips(tmp_path: Path) -> None:
    skipped = tmp_path / "test_skipped_module.py"
    passing = tmp_path / "test_passing_module.py"
    skipped.write_text("import pytest\npytest.skip('synthetic module skip', allow_module_level=True)\n", encoding="utf-8")
    passing.write_text("def test_demo(): pass\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "scripts.browser_ci_no_skips", str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode != 0
    assert "Browser CI cannot pass with skipped tests" in result.stdout + result.stderr
