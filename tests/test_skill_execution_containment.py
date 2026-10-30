"""M5-P0-03 containment: skill-script execution never bypasses the execution boundary.

Skill sources (`SKILL.md` plus `scripts/search.py`) are untrusted code. These
tests pin the fail-closed contract of `app._run_skill_script` at both discovered
call sites (docs/m5-2-0-integration-audit.md section 5):

- skill code requires the ``STUDIO_ALLOW_SKILL_CODE`` opt-in (default off);
- ``STUDIO_EXECUTION_MODE=disabled`` (the default) never executes skill code;
- host mode requires ``STUDIO_ALLOW_HOST_EXECUTION=1``;
- sandbox mode routes through the container boundary and never falls back to
  host execution.

No test contacts the network or a model endpoint; subprocesses run only for the
explicit host-mode positive cases, against a temporary script.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import app as studio


def _forbidden_spawn(*_args, **_kwargs):  # pragma: no cover - only runs on regression
    raise AssertionError("skill code must not spawn a host subprocess in this configuration")


def _forbid_host_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio.asyncio, "create_subprocess_exec", _forbidden_spawn)
    monkeypatch.setattr(studio.asyncio, "create_subprocess_shell", _forbidden_spawn)


def _install_fake_skill(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "demo-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\nDemo body text.\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "search.py").write_text(
        "import sys\nprint('skill-script-ran:' + (sys.argv[1] if len(sys.argv) > 1 else 'none'))\n",
        encoding="utf-8",
    )
    return skill_dir


def _patch_skill_lookup(monkeypatch: pytest.MonkeyPatch, skill_dir: Path) -> None:
    monkeypatch.setattr(
        studio,
        "discover_installed_skills",
        lambda: [{"id": "demo", "name": "demo", "description": "d", "path": str(skill_dir)}],
    )
    monkeypatch.setattr(studio, "_task_needs_design_engine", lambda _task: True)
    monkeypatch.setattr(studio, "_extract_search_keywords", lambda _task: "kw")


class _NoNetworkClient:
    """Fake httpx.AsyncClient: any request fails immediately (tests never hit network)."""

    async def get(self, *_args, **_kwargs):
        raise RuntimeError("network disabled in tests")


def _patch_inspiration_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "list_mcp_servers", lambda **_kwargs: [])


@pytest.fixture(autouse=True)
def _isolated_skill_audit_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_file = tmp_path / "skill-audit.db"
    monkeypatch.setattr(studio, "DB_PATH", db_file)
    studio.init_db()
    return db_file


# ---------------------------------------------------------------------------
# Call site 1: build_skills_context
# ---------------------------------------------------------------------------


def test_build_skills_context_denies_skill_code_by_default(monkeypatch, tmp_path):
    skill_dir = _install_fake_skill(tmp_path)
    _patch_skill_lookup(monkeypatch, skill_dir)
    _forbid_host_spawn(monkeypatch)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", False)

    block, summaries = asyncio.run(studio.build_skills_context(["demo"], "design the ui"))

    assert "Demo body text." in block  # static skill text still applies
    assert summaries[0]["ran_design_engine"] == "no"
    assert "skill-script-ran" not in block


def test_build_skills_context_fails_closed_when_execution_disabled(monkeypatch, tmp_path):
    skill_dir = _install_fake_skill(tmp_path)
    _patch_skill_lookup(monkeypatch, skill_dir)
    _forbid_host_spawn(monkeypatch)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "disabled")

    block, summaries = asyncio.run(studio.build_skills_context(["demo"], "design the ui"))

    assert summaries[0]["ran_design_engine"] == "no"
    assert "skill-script-ran" not in block


def test_build_skills_context_host_mode_requires_host_opt_in(monkeypatch, tmp_path):
    skill_dir = _install_fake_skill(tmp_path)
    _patch_skill_lookup(monkeypatch, skill_dir)
    _forbid_host_spawn(monkeypatch)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", False)

    block, summaries = asyncio.run(studio.build_skills_context(["demo"], "design the ui"))

    assert summaries[0]["ran_design_engine"] == "no"
    assert "skill-script-ran" not in block


def test_build_skills_context_runs_in_host_mode_with_opt_in(monkeypatch, tmp_path):
    skill_dir = _install_fake_skill(tmp_path)
    _patch_skill_lookup(monkeypatch, skill_dir)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    block, summaries = asyncio.run(studio.build_skills_context(["demo"], "design the ui"))

    assert summaries[0]["ran_design_engine"] == "yes"
    assert "skill-script-ran:kw" in block


def test_build_skills_context_sandbox_mode_never_falls_back_to_host(monkeypatch, tmp_path):
    skill_dir = _install_fake_skill(tmp_path)
    _patch_skill_lookup(monkeypatch, skill_dir)
    _forbid_host_spawn(monkeypatch)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")

    attempted: list[Path] = []

    async def _no_container_runtime(root, command, *, timeout, max_output_bytes, python_code=False):
        attempted.append(root)
        raise RuntimeError("no container runtime in tests")

    monkeypatch.setattr(studio, "_execute_limited", _no_container_runtime)

    block, summaries = asyncio.run(studio.build_skills_context(["demo"], "design the ui"))

    # Container boundary was attempted (not host exec); with no runtime the
    # script output is skipped and the static skill text still applies.
    assert attempted == [skill_dir.resolve()]
    assert summaries[0]["ran_design_engine"] == "no"
    assert "skill-script-ran" not in block
    assert "Demo body text." in block


# ---------------------------------------------------------------------------
# Call site 2: _fetch_online_inspiration (previously ungated)
# ---------------------------------------------------------------------------


def _install_ux_skill(tmp_path: Path) -> Path:
    ux_dir = tmp_path / ".agents" / "skills" / "ui-ux-pro-max"
    (ux_dir / "scripts").mkdir(parents=True)
    script = ux_dir / "scripts" / "search.py"
    script.write_text("print('ux-script-ran')\n", encoding="utf-8")
    return script


def test_fetch_inspiration_skill_script_denied_by_default(monkeypatch, tmp_path):
    _install_ux_skill(tmp_path)
    _patch_inspiration_seams(monkeypatch)
    _forbid_host_spawn(monkeypatch)
    monkeypatch.setattr(studio, "BASE_DIR", tmp_path)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", False)

    result = asyncio.run(studio._fetch_online_inspiration(_NoNetworkClient(), "design patterns", tmp_path))

    assert "ui-ux-pro-max" not in result
    assert "ux-script-ran" not in result


def test_fetch_inspiration_skill_script_fails_closed_when_execution_disabled(monkeypatch, tmp_path):
    _install_ux_skill(tmp_path)
    _patch_inspiration_seams(monkeypatch)
    _forbid_host_spawn(monkeypatch)
    monkeypatch.setattr(studio, "BASE_DIR", tmp_path)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "disabled")

    result = asyncio.run(studio._fetch_online_inspiration(_NoNetworkClient(), "design patterns", tmp_path))

    assert "ui-ux-pro-max" not in result
    assert "ux-script-ran" not in result


def test_fetch_inspiration_skill_script_sandbox_mode_never_falls_back_to_host(monkeypatch, tmp_path):
    _install_ux_skill(tmp_path)
    _patch_inspiration_seams(monkeypatch)
    _forbid_host_spawn(monkeypatch)
    monkeypatch.setattr(studio, "BASE_DIR", tmp_path)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")

    async def _no_container_runtime(root, command, *, timeout, max_output_bytes, python_code=False):
        raise RuntimeError("no container runtime in tests")

    monkeypatch.setattr(studio, "_execute_limited", _no_container_runtime)

    result = asyncio.run(studio._fetch_online_inspiration(_NoNetworkClient(), "design patterns", tmp_path))

    assert "ui-ux-pro-max" not in result
    assert "ux-script-ran" not in result


def test_fetch_inspiration_skill_script_runs_with_opt_in(monkeypatch, tmp_path):
    _install_ux_skill(tmp_path)
    _patch_inspiration_seams(monkeypatch)
    monkeypatch.setattr(studio, "BASE_DIR", tmp_path)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    result = asyncio.run(studio._fetch_online_inspiration(_NoNetworkClient(), "design patterns", tmp_path))

    assert "ui-ux-pro-max" in result
    assert "ux-script-ran" in result
