"""Pytest plugin for the mandatory browser job: a skip is not a pass."""
from __future__ import annotations

import pytest

_skipped: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _skipped:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line("Browser CI cannot pass with skipped tests: " + ", ".join(_skipped))
        if exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
