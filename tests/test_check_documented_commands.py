"""Documented CLI flags are checked against the scripts they claim to drive.

The defect: `docs/packaging-rollback.md` documented `release.py package --root <root>`. The script does
not accept `--root` on `package`, so an operator following the page got `unrecognized arguments` and no
release. Nothing noticed, because prose about a command is not tied to the command.

These tests are written around two ways such a checker goes quietly useless:

* **scope**, because the obvious implementation unions every `add_argument` in the file. That reader was
  written first here and reported `package --root` as valid by borrowing `--root` from `install` --
  the original defect reproduced inside its own detector. `test_a_flag_from_another_subcommand_does_not_
  count` fails if scope is dropped.
* **abstention**, because a reader that cannot follow a parser and reports "accepts nothing" turns
  every documented flag into a false finding. The abstain paths are asserted directly.

The checker is static on purpose: several documented scripts write files and one starts a server, so a
documentation check that executed them would edit the tree it is checking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_documented_commands as cdc

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tool.py"
    path.write_text("import argparse\n" + body, encoding="utf-8")
    return path


SUBCOMMANDS = '''
def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    package = sub.add_parser("package")
    package.add_argument("--version", required=True)
    package.add_argument("--artifact")
    install = sub.add_parser("install")
    install.add_argument("--version", required=True)
    install.add_argument("--root")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--root")
'''


# ── scope: the defect this module was written for ────────────────────────────────────────────────


def test_a_flag_from_another_subcommand_does_not_count(tmp_path: Path) -> None:
    """`package` takes no `--root`; `install` does. A file-wide union says otherwise."""
    shape = cdc.flags_a_script_accepts(_script(tmp_path, SUBCOMMANDS))
    assert shape.static is True
    assert "--root" in shape.subcommand_flags["install"]
    assert "--root" not in shape.subcommand_flags["package"]
    assert shape.root_flags == frozenset()


def test_the_checker_catches_the_documented_root_on_package(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", SUBCOMMANDS).rename(tmp_path / "scripts" / "tool.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "python scripts/tool.py package --version 1.0.0 --root /tmp/x\n",
        encoding="utf-8",
    )
    report = cdc.check_repository(tmp_path)
    assert report.ok is False
    problem = report.problems[0]
    assert problem.flag == "--root"
    assert problem.subcommand == "package"
    assert "--artifact" in problem.accepted
    assert "does not accept it" in problem.describe()


def test_a_valid_command_produces_no_problem(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", SUBCOMMANDS).rename(tmp_path / "scripts" / "tool.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "python scripts/tool.py install --version 1.0.0 --root /tmp/x\n"
        "python scripts/tool.py package --version 1.0.0\n",
        encoding="utf-8",
    )
    assert cdc.check_repository(tmp_path).ok is True


def test_a_root_level_flag_applies_to_every_subcommand(tmp_path: Path) -> None:
    body = '''
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers()
    package = sub.add_parser("package")
    package.add_argument("--version")
'''
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", body)
    shape = cdc.flags_a_script_accepts(tmp_path / "scripts" / "tool.py")
    assert "--verbose" in shape.root_flags
    commands = [cdc.DocCommand("page.md", 1, "scripts/tool.py", ("package", "--verbose"))]
    problems, unchecked = cdc.check_flags(commands, tmp_path)
    assert problems == [] and unchecked == []


# ── abstention: never a false pass, never a false finding ────────────────────────────────────────


def test_a_script_without_argparse_is_not_read_as_accepting_nothing(tmp_path: Path) -> None:
    path = tmp_path / "plain.py"
    path.write_text("print('hi')\n", encoding="utf-8")
    shape = cdc.flags_a_script_accepts(path)
    assert shape.static is False
    assert shape.reason == "no argparse"


def test_non_literal_flags_abstain_rather_than_guessing(tmp_path: Path) -> None:
    body = '''
FLAGS = ["--version"]
def main():
    parser = argparse.ArgumentParser()
    for flag in FLAGS:
        parser.add_argument(flag)
'''
    assert cdc.flags_a_script_accepts(_script(tmp_path, body)).static is False


def test_a_parser_variable_created_elsewhere_abstains_instead_of_leaking_to_root(tmp_path: Path) -> None:
    """Attributing an unknown parser's flags to the root is exactly how a wrong flag gets validated.

    `parser` here is a parameter: this reader never sees it created, so it cannot know whether it is
    the root parser or a subparser. Assuming root would accept `--root` for every subcommand.
    """
    body = '''
def main(parser):
    parser.add_argument("--root")
'''
    shape = cdc.flags_a_script_accepts(_script(tmp_path, body))
    assert shape.static is False
    assert "was not created here" in shape.reason
    assert "--root" not in shape.root_flags
    assert shape.subcommand_flags == {}


def test_an_unknown_parser_makes_the_command_unchecked_rather_than_wrongly_flagged(tmp_path: Path) -> None:
    """Abstention must reach the report: an unreadable parser is "not checked", never a finding."""
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", '''
def main(parser):
    parser.add_argument("--root")
''')
    commands = [cdc.DocCommand("page.md", 1, "scripts/tool.py", ("package", "--root"))]
    problems, unchecked = cdc.check_flags(commands, tmp_path)
    assert problems == [], "an unreadable parser produced a finding, which is a guess dressed as evidence"
    assert unchecked == commands


def test_a_subparser_built_in_a_loop_is_read_when_the_names_are_literals(tmp_path: Path) -> None:
    body = '''
def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    for name, help_text in (("install", "i"), ("update", "u")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--version")
        command.add_argument("--root")
'''
    shape = cdc.flags_a_script_accepts(_script(tmp_path, body))
    assert shape.static is True
    assert set(shape.subcommand_flags) == {"install", "update"}
    assert "--root" in shape.subcommand_flags["install"]
    # The loop body is shared, so the two commands' flags are a union. That is weaker than separate
    # parsers and must be reported, not presented as precision.
    assert set(shape.widened) == {"install", "update"}


def test_a_widened_subcommand_is_reported_in_the_repository_check(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    _script(
        tmp_path / "scripts",
        '''
def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    for name in ("install", "update"):
        command = sub.add_parser(name)
        command.add_argument("--version")
''',
    ).rename(tmp_path / "scripts" / "tool.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text("python scripts/tool.py install --version 1\n", encoding="utf-8")
    report = cdc.check_repository(tmp_path)
    assert report.ok is True
    assert report.widened == {"scripts/tool.py": ("install", "update")}


# ── extracting the commands from the documentation ──────────────────────────────────────────────


def test_a_placeholder_line_is_skipped_not_passed(tmp_path: Path) -> None:
    """`--root <root>` cannot be checked without guessing what the placeholder becomes."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "python scripts/tool.py install --root <root>\n"
        "python scripts/tool.py install --root $STUDIO_ROOT\n"
        "python scripts/tool.py install --version 1.0.0\n",
        encoding="utf-8",
    )
    checkable, skipped = cdc.documented_commands(tmp_path)
    assert len(checkable) == 1
    assert len(skipped) == 2
    assert all(command.script == "scripts/tool.py" for command in checkable + skipped)


