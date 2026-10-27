"""Filesystem confinement and container isolation policy primitives."""

from __future__ import annotations

import os
from pathlib import Path


class SandboxBoundaryError(ValueError):
    """Raised when a workspace or execution request crosses the boundary."""


SECRET_NAMES = frozenset({
    ".env",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "token",
    "api-token",
})


def confined_path(root: Path, raw_path: str) -> Path:
    """Resolve a path and reject traversal, sibling-prefix, and symlink escapes."""
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise SandboxBoundaryError("path must be a non-empty string without NUL characters.")
    workspace = root.expanduser().resolve()
    lexical = Path(raw_path)
    if not lexical.is_absolute():
        current = workspace
        for part in lexical.parts:
            if part in ("", "."):
                continue
            if part == "..":
                current = current.parent
                continue
            current = current / part
            if current.is_symlink():
                resolved = current.resolve()
                try:
                    resolved.relative_to(workspace)
                except ValueError as exc:
                    raise SandboxBoundaryError("symlink escapes the workspace boundary.") from exc
    candidate = (workspace / raw_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise SandboxBoundaryError("path escapes the workspace boundary.") from exc
    return candidate


def validate_workspace(root: Path, *, max_entries: int = 100_000) -> Path:
    """Validate a real directory and reject known secret-bearing names."""
    workspace = root.expanduser()
    if workspace.is_symlink() or not workspace.is_dir():
        raise SandboxBoundaryError("workspace must be a real directory.")
    resolved = workspace.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise SandboxBoundaryError("workspace is too broad for sandbox execution.")
    entries = 0
    for directory, directories, files in os.walk(resolved, followlinks=False):
        entries += len(directories) + len(files)
        if entries > max_entries:
            raise SandboxBoundaryError("workspace exceeds the entry scan budget.")
        for name in (*directories, *files):
            if name.lower() in SECRET_NAMES:
                raise SandboxBoundaryError("workspace contains a known secret-bearing filename.")
    return resolved


def sandbox_command(
    workspace: Path,
    command: str,
    *,
    image: str,
    runtime: str = "docker",
    container_name: str | None = None,
) -> list[str]:
    """Build a defense-in-depth command without enabling network or privileges."""
    if not command.strip() or not image.strip():
        raise SandboxBoundaryError("command and image must be non-empty.")
    if runtime not in {"docker", "podman"}:
        raise SandboxBoundaryError("runtime must be docker or podman.")
    root = validate_workspace(workspace)
    args = [
        runtime,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--memory=2g",
        "--cpus=2",
    ]
    if container_name:
        if not container_name.replace("-", "").replace("_", "").isalnum():
            raise SandboxBoundaryError("container_name contains unsafe characters.")
        args.extend(["--name", container_name])
    args.extend(["--mount", f"type=bind,src={root},dst=/workspace,rw", "--workdir", "/workspace", image, "sh", "-lc", command])
    return args
