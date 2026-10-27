from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_boundary import (
    SandboxBoundaryError,
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
