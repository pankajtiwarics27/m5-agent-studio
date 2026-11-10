"""The identity check must fail on every way a split run can lose a test."""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from scripts import verify_test_identity

JUNIT_HEAD = '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'


def _collect(path: Path, nodes: list[str]) -> Path:
    path.write_text("\n".join(nodes) + "\n", encoding="utf-8")
    return path


def _junit(path: Path, cases: list[str]) -> Path:
    path.write_text(f"{JUNIT_HEAD}{''.join(cases)}</testsuite></testsuites>", encoding="utf-8")
    return path


def _attr(value: str) -> str:
    """Escape an attribute the way pytest does, so `<` survives in a test name."""
    return escape(value, {'"': "&quot;"})


def _passed(classname: str, name: str) -> str:
    return f'<testcase classname="{_attr(classname)}" name="{_attr(name)}" time="0.01"/>'


def _skipped(classname: str, name: str, message: str, kind: str = "pytest.skip") -> str:
    return (
        f'<testcase classname="{_attr(classname)}" name="{_attr(name)}">'
        f'<skipped type="{kind}" message="{_attr(message)}"/></testcase>'
    )


def _failed(classname: str, name: str) -> str:
    return (
        f'<testcase classname="{_attr(classname)}" name="{_attr(name)}">'
        f'<failure message="boom">trace</failure></testcase>'
    )


@pytest.fixture()
def runs(tmp_path: Path) -> dict[str, Path]:
    """A baseline of four tests and two clean split phases over it."""
    nodes = ["tests/test_alpha.py::test_one", "tests/test_alpha.py::test_two", "tests/test_beta.py::test_three", "tests/test_beta.py::TestCase::test_four"]
    baseline = {
        "collect": _collect(tmp_path / "baseline.txt", nodes),
        "junit": _junit(
            tmp_path / "baseline.xml",
            [
                _passed("tests.test_alpha", "test_one"),
                _passed("tests.test_alpha", "test_two"),
                _skipped("tests.test_beta", "test_three", "needs a browser"),
                _passed("tests.test_beta.TestCase", "test_four"),
            ],
        ),
    }
    baseline_nodes = nodes
    for index in range(2):
        subset = baseline_nodes[index * 2 : index * 2 + 2]
        collect = _collect(tmp_path / f"phase{index}.txt", subset)
        cases = []
        for node in subset:
            module, *rest = node.split("::")
            classname = ".".join([*module[:-3].split("/"), *rest[:-1]])
            name = rest[-1]
            if name == "test_three":
                cases.append(_skipped(classname, name, "needs a browser"))
            else:
                cases.append(_passed(classname, name))
        baseline[f"phase{index}_collect"] = collect
        baseline[f"phase{index}_junit"] = _junit(tmp_path / f"phase{index}.xml", cases)
    return baseline


def _argv(runs: dict[str, Path], *phases: str) -> list[str]:
    argv = ["--version", "3.11", "--baseline-collect", str(runs["collect"]), "--baseline-junit", str(runs["junit"])]
    for phase in phases:
        argv.extend(["--phase", phase])
    return argv


