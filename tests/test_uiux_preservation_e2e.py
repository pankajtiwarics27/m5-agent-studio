"""UIUX-P6-01: the preservation matrix must be provable, not readable.

The ledger has carried 21 "must preserve" rows since `UIUX-P0-06`. `scripts/preservation_suite.py`
is the command that proves them; this file is the part that keeps the command honest, because a
suite that proves nothing is worse than no suite at all - it produces a green line.

Every rule is proved by breaking a *temporary copy* of the thing it reads: a walk with a check
deleted, a test renamed, a route unmounted, a button unwired, a matrix row invented, a walk that
quietly moved to the wrong side of the server classification. Each test asserts its anchor existed
before it broke it, so a later edit cannot disarm the test by removing the text it edits.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = PROJECT_ROOT / "scripts" / "uiux_preservation_e2e.py"
SUITE = PROJECT_ROOT / "scripts" / "preservation_suite.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Preservation_E2E.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-preservation-e2e.md"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
SCRIPTS = PROJECT_ROOT / "scripts"
APP = PROJECT_ROOT / "app.py"
FRONTEND = PROJECT_ROOT / "static" / "index.html"


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_preservation_e2e", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def payload() -> dict:
    return _load_generator().build_inventory()


def _checks(module) -> list[str]:
    return sorted({finding["check"] for finding in module.build_inventory()["findings"]})


def test_the_committed_artifacts_are_the_generators_output() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True, cwd=PROJECT_ROOT
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_matrix_row_has_a_proof_and_every_proof_has_a_row(payload: dict) -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    matrix = {(row["tab"], row["existing"]) for row in ledger["feature_preservation_matrix"]}
    proved = {(row["tab"], row["capability"]) for row in payload["rows"]}
    assert matrix == proved
    assert payload["findings"] == []
    assert payload["totals"]["matrix_rows"] == 21
    assert payload["totals"]["provers"] >= 40


def test_the_proofs_are_spread_across_every_kind(payload: dict) -> None:
    totals = payload["totals"]
    assert totals["walk_provers"] > 20
    assert totals["pytest_provers"] >= 4
    assert totals["route_provers"] >= 8
    assert totals["control_provers"] >= 2
    assert totals["walks"] >= 15
    assert totals["support_walks"] >= 10


def test_every_walk_is_either_proving_a_row_or_listed_with_its_reason(payload: dict) -> None:
    on_disk = {path.name for path in SCRIPTS.glob("*_walk.mjs")}
    accounted = set(payload["walks"]) | set(payload["support_walks"])
    assert on_disk == accounted
    for reason in payload["support_walks"].values():
        assert len(reason) > 20, reason


def test_a_walk_that_lost_its_check_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "scripts"
    copy.mkdir()
    for path in SCRIPTS.glob("*_walk.mjs"):
        shutil.copy2(path, copy / path.name)
    target = copy / "start_walk.mjs"
    source = target.read_text(encoding="utf-8")
    anchor = "the lead names the server's next step"
    # The phrase appears in the walk's own prose as well as in its check, so every copy goes: a
    # test that removed only one of them would prove the walk still asserts what it lost.
    assert anchor in source
    target.write_text(source.replace(anchor, "the lead says something else entirely"), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "SCRIPTS", copy)
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert any("no longer asserts" in detail and "start_walk.mjs" in detail for detail in details)


def test_a_test_that_was_renamed_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path
    (root / "tests").mkdir()
    source = (PROJECT_ROOT / "tests" / "test_harness_evidence.py").read_text(encoding="utf-8")
    anchor = "def test_apply_still_refuses_and_reports("
    assert source.count(anchor) == 1
    (root / "tests" / "test_harness_evidence.py").write_text(
        source.replace(anchor, "def test_apply_was_renamed_by_a_later_story("), encoding="utf-8"
    )
    module = _load_generator()
    monkeypatch.setattr(module, "PROJECT_ROOT", root)
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert any("test_apply_still_refuses_and_reports" in detail for detail in details)


def test_a_route_that_is_no_longer_mounted_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "app.py"
    source = APP.read_text(encoding="utf-8")
    anchor = '@app.post("/api/harness/abort/{session_id}")'
    assert source.count(anchor) == 1
    copy.write_text(source.replace(anchor, '@app.post("/api/harness/abort-run/{session_id}")'), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "APP", copy)
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert any("`POST /api/harness/abort/{session_id}` is not mounted" in detail for detail in details)


def test_a_control_that_lost_its_handler_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "index.html"
    source = FRONTEND.read_text(encoding="utf-8")
    anchor = 'onclick="abortHarnessRun()"'
    assert source.count(anchor) == 1
    copy.write_text(source.replace(anchor, ""), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "FRONTEND", copy)
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert any("nothing is wired to it" in detail for detail in details)


def test_a_matrix_row_with_no_proof_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    ledger["feature_preservation_matrix"].append(
        {
            "tab": "Start",
            "existing": "a capability nobody proved",
            "target": "Start primary view",
            "must_preserve": "existing endpoint",
        }
    )
    copy = tmp_path / "ledger.json"
    copy.write_text(json.dumps(ledger), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "LEDGER", copy)
    details = [finding["detail"] for finding in module.build_inventory()["findings"]]
    assert any("a capability nobody proved" in detail for detail in details)


def test_a_walk_on_the_wrong_side_of_the_server_split_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "scripts"
    copy.mkdir()
    for path in SCRIPTS.glob("*_walk.mjs"):
        shutil.copy2(path, copy / path.name)
    target = copy / "polish_walk.mjs"
    source = target.read_text(encoding="utf-8")
    assert "function startFixture" in source
    target.write_text(
        source.replace("async function startFixture", "async function unusedFixture").replace(
            "function startFixture =", "function unusedFixture ="
        ),
        encoding="utf-8",
    )
    module = _load_generator()
    monkeypatch.setattr(module, "SCRIPTS", copy)
    assert "server-classification" in _checks(module)


def test_a_support_walk_that_also_proves_a_row_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_generator()
    doubled = dict(module.SUPPORT_WALKS)
    doubled["start_walk.mjs"] = "this walk already proves five rows, so it cannot also be a support walk"
    monkeypatch.setattr(module, "SUPPORT_WALKS", doubled)
    assert "unlisted-walk" in _checks(module)


def test_the_suite_lists_the_plan_before_it_runs_anything() -> None:
    result = subprocess.run(
        [sys.executable, str(SUITE), "--list"], capture_output=True, text=True, cwd=PROJECT_ROOT
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "21 rows" in text
    assert "routes:" in text and "tests:" in text and "walks:" in text
    assert "POST /api/harness/approve" in text
    assert "harness_permissions_walk.mjs" in text


def test_the_page_states_what_it_does_not_prove(payload: dict) -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "never hand-edit this page" in doc
    assert "## What this does not prove" in doc
    assert "demo fixture with no" in doc
    assert "executed by" in doc
    assert json.loads(JSON_PATH.read_text(encoding="utf-8"))["story"] == "UIUX-P6-01"
    assert payload["totals"]["findings"] == 0


def _load_suite():
    spec = importlib.util.spec_from_file_location("preservation_suite", SUITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(tab: str, capability: str, *provers: dict) -> dict:
    return {"tab": tab, "capability": capability, "provers": list(provers), "proof_count": len(provers)}


def test_a_known_failure_is_reported_as_a_known_defect_not_as_a_pass_or_a_break() -> None:
    suite = _load_suite()
    rows = [
        _row("Harness", "diffs", {"kind": "walk", "script": "scripts/harness_evidence_walk.mjs", "check": "x"}),
        _row("Harness", "policy", {"kind": "walk", "script": "scripts/harness_settings_walk.mjs", "check": "y"}),
        _row("Harness", "abort", {"kind": "walk", "script": "scripts/harness_recovery_walk.mjs", "check": "z"}),
    ]
    results = {
        "routes": {},
        "pytest": {"passed": True},
        "walks": {
            "harness_evidence_walk.mjs": {"passed": False, "known": {"issue": "#169"}},
            "harness_settings_walk.mjs": {"passed": True},
            "harness_recovery_walk.mjs": {"passed": False},
        },
    }
    statuses = {row["row"]: row["status"] for row in suite.coverage(rows, results)}
    assert statuses["Harness / diffs"] == "known"
    assert statuses["Harness / policy"] == "pass"
    assert statuses["Harness / abort"] == "fail"


def test_a_walk_that_never_ran_is_not_a_pass() -> None:
    suite = _load_suite()
    rows = [_row("Start", "recheck", {"kind": "walk", "script": "scripts/start_walk.mjs", "check": "q"})]
    statuses = suite.coverage(rows, {"routes": {}, "pytest": {"passed": True}, "walks": {}})
    assert statuses[0]["status"] == "not run"


def test_every_known_failure_names_an_owner_an_issue_and_a_reason() -> None:
    """An entry is checkable, and an empty map is the honest state after a fix.

    The map carried two entries (#169, #170) until both walks passed. An exemption that outlives
    its defect is the same lie as a pass that never ran, so emptiness is allowed - and when it is
    empty the mechanism has to still be in the file, which `stale_known_failures` reads.
    """
    suite = _load_suite()
    source = (PROJECT_ROOT / "scripts" / "preservation_suite.py").read_text(encoding="utf-8")
    assert "KNOWN_FAILURES" in source, "the suite no longer carries the mechanism at all"
    for walk, known in suite.KNOWN_FAILURES.items():
        assert (PROJECT_ROOT / "scripts" / walk).is_file(), walk
        assert re.match(r"^#\d+$", known["issue"]), known
        assert re.match(r"^UIUX-P\d-\d\d$", known["owner"]), known
        assert len(known["reason"]) > 60, known


def test_stopping_a_fixture_stops_the_app_it_forked() -> None:
    """The leak that OOM-killed a run: the app is a child of the fixture, not the fixture.

    `demo_tour.py --serve` forks the server. Terminating the parent leaves the child holding its
    port and ~60 MB of RSS, and the suite starts fourteen of them per run; on a 4 GB runner the
    leak is the memory the next run needs. `stop_server` signals the session, so the whole tree
    goes - which is what this test checks, with a real grandchild rather than a mock.
    """
    suite = _load_suite()
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    process = subprocess.Popen(  # noqa: S603 - fixed argv, local python only
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    grandchild = int(process.stdout.readline().strip())
    try:
        suite.stop_server(process)
        assert process.poll() is not None, "the fixture survived its own stop"
        assert not _alive(grandchild), "the server the fixture forked outlived it - that is the leak"
    finally:
        for pid in (grandchild, process.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _alive(pid: int) -> bool:
    """True while the pid is a live, non-zombie process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except OSError:  # pragma: no cover - no procfs
        return True
    return status != "Z"
