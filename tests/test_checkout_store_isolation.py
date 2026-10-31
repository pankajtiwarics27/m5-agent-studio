from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "test_audit_log.py::test_allowed_builtin_tool_is_audited_with_hashes_only",
    "test_prompt_injection_adversarial.py::test_injected_instruction_cannot_read_secret_files",
    "test_security_and_quality.py::test_shared_approval_flow_resolves_and_returns_session_to_running",
    "test_security_release_gate.py::test_full_fault_injection_and_adversarial_suite_passes_and_blocks_deferred_release",
    "test_security_release_gate.py::test_security_release_gate_cli_and_release_py_gate_enforce_block_vs_ready",
    "test_skill_execution_containment.py::test_build_skills_context_runs_in_host_mode_with_opt_in",
    "test_skill_execution_containment.py::test_fetch_inspiration_skill_script_runs_with_opt_in",
    "test_sandbox_boundary.py::test_build_container_invocation_matches_app_sandbox_command_and_rejects_unsafe_inputs",
    "test_qualify_environment.py::test_container_argv_matches_the_product_hardening_flags",
    "test_qualify_environment.py::test_suite_argv_matches_the_product_argv",
    "test_qualify_environment.py::test_script_is_runnable_end_to_end_in_this_environment",
    "test_isolation_smoke.py::test_smoke_script_reports_observable_checks_and_unqualified_status",
    "test_trusted_transformations.py::test_http_endpoints_and_standalone_studio_bundle_parity",
)


@pytest.mark.parametrize("case", CASES)
def test_selected_security_tests_preserve_source_store(tmp_path: Path, case: str) -> None:
    """Run real tests against synthetic same-session sentinels in a source copy."""
    source = tmp_path / "source"
    source.mkdir()
    for module in ROOT.glob("*.py"):
        shutil.copy2(module, source / module.name)
    for directory in ("docs", "product", "scripts", "static"):
        shutil.copytree(ROOT / directory, source / directory, ignore=shutil.ignore_patterns("__pycache__"))
    (source / "tests").mkdir()
    filename = case.split("::", 1)[0]
    shutil.copy2(ROOT / "tests" / filename, source / "tests" / filename)
    store = source / ".harnesses" / "sessions"
    (store / "edits").mkdir(parents=True)
    sentinel_files = []
    for session, name in (("allowed-read", "note.txt"), ("injection-session", ".env.example")):
        observed = store / "edits" / f"{session}.observed.json"
        observed.write_text(json.dumps({
            "schema_version": "1.0.0",
            "session_id": session,
            "entries": {name: {"sha256": "a" * 64, "recorded_at": 1}},
        }), encoding="utf-8")
        sentinel_files.append(observed)
    prefs = store / "workspace_prefs.json"
    prefs.write_text('{"default_dir":"synthetic-sentinel"}', encoding="utf-8")
    sentinel_files.append(prefs)
    db = source / "studio_history.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('synthetic-db-sentinel')")
    sentinel_files.append(db)
    before = {p.relative_to(source): p.read_bytes() for p in sentinel_files}
    store_mode = store.stat().st_mode
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("STUDIO_DB_PATH", None)
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"tests/{case}"],
        cwd=source, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert {path: (source / path).read_bytes() for path in before} == before
    assert store.stat().st_mode == store_mode
    assert {p.relative_to(source) for p in store.rglob("*") if p.is_file()} == {
        p.relative_to(source) for p in sentinel_files if p != db
    }