def test_identical_split_passes(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    phases = [
        f"alpha:{runs['phase0_collect']}:{runs['phase0_junit']}",
        f"beta:{runs['phase1_collect']}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 0
    out = capsys.readouterr().out
    assert "identical" in out
    assert "4 tests" in out and "2 phases" in out


def test_a_missing_test_fails(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Dropping one node from a phase must fail even though the rest pass."""
    dropped = _collect(runs["phase0_collect"], ["tests/test_alpha.py::test_one"])
    phases = [
        f"alpha:{dropped}:{runs['phase0_junit']}",
        f"beta:{runs['phase1_collect']}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 1
    err = capsys.readouterr().err
    assert "missing from the split run: tests/test_alpha.py::test_two" in err


def test_a_test_collected_by_two_phases_fails(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A duplicate is a defect even when both phases pass it."""
    doubled = _collect(
        runs["phase1_collect"],
        ["tests/test_alpha.py::test_two", "tests/test_beta.py::test_three", "tests/test_beta.py::TestCase::test_four"],
    )
    phases = [
        f"alpha:{runs['phase0_collect']}:{runs['phase0_junit']}",
        f"beta:{doubled}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 1
    err = capsys.readouterr().err
    assert "collected twice: tests/test_alpha.py::test_two in alpha, beta" in err


def test_a_test_that_turns_into_a_skip_fails(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Totals would hide this: four tests collected, one no longer runs."""
    junit = _junit(
        runs["phase0_junit"],
        [
            _passed("tests.test_alpha", "test_one"),
            _skipped("tests.test_alpha", "test_two", "quarantined"),
        ],
    )
    phases = [
        f"alpha:{runs['phase0_collect']}:{junit}",
        f"beta:{runs['phase1_collect']}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 1
    err = capsys.readouterr().err
    assert "outcome changed: tests/test_alpha.py::test_two passed -> skipped" in err


def test_a_changed_skip_reason_fails(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A skip that stays a skip but changed cause is a different skip."""
    junit = _junit(
        runs["phase1_junit"],
        [
            _skipped("tests.test_beta", "test_three", "requires an explicit opt-in"),
            _passed("tests.test_beta.TestCase", "test_four"),
        ],
    )
    phases = [
        f"alpha:{runs['phase0_collect']}:{runs['phase0_junit']}",
        f"beta:{runs['phase1_collect']}:{junit}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 1
    assert "skip reason changed: tests/test_beta.py::test_three" in capsys.readouterr().err


def test_an_empty_phase_fails(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A phase that collects nothing cannot report a green."""
    empty_collect = runs["phase1_collect"]
    empty_collect.write_text("", encoding="utf-8")
    empty_junit = _junit(runs["phase1_junit"], [])
    phases = [
        f"alpha:{runs['phase0_collect']}:{runs['phase0_junit']}",
        f"beta:{empty_collect}:{empty_junit}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 1
    err = capsys.readouterr().err
    assert "empty phase collected no tests: beta" in err
    assert "missing from the split run: tests/test_beta.py::TestCase::test_four" in err


def test_a_case_with_no_collected_node_is_refused(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """An unmappable case must stop the comparison, not be counted as a match."""
    junit = _junit(runs["phase0_junit"], [_passed("tests.test_alpha", "test_one"), _passed("tests.test_ghost", "test_phantom")])
    phases = [
        f"alpha:{runs['phase0_collect']}:{junit}",
        f"beta:{runs['phase1_collect']}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 2
    assert "cannot map tests.test_ghost::test_phantom" in capsys.readouterr().err


def test_a_collection_with_no_node_ids_is_refused(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """`pytest -q` output is not a collection; saying so beats a false pass."""
    wrong = runs["collect"].write_text("....\n4 passed in 1.2s\n", encoding="utf-8")
    assert wrong is not None  # the write is the setup, not the assertion
    phases = [
        f"alpha:{runs['phase0_collect']}:{runs['phase0_junit']}",
        f"beta:{runs['phase1_collect']}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 2
    assert "contains no `path.py::test` node ids" in capsys.readouterr().err


def test_json_report_is_machine_readable(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    phases = [
        f"alpha:{runs['phase0_collect']}:{runs['phase0_junit']}",
        f"beta:{runs['phase1_collect']}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main([*_argv(runs, *phases), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["identical"] is True
    assert payload["version"] == "3.11"
    assert payload["baseline_count"] == payload["union_count"] == 4
    assert payload["phase_counts"] == {"alpha": 2, "beta": 2}
    assert payload["duplicate_ids"] == {}
    assert payload["outcome_changes"] == []


def test_xfail_is_not_a_pass(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """`xfail` and `skipped` are different outcomes; the diff says so."""
    junit = _junit(
        runs["phase1_junit"],
        [
            _skipped("tests.test_beta", "test_three", "needs a browser", kind="pytest.xfail"),
            _passed("tests.test_beta.TestCase", "test_four"),
        ],
    )
    phases = [
        f"alpha:{runs['phase0_collect']}:{runs['phase0_junit']}",
        f"beta:{runs['phase1_collect']}:{junit}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 1
    assert "outcome changed: tests/test_beta.py::test_three skipped -> xfail" in capsys.readouterr().err


def test_failures_are_compared_as_outcomes_too(runs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A phase that turns a pass into a failure is a mismatch, not a broken check."""
    junit = _junit(
        runs["phase0_junit"],
        [_failed("tests.test_alpha", "test_one"), _passed("tests.test_alpha", "test_two")],
    )
    phases = [
        f"alpha:{runs['phase0_collect']}:{junit}",
        f"beta:{runs['phase1_collect']}:{runs['phase1_junit']}",
    ]
    assert verify_test_identity.main(_argv(runs, *phases)) == 1
    assert "outcome changed: tests/test_alpha.py::test_one passed -> failed" in capsys.readouterr().err

def test_a_parametrised_node_id_may_contain_spaces(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """pytest keeps the parameter text verbatim, `[<placeholder here>]` included.

    A regex that rejected whitespace there would refuse a real collection, which
    is how this was found.
    """
    node = "tests/test_alpha.py::test_param[<placeholder here>]"
    baseline_collect = _collect(tmp_path / "baseline.txt", [node])
    baseline_junit = _junit(tmp_path / "baseline.xml", [_passed("tests.test_alpha", "test_param[<placeholder here>]")])
    phase_collect = _collect(tmp_path / "phase.txt", [node])
    phase_junit = _junit(tmp_path / "phase.xml", [_passed("tests.test_alpha", "test_param[<placeholder here>]")])
    argv = [
        "--version",
        "3.11",
        "--baseline-collect",
        str(baseline_collect),
        "--baseline-junit",
        str(baseline_junit),
        "--phase",
        f"only:{phase_collect}:{phase_junit}",
    ]
    assert verify_test_identity.main(argv) == 0
    assert "identical - 1 tests" in capsys.readouterr().out
