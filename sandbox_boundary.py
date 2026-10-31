"""Filesystem confinement and container isolation policy primitives."""

from __future__ import annotations

import os
import shlex
import uuid
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


def build_container_invocation(
    resolved_workspace: Path,
    command: str,
    *,
    runtime_path: str,
    image: str,
    python_code: bool = False,
    container_name: str | None = None,
) -> list[str]:
    """Construct the canonical least-privilege, no-network container argv for untrusted code."""
    if not resolved_workspace.is_dir():
        raise SandboxBoundaryError("Sandbox workspace must be an existing directory.")
    if "," in str(resolved_workspace):
        raise SandboxBoundaryError("Sandbox workspace paths containing commas are unsupported by Docker --mount syntax.")
    if not image or not image.strip() or image.startswith("-"):
        raise SandboxBoundaryError("STUDIO_SANDBOX_IMAGE must be a configured, trusted image name or digest.")
    if not runtime_path or not runtime_path.strip():
        raise SandboxBoundaryError("Sandbox runtime executable path must be non-empty.")
    name = container_name or f"m5-studio-{uuid.uuid4().hex[:20]}"
    if not name.replace("-", "").replace("_", "").isalnum():
        raise SandboxBoundaryError("container_name contains unsafe characters.")
    argv = [
        runtime_path,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--pids-limit=128",
        "--memory=2g",
        "--cpus=2",
        "--ipc=private",
        "--ulimit",
        "nofile=1024:1024",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
        "--mount",
        f"type=bind,source={resolved_workspace},target=/workspace",
        "--workdir",
        "/workspace",
        "--entrypoint",
        "/bin/sh",
    ]
    if os.name == "posix" and hasattr(os, "getuid") and hasattr(os, "getgid"):
        argv.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    argv.append(image)
    shell_command = f"exec python3 -c {shlex.quote(command)}" if python_code else command
    argv.extend(["-lc", shell_command])
    return argv