def test_python_and_python3_are_both_recognised(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "python scripts/tool.py a --version 1\npython3 scripts/tool.py b --version 1\n",
        encoding="utf-8",
    )
    checkable, _ = cdc.documented_commands(tmp_path)
    assert len(checkable) == 2


def test_a_line_that_is_not_a_python_invocation_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "node scripts/walk.mjs --out x.json\n$ curl http://localhost\n", encoding="utf-8"
    )
    checkable, skipped = cdc.documented_commands(tmp_path)
    assert checkable == [] and skipped == []


# ── the repository, which is the reason this exists ─────────────────────────────────────────────


def test_the_committed_documentation_names_no_missing_flag() -> None:
    report = cdc.check_repository(PROJECT_ROOT)
    assert report.ok, [problem.describe() for problem in report.problems]


def test_the_repository_check_actually_reads_commands() -> None:
    """A check that silently reads nothing passes forever. This pins that it has something to read."""
    report = cdc.check_repository(PROJECT_ROOT)
    assert len(report.checked) > 40, f"only {len(report.checked)} documented commands were checked"
    assert any(command.script == "scripts/release.py" for command in report.checked)
    assert any(command.script == "scripts/db_maintenance.py" for command in report.checked)


def test_every_unchecked_script_is_named_with_its_reason() -> None:
    """Abstention is allowed; silence is not. Each unchecked script must say why it could not be read."""
    report = cdc.check_repository(PROJECT_ROOT)
    for script in report.unresolved:
        shape = cdc.flags_a_script_accepts(PROJECT_ROOT / script)
        assert shape.static is False
        assert shape.reason, f"{script} was skipped without saying why"


def test_the_cli_returns_nonzero_on_a_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", SUBCOMMANDS).rename(tmp_path / "scripts" / "tool.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "python scripts/tool.py package --root /tmp/x\n", encoding="utf-8"
    )
    assert cdc.main([str(tmp_path)]) == 1
    assert "does not accept it" in capsys.readouterr().err


def test_the_cli_reports_what_it_did_not_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "plain.py").write_text("print('no parser here')\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text("python scripts/plain.py --anything\n", encoding="utf-8")
    assert cdc.main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "not checked" in captured.err
    assert "scripts/plain.py" in captured.err


# ── the counter-example exemption ───────────────────────────────────────────────────────────────


def test_a_document_quoting_a_wrong_command_can_opt_out_and_is_named(tmp_path: Path) -> None:
    """A page that shows a wrong command to explain it is not instructing anyone to run it.

    The exemption must be visible: an opt-out nobody can see is a silent hole in the check.
    """
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", SUBCOMMANDS).rename(tmp_path / "scripts" / "tool.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "bad-example.md").write_text(
        f"<!-- {cdc.COUNTER_EXAMPLE_MARKER} -->\n"
        "python scripts/tool.py package --root /tmp/x\n",
        encoding="utf-8",
    )
    report = cdc.check_repository(tmp_path)
    assert report.ok is True, "a declared counter-example was checked as an instruction"
    assert report.counter_examples == ["bad-example.md"]


def test_a_page_without_the_marker_is_still_checked(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", SUBCOMMANDS).rename(tmp_path / "scripts" / "tool.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "instructions.md").write_text(
        "python scripts/tool.py package --root /tmp/x\n", encoding="utf-8"
    )
    report = cdc.check_repository(tmp_path)
    assert report.ok is False
    assert report.counter_examples == []


def test_the_cli_names_every_exempted_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Silence about an exemption would let the list of opt-outs grow unnoticed."""
    (tmp_path / "scripts").mkdir()
    _script(tmp_path / "scripts", SUBCOMMANDS).rename(tmp_path / "scripts" / "tool.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "quoted.md").write_text(
        f"<!-- {cdc.COUNTER_EXAMPLE_MARKER} -->\n"
        "python scripts/tool.py package --root /tmp/x\n",
        encoding="utf-8",
    )
    assert cdc.main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "exempt as counter-examples" in err
    assert "quoted.md" in err, "the exemption was announced without naming the document"
