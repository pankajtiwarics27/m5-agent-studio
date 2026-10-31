"""Filesystem confinement, container isolation primitives, security dependency tracking,
incident containment, and fallback/restoration qualification (AS-028, M5-P3-02, M5-P3-06).

Stdlib-only sibling module (plus ``process_supervision`` which precedes it in
``scripts/build_studio.py`` ``INLINED_MODULES``). Private module-level helpers use the
``_sb_`` / ``_SB_`` prefix so standalone ``studio.py`` inlining stays collision-free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import threading
import time
from typing import Any
import uuid

import process_supervision


class SandboxBoundaryError(ValueError):
    """Raised when a workspace or execution request crosses the boundary."""


class SecurityIncidentError(RuntimeError):
    """Base error for security incident containment and qualification failures."""


class SecurityIncidentIntegrityError(SecurityIncidentError):
    """Raised when durable security-incident / qualification state is corrupt or a symlink."""


SECRET_NAMES = frozenset({
    ".env",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "token",
    "api-token",
})

SB_INCIDENT_SCHEMA_VERSION = "m5-security-incidents-v1"
MAX_CONFIG_GENERATIONS = 100
MAX_SECURITY_INCIDENTS = 200

KNOWN_SECURITY_GUARANTEES: frozenset[str] = frozenset(
    {
        "container_filesystem_isolation",
        "container_network_isolation",
        "container_privilege_containment",
        "container_resource_limits",
        "container_process_lifetime",
        "workspace_confinement",
        "secret_and_vcs_protection",
        "mcp_transport_boundary",
    }
)

CONTAINER_ISOLATION_GUARANTEES: frozenset[str] = frozenset(
    {
        "container_filesystem_isolation",
        "container_network_isolation",
        "container_privilege_containment",
        "container_resource_limits",
        "container_process_lifetime",
    }
)

SHARED_ENGINE_GUARANTEES: frozenset[str] = frozenset(
    {
        "workspace_confinement",
        "secret_and_vcs_protection",
        "mcp_transport_boundary",
    }
)

REQUIRED_CONTAINER_HARDENING_FLAGS: tuple[str, ...] = (
    "--pull=never",
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=128",
    "--memory=2g",
    "--cpus=2",
    "--ipc=private",
    "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m",
)

QUALIFICATION_CHECK_GUARANTEES: dict[str, tuple[str, ...]] = {
    "p3.runtime.cli": ("container_filesystem_isolation", "container_process_lifetime"),
    "p3.runtime.image": ("container_filesystem_isolation",),
    "p3.positive.execute": ("container_filesystem_isolation",),
    "p3.adversarial.escape_matrix": (
        "container_filesystem_isolation",
        "container_network_isolation",
        "container_privilege_containment",
        "workspace_confinement",
    ),
    "p3.adversarial.credentials": (
        "container_filesystem_isolation",
        "secret_and_vcs_protection",
    ),
    "p3.adversarial.memory_limit": ("container_resource_limits",),
    "p3.adversarial.descendants": ("container_process_lifetime",),
    "p3.product.argv": (
        "container_filesystem_isolation",
        "container_network_isolation",
        "container_privilege_containment",
        "container_resource_limits",
    ),
    "p3.product.argv_parity": (
        "container_filesystem_isolation",
        "container_network_isolation",
        "container_privilege_containment",
        "container_resource_limits",
    ),
    "p3.product.no_host_fallback": ("container_privilege_containment",),
    "p3.live.isolation_smoke": (
        "container_filesystem_isolation",
        "container_network_isolation",
        "container_privilege_containment",
    ),
    "p3.workspace.confinement": ("workspace_confinement",),
    "p3.secrets.hygiene": ("secret_and_vcs_protection",),
    "p3.mcp.boundary": ("mcp_transport_boundary",),
}

_SB_BACKEND_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SB_DEPENDENCY_RE = re.compile(r"^dep:[A-Za-z0-9_.:/@+-]{1,200}$")
_SB_FORBIDDEN_BACKEND_IDS: frozenset[str] = frozenset(
    {"host", "unsandboxed", "disabled", "none", "local_host", "raw_host"}
)


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
    for _directory, directories, files in os.walk(resolved, followlinks=False):
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
    args.extend(
        [
            "--mount",
            f"type=bind,src={root},dst=/workspace,rw",
            "--workdir",
            "/workspace",
            image,
            "sh",
            "-lc",
            command,
        ]
    )
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
        raise SandboxBoundaryError(
            "Sandbox workspace paths containing commas are unsupported by Docker --mount syntax."
        )
    if not image or not image.strip() or image.startswith("-"):
        raise SandboxBoundaryError(
            "STUDIO_SANDBOX_IMAGE must be a configured, trusted image name or digest."
        )
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


# ---------------------------------------------------------------------------
# M5-P3-06: Security dependencies, configuration generations, incident
# suspension, fallback qualification, and requalification before restoration
# ---------------------------------------------------------------------------


def _sb_normalize_dep_token(raw: str) -> str:
    clean = str(raw or "").strip()
    if not clean:
        raise SandboxBoundaryError("Security dependency identifier must be non-empty.")
    if not clean.startswith("dep:"):
        clean = f"dep:custom:{clean}"
    if not _SB_DEPENDENCY_RE.match(clean):
        raise SandboxBoundaryError(f"Invalid security dependency identifier: {raw!r}.")
    return clean


def _sb_normalize_guarantees(raw: Sequence[str], *, allow_empty: bool = False) -> tuple[str, ...]:
    cleaned: list[str] = []
    for item in raw:
        g = str(item or "").strip()
        if not g:
            continue
        if g not in KNOWN_SECURITY_GUARANTEES:
            raise SandboxBoundaryError(
                f"Unknown security guarantee {g!r}; expected one of {sorted(KNOWN_SECURITY_GUARANTEES)}."
            )
        if g not in cleaned:
            cleaned.append(g)
    if not cleaned and not allow_empty:
        raise SandboxBoundaryError("At least one security guarantee must be specified.")
    return tuple(sorted(cleaned))


def _sb_parse_iso_or_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        txt = value.strip()
        with contextlib.suppress(ValueError):
            return float(txt)
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00"):
            try:
                import calendar

                parsed = time.strptime(txt, fmt)
                return float(calendar.timegm(parsed))
            except ValueError:
                continue
    return None


@dataclass(frozen=True, slots=True)
class HardeningInspectionResult:
    """Result of checking a constructed container invocation against mandatory invariants."""

    valid: bool
    canonical_flags: tuple[str, ...]
    regressions: tuple[str, ...]
    failed_guarantees: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "canonical_flags": list(self.canonical_flags),
            "regressions": list(self.regressions),
            "failed_guarantees": list(self.failed_guarantees),
        }


def inspect_container_argv_hardening(argv: Sequence[str]) -> HardeningInspectionResult:
    """Inspect a container CLI ``argv`` and map any regression to failed guarantees."""
    tokens = [str(tok) for tok in argv]
    regressions: list[str] = []
    failed: set[str] = set()
    canonical: list[str] = []

    if len(tokens) < 3 or tokens[1] != "run":
        regressions.append("missing_container_run_subcommand")
        failed.update(CONTAINER_ISOLATION_GUARANTEES)
        return HardeningInspectionResult(
            valid=False,
            canonical_flags=(),
            regressions=tuple(regressions),
            failed_guarantees=tuple(sorted(failed)),
        )

    # Extract option portion before image/entrypoint payload (`-lc`).
    end_idx = tokens.index("-lc") if "-lc" in tokens else len(tokens)
    opt_tokens = tokens[2:end_idx]

    has_pull_never = False
    has_net_none = False
    has_read_only = False
    has_cap_drop_all = False
    has_no_new_privs = False
    has_pids_limit = False
    has_memory_limit = False
    has_cpus_limit = False
    has_ipc_private = False
    has_noexec_tmpfs = False
    bind_mounts: list[str] = []

    idx = 0
    while idx < len(opt_tokens):
        tok = opt_tokens[idx]
        nxt = opt_tokens[idx + 1] if idx + 1 < len(opt_tokens) else ""

        if tok == "--privileged" or tok.startswith("--privileged=true"):
            regressions.append("privileged_container_enabled")
            failed.add("container_privilege_containment")
        elif tok in {"--network=host", "--net=host"} or (tok in {"--network", "--net"} and nxt == "host"):
            regressions.append("host_network_namespace_enabled")
            failed.add("container_network_isolation")
        elif tok in {"--pid=host", "--ipc=host", "--uts=host", "--userns=host"}:
            regressions.append(f"host_namespace_shared:{tok}")
            failed.update({"container_privilege_containment", "container_process_lifetime"})
        elif tok == "--cap-add" or tok.startswith("--cap-add="):
            regressions.append("capability_added")
            failed.add("container_privilege_containment")

        if tok == "--pull=never" or (tok == "--pull" and nxt == "never"):
            has_pull_never = True
            canonical.append("--pull=never")
        if tok in {"--network=none", "--net=none"} or (tok in {"--network", "--net"} and nxt == "none"):
            has_net_none = True
            canonical.append("--network=none")
        if tok == "--read-only":
            has_read_only = True
            canonical.append("--read-only")
        if tok.upper() == "--CAP-DROP=ALL" or (tok == "--cap-drop" and nxt.upper() == "ALL"):
            has_cap_drop_all = True
            canonical.append("--cap-drop=ALL")
        if tok == "--security-opt=no-new-privileges" or (
            tok == "--security-opt" and nxt == "no-new-privileges"
        ):
            has_no_new_privs = True
            canonical.append("--security-opt=no-new-privileges")
        if tok.startswith("--pids-limit="):
            has_pids_limit = True
            canonical.append(tok)
        elif tok == "--pids-limit" and nxt:
            has_pids_limit = True
            canonical.append(f"--pids-limit={nxt}")
        if tok.startswith("--memory="):
            has_memory_limit = True
            canonical.append(tok)
        elif tok == "--memory" and nxt:
            has_memory_limit = True
            canonical.append(f"--memory={nxt}")
        if tok.startswith("--cpus="):
            has_cpus_limit = True
            canonical.append(tok)
        elif tok == "--cpus" and nxt:
            has_cpus_limit = True
            canonical.append(f"--cpus={nxt}")
        if tok == "--ipc=private" or (tok == "--ipc" and nxt == "private"):
            has_ipc_private = True
            canonical.append("--ipc=private")

        if tok == "--tmpfs" and nxt:
            if nxt.startswith("/tmp:") and all(opt in nxt for opt in ("noexec", "nosuid", "nodev")):
                has_noexec_tmpfs = True
                canonical.append(f"--tmpfs={nxt}")
            idx += 2
            continue
        if tok.startswith("--tmpfs="):
            spec = tok.partition("=")[2]
            if spec.startswith("/tmp:") and all(opt in spec for opt in ("noexec", "nosuid", "nodev")):
                has_noexec_tmpfs = True
                canonical.append(tok)

        if tok in {"--mount", "-v", "--volume"} and nxt:
            bind_mounts.append(nxt)
            if "docker.sock" in nxt or "podman.sock" in nxt:
                regressions.append("runtime_socket_mounted")
                failed.update({"container_filesystem_isolation", "container_privilege_containment"})
            idx += 2
            continue
        if tok.startswith("--mount=") or tok.startswith("--volume="):
            mnt_spec = tok.partition("=")[2]
            bind_mounts.append(mnt_spec)
            if "docker.sock" in mnt_spec or "podman.sock" in mnt_spec:
                regressions.append("runtime_socket_mounted")
                failed.update({"container_filesystem_isolation", "container_privilege_containment"})

        idx += 1

    if not has_pull_never:
        regressions.append("missing_flag:--pull=never")
        failed.add("container_filesystem_isolation")
    if not has_net_none:
        regressions.append("missing_flag:--network=none")
        failed.add("container_network_isolation")
    if not has_read_only:
        regressions.append("missing_flag:--read-only")
        failed.add("container_filesystem_isolation")
    if not has_cap_drop_all:
        regressions.append("missing_flag:--cap-drop=ALL")
        failed.add("container_privilege_containment")
    if not has_no_new_privs:
        regressions.append("missing_flag:--security-opt=no-new-privileges")
        failed.add("container_privilege_containment")
    if not has_pids_limit:
        regressions.append("missing_flag:--pids-limit")
        failed.update({"container_resource_limits", "container_process_lifetime"})
    if not has_memory_limit:
        regressions.append("missing_flag:--memory")
        failed.add("container_resource_limits")
    if not has_cpus_limit:
        regressions.append("missing_flag:--cpus")
        failed.add("container_resource_limits")
    if not has_ipc_private:
        regressions.append("missing_flag:--ipc=private")
        failed.add("container_privilege_containment")
    if not has_noexec_tmpfs:
        regressions.append("missing_flag:--tmpfs=/tmp:noexec,nosuid,nodev")
        failed.update({"container_filesystem_isolation", "container_resource_limits"})
    if len(bind_mounts) != 1 or ("target=/workspace" not in bind_mounts[0] and "dst=/workspace" not in bind_mounts[0]):
        regressions.append(f"invalid_workspace_bind_mounts:{len(bind_mounts)}")
        failed.update({"container_filesystem_isolation", "workspace_confinement"})

    dedup_flags = tuple(sorted(set(canonical)))
    return HardeningInspectionResult(
        valid=(len(regressions) == 0 and len(failed) == 0),
        canonical_flags=dedup_flags,
        regressions=tuple(regressions),
        failed_guarantees=tuple(sorted(failed)),
    )


@dataclass(frozen=True, slots=True)
class SandboxBackendProfile:
    """Concrete container backend descriptor and its shared security dependencies."""

    backend_id: str
    runtime: str
    runtime_path: str
    image: str
    image_digest: str = ""
    runtime_version: str = ""
    oci_runtime: str = "runc"
    kernel_boundary: str = "host_linux_kernel"
    network_driver: str = "netns_none"
    cgroup_driver: str = "cgroup_v2"
    mount_driver: str = "bindfs"
    hardening_flags: tuple[str, ...] = REQUIRED_CONTAINER_HARDENING_FLAGS
    extra_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        bid = str(self.backend_id or "").strip().lower()
        if bid in _SB_FORBIDDEN_BACKEND_IDS:
            raise SandboxBoundaryError(
                f"Unsandboxed or disabled execution mode {bid!r} cannot be registered as a sandbox backend."
            )
        if not _SB_BACKEND_ID_RE.match(bid):
            raise SandboxBoundaryError(f"Invalid sandbox backend_id: {self.backend_id!r}.")
        rt = str(self.runtime or "").strip().lower()
        if rt not in {"docker", "podman"}:
            raise SandboxBoundaryError(
                f"Sandbox backend runtime must be 'docker' or 'podman', got {self.runtime!r}."
            )
        rpath = str(self.runtime_path or "").strip()
        if not rpath:
            raise SandboxBoundaryError("Sandbox backend runtime_path must be non-empty.")
        img = str(self.image or "").strip()
        if not img or img.startswith("-"):
            raise SandboxBoundaryError("Sandbox backend image must be a non-empty trusted image reference.")
        norm_extra = tuple(sorted({_sb_normalize_dep_token(d) for d in self.extra_dependencies if str(d).strip()}))
        norm_flags = tuple(sorted({str(f).strip() for f in self.hardening_flags if str(f).strip()}))

        object.__setattr__(self, "backend_id", bid)
        object.__setattr__(self, "runtime", rt)
        object.__setattr__(self, "runtime_path", rpath)
        object.__setattr__(self, "image", img)
        object.__setattr__(self, "image_digest", str(self.image_digest or "").strip())
        object.__setattr__(self, "runtime_version", str(self.runtime_version or "").strip())
        object.__setattr__(self, "oci_runtime", str(self.oci_runtime or "runc").strip() or "runc")
        object.__setattr__(
            self,
            "kernel_boundary",
            str(self.kernel_boundary or "host_linux_kernel").strip() or "host_linux_kernel",
        )
        object.__setattr__(self, "network_driver", str(self.network_driver or "netns_none").strip() or "netns_none")
        object.__setattr__(self, "cgroup_driver", str(self.cgroup_driver or "cgroup_v2").strip() or "cgroup_v2")
        object.__setattr__(self, "mount_driver", str(self.mount_driver or "bindfs").strip() or "bindfs")
        object.__setattr__(self, "hardening_flags", norm_flags)
        object.__setattr__(self, "extra_dependencies", norm_extra)

    def security_dependencies(self) -> tuple[str, ...]:
        deps: set[str] = {
            f"dep:runtime:{self.runtime}",
            f"dep:oci_runtime:{self.oci_runtime}",
            f"dep:kernel:{self.kernel_boundary}",
            f"dep:netns:{self.network_driver}",
            f"dep:cgroup:{self.cgroup_driver}",
            f"dep:mount:{self.mount_driver}",
            f"dep:image_ref:{self.image}",
            "dep:workspace_confinement",
            "dep:secret_hygiene",
        }
        if self.image_digest:
            deps.add(f"dep:image:{self.image_digest}")
        else:
            deps.add(f"dep:image:{self.image}")
        deps.update(self.extra_dependencies)
        return tuple(sorted(deps))

    def compute_config_fingerprint(
        self,
        *,
        execution_mode: str = "sandbox",
        allow_host_execution: bool = False,
    ) -> str:
        payload = {
            "backend_id": self.backend_id,
            "runtime": self.runtime,
            "runtime_path": self.runtime_path,
            "runtime_version": self.runtime_version,
            "oci_runtime": self.oci_runtime,
            "kernel_boundary": self.kernel_boundary,
            "network_driver": self.network_driver,
            "cgroup_driver": self.cgroup_driver,
            "mount_driver": self.mount_driver,
            "image": self.image,
            "image_digest": self.image_digest,
            "hardening_flags": list(self.hardening_flags),
            "security_dependencies": list(self.security_dependencies()),
            "execution_mode": str(execution_mode or "disabled").strip().lower(),
            "allow_host_execution": bool(allow_host_execution),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "runtime": self.runtime,
            "runtime_path": self.runtime_path,
            "image": self.image,
            "image_digest": self.image_digest,
            "runtime_version": self.runtime_version,
            "oci_runtime": self.oci_runtime,
            "kernel_boundary": self.kernel_boundary,
            "network_driver": self.network_driver,
            "cgroup_driver": self.cgroup_driver,
            "mount_driver": self.mount_driver,
            "hardening_flags": list(self.hardening_flags),
            "extra_dependencies": list(self.extra_dependencies),
            "security_dependencies": list(self.security_dependencies()),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SandboxBackendProfile:
        if not isinstance(raw, Mapping):
            raise SecurityIncidentIntegrityError("SandboxBackendProfile must be a JSON object.")
        raw_flags = raw.get("hardening_flags")
        flags = (
            tuple(str(x) for x in raw_flags)
            if isinstance(raw_flags, (list, tuple))
            else REQUIRED_CONTAINER_HARDENING_FLAGS
        )
        raw_extra = raw.get("extra_dependencies")
        extra = tuple(str(x) for x in raw_extra) if isinstance(raw_extra, (list, tuple)) else ()
        return cls(
            backend_id=str(raw.get("backend_id") or ""),
            runtime=str(raw.get("runtime") or ""),
            runtime_path=str(raw.get("runtime_path") or ""),
            image=str(raw.get("image") or ""),
            image_digest=str(raw.get("image_digest") or ""),
            runtime_version=str(raw.get("runtime_version") or ""),
            oci_runtime=str(raw.get("oci_runtime") or "runc"),
            kernel_boundary=str(raw.get("kernel_boundary") or "host_linux_kernel"),
            network_driver=str(raw.get("network_driver") or "netns_none"),
            cgroup_driver=str(raw.get("cgroup_driver") or "cgroup_v2"),
            mount_driver=str(raw.get("mount_driver") or "bindfs"),
            hardening_flags=flags,
            extra_dependencies=extra,
        )


@dataclass(frozen=True, slots=True)
class ConfigurationGeneration:
    """Monotonic, content-addressed security configuration generation record (Task 1)."""

    generation_seq: int
    config_fingerprint: str
    backend_id: str
    execution_mode: str
    allow_host_execution: bool
    runtime: str
    runtime_path: str
    runtime_version: str
    image: str
    image_digest: str
    hardening_flags: tuple[str, ...]
    security_dependencies: tuple[str, ...]
    hardening_valid: bool
    hardening_regressions: tuple[str, ...]
    observed_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "generation_seq": self.generation_seq,
            "config_fingerprint": self.config_fingerprint,
            "backend_id": self.backend_id,
            "execution_mode": self.execution_mode,
            "allow_host_execution": self.allow_host_execution,
            "runtime": self.runtime,
            "runtime_path": self.runtime_path,
            "runtime_version": self.runtime_version,
            "image": self.image,
            "image_digest": self.image_digest,
            "hardening_flags": list(self.hardening_flags),
            "security_dependencies": list(self.security_dependencies),
            "hardening_valid": self.hardening_valid,
            "hardening_regressions": list(self.hardening_regressions),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ConfigurationGeneration:
        if not isinstance(raw, Mapping):
            raise SecurityIncidentIntegrityError("ConfigurationGeneration must be a mapping.")
        return cls(
            generation_seq=int(raw["generation_seq"]),
            config_fingerprint=str(raw["config_fingerprint"]),
            backend_id=str(raw["backend_id"]),
            execution_mode=str(raw.get("execution_mode") or "sandbox"),
            allow_host_execution=bool(raw.get("allow_host_execution", False)),
            runtime=str(raw.get("runtime") or ""),
            runtime_path=str(raw.get("runtime_path") or ""),
            runtime_version=str(raw.get("runtime_version") or ""),
            image=str(raw.get("image") or ""),
            image_digest=str(raw.get("image_digest") or ""),
            hardening_flags=tuple(str(x) for x in (raw.get("hardening_flags") or ())),
            security_dependencies=tuple(str(x) for x in (raw.get("security_dependencies") or ())),
            hardening_valid=bool(raw.get("hardening_valid", True)),
            hardening_regressions=tuple(str(x) for x in (raw.get("hardening_regressions") or ())),
            observed_at=float(raw.get("observed_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class BackendQualificationRecord:
    """Verified qualification record bound to a specific backend and config generation."""

    qualification_id: str
    backend_id: str
    generation_seq: int
    config_fingerprint: str
    verdict: str
    covered_guarantees: tuple[str, ...]
    passed_checks: tuple[str, ...]
    verified_disjoint_from: tuple[str, ...]
    qualified_at: float
    report_generated_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "qualification_id": self.qualification_id,
            "backend_id": self.backend_id,
            "generation_seq": self.generation_seq,
            "config_fingerprint": self.config_fingerprint,
            "verdict": self.verdict,
            "covered_guarantees": list(self.covered_guarantees),
            "passed_checks": list(self.passed_checks),
            "verified_disjoint_from": list(self.verified_disjoint_from),
            "qualified_at": self.qualified_at,
            "report_generated_at": self.report_generated_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BackendQualificationRecord:
        if not isinstance(raw, Mapping):
            raise SecurityIncidentIntegrityError("BackendQualificationRecord must be a mapping.")
        return cls(
            qualification_id=str(raw["qualification_id"]),
            backend_id=str(raw["backend_id"]),
            generation_seq=int(raw["generation_seq"]),
            config_fingerprint=str(raw["config_fingerprint"]),
            verdict=str(raw["verdict"]),
            covered_guarantees=tuple(str(x) for x in (raw.get("covered_guarantees") or ())),
            passed_checks=tuple(str(x) for x in (raw.get("passed_checks") or ())),
            verified_disjoint_from=tuple(str(x) for x in (raw.get("verified_disjoint_from") or ())),
            qualified_at=float(raw.get("qualified_at") or 0.0),
            report_generated_at=float(raw.get("report_generated_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class SecurityIncidentRecord:
    """Durable security incident tracking failed guarantees, dependencies, and holds."""

    incident_id: str
    status: str  # "active" | "mitigated_via_fallback" | "restored"
    severity: str
    source: str
    reason: str
    failed_guarantees: tuple[str, ...]
    failed_dependencies: tuple[str, ...]
    affected_backends: tuple[str, ...]
    scope_session_id: str
    scope_workspace_root: str
    detected_generation_seq: int
    detected_config_fingerprint: str
    barrier_hold_ids: tuple[str, ...]
    fallback_backend_id: str | None = None
    fallback_qualification_id: str | None = None
    restoration_qualification_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    restored_at: float | None = None
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        ws_norm = ""
        if self.scope_workspace_root and str(self.scope_workspace_root).strip():
            with contextlib.suppress(OSError):
                ws_norm = str(Path(self.scope_workspace_root).expanduser().resolve())
            if not ws_norm:
                ws_norm = str(self.scope_workspace_root).strip()
        object.__setattr__(self, "scope_workspace_root", ws_norm)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "status": self.status,
            "severity": self.severity,
            "source": self.source,
            "reason": self.reason,
            "failed_guarantees": list(self.failed_guarantees),
            "failed_dependencies": list(self.failed_dependencies),
            "affected_backends": list(self.affected_backends),
            "scope_session_id": self.scope_session_id,
            "scope_workspace_root": self.scope_workspace_root,
            "detected_generation_seq": self.detected_generation_seq,
            "detected_config_fingerprint": self.detected_config_fingerprint,
            "barrier_hold_ids": list(self.barrier_hold_ids),
            "fallback_backend_id": self.fallback_backend_id,
            "fallback_qualification_id": self.fallback_qualification_id,
            "restoration_qualification_id": self.restoration_qualification_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "restored_at": self.restored_at,
            "details": dict(self.details) if isinstance(self.details, Mapping) else {},
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SecurityIncidentRecord:
        if not isinstance(raw, Mapping):
            raise SecurityIncidentIntegrityError("SecurityIncidentRecord must be a mapping.")
        status = str(raw.get("status") or "")
        if status not in {"active", "mitigated_via_fallback", "restored"}:
            raise SecurityIncidentIntegrityError(f"Invalid SecurityIncidentRecord.status: {status!r}.")
        return cls(
            incident_id=str(raw["incident_id"]),
            status=status,
            severity=str(raw.get("severity") or "critical"),
            source=str(raw.get("source") or "operator"),
            reason=str(raw.get("reason") or "security_incident"),
            failed_guarantees=_sb_normalize_guarantees(
                tuple(str(x) for x in (raw.get("failed_guarantees") or ()))
            ),
            failed_dependencies=tuple(
                _sb_normalize_dep_token(str(x)) for x in (raw.get("failed_dependencies") or ())
            ),
            affected_backends=tuple(str(x) for x in (raw.get("affected_backends") or ())),
            scope_session_id=str(raw.get("scope_session_id") or ""),
            scope_workspace_root=str(raw.get("scope_workspace_root") or ""),
            detected_generation_seq=int(raw.get("detected_generation_seq") or 0),
            detected_config_fingerprint=str(raw.get("detected_config_fingerprint") or ""),
            barrier_hold_ids=tuple(str(x) for x in (raw.get("barrier_hold_ids") or ())),
            fallback_backend_id=(
                str(raw["fallback_backend_id"]) if raw.get("fallback_backend_id") else None
            ),
            fallback_qualification_id=(
                str(raw["fallback_qualification_id"]) if raw.get("fallback_qualification_id") else None
            ),
            restoration_qualification_id=(
                str(raw["restoration_qualification_id"])
                if raw.get("restoration_qualification_id")
                else None
            ),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            restored_at=(
                float(raw["restored_at"]) if raw.get("restored_at") is not None else None
            ),
            details=dict(raw["details"]) if isinstance(raw.get("details"), Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class SecurityAdmissionDecision:
    """Decision returned when checking whether an execution profile may proceed."""

    allowed: bool
    error_code: str = ""
    reason: str = ""
    incident_id: str | None = None
    suspended_guarantees: tuple[str, ...] = ()
    failed_dependencies: tuple[str, ...] = ()
    active_backend: SandboxBackendProfile | None = None
    container_qualified: bool = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "error_code": self.error_code,
            "reason": self.reason,
            "incident_id": self.incident_id,
            "suspended_guarantees": list(self.suspended_guarantees),
            "failed_dependencies": list(self.failed_dependencies),
            "active_backend": self.active_backend.to_mapping() if self.active_backend else None,
            "container_qualified": self.container_qualified,
        }


@dataclass(frozen=True, slots=True)
class QualificationOutcome:
    """Outcome of qualifying a fallback backend or requalifying before restoration."""

    ok: bool
    error_code: str = ""
    reason: str = ""
    incident: SecurityIncidentRecord | None = None
    qualification: BackendQualificationRecord | None = None
    shared_dependencies: tuple[str, ...] = ()
    missing_guarantees: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_code": self.error_code,
            "reason": self.reason,
            "incident": self.incident.to_mapping() if self.incident else None,
            "qualification": self.qualification.to_mapping() if self.qualification else None,
            "shared_dependencies": list(self.shared_dependencies),
            "missing_guarantees": list(self.missing_guarantees),
        }


def _sb_evaluate_qualification_evidence(
    report: Mapping[str, Any] | None,
    *,
    backend: SandboxBackendProfile,
    expected_fingerprint: str,
    required_guarantees: Sequence[str],
    forbidden_dependencies: Sequence[str] = (),
    min_timestamp: float | None = None,
) -> tuple[bool, str, str, tuple[str, ...], tuple[str, ...], float]:
    """Validate qualification report bindings, verdict, check coverage, and freshness."""
    if not isinstance(report, Mapping):
        return (
            False,
            "requalification_required",
            "Qualification evidence report is required and must be a JSON object.",
            (),
            tuple(sorted(required_guarantees)),
            0.0,
        )

    # Extract verdict from `verdicts["M5-P3-01"]["verdict"]` or top-level `verdict`.
    verdict = ""
    verdicts_block = report.get("verdicts")
    if isinstance(verdicts_block, Mapping):
        p3_block = verdicts_block.get("M5-P3-01")
        if isinstance(p3_block, Mapping):
            verdict = str(p3_block.get("verdict") or "").strip().lower()
            failed_list = p3_block.get("failed")
            if isinstance(failed_list, list) and failed_list:
                return (
                    False,
                    "qualification_failed",
                    f"Qualification report contains failed check(s): {failed_list}.",
                    (),
                    (),
                    0.0,
                )
    if not verdict:
        verdict = str(report.get("verdict") or "").strip().lower()

    if verdict != "qualified":
        return (
            False,
            "qualification_failed",
            f"Qualification verdict must be 'qualified', got {verdict!r} (unverified guarantees stay unavailable).",
            (),
            (),
            0.0,
        )

    # Check sandbox identity binding against `backend`.
    sb_block = report.get("sandbox")
    if not isinstance(sb_block, Mapping):
        return (
            False,
            "qualification_generation_mismatch",
            "Qualification report is missing the `sandbox` identity binding block.",
            (),
            (),
            0.0,
        )

    rep_runtime = str(sb_block.get("runtime") or "").strip().lower()
    rep_image = str(sb_block.get("image") or "").strip()
    rep_digest = str(sb_block.get("image_digest") or "").strip()
    rep_fp = str(report.get("config_fingerprint") or sb_block.get("config_fingerprint") or "").strip()

    if rep_runtime != backend.runtime:
        return (
            False,
            "qualification_generation_mismatch",
            f"Qualification report runtime {rep_runtime!r} does not match backend runtime {backend.runtime!r}.",
            (),
            (),
            0.0,
        )
    if rep_image != backend.image:
        return (
            False,
            "qualification_generation_mismatch",
            f"Qualification report image {rep_image!r} does not match backend image {backend.image!r}.",
            (),
            (),
            0.0,
        )
    if backend.image_digest and rep_digest and rep_digest != backend.image_digest:
        return (
            False,
            "qualification_generation_mismatch",
            f"Qualification report image_digest {rep_digest!r} does not match backend digest {backend.image_digest!r}.",
            (),
            (),
            0.0,
        )
    if rep_fp and rep_fp != expected_fingerprint:
        return (
            False,
            "qualification_generation_mismatch",
            f"Qualification report config_fingerprint {rep_fp!r} does not match current generation {expected_fingerprint!r}.",
            (),
            (),
            0.0,
        )

    # Ensure report metadata itself did not exercise any forbidden incident dependencies.
    if forbidden_dependencies:
        report_deps: set[str] = {f"dep:runtime:{rep_runtime}", f"dep:image_ref:{rep_image}"}
        if rep_digest:
            report_deps.add(f"dep:image:{rep_digest}")
        else:
            report_deps.add(f"dep:image:{rep_image}")
        if sb_block.get("oci_runtime"):
            report_deps.add(f"dep:oci_runtime:{str(sb_block['oci_runtime']).strip()}")
        if sb_block.get("kernel_boundary"):
            report_deps.add(f"dep:kernel:{str(sb_block['kernel_boundary']).strip()}")
        leaked_deps = sorted(report_deps & set(forbidden_dependencies))
        if leaked_deps:
            return (
                False,
                "shared_failed_dependency",
                f"Qualification report binds to failed incident dependencies: {leaked_deps}.",
                (),
                (),
                0.0,
            )

    # Timestamp freshness check (reject replay of pre-incident qualification artifacts).
    rep_ts = (
        _sb_parse_iso_or_epoch(report.get("generated_at_epoch"))
        or _sb_parse_iso_or_epoch(report.get("qualified_at"))
        or _sb_parse_iso_or_epoch(report.get("generated_at_utc"))
    )
    if rep_ts is None:
        rep_ts = time.time()
    if min_timestamp is not None and rep_ts + 0.001 < min_timestamp:
        return (
            False,
            "stale_pre_incident_qualification",
            (
                f"Qualification evidence timestamp ({rep_ts:.3f}) predates the security incident "
                f"({min_timestamp:.3f}); fresh post-incident requalification is required."
            ),
            (),
            (),
            rep_ts,
        )

    raw_checks = report.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        return (
            False,
            "qualification_failed",
            "Qualification report contains no executed `checks` entries.",
            (),
            tuple(sorted(required_guarantees)),
            rep_ts,
        )

    passed_ids: list[str] = []
    covered: set[str] = set()
    for item in raw_checks:
        if not isinstance(item, Mapping):
            continue
        cid = str(item.get("check_id") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        req = bool(item.get("required", True))
        if status == "fail":
            return (
                False,
                "qualification_failed",
                f"Qualification check `{cid}` failed.",
                (),
                (),
                rep_ts,
            )
        if req and status != "pass":
            return (
                False,
                "qualification_failed",
                f"Required qualification check `{cid}` did not pass (status={status!r}).",
                (),
                (),
                rep_ts,
            )
        if status == "pass" and cid:
            passed_ids.append(cid)
            covered.update(QUALIFICATION_CHECK_GUARANTEES.get(cid, ()))
            explicit_guarantees = item.get("guarantees")
            if isinstance(explicit_guarantees, (list, tuple)):
                for eg in explicit_guarantees:
                    eg_str = str(eg).strip()
                    if eg_str in KNOWN_SECURITY_GUARANTEES:
                        covered.add(eg_str)

    missing = tuple(sorted(set(required_guarantees) - covered))
    if missing:
        return (
            False,
            "qualification_incomplete_coverage",
            f"Qualification report did not verify required incident guarantees: {list(missing)}.",
            tuple(passed_ids),
            missing,
            rep_ts,
        )

    return (
        True,
        "",
        "",
        tuple(passed_ids),
        tuple(sorted(covered)),
        rep_ts,
    )


class SecurityIncidentStore:
    """Crash-durable store for security configuration generations, incidents, and qualifications.

    Persists atomically in ``<store_dir>/security_incidents/state.json`` with ``0700`` directory
    permissions, ``0600`` file permissions, ``O_NOFOLLOW`` symlink refusal, temp-file ``os.fsync``,
    ``os.replace``, and parent-directory ``os.fsync`` — matching ``process_supervision.ContainmentStore``.
    """

    def __init__(
        self,
        store_dir: Path | str,
        *,
        containment_store: process_supervision.ContainmentStore | None = None,
    ) -> None:
        base = Path(store_dir).expanduser()
        self._base_dir = base
        self._dir = base / "security_incidents"
        self._state_path = self._dir / "state.json"
        self._lock = threading.RLock()
        self._containment_store = containment_store or process_supervision.ContainmentStore(base)

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def containment_store(self) -> process_supervision.ContainmentStore:
        return self._containment_store

    def _ensure_dir_unlocked(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        if self._dir.is_symlink():
            raise SecurityIncidentIntegrityError(
                f"Security incident directory {self._dir} must not be a symlink."
            )
        self._dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self._dir, 0o700)

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": SB_INCIDENT_SCHEMA_VERSION,
            "current_generation_seq": 0,
            "active_backend_id": "",
            "backends": {},
            "generations": [],
            "qualifications": {},
            "incidents": {},
        }

    def _read_state_unlocked(self) -> dict[str, Any]:
        if os.path.lexists(self._state_path):
            try:
                st = os.lstat(self._state_path)
            except OSError as exc:
                raise SecurityIncidentIntegrityError(
                    f"Cannot stat security incident state {self._state_path}: {exc}"
                ) from exc
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise SecurityIncidentIntegrityError(
                    f"Security incident state {self._state_path} must be a regular non-symlink file."
                )
        else:
            return self._empty_state()

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(self._state_path), flags)
            try:
                raw_bytes = os.read(fd, 16 * 1024 * 1024)
            finally:
                os.close(fd)
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecurityIncidentIntegrityError(
                f"Corrupt or unreadable security incident state {self._state_path}: {exc}"
            ) from exc

        if not isinstance(parsed, dict) or parsed.get("schema_version") != SB_INCIDENT_SCHEMA_VERSION:
            raise SecurityIncidentIntegrityError(
                f"Unsupported or tampered security incident schema in {self._state_path}."
            )
        for dict_key in ("backends", "qualifications", "incidents"):
            if not isinstance(parsed.get(dict_key), dict):
                raise SecurityIncidentIntegrityError(
                    f"Malformed `{dict_key}` map in security incident state."
                )
        if not isinstance(parsed.get("generations"), list):
            raise SecurityIncidentIntegrityError("Malformed `generations` list in security incident state.")
        # Validate nested records eagerly so corruption fails closed on read.
        for b_raw in parsed["backends"].values():
            SandboxBackendProfile.from_mapping(b_raw)
        for g_raw in parsed["generations"]:
            ConfigurationGeneration.from_mapping(g_raw)
        for q_raw in parsed["qualifications"].values():
            BackendQualificationRecord.from_mapping(q_raw)
        for i_raw in parsed["incidents"].values():
            SecurityIncidentRecord.from_mapping(i_raw)
        return parsed

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        self._ensure_dir_unlocked()
        if len(state["generations"]) > MAX_CONFIG_GENERATIONS:
            state["generations"] = state["generations"][-MAX_CONFIG_GENERATIONS:]
        if len(state["incidents"]) > MAX_SECURITY_INCIDENTS:
            # Never prune active or fallback-mitigated incidents; only prune oldest restored ones.
            restored_ids = sorted(
                (
                    (float(rec.get("updated_at") or 0.0), iid)
                    for iid, rec in state["incidents"].items()
                    if isinstance(rec, Mapping) and rec.get("status") == "restored"
                ),
                key=lambda pair: pair[0],
            )
            while len(state["incidents"]) > MAX_SECURITY_INCIDENTS and restored_ids:
                _, drop_id = restored_ids.pop(0)
                state["incidents"].pop(drop_id, None)

        payload = (
            json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
        ).encode("utf-8")
        tmp_path = self._dir / f".state.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:12]}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        except Exception:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        os.close(fd)
        os.replace(tmp_path, self._state_path)
        with contextlib.suppress(OSError):
            os.chmod(self._state_path, 0o600)
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        try:
            dir_fd = os.open(str(self._dir), dir_flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def _persist_incident_holds(
        self,
        incident: SecurityIncidentRecord,
        *,
        lingering_pids: Sequence[tuple[int, str]] = (),
    ) -> tuple[str, ...]:
        hold_id = f"hold-{incident.incident_id}"
        barriers: list[tuple[str, str]] = [
            (f"security_guarantee:{g}", "exclusive") for g in incident.failed_guarantees
        ]
        for bid in incident.affected_backends:
            barriers.append((f"security_backend:{bid}", "exclusive"))
        if incident.scope_workspace_root and (
            lingering_pids
            or bool(set(incident.failed_guarantees) - CONTAINER_ISOLATION_GUARANTEES)
        ):
            barriers.append((f"workspace:{incident.scope_workspace_root}", "exclusive"))

        hold = process_supervision.PersistedBarrierHold(
            hold_id=hold_id,
            session_id=incident.scope_session_id,
            task_id=(incident.scope_session_id or None),
            action_id=incident.incident_id,
            tool_name="security_incident_containment",
            footprint_scope="conservative_workspace",
            workspace_root=incident.scope_workspace_root,
            barriers=tuple(barriers),
            reason=f"security_incident:{incident.reason}",
            lingering_pids=tuple(lingering_pids),
        )
        self._containment_store.persist_barrier_hold(hold)
        return (hold_id,)

    def reconcile_persisted_holds(self) -> int:
        """Ensure every un-restored incident has its durable barrier hold in ``ContainmentStore``."""
        with self._lock:
            state = self._read_state_unlocked()
            existing_ids = {h.hold_id for h in self._containment_store.list_holds()}
            repaired = 0
            dirty = False
            for iid, raw in list(state["incidents"].items()):
                inc = SecurityIncidentRecord.from_mapping(raw)
                if inc.status == "restored":
                    continue
                expected_hold_id = f"hold-{inc.incident_id}"
                if expected_hold_id not in existing_ids or expected_hold_id not in inc.barrier_hold_ids:
                    hold_ids = self._persist_incident_holds(inc)
                    if hold_ids != inc.barrier_hold_ids:
                        updated = SecurityIncidentRecord(
                            incident_id=inc.incident_id,
                            status=inc.status,
                            severity=inc.severity,
                            source=inc.source,
                            reason=inc.reason,
                            failed_guarantees=inc.failed_guarantees,
                            failed_dependencies=inc.failed_dependencies,
                            affected_backends=inc.affected_backends,
                            scope_session_id=inc.scope_session_id,
                            scope_workspace_root=inc.scope_workspace_root,
                            detected_generation_seq=inc.detected_generation_seq,
                            detected_config_fingerprint=inc.detected_config_fingerprint,
                            barrier_hold_ids=hold_ids,
                            fallback_backend_id=inc.fallback_backend_id,
                            fallback_qualification_id=inc.fallback_qualification_id,
                            restoration_qualification_id=inc.restoration_qualification_id,
                            created_at=inc.created_at,
                            updated_at=time.time(),
                            restored_at=inc.restored_at,
                            details=inc.details,
                        )
                        state["incidents"][iid] = updated.to_mapping()
                        dirty = True
                    repaired += 1
            if dirty:
                self._write_state_unlocked(state)
            return repaired

    def observe_configuration(
        self,
        backend: SandboxBackendProfile,
        *,
        execution_mode: str = "sandbox",
        allow_host_execution: bool = False,
        sample_argv: Sequence[str] | None = None,
    ) -> tuple[ConfigurationGeneration, SecurityIncidentRecord | None]:
        """Record a configuration generation if changed and detect hardening or drift regressions."""
        hardening_valid = True
        hardening_regressions: tuple[str, ...] = ()
        failed_from_argv: tuple[str, ...] = ()
        effective_flags = backend.hardening_flags

        if sample_argv is not None:
            inspected = inspect_container_argv_hardening(sample_argv)
            hardening_valid = inspected.valid
            hardening_regressions = inspected.regressions
            failed_from_argv = inspected.failed_guarantees
            if inspected.canonical_flags:
                effective_flags = inspected.canonical_flags
                backend = SandboxBackendProfile(
                    backend_id=backend.backend_id,
                    runtime=backend.runtime,
                    runtime_path=backend.runtime_path,
                    image=backend.image,
                    image_digest=backend.image_digest,
                    runtime_version=backend.runtime_version,
                    oci_runtime=backend.oci_runtime,
                    kernel_boundary=backend.kernel_boundary,
                    network_driver=backend.network_driver,
                    cgroup_driver=backend.cgroup_driver,
                    mount_driver=backend.mount_driver,
                    hardening_flags=effective_flags,
                    extra_dependencies=backend.extra_dependencies,
                )

        fp = backend.compute_config_fingerprint(
            execution_mode=execution_mode,
            allow_host_execution=allow_host_execution,
        )
        if not hardening_valid:
            # Include regression details in fingerprint so broken generations never collide with valid ones.
            reg_hash = hashlib.sha256(
                f"{fp}|{'|'.join(hardening_regressions)}".encode("utf-8")
            ).hexdigest()
            fp = f"sha256:{reg_hash}"

        now = time.time()
        with self._lock:
            state = self._read_state_unlocked()
            state["backends"][backend.backend_id] = backend.to_mapping()
            if not state["active_backend_id"]:
                state["active_backend_id"] = backend.backend_id

            existing_gen: ConfigurationGeneration | None = None
            for g_raw in reversed(state["generations"]):
                cand = ConfigurationGeneration.from_mapping(g_raw)
                if cand.backend_id == backend.backend_id:
                    existing_gen = cand
                    break

            dirty = True
            if existing_gen is not None and existing_gen.config_fingerprint == fp:
                gen = existing_gen
            else:
                next_seq = int(state["current_generation_seq"]) + 1
                state["current_generation_seq"] = next_seq
                gen = ConfigurationGeneration(
                    generation_seq=next_seq,
                    config_fingerprint=fp,
                    backend_id=backend.backend_id,
                    execution_mode=str(execution_mode or "disabled").strip().lower(),
                    allow_host_execution=bool(allow_host_execution),
                    runtime=backend.runtime,
                    runtime_path=backend.runtime_path,
                    runtime_version=backend.runtime_version,
                    image=backend.image,
                    image_digest=backend.image_digest,
                    hardening_flags=backend.hardening_flags,
                    security_dependencies=backend.security_dependencies(),
                    hardening_valid=hardening_valid,
                    hardening_regressions=hardening_regressions,
                    observed_at=now,
                )
                state["generations"].append(gen.to_mapping())

            triggered_incident: SecurityIncidentRecord | None = None

            # 1. Hardening regression -> immediate active security incident!
            if not hardening_valid:
                triggered_incident = self._open_or_reuse_incident_unlocked(
                    state,
                    source="regression_detector",
                    reason=f"Container hardening regression detected: {', '.join(hardening_regressions)}",
                    failed_guarantees=(failed_from_argv or tuple(sorted(CONTAINER_ISOLATION_GUARANTEES))),
                    failed_dependencies=(
                        f"dep:runtime:{backend.runtime}",
                        f"dep:oci_runtime:{backend.oci_runtime}",
                    ),
                    affected_backends=(backend.backend_id,),
                    generation_seq=gen.generation_seq,
                    config_fingerprint=gen.config_fingerprint,
                    details={"regressions": list(hardening_regressions)},
                )
            else:
                # 2. Configuration generation drift on a previously qualified backend -> invalidate & suspend!
                qual_raw = state["qualifications"].get(backend.backend_id)
                if isinstance(qual_raw, Mapping):
                    prev_qual = BackendQualificationRecord.from_mapping(qual_raw)
                    if prev_qual.config_fingerprint != gen.config_fingerprint:
                        state["qualifications"].pop(backend.backend_id, None)
                        # Only auto-suspend if there isn't already an open incident for this backend
                        # that the operator is actively remediating via `requalify_and_restore`.
                        already_suspended = any(
                            SecurityIncidentRecord.from_mapping(ir).status != "restored"
                            and backend.backend_id
                            in SecurityIncidentRecord.from_mapping(ir).affected_backends
                            for ir in state["incidents"].values()
                        )
                        drift_guarantees = tuple(
                            sorted(set(prev_qual.covered_guarantees) & CONTAINER_ISOLATION_GUARANTEES)
                        ) or tuple(sorted(CONTAINER_ISOLATION_GUARANTEES))
                        if not already_suspended:
                            triggered_incident = self._open_or_reuse_incident_unlocked(
                                state,
                                source="configuration_drift",
                                reason=(
                                    f"Configuration generation drifted ({prev_qual.generation_seq} -> "
                                    f"{gen.generation_seq}) on qualified backend `{backend.backend_id}`; "
                                    "requalification is required before admitting dependent profiles."
                                ),
                                failed_guarantees=drift_guarantees,
                                failed_dependencies=(
                                    f"dep:runtime:{backend.runtime}",
                                    (
                                        f"dep:image:{backend.image_digest}"
                                        if backend.image_digest
                                        else f"dep:image:{backend.image}"
                                    ),
                                ),
                                affected_backends=(backend.backend_id,),
                                generation_seq=gen.generation_seq,
                                config_fingerprint=gen.config_fingerprint,
                                details={
                                    "previous_generation_seq": prev_qual.generation_seq,
                                    "previous_fingerprint": prev_qual.config_fingerprint,
                                    "new_generation_seq": gen.generation_seq,
                                    "new_fingerprint": gen.config_fingerprint,
                                },
                            )

            if dirty:
                self._write_state_unlocked(state)
            return gen, triggered_incident

    def _open_or_reuse_incident_unlocked(
        self,
        state: dict[str, Any],
        *,
        source: str,
        reason: str,
        failed_guarantees: Sequence[str],
        failed_dependencies: Sequence[str],
        affected_backends: Sequence[str],
        generation_seq: int,
        config_fingerprint: str,
        severity: str = "critical",
        scope_session_id: str = "",
        scope_workspace_root: str = "",
        lingering_pids: Sequence[tuple[int, str]] = (),
        details: Mapping[str, Any] | None = None,
    ) -> SecurityIncidentRecord:
        norm_guarantees = _sb_normalize_guarantees(failed_guarantees)
        norm_deps = tuple(sorted({_sb_normalize_dep_token(d) for d in failed_dependencies if str(d).strip()}))
        norm_backends = tuple(sorted({str(b).strip().lower() for b in affected_backends if str(b).strip()}))

        # Reuse identical active incident if already recorded to keep `observe_configuration` idempotent.
        for raw in state["incidents"].values():
            existing = SecurityIncidentRecord.from_mapping(raw)
            if (
                existing.status == "active"
                and existing.source == source
                and existing.failed_guarantees == norm_guarantees
                and existing.failed_dependencies == norm_deps
                and existing.affected_backends == norm_backends
                and existing.scope_session_id == scope_session_id
                and existing.detected_config_fingerprint == config_fingerprint
            ):
                return existing

        now = time.time()
        incident_id = f"secinc-{uuid.uuid4().hex[:16]}"
        provisional = SecurityIncidentRecord(
            incident_id=incident_id,
            status="active",
            severity=str(severity or "critical"),
            source=str(source or "operator"),
            reason=str(reason)[:300],
            failed_guarantees=norm_guarantees,
            failed_dependencies=norm_deps,
            affected_backends=norm_backends,
            scope_session_id=str(scope_session_id or ""),
            scope_workspace_root=str(scope_workspace_root or ""),
            detected_generation_seq=generation_seq,
            detected_config_fingerprint=config_fingerprint,
            barrier_hold_ids=(),
            created_at=now,
            updated_at=now,
            details=dict(details) if isinstance(details, Mapping) else {},
        )
        hold_ids = self._persist_incident_holds(provisional, lingering_pids=lingering_pids)
        final_inc = SecurityIncidentRecord(
            incident_id=provisional.incident_id,
            status=provisional.status,
            severity=provisional.severity,
            source=provisional.source,
            reason=provisional.reason,
            failed_guarantees=provisional.failed_guarantees,
            failed_dependencies=provisional.failed_dependencies,
            affected_backends=provisional.affected_backends,
            scope_session_id=provisional.scope_session_id,
            scope_workspace_root=provisional.scope_workspace_root,
            detected_generation_seq=provisional.detected_generation_seq,
            detected_config_fingerprint=provisional.detected_config_fingerprint,
            barrier_hold_ids=hold_ids,
            created_at=provisional.created_at,
            updated_at=provisional.updated_at,
            details=provisional.details,
        )
        # Invalidate prior qualifications on affected backends immediately!
        for bid in norm_backends:
            state["qualifications"].pop(bid, None)
        state["incidents"][incident_id] = final_inc.to_mapping()
        self._containment_store.record_incident(
            session_id=scope_session_id,
            task_id=(scope_session_id or None),
            reason=f"security_guarantee_suspended:{source}",
            details={
                "security_incident_id": incident_id,
                "failed_guarantees": list(norm_guarantees),
                "failed_dependencies": list(norm_deps),
                "affected_backends": list(norm_backends),
            },
        )
        return final_inc

    def record_incident(
        self,
        *,
        reason: str,
        failed_guarantees: Sequence[str],
        failed_dependencies: Sequence[str] = (),
        affected_backends: Sequence[str] = (),
        source: str = "operator",
        severity: str = "critical",
        session_id: str = "",
        workspace_root: Path | str = "",
        lingering_pids: Sequence[tuple[int, str]] = (),
        details: Mapping[str, Any] | None = None,
    ) -> SecurityIncidentRecord:
        """Record an active security incident, invalidate affected qualifications, and persist holds."""
        with self._lock:
            state = self._read_state_unlocked()
            gen_seq = int(state["current_generation_seq"])
            latest_fp = ""
            if state["generations"]:
                latest_fp = str(state["generations"][-1].get("config_fingerprint") or "")
            eff_backends = list(affected_backends)
            if not eff_backends and state["active_backend_id"]:
                if set(failed_guarantees) & CONTAINER_ISOLATION_GUARANTEES:
                    eff_backends = [str(state["active_backend_id"])]
            inc = self._open_or_reuse_incident_unlocked(
                state,
                source=source,
                reason=reason,
                failed_guarantees=failed_guarantees,
                failed_dependencies=failed_dependencies,
                affected_backends=eff_backends,
                generation_seq=gen_seq,
                config_fingerprint=latest_fp,
                severity=severity,
                scope_session_id=session_id,
                scope_workspace_root=str(workspace_root) if workspace_root else "",
                lingering_pids=lingering_pids,
                details=details,
            )
            self._write_state_unlocked(state)
            return inc

    def ingest_qualification_report(
        self,
        report: Mapping[str, Any],
        *,
        backend: SandboxBackendProfile,
        execution_mode: str = "sandbox",
        allow_host_execution: bool = False,
    ) -> QualificationOutcome:
        """Ingest a ``qualify_environment.py`` report: open an incident on regression or record qualification."""
        gen, hard_inc = self.observe_configuration(
            backend,
            execution_mode=execution_mode,
            allow_host_execution=allow_host_execution,
        )
        if hard_inc is not None:
            return QualificationOutcome(
                ok=False,
                error_code="hardening_regression",
                reason=hard_inc.reason,
                incident=hard_inc,
            )

        # Inspect failed checks specifically so a regression in `qualify_environment.py`
        # immediately suspends affected guarantees (`AC 1`).
        failed_guarantees: set[str] = set()
        failed_check_ids: list[str] = []
        raw_checks = report.get("checks") if isinstance(report, Mapping) else None
        if isinstance(raw_checks, list):
            for item in raw_checks:
                if not isinstance(item, Mapping):
                    continue
                cid = str(item.get("check_id") or "").strip()
                st = str(item.get("status") or "").strip().lower()
                if st == "fail":
                    failed_check_ids.append(cid)
                    mapped = QUALIFICATION_CHECK_GUARANTEES.get(cid)
                    if mapped:
                        failed_guarantees.update(mapped)
                    elif cid.startswith("p3."):
                        failed_guarantees.update(CONTAINER_ISOLATION_GUARANTEES)

        verdict = ""
        verdicts_block = report.get("verdicts") if isinstance(report, Mapping) else None
        if isinstance(verdicts_block, Mapping) and isinstance(verdicts_block.get("M5-P3-01"), Mapping):
            verdict = str(verdicts_block["M5-P3-01"].get("verdict") or "").strip().lower()
        if not verdict and isinstance(report, Mapping):
            verdict = str(report.get("verdict") or "").strip().lower()

        if verdict == "unqualified" or failed_check_ids:
            if not failed_guarantees:
                failed_guarantees.update(CONTAINER_ISOLATION_GUARANTEES)
            raw_failed_deps = (
                report.get("failed_dependencies") if isinstance(report, Mapping) else None
            )
            deps = (
                tuple(str(x) for x in raw_failed_deps)
                if isinstance(raw_failed_deps, (list, tuple)) and raw_failed_deps
                else (
                    f"dep:runtime:{backend.runtime}",
                    f"dep:oci_runtime:{backend.oci_runtime}",
                    f"dep:kernel:{backend.kernel_boundary}",
                    (
                        f"dep:image:{backend.image_digest}"
                        if backend.image_digest
                        else f"dep:image:{backend.image}"
                    ),
                )
            )
            inc = self.record_incident(
                reason=f"Qualification regression on `{backend.backend_id}` (failed checks: {failed_check_ids or ['verdict=unqualified']})",
                failed_guarantees=tuple(sorted(failed_guarantees)),
                failed_dependencies=deps,
                affected_backends=(backend.backend_id,),
                source="qualification_failure",
                details={"failed_checks": failed_check_ids, "verdict": verdict},
            )
            return QualificationOutcome(
                ok=False,
                error_code="qualification_regression_suspended",
                reason=inc.reason,
                incident=inc,
            )

        ok, err_code, err_reason, passed_ids, covered, rep_ts = _sb_evaluate_qualification_evidence(
            report,
            backend=backend,
            expected_fingerprint=gen.config_fingerprint,
            required_guarantees=tuple(sorted(CONTAINER_ISOLATION_GUARANTEES)),
        )
        if not ok:
            return QualificationOutcome(
                ok=False,
                error_code=err_code,
                reason=err_reason,
                missing_guarantees=(covered if err_code == "qualification_incomplete_coverage" else ()),
            )

        with self._lock:
            state = self._read_state_unlocked()
            # Refuse silent restoration of an active incident via `ingest_qualification_report`;
            # callers must invoke `requalify_and_restore` explicitly for an incident!
            for raw_inc in state["incidents"].values():
                inc_rec = SecurityIncidentRecord.from_mapping(raw_inc)
                if inc_rec.status != "restored" and backend.backend_id in inc_rec.affected_backends:
                    return QualificationOutcome(
                        ok=False,
                        error_code="active_incident_requires_explicit_restoration",
                        reason=(
                            f"Backend `{backend.backend_id}` is subject to incident `{inc_rec.incident_id}`; "
                            "use requalify_and_restore to verify post-incident remediation."
                        ),
                        incident=inc_rec,
                    )

            qual = BackendQualificationRecord(
                qualification_id=f"qual-{uuid.uuid4().hex[:16]}",
                backend_id=backend.backend_id,
                generation_seq=gen.generation_seq,
                config_fingerprint=gen.config_fingerprint,
                verdict="qualified",
                covered_guarantees=covered,
                passed_checks=passed_ids,
                verified_disjoint_from=(),
                qualified_at=time.time(),
                report_generated_at=rep_ts,
            )
            state["qualifications"][backend.backend_id] = qual.to_mapping()
            state["active_backend_id"] = backend.backend_id
            self._write_state_unlocked(state)
            return QualificationOutcome(ok=True, qualification=qual)

    def qualify_fallback_backend(
        self,
        incident_id: str,
        candidate: SandboxBackendProfile,
        qualification_report: Mapping[str, Any] | None = None,
        *,
        sample_argv: Sequence[str] | None = None,
        probe_runner: Callable[
            [SandboxBackendProfile, tuple[str, ...], tuple[str, ...]],
            Mapping[str, Any],
        ]
        | None = None,
    ) -> QualificationOutcome:
        """Qualify an alternative sandbox backend against incident-specific shared dependencies (Task 3, AC 2)."""
        with self._lock:
            state = self._read_state_unlocked()
            raw_inc = state["incidents"].get(incident_id)
            if not isinstance(raw_inc, Mapping):
                return QualificationOutcome(
                    ok=False,
                    error_code="incident_not_found",
                    reason=f"Security incident `{incident_id}` was not found.",
                )
            inc = SecurityIncidentRecord.from_mapping(raw_inc)
            if inc.status == "restored":
                return QualificationOutcome(
                    ok=False,
                    error_code="incident_already_restored",
                    reason=f"Security incident `{incident_id}` is already restored.",
                    incident=inc,
                )

            # 1. Shared engine guarantees (`workspace_confinement`, `secret_and_vcs_protection`,
            #    `mcp_transport_boundary`) are shared across all container runtimes -> cannot fallback!
            non_container_failures = sorted(set(inc.failed_guarantees) - CONTAINER_ISOLATION_GUARANTEES)
            if non_container_failures:
                shared_engine_deps = tuple(
                    sorted(set(candidate.security_dependencies()) & set(inc.failed_dependencies))
                ) or tuple(f"dep:{g}" for g in non_container_failures)
                return QualificationOutcome(
                    ok=False,
                    error_code="shared_failed_dependency",
                    reason=(
                        f"Fallback backend `{candidate.backend_id}` cannot bypass non-container shared "
                        f"guarantee failure(s): {non_container_failures}."
                    ),
                    incident=inc,
                    shared_dependencies=shared_engine_deps,
                )

            # 2. Candidate cannot be one of the backends already marked broken by the incident.
            if candidate.backend_id in inc.affected_backends:
                return QualificationOutcome(
                    ok=False,
                    error_code="backend_affected_by_incident",
                    reason=(
                        f"Backend `{candidate.backend_id}` is directly affected by incident `{incident_id}`; "
                        "remediate and call requalify_and_restore instead of fallback."
                    ),
                    incident=inc,
                )

            # 3. AC 2: Candidate MUST NOT share ANY failed dependency with the active incident!
            shared_deps = tuple(
                sorted(set(candidate.security_dependencies()) & set(inc.failed_dependencies))
            )
            if shared_deps:
                return QualificationOutcome(
                    ok=False,
                    error_code="shared_failed_dependency",
                    reason=(
                        f"Rejected fallback backend `{candidate.backend_id}`: shares failed security "
                        f"dependencies {list(shared_deps)} with incident `{incident_id}`."
                    ),
                    incident=inc,
                    shared_dependencies=shared_deps,
                )

        # 4. Observe candidate configuration generation and verify hardening flags.
        cand_gen, hard_inc = self.observe_configuration(
            candidate,
            execution_mode="sandbox",
            allow_host_execution=False,
            sample_argv=sample_argv,
        )
        if hard_inc is not None or not cand_gen.hardening_valid:
            return QualificationOutcome(
                ok=False,
                error_code="fallback_hardening_invalid",
                reason=(
                    hard_inc.reason
                    if hard_inc is not None
                    else f"Candidate backend `{candidate.backend_id}` failed hardening inspection."
                ),
                incident=inc,
            )

        eff_report = qualification_report
        if eff_report is None and probe_runner is not None:
            eff_report = probe_runner(candidate, inc.failed_guarantees, inc.failed_dependencies)

        ok, err_code, err_reason, passed_ids, covered, rep_ts = _sb_evaluate_qualification_evidence(
            eff_report,
            backend=candidate,
            expected_fingerprint=cand_gen.config_fingerprint,
            required_guarantees=inc.failed_guarantees,
            forbidden_dependencies=inc.failed_dependencies,
            min_timestamp=inc.created_at,
        )
        if not ok:
            return QualificationOutcome(
                ok=False,
                error_code=err_code,
                reason=err_reason,
                incident=inc,
                missing_guarantees=(covered if err_code == "qualification_incomplete_coverage" else ()),
            )

        with self._lock:
            state = self._read_state_unlocked()
            # Re-read incident inside lock to guard concurrent updates.
            cur_inc = SecurityIncidentRecord.from_mapping(state["incidents"][incident_id])
            qual = BackendQualificationRecord(
                qualification_id=f"qual-fb-{uuid.uuid4().hex[:14]}",
                backend_id=candidate.backend_id,
                generation_seq=cand_gen.generation_seq,
                config_fingerprint=cand_gen.config_fingerprint,
                verdict="qualified",
                covered_guarantees=covered,
                passed_checks=passed_ids,
                verified_disjoint_from=cur_inc.failed_dependencies,
                qualified_at=time.time(),
                report_generated_at=rep_ts,
            )
            updated_inc = SecurityIncidentRecord(
                incident_id=cur_inc.incident_id,
                status="mitigated_via_fallback",
                severity=cur_inc.severity,
                source=cur_inc.source,
                reason=cur_inc.reason,
                failed_guarantees=cur_inc.failed_guarantees,
                failed_dependencies=cur_inc.failed_dependencies,
                affected_backends=cur_inc.affected_backends,
                scope_session_id=cur_inc.scope_session_id,
                scope_workspace_root=cur_inc.scope_workspace_root,
                detected_generation_seq=cur_inc.detected_generation_seq,
                detected_config_fingerprint=cur_inc.detected_config_fingerprint,
                barrier_hold_ids=cur_inc.barrier_hold_ids,
                fallback_backend_id=candidate.backend_id,
                fallback_qualification_id=qual.qualification_id,
                restoration_qualification_id=cur_inc.restoration_qualification_id,
                created_at=cur_inc.created_at,
                updated_at=time.time(),
                restored_at=None,
                details=cur_inc.details,
            )
            state["backends"][candidate.backend_id] = candidate.to_mapping()
            state["qualifications"][candidate.backend_id] = qual.to_mapping()
            state["incidents"][incident_id] = updated_inc.to_mapping()
            state["active_backend_id"] = candidate.backend_id
            self._write_state_unlocked(state)
            return QualificationOutcome(
                ok=True,
                incident=updated_inc,
                qualification=qual,
            )

    def requalify_and_restore(
        self,
        incident_id: str,
        remediated_backend: SandboxBackendProfile,
        qualification_report: Mapping[str, Any] | None = None,
        *,
        sample_argv: Sequence[str] | None = None,
        probe_runner: Callable[
            [SandboxBackendProfile, tuple[str, ...], tuple[str, ...]],
            Mapping[str, Any],
        ]
        | None = None,
    ) -> QualificationOutcome:
        """Requalify a remediated backend and release durable incident barriers before restoration (Task 4)."""
        with self._lock:
            state = self._read_state_unlocked()
            raw_inc = state["incidents"].get(incident_id)
            if not isinstance(raw_inc, Mapping):
                return QualificationOutcome(
                    ok=False,
                    error_code="incident_not_found",
                    reason=f"Security incident `{incident_id}` was not found.",
                )
            inc = SecurityIncidentRecord.from_mapping(raw_inc)
            if inc.status == "restored":
                return QualificationOutcome(
                    ok=False,
                    error_code="incident_already_restored",
                    reason=f"Security incident `{incident_id}` is already restored.",
                    incident=inc,
                )

        # 1. Observe remediated configuration and ensure hardening regressions are fixed.
        rem_gen, hard_inc = self.observe_configuration(
            remediated_backend,
            execution_mode="sandbox",
            allow_host_execution=False,
            sample_argv=sample_argv,
        )
        if hard_inc is not None or not rem_gen.hardening_valid:
            return QualificationOutcome(
                ok=False,
                error_code="regression_still_present",
                reason=(
                    hard_inc.reason
                    if hard_inc is not None
                    else "Remediated backend still fails container hardening invariants."
                ),
                incident=inc,
            )

        # 1b. An immutable content-addressed image digest (`dep:image:sha256:...`) marked failed
        #     by the incident cannot be restored without moving to a clean image digest.
        compromised_digests = sorted(
            dep
            for dep in (set(remediated_backend.security_dependencies()) & set(inc.failed_dependencies))
            if dep.startswith("dep:image:sha256:")
        )
        if compromised_digests:
            return QualificationOutcome(
                ok=False,
                error_code="regression_still_present",
                reason=(
                    f"Remediated backend `{remediated_backend.backend_id}` still references failed "
                    f"immutable image digest(s): {compromised_digests}."
                ),
                incident=inc,
            )

        # 2. Evaluate fresh post-incident qualification evidence covering all failed guarantees.
        eff_report = qualification_report
        if eff_report is None and probe_runner is not None:
            eff_report = probe_runner(remediated_backend, inc.failed_guarantees, ())

        ok, err_code, err_reason, passed_ids, covered, rep_ts = _sb_evaluate_qualification_evidence(
            eff_report,
            backend=remediated_backend,
            expected_fingerprint=rem_gen.config_fingerprint,
            required_guarantees=inc.failed_guarantees,
            forbidden_dependencies=(),
            min_timestamp=inc.created_at,
        )
        if not ok:
            return QualificationOutcome(
                ok=False,
                error_code=err_code,
                reason=err_reason,
                incident=inc,
                missing_guarantees=(covered if err_code == "qualification_incomplete_coverage" else ()),
            )

        with self._lock:
            state = self._read_state_unlocked()
            cur_inc = SecurityIncidentRecord.from_mapping(state["incidents"][incident_id])
            # 3. Release durable barrier holds in ContainmentStore (fails closed if lingering PIDs remain!).
            try:
                for hold_id in cur_inc.barrier_hold_ids:
                    self._containment_store.release_hold(hold_id, force=False)
            except process_supervision.ContainmentHoldActiveError as exc:
                return QualificationOutcome(
                    ok=False,
                    error_code="lingering_processes_active",
                    reason=str(exc),
                    incident=cur_inc,
                )

            now = time.time()
            qual = BackendQualificationRecord(
                qualification_id=f"qual-rest-{uuid.uuid4().hex[:12]}",
                backend_id=remediated_backend.backend_id,
                generation_seq=rem_gen.generation_seq,
                config_fingerprint=rem_gen.config_fingerprint,
                verdict="qualified",
                covered_guarantees=covered,
                passed_checks=passed_ids,
                verified_disjoint_from=(),
                qualified_at=now,
                report_generated_at=rep_ts,
            )
            restored_inc = SecurityIncidentRecord(
                incident_id=cur_inc.incident_id,
                status="restored",
                severity=cur_inc.severity,
                source=cur_inc.source,
                reason=cur_inc.reason,
                failed_guarantees=cur_inc.failed_guarantees,
                failed_dependencies=cur_inc.failed_dependencies,
                affected_backends=cur_inc.affected_backends,
                scope_session_id=cur_inc.scope_session_id,
                scope_workspace_root=cur_inc.scope_workspace_root,
                detected_generation_seq=cur_inc.detected_generation_seq,
                detected_config_fingerprint=cur_inc.detected_config_fingerprint,
                barrier_hold_ids=cur_inc.barrier_hold_ids,
                fallback_backend_id=cur_inc.fallback_backend_id,
                fallback_qualification_id=cur_inc.fallback_qualification_id,
                restoration_qualification_id=qual.qualification_id,
                created_at=cur_inc.created_at,
                updated_at=now,
                restored_at=now,
                details=cur_inc.details,
            )
            state["backends"][remediated_backend.backend_id] = remediated_backend.to_mapping()
            state["qualifications"][remediated_backend.backend_id] = qual.to_mapping()
            state["incidents"][incident_id] = restored_inc.to_mapping()
            state["active_backend_id"] = remediated_backend.backend_id
            self._write_state_unlocked(state)
            return QualificationOutcome(
                ok=True,
                incident=restored_inc,
                qualification=qual,
            )

    def check_admission(
        self,
        *,
        required_guarantees: Sequence[str],
        execution_surface: str,
        execution_mode: str = "disabled",
        session_id: str = "",
        workspace_root: Path | str | None = None,
    ) -> SecurityAdmissionDecision:
        """Evaluate whether ``required_guarantees`` are currently intact or satisfied by qualified fallback."""
        ws_norm = ""
        if workspace_root is not None and str(workspace_root).strip():
            with contextlib.suppress(OSError):
                ws_norm = str(Path(workspace_root).expanduser().resolve())
            if not ws_norm:
                ws_norm = str(workspace_root).strip()

        req_set = {str(g).strip() for g in required_guarantees if str(g).strip()}
        mode_norm = str(execution_mode or "disabled").strip().lower()

        with self._lock:
            state = self._read_state_unlocked()
            active_bid = str(state.get("active_backend_id") or "")
            active_backend: SandboxBackendProfile | None = None
            if active_bid and isinstance(state["backends"].get(active_bid), Mapping):
                active_backend = SandboxBackendProfile.from_mapping(state["backends"][active_bid])

            is_qualified = False
            if active_backend is not None and mode_norm == "sandbox":
                q_raw = state["qualifications"].get(active_backend.backend_id)
                if isinstance(q_raw, Mapping):
                    q_rec = BackendQualificationRecord.from_mapping(q_raw)
                    cur_fp = active_backend.compute_config_fingerprint(
                        execution_mode="sandbox", allow_host_execution=False
                    )
                    if q_rec.verdict == "qualified" and q_rec.config_fingerprint == cur_fp:
                        is_qualified = True

            for raw_inc in state["incidents"].values():
                inc = SecurityIncidentRecord.from_mapping(raw_inc)
                if inc.status == "restored":
                    continue
                if inc.scope_session_id and session_id and inc.scope_session_id != session_id:
                    continue
                if inc.scope_session_id and not session_id:
                    continue
                if inc.scope_workspace_root and ws_norm and inc.scope_workspace_root != ws_norm:
                    continue
                if inc.scope_workspace_root and not ws_norm:
                    continue

                overlap = sorted(req_set & set(inc.failed_guarantees))
                backend_affected = bool(
                    execution_surface == "subprocess"
                    and active_backend is not None
                    and active_backend.backend_id in inc.affected_backends
                )
                if not overlap and not backend_affected:
                    continue

                if inc.status == "active":
                    susp = tuple(overlap) if overlap else inc.failed_guarantees
                    return SecurityAdmissionDecision(
                        allowed=False,
                        error_code="security_guarantee_suspended",
                        reason=(
                            f"Action blocked by active security incident `{inc.incident_id}` ({inc.reason}): "
                            f"suspended guarantees {list(susp)}."
                        ),
                        incident_id=inc.incident_id,
                        suspended_guarantees=susp,
                        failed_dependencies=inc.failed_dependencies,
                        active_backend=active_backend,
                        container_qualified=False,
                    )

                if inc.status == "mitigated_via_fallback":
                    # Non-container guarantees cannot be mitigated by switching container runtimes.
                    non_container = sorted(set(overlap) - CONTAINER_ISOLATION_GUARANTEES)
                    if non_container:
                        return SecurityAdmissionDecision(
                            allowed=False,
                            error_code="security_guarantee_suspended",
                            reason=(
                                f"Action blocked by incident `{inc.incident_id}`: guarantees "
                                f"{non_container} cannot be satisfied by container fallback."
                            ),
                            incident_id=inc.incident_id,
                            suspended_guarantees=tuple(non_container),
                            failed_dependencies=inc.failed_dependencies,
                            active_backend=active_backend,
                            container_qualified=False,
                        )
                    if execution_surface == "subprocess" and mode_norm != "sandbox":
                        return SecurityAdmissionDecision(
                            allowed=False,
                            error_code="unsafe_host_fallback_forbidden",
                            reason=(
                                f"Subprocess execution in `{mode_norm}` mode is forbidden while container "
                                f"incident `{inc.incident_id}` is mitigated only for sandbox fallback "
                                f"`{inc.fallback_backend_id}`."
                            ),
                            incident_id=inc.incident_id,
                            suspended_guarantees=tuple(overlap) or inc.failed_guarantees,
                            failed_dependencies=inc.failed_dependencies,
                            active_backend=active_backend,
                            container_qualified=False,
                        )

                    fb_id = inc.fallback_backend_id or ""
                    fb_raw = state["backends"].get(fb_id)
                    fb_q_raw = state["qualifications"].get(fb_id)
                    if not isinstance(fb_raw, Mapping) or not isinstance(fb_q_raw, Mapping):
                        return SecurityAdmissionDecision(
                            allowed=False,
                            error_code="fallback_qualification_invalid",
                            reason=f"Fallback backend `{fb_id}` for incident `{inc.incident_id}` lacks qualification.",
                            incident_id=inc.incident_id,
                            suspended_guarantees=tuple(overlap) or inc.failed_guarantees,
                            failed_dependencies=inc.failed_dependencies,
                        )
                    fb_profile = SandboxBackendProfile.from_mapping(fb_raw)
                    fb_qual = BackendQualificationRecord.from_mapping(fb_q_raw)
                    shared_now = sorted(
                        set(fb_profile.security_dependencies()) & set(inc.failed_dependencies)
                    )
                    if shared_now:
                        return SecurityAdmissionDecision(
                            allowed=False,
                            error_code="shared_failed_dependency",
                            reason=(
                                f"Fallback backend `{fb_id}` now intersects failed incident dependencies "
                                f"{shared_now}; suspending execution."
                            ),
                            incident_id=inc.incident_id,
                            suspended_guarantees=tuple(overlap) or inc.failed_guarantees,
                            failed_dependencies=tuple(shared_now),
                        )
                    expected_fb_fp = fb_profile.compute_config_fingerprint(
                        execution_mode="sandbox", allow_host_execution=False
                    )
                    if fb_qual.config_fingerprint != expected_fb_fp or fb_qual.verdict != "qualified":
                        return SecurityAdmissionDecision(
                            allowed=False,
                            error_code="fallback_qualification_invalid",
                            reason=(
                                f"Fallback backend `{fb_id}` configuration drifted from qualified "
                                f"generation {fb_qual.generation_seq}; requalification required."
                            ),
                            incident_id=inc.incident_id,
                            suspended_guarantees=tuple(overlap) or inc.failed_guarantees,
                            failed_dependencies=inc.failed_dependencies,
                        )
                    active_backend = fb_profile
                    is_qualified = True

            return SecurityAdmissionDecision(
                allowed=True,
                active_backend=active_backend,
                container_qualified=is_qualified,
            )

    def get_incident(self, incident_id: str) -> SecurityIncidentRecord | None:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["incidents"].get(incident_id)
            return SecurityIncidentRecord.from_mapping(raw) if isinstance(raw, Mapping) else None

    def list_incidents(self, *, include_restored: bool = True) -> list[SecurityIncidentRecord]:
        with self._lock:
            state = self._read_state_unlocked()
            records = [SecurityIncidentRecord.from_mapping(r) for r in state["incidents"].values()]
        if not include_restored:
            records = [r for r in records if r.status != "restored"]
        return sorted(records, key=lambda r: r.created_at)

    def active_sandbox_backend(self) -> SandboxBackendProfile | None:
        with self._lock:
            state = self._read_state_unlocked()
            bid = str(state.get("active_backend_id") or "")
            raw = state["backends"].get(bid) if bid else None
            return SandboxBackendProfile.from_mapping(raw) if isinstance(raw, Mapping) else None

    def summary(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_state_unlocked()
            incidents = [SecurityIncidentRecord.from_mapping(r) for r in state["incidents"].values()]
            suspended: set[str] = set()
            for inc in incidents:
                if inc.status == "active":
                    suspended.update(inc.failed_guarantees)
            return {
                "schema_version": SB_INCIDENT_SCHEMA_VERSION,
                "current_generation_seq": int(state["current_generation_seq"]),
                "active_backend_id": str(state.get("active_backend_id") or ""),
                "suspended_guarantees": sorted(suspended),
                "active_incident_count": sum(1 for i in incidents if i.status == "active"),
                "mitigated_incident_count": sum(
                    1 for i in incidents if i.status == "mitigated_via_fallback"
                ),
                "restored_incident_count": sum(1 for i in incidents if i.status == "restored"),
                "backends": {
                    k: SandboxBackendProfile.from_mapping(v).to_mapping()
                    for k, v in state["backends"].items()
                },
                "qualifications": {
                    k: BackendQualificationRecord.from_mapping(v).to_mapping()
                    for k, v in state["qualifications"].items()
                },
                "generations": [
                    ConfigurationGeneration.from_mapping(g).to_mapping()
                    for g in state["generations"][-20:]
                ],
                "incidents": [
                    i.to_mapping() for i in sorted(incidents, key=lambda x: x.created_at)
                ],
            }
