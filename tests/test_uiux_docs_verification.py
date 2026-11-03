"""UIUX-P6-04: the commands the docs tell a reader to run must actually run.

`scripts/uiux_docs_verification.py` classifies every fenced command on the five reader-facing pages
and checks their links and claims. This file is the other half: it executes the commands classified
as `runs` — the read-only ones — and asserts their exit codes, so a page cannot keep telling a
reader to run something that stopped working.

The interpreter is this environment's own: the docs say `python` because they assume an activated
virtualenv, and a bare `python` may not exist on a machine that has only `python3`. The substitution
is stated here rather than hidden, and it changes nothing about what the command does.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = PROJECT_ROOT / "scripts" / "uiux_docs_verification.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Readme_Onboarding.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-readme-onboarding.md"


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_docs_verification", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_generator()


@pytest.fixture(scope="module")
def payload(generator) -> dict:
    return generator.build()


def _executable_commands(payload: dict) -> list[str]:
    """The distinct commands the review says it executes, in the order the pages present them."""
    seen: list[str] = []
    for row in payload["commands"]:
        if row["executes"] and row["normalized"] not in seen:
            seen.append(str(row["normalized"]))
    return seen


def _localize(command: str) -> list[str]:
    """The command with this environment's interpreter and tool paths, as argv."""
    parts = command.split()
    if parts[0] in ("python", "python3"):
        return [sys.executable, *parts[1:]]
    tool = shutil.which(parts[0]) or str(Path(sys.executable).parent / parts[0])
    return [tool, *parts[1:]]


def test_the_committed_verification_is_the_generators_output(generator) -> None:
    assert json.loads(JSON_PATH.read_text(encoding="utf-8")) == generator.build()
    assert DOC_PATH.read_text(encoding="utf-8") == generator.render_doc(generator.build())


def test_the_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(GENERATOR), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 findings" in result.stdout


def test_every_fenced_command_is_classified_with_a_reason(payload: dict) -> None:
    assert payload["findings"] == []
    assert payload["commands"], "the parser found no commands; the pages certainly have some"
    for row in payload["commands"]:
        assert row["kind"], row["command"]
        assert row["reason"], row["command"]
        assert row["kind"] in {"runs", "writes", "needs-network", "needs-model", "destructive", "illustrative"}, row


def test_the_parser_does_not_turn_prose_into_commands(generator) -> None:
    """A page that shows output in its own block must not have its paragraphs read as shell."""
    page = generator.read("docs/demo.md")
    commands = generator.fenced_commands(page)
    assert commands
    # Every command starts with a program, an assignment or a shell builtin; a paragraph does not.
    first_words = {"python", "python3", "pip", "npm", "node", "git", "cd", "export", ".", "ruff", "mypy", "tar", "LD_LIBRARY_PATH=x"}
    for command in commands:
        first = command.split()[0]
        assert first in first_words or "=" in first or "/" in first, f"a sentence was read as a command: {command!r}"


def test_the_safe_commands_are_the_ones_that_are_safe(payload: dict) -> None:
    """Nothing that starts a server, installs, or regenerates a tracked file is executed here."""
    forbidden = ("--serve", "pip install", "venv", "build_studio.py", "npm i", "--write", "app.py", "studio.py")
    for row in payload["commands"]:
        if row["executes"]:
            assert not any(token in row["normalized"] for token in forbidden), row


@pytest.mark.parametrize("command", _executable_commands(_load_generator().build()))
def test_the_docs_command_runs(command: str) -> None:
    """One test per command, so a failure names the page's own instruction."""
    argv = _localize(command)
    result = subprocess.run(  # noqa: S603 - argv built from the docs, checked above
        argv,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, f"`{command}` exited {result.returncode}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"


def test_the_published_set_command_agrees_with_the_review(payload: dict) -> None:
    """`validate_screenshots.py` is in the docs and in the review; both must say six of six."""
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(PROJECT_ROOT / "scripts" / "validate_screenshots.py")],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "6 of 6" in result.stdout
    review = json.loads((PROJECT_ROOT / "product" / "M5_2_0_UIUX_Screenshot_Review.json").read_text(encoding="utf-8"))
    assert len(review["published"]["files"]) == 6


def test_the_onboarding_path_the_quickstart_describes_is_the_one_the_server_serves() -> None:
    """The page's six steps, from a live server: a reader's first minute, verified end to end.

    No browser is needed for this: the preflight is the one read-only route the Quickstart's "First
    run" section is entirely about, and it is the only part of onboarding that can be checked
    without a human at the keyboard. The demo tour starts a seeded, provider-free instance and
    prints its throwaway token, exactly as the demo page describes.
    """
    import signal

    demo = PROJECT_ROOT / "scripts" / "demo_tour.py"
    process = subprocess.Popen(  # noqa: S603 - fixed argv, repository script
        [sys.executable, str(demo), "--serve"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        assert line, "the demo did not publish its URL"
        instance = json.loads(line)
        request = subprocess.run(  # noqa: S603 - fixed argv, loopback only
            [
                sys.executable,
                "-c",
                (
                    "import json,sys,urllib.request;"
                    "url,token=sys.argv[1],sys.argv[2];"
                    "req=urllib.request.Request(url+'/api/onboarding/preflight',"
                    "headers={'Authorization':'Bearer '+token});"
                    "print(urllib.request.urlopen(req,timeout=30).read().decode())"
                ),
                instance["url"],
                instance["token"],
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert request.returncode == 0, request.stderr
        report = json.loads(request.stdout)["preflight"]
        steps = report["steps"]
        ids = [step["id"] for step in steps]
        assert ids == ["auth", "model-source", "model-available", "workspace", "execution", "first-task"], ids
        # The preflight names the next step once, at the top, rather than flagging a step in place.
        next_step = report["next_step"]
        assert next_step, "the preflight named no next step"
        assert [step["id"] for step in steps].count(next_step["id"]) == 1
        for step in steps:
            assert step["status"] in {"ready", "optional", "action-required", "blocked"}, step
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                import os

                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
