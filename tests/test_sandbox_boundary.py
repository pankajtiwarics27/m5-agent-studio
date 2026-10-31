from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_boundary import (
    SandboxBoundaryError,
    build_container_invocation,
    confined_path,
    sandbox_command,
    validate_workspace,
)


def test_confined_path_accepts_children_and_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = workspace / "src"
    child.mkdir()
    assert confined_path(workspace, "src") == child.resolve()
    with pytest.raises(SandboxBoundaryError, match="escapes"):
        confined_path(workspace, "../outside")

    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxBoundaryError, match="symlink"):
        confined_path(workspace, "link/file.txt")


def test_workspace_validation_rejects_broad_secret_and_oversized_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("secret", encoding="utf-8")
    with pytest.raises(SandboxBoundaryError, match="secret"):
        validate_workspace(workspace)

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "file").write_text("data", encoding="utf-8")
    with pytest.raises(SandboxBoundaryError, match="entry scan"):
        validate_workspace(clean, max_entries=0)


def test_sandbox_command_enforces_isolation_for_docker_and_podman(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for runtime in ("docker", "podman"):
        args = sandbox_command(workspace, "printf ok", image="trusted/image:1", runtime=runtime, container_name="test-box")
        assert args[:2] == [runtime, "run"]
        for required in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=2g", "--cpus=2"):
            assert required in args
        assert "type=bind" in args[args.index("--mount") + 1]
        assert args[-3:] == ["sh", "-lc", "printf ok"]


def test_invalid_runtime_and_container_name_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SandboxBoundaryError, match="runtime"):
        sandbox_command(workspace, "true", image="image", runtime="other")
    with pytest.raises(SandboxBoundaryError, match="container_name"):
        sandbox_command(workspace, "true", image="image", container_name="bad name")


def test_build_container_invocation_matches_app_sandbox_command_and_rejects_unsafe_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app as studio

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(studio.shutil, "which", lambda _rt: "/usr/bin/docker")
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_IMAGE", "python:3.12-slim")

    direct = build_container_invocation(
        workspace.resolve(),
        "print('ok')",
        runtime_path="/usr/bin/docker",
        image="python:3.12-slim",
        python_code=True,
        container_name="m5-unified-box",
    )
    via_app = studio._sandbox_command(
        workspace,
        "print('ok')",
        python_code=True,
        container_name="m5-unified-box",
    )
    assert via_app == direct
    for flag in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--ipc=private",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
    ):
        assert flag in direct

    comma_ws = tmp_path / "bad,ws"
    comma_ws.mkdir()
    with pytest.raises(SandboxBoundaryError, match="comma"):
        build_container_invocation(
            comma_ws, "true", runtime_path="/usr/bin/docker", image="python:3.12-slim"
        )
    with pytest.raises(SandboxBoundaryError, match="STUDIO_SANDBOX_IMAGE"):
        build_container_invocation(
            workspace, "true", runtime_path="/usr/bin/docker", image="--privileged"
        )



# --- execution-mode gate: no path from "sandbox" to the host -------------------


def _run_untrusted(tmp_path: Path, command: str) -> tuple[object, object]:
    import asyncio
    import app as studio

    return asyncio.run(studio._execute_limited(tmp_path, command, timeout=10, max_output_bytes=4096))


def test_disabled_mode_refuses_without_running_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "disabled")
    marker = tmp_path / "EXECUTED"
    with pytest.raises(PermissionError, match="Code execution is disabled"):
        _run_untrusted(tmp_path, f"touch {marker}")
    assert not marker.exists()


def test_sandbox_mode_without_a_runtime_refuses_instead_of_falling_back_to_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release constraint is "unverified isolation stays unavailable".

    A missing container runtime is the moment where a convenient fallback would be
    tempting: the command is already admitted, the workspace exists, and spawning it
    on the host would work. It must not.
    """
    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_RUNTIME", "definitely-not-a-container-runtime")
    marker = tmp_path / "EXECUTED"
    with pytest.raises(RuntimeError, match="not installed or not on PATH"):
        _run_untrusted(tmp_path, f"touch {marker}")
    assert not marker.exists()


def test_sandbox_mode_never_takes_the_host_shell_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with a runnable argv, sandbox mode must go through the container shape."""
    import asyncio
    import sys

    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")
    monkeypatch.setattr(studio, "_sandbox_command", lambda *a, **k: [sys.executable, "-c", "pass"])

    def _host_shell_used(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("sandbox mode used the host shell")

    monkeypatch.setattr(studio.asyncio, "create_subprocess_shell", _host_shell_used)

    async def _go() -> tuple[object, object]:
        proc, container = await studio._start_code_process(tmp_path, "echo hi")
        await proc.communicate()
        return proc, container

    proc, container = asyncio.run(_go())
    assert container is not None, "sandbox mode must return the container name it created"
    assert getattr(proc, "returncode", None) == 0


def test_host_mode_without_the_explicit_opt_in_degrades_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent gates: the mode *and* the opt-in, both fail closed."""
    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", False)
    assert studio._execution_mode() == "disabled"

    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    assert studio._execution_mode() == "host"


def test_settings_reject_host_mode_without_the_opt_in() -> None:
    from app import ConfigurationError, load_settings

    with pytest.raises(ConfigurationError, match="STUDIO_ALLOW_HOST_EXECUTION"):
        load_settings({"STUDIO_EXECUTION_MODE": "host"}, base_dir=Path("."))
