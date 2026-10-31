"""Process supervision, bounded tree/detached-child termination, and durable containment (M5-P3-03).

Stdlib-only module (inlined into ``studio.py`` by ``scripts/build_studio.py``; private
globals use the ``_ps_`` / ``_PS_`` prefix to avoid namespace collisions).

Guarantees:
1. Subprocess identities (``pid``, ``pgid``, ``uid``, OS ``start_fingerprint``,
   ``container_name``, ``supervision_id``) and authoritative ownership metadata
   (``session_id``, ``task_id``, ``epoch``, ``worker_id``, ``boot_id``, ``action_id``,
   acquired ``barriers``) are tracked in memory and durably recorded before execution
   completes.
2. Cancellation, abort, ownership loss (lease fencing / takeover), timeout, crash
   recovery, and security incidents execute a bounded 3-stage containment pipeline:
   origin fencing -> cooperative ``SIGTERM`` -> bounded grace wait -> ``SIGKILL`` across
   process groups AND ``os.setsid()`` detached descendants (tracked via
   ``M5_SUPERVISION_ID`` and transitive ``ppid`` chains), never signaling PID <= 1,
   Studio's own PID/PGID, or a recycled OS PID whose ``start_fingerprint`` changed.
3. Unresolved side effects and unverified/lingering process barriers are persisted
   atomically in ``<store_dir>/containment/state.json`` (``0700`` dir, ``0600`` file),
   surviving process crashes and restarts, blocking conflicting actions and conflicting
   task/workspace resumption until reconciled.
4. Stale / fenced workers, superseded epochs, revoked supervision scopes, and foreign
   process boots are durably fenced and cannot admit new actions across any tool class.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
import contextlib
import contextvars
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
from typing import Any
import uuid

PS_CONTAINMENT_SCHEMA_VERSION = "m5-process-containment-v1"
CONTAINMENT_SCHEMA_VERSION = PS_CONTAINMENT_SCHEMA_VERSION
SUPERVISION_ENV_VAR = "M5_SUPERVISION_ID"
SUPERVISION_ENV_KEY = SUPERVISION_ENV_VAR

DEFAULT_COOPERATIVE_GRACE_S = 0.25
DEFAULT_FORCE_KILL_WAIT_S = 0.50
MAX_FENCED_ORIGINS = 500
MAX_INCIDENT_HISTORY = 200

_PS_SUPERVISION_ID_RE = re.compile(r"^sup-[0-9a-f]{12,32}$")
_PS_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_PS_ACTIVE_SUPERVISION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "m5_active_supervision_id",
    default="",
)


def current_supervision_id() -> str:
    """Return the active ``supervision_id`` in the current execution context or OS env, if any."""
    ctx_val = _PS_ACTIVE_SUPERVISION_ID.get()
    if ctx_val:
        return ctx_val
    return (os.environ.get(SUPERVISION_ENV_KEY) or "").strip()


class ProcessSupervisionError(RuntimeError):
    """Base error for process supervision and containment failures."""


class ContainmentIntegrityError(ProcessSupervisionError):
    """Raised when durable containment state is corrupt, tampered, or a symlink."""


class ContainmentHoldActiveError(ProcessSupervisionError):
    """Raised when attempting to clear a hold or resume while a barrier/process remains active."""


class StaleOriginFencedError(PermissionError):
    """Raised when a stale or fenced worker/epoch/process attempts to spawn or admit actions."""


@dataclass(frozen=True, slots=True)
class ProcessProbe:
    """Point-in-time OS liveness and identity measurement for one PID."""

    pid: int
    uid: int
    ppid: int
    pgid: int
    state: str
    start_fingerprint: str
    alive: bool


@dataclass(frozen=True, slots=True)
class SupervisedProcessRecord:
    """Authoritative identity, ownership, and barrier record for a supervised subprocess."""

    supervision_id: str
    pid: int
    pgid: int
    uid: int
    start_fingerprint: str
    container_name: str | None
    tool_name: str
    session_id: str
    task_id: str | None
    epoch: int | None
    worker_id: str | None
    boot_id: str
    action_id: str
    workspace_root: str
    barriers: tuple[tuple[str, str], ...]
    started_at: float
    status: str = "running"
    containment_reason: str = ""
    grant_id: str | None = None
    capabilities: tuple[str, ...] = ()
    resource_scopes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "supervision_id": self.supervision_id,
            "pid": self.pid,
            "pgid": self.pgid,
            "uid": self.uid,
            "start_fingerprint": self.start_fingerprint,
            "container_name": self.container_name,
            "tool_name": self.tool_name,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "epoch": self.epoch,
            "worker_id": self.worker_id,
            "boot_id": self.boot_id,
            "action_id": self.action_id,
            "workspace_root": self.workspace_root,
            "barriers": [[t, m] for t, m in self.barriers],
            "started_at": self.started_at,
            "status": self.status,
            "containment_reason": self.containment_reason,
            "grant_id": self.grant_id,
            "capabilities": list(self.capabilities),
            "resource_scopes": list(self.resource_scopes),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SupervisedProcessRecord:
        raw_barriers = raw.get("barriers")
        if not isinstance(raw_barriers, list):
            raise ContainmentIntegrityError("SupervisedProcessRecord.barriers must be a list.")
        parsed_barriers: list[tuple[str, str]] = []
        for item in raw_barriers:
            if not isinstance(item, list) or len(item) != 2:
                raise ContainmentIntegrityError("Malformed barrier pair in SupervisedProcessRecord.")
            parsed_barriers.append((str(item[0]), str(item[1])))
        epoch_raw = raw.get("epoch")
        raw_caps = raw.get("capabilities") or ()
        raw_scopes = raw.get("resource_scopes") or ()
        return cls(
            supervision_id=str(raw["supervision_id"]),
            pid=int(raw["pid"]),
            pgid=int(raw["pgid"]),
            uid=int(raw.get("uid", -1)),
            start_fingerprint=str(raw.get("start_fingerprint") or ""),
            container_name=str(raw["container_name"]) if raw.get("container_name") else None,
            tool_name=str(raw.get("tool_name") or ""),
            session_id=str(raw.get("session_id") or ""),
            task_id=str(raw["task_id"]) if raw.get("task_id") is not None else None,
            epoch=int(epoch_raw) if epoch_raw is not None else None,
            worker_id=str(raw["worker_id"]) if raw.get("worker_id") is not None else None,
            boot_id=str(raw.get("boot_id") or ""),
            action_id=str(raw.get("action_id") or ""),
            workspace_root=str(raw.get("workspace_root") or ""),
            barriers=tuple(parsed_barriers),
            started_at=float(raw.get("started_at") or 0.0),
            status=str(raw.get("status") or "running"),
            containment_reason=str(raw.get("containment_reason") or ""),
            grant_id=(str(raw["grant_id"]) if raw.get("grant_id") else None),
            capabilities=tuple(str(c) for c in raw_caps if str(c).strip()),
            resource_scopes=tuple(str(s) for s in raw_scopes if str(s).strip()),
        )


@dataclass(frozen=True, slots=True)
class TerminationOutcome:
    """Measured outcome of bounded cooperative cancel -> force kill -> reap verification."""

    supervision_id: str
    reason: str
    cooperative_sent: bool
    escalated_kill: bool
    signaled_pids: tuple[int, ...] = ()
    detached_pids: tuple[int, ...] = ()
    lingering_pids: tuple[tuple[int, str], ...] = ()
    cleanly_reaped: bool = True
    elapsed_ms: int = 0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "supervision_id": self.supervision_id,
            "reason": self.reason,
            "cooperative_sent": self.cooperative_sent,
            "escalated_kill": self.escalated_kill,
            "signaled_pids": list(self.signaled_pids),
            "detached_pids": list(self.detached_pids),
            "lingering_pids": [[pid, fp] for pid, fp in self.lingering_pids],
            "cleanly_reaped": self.cleanly_reaped,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class PersistedBarrierHold:
    """Durable barrier hold representing an unresolved side effect or lingering process."""

    hold_id: str
    session_id: str = ""
    task_id: str | None = None
    epoch: int | None = None
    worker_id: str | None = None
    action_id: str = ""
    supervision_id: str = ""
    tool_name: str = ""
    footprint_scope: str = ""
    workspace_root: str = ""
    barriers: tuple[tuple[str, str], ...] = ()
    reason: str = "unresolved_side_effect"
    lingering_pids: tuple[tuple[int, str], ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        # Callers naturally hold a `Path`; without this the hold is applied in
        # memory and then blows up inside `json.dumps` *after* the state dict was
        # mutated, which is the worst moment for a containment record to fail.
        if not isinstance(self.workspace_root, str):
            object.__setattr__(self, "workspace_root", str(self.workspace_root))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "hold_id": self.hold_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "epoch": self.epoch,
            "worker_id": self.worker_id,
            "action_id": self.action_id,
            "supervision_id": self.supervision_id,
            "tool_name": self.tool_name,
            "footprint_scope": self.footprint_scope,
            "workspace_root": self.workspace_root,
            "barriers": [[t, m] for t, m in self.barriers],
            "reason": self.reason,
            "lingering_pids": [[pid, fp] for pid, fp in self.lingering_pids],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PersistedBarrierHold:
        raw_barriers = raw.get("barriers")
        raw_lingering = raw.get("lingering_pids") or []
        if not isinstance(raw_barriers, list) or not isinstance(raw_lingering, list):
            raise ContainmentIntegrityError("Malformed barriers/lingering_pids in PersistedBarrierHold.")
        barriers = tuple(
            (str(b[0]), str(b[1]))
            for b in raw_barriers
            if isinstance(b, list) and len(b) == 2
        )
        lingering = tuple(
            (int(item[0]), str(item[1]))
            for item in raw_lingering
            if isinstance(item, list) and len(item) == 2
        )
        epoch_raw = raw.get("epoch")
        return cls(
            hold_id=str(raw["hold_id"]),
            session_id=str(raw.get("session_id") or ""),
            task_id=str(raw["task_id"]) if raw.get("task_id") is not None else None,
            epoch=int(epoch_raw) if epoch_raw is not None else None,
            worker_id=str(raw["worker_id"]) if raw.get("worker_id") is not None else None,
            action_id=str(raw.get("action_id") or ""),
            supervision_id=str(raw.get("supervision_id") or ""),
            tool_name=str(raw.get("tool_name") or ""),
            footprint_scope=str(raw.get("footprint_scope") or ""),
            workspace_root=str(raw.get("workspace_root") or ""),
            barriers=barriers,
            reason=str(raw.get("reason") or "unresolved_side_effect"),
            lingering_pids=lingering,
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class FencedOriginRecord:
    """Durable record of a fenced worker, epoch, supervision scope, or quarantined session."""

    origin_key: str
    reason: str
    session_id: str = ""
    task_id: str | None = None
    epoch: int | None = None
    worker_id: str | None = None
    fenced_at: float = 0.0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "origin_key": self.origin_key,
            "reason": self.reason,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "epoch": self.epoch,
            "worker_id": self.worker_id,
            "fenced_at": self.fenced_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> FencedOriginRecord:
        epoch_raw = raw.get("epoch")
        return cls(
            origin_key=str(raw["origin_key"]),
            reason=str(raw.get("reason") or "fenced"),
            session_id=str(raw.get("session_id") or ""),
            task_id=str(raw["task_id"]) if raw.get("task_id") is not None else None,
            epoch=int(epoch_raw) if epoch_raw is not None else None,
            worker_id=str(raw["worker_id"]) if raw.get("worker_id") is not None else None,
            fenced_at=float(raw.get("fenced_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class StartupContainmentReport:
    """Summary of startup crash-recovery containment sweep."""

    swept_supervision_ids: tuple[str, ...] = ()
    terminated_pids: tuple[int, ...] = ()
    lingering_pids: tuple[int, ...] = ()
    persisted_hold_ids: tuple[str, ...] = ()
    fenced_origins_added: int = 0


# ---------------------------------------------------------------------------
# OS Process Probing & Detached-Descendant Discovery (Linux /proc + BSD/macOS ps)
# ---------------------------------------------------------------------------


def _ps_current_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _ps_has_procfs() -> bool:
    return Path("/proc/self/stat").is_file()


def _ps_parse_proc_stat(pid: int) -> ProcessProbe | None:
    proc_dir = Path(f"/proc/{pid}")
    try:
        st = os.stat(proc_dir)
        raw = (proc_dir / "stat").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    rparen = raw.rfind(")")
    if rparen < 0 or rparen + 2 >= len(raw):
        return None
    fields = raw[rparen + 2 :].split()
    if len(fields) < 20:
        return None
    state = fields[0]
    try:
        ppid = int(fields[1])
        pgid = int(fields[2])
        starttime = int(fields[19])
    except ValueError:
        return None
    alive = state not in {"Z", "X", "x"}
    return ProcessProbe(
        pid=pid,
        uid=int(st.st_uid),
        ppid=ppid,
        pgid=pgid,
        state=state,
        start_fingerprint=f"proc:{starttime}",
        alive=alive,
    )


def _ps_probe_via_ps(pid: int) -> ProcessProbe | None:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "uid=,ppid=,pgid=,state=,lstart="],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line:
        return None
    parts = line.split(None, 4)
    if len(parts) < 5:
        return None
    try:
        uid = int(parts[0])
        ppid = int(parts[1])
        pgid = int(parts[2])
    except ValueError:
        return None
    state = parts[3]
    lstart = " ".join(parts[4].split())
    alive = not state.upper().startswith("Z")
    return ProcessProbe(
        pid=pid,
        uid=uid,
        ppid=ppid,
        pgid=pgid,
        state=state,
        start_fingerprint=f"ps:{lstart}",
        alive=alive,
    )


def probe_process(pid: int) -> ProcessProbe | None:
    """Probe OS metadata for ``pid`` without treating zombie (``Z``) processes as alive."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    if _ps_has_procfs():
        return _ps_parse_proc_stat(pid)
    return _ps_probe_via_ps(pid)


def is_same_live_process(
    pid: int,
    expected_fingerprint: str = "",
    *,
    probe_fn: Callable[[int], ProcessProbe | None] = probe_process,
) -> bool:
    """Return True iff ``pid`` is alive (non-zombie) and matches ``expected_fingerprint``."""
    probe = probe_fn(pid)
    if probe is None or not probe.alive:
        return False
    if expected_fingerprint and probe.start_fingerprint != expected_fingerprint:
        return False
    return True


def _ps_proc_has_supervision_token(pid: int, supervision_id: str) -> bool:
    needle = f"{SUPERVISION_ENV_VAR}={supervision_id}".encode("utf-8")
    if _ps_has_procfs():
        try:
            raw_env = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return False
        return any(entry == needle for entry in raw_env.split(b"\x00") if entry)
    try:
        completed = subprocess.run(
            ["/bin/ps", "eww", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    token_str = f"{SUPERVISION_ENV_VAR}={supervision_id}"
    return completed.returncode == 0 and token_str in completed.stdout


def _ps_list_same_uid_live_probes(target_uid: int) -> list[ProcessProbe]:
    self_pid = os.getpid()
    probes: list[ProcessProbe] = []
    if _ps_has_procfs():
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for name in entries:
            if not name.isdigit():
                continue
            pid = int(name)
            if pid <= 1 or pid == self_pid:
                continue
            p = _ps_parse_proc_stat(pid)
            if p is not None and p.alive and (target_uid < 0 or p.uid == target_uid):
                probes.append(p)
        return probes

    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,uid=,ppid=,pgid=,state="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    for raw_ln in completed.stdout.splitlines():
        cols = raw_ln.split()
        if len(cols) < 5:
            continue
        try:
            pid = int(cols[0])
            uid = int(cols[1])
            ppid = int(cols[2])
            pgid = int(cols[3])
        except ValueError:
            continue
        state = cols[4]
        if pid <= 1 or pid == self_pid or state.upper().startswith("Z"):
            continue
        if target_uid >= 0 and uid != target_uid:
            continue
        probes.append(
            ProcessProbe(
                pid=pid,
                uid=uid,
                ppid=ppid,
                pgid=pgid,
                state=state,
                start_fingerprint="",
                alive=True,
            )
        )
    return probes


def discover_process_tree(
    record: SupervisedProcessRecord,
    *,
    probe_fn: Callable[[int], ProcessProbe | None] = probe_process,
) -> tuple[list[ProcessProbe], list[ProcessProbe]]:
    """Discover live in-group and ``setsid()``-detached descendants for ``record``.

    Combines four orthogonal signals restricted to ``record.uid`` and excluding
    Studio's own PID:
    1. Verified leader ``(record.pid, record.start_fingerprint)``.
    2. Same-PGID members when ``record.pgid > 1`` and the leader still matches its
       ``start_fingerprint`` (or the member carries ``M5_SUPERVISION_ID``).
    3. Transitive ``ppid`` children of any discovered member.
    4. Same-UID processes carrying ``M5_SUPERVISION_ID=<record.supervision_id>``
       even after ``os.setsid()`` + double-fork reparenting to PID 1.
    """
    self_pid = os.getpid()
    self_pgid = os.getpgrp() if hasattr(os, "getpgrp") else -1
    leader_probe = probe_fn(record.pid) if record.pid > 1 and record.pid != self_pid else None
    leader_verified = bool(
        leader_probe is not None
        and leader_probe.alive
        and (
            not record.start_fingerprint
            or leader_probe.start_fingerprint == record.start_fingerprint
        )
    )

    all_live = _ps_list_same_uid_live_probes(record.uid)
    by_pid: dict[int, ProcessProbe] = {p.pid: p for p in all_live}
    if leader_verified and leader_probe is not None:
        by_pid[leader_probe.pid] = leader_probe

    matched_pids: set[int] = set()
    if leader_verified and leader_probe is not None:
        matched_pids.add(leader_probe.pid)

    for pid, p in by_pid.items():
        if pid == self_pid:
            continue
        if (
            record.pgid > 1
            and record.pgid != self_pgid
            and p.pgid == record.pgid
            and (leader_verified or _ps_proc_has_supervision_token(pid, record.supervision_id))
        ):
            matched_pids.add(pid)
            continue
        if record.supervision_id and _ps_proc_has_supervision_token(pid, record.supervision_id):
            matched_pids.add(pid)

    # Expand transitive `ppid` descendants iteratively.
    added = True
    while added:
        added = False
        for pid, p in by_pid.items():
            if pid not in matched_pids and p.ppid in matched_pids:
                matched_pids.add(pid)
                added = True

    in_group: list[ProcessProbe] = []
    detached: list[ProcessProbe] = []
    for pid in sorted(matched_pids):
        cand = by_pid.get(pid) or probe_fn(pid)
        if cand is None or not cand.alive:
            continue
        # Ensure full start_fingerprint on BSD/macOS where bulk `ps` omitted `lstart`.
        if not cand.start_fingerprint:
            refreshed = probe_fn(pid)
            if refreshed is None or not refreshed.alive:
                continue
            cand = refreshed
        if record.pgid > 1 and cand.pgid == record.pgid:
            in_group.append(cand)
        else:
            detached.append(cand)
    return in_group, detached


# ---------------------------------------------------------------------------
# Durable Containment Store (`<store_dir>/containment/state.json`)
# ---------------------------------------------------------------------------


def _ps_restrict_file(path: Path) -> None:
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _ps_ensure_private_dir(path: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise ContainmentIntegrityError(f"Containment directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
    return path


def _ps_barriers_conflict(
    held_barriers: Sequence[tuple[str, str]],
    requested_barriers: Sequence[tuple[str, str]],
) -> list[str]:
    held_map: dict[str, str] = {}
    for target, mode in held_barriers:
        if mode == "exclusive" or target not in held_map:
            held_map[target] = mode
    conflicts: list[str] = []
    for req_target, req_mode in requested_barriers:
        held_mode = held_map.get(req_target)
        if held_mode is None:
            continue
        if held_mode == "exclusive" or req_mode == "exclusive":
            if req_target not in conflicts:
                conflicts.append(req_target)
    return conflicts


class ContainmentStore:
    """Crash-safe durable store for in-flight subprocesses, barrier holds, and fenced origins."""

    def __init__(
        self,
        store_dir: Path,
        *,
        probe_fn: Callable[[int], ProcessProbe | None] = probe_process,
    ) -> None:
        self._root = Path(store_dir) / "containment"
        self._state_path = self._root / "state.json"
        self._probe_fn = probe_fn
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _read_state_unlocked(self) -> dict[str, Any]:
        if self._root.exists() and self._root.is_symlink():
            raise ContainmentIntegrityError(f"Containment root is a symlink: {self._root}")
        if not self._state_path.exists():
            if self._state_path.is_symlink():
                raise ContainmentIntegrityError(f"Containment state file is a dangling symlink: {self._state_path}")
            return {
                "schema_version": PS_CONTAINMENT_SCHEMA_VERSION,
                "in_flight_processes": {},
                "persisted_barriers": {},
                "fenced_origins": {},
                "incident_history": [],
            }
        st = os.lstat(self._state_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ContainmentIntegrityError(f"Containment state path is not a regular file: {self._state_path}")
        try:
            raw_text = self._state_path.read_text(encoding="utf-8")
            parsed = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContainmentIntegrityError(f"Containment state file could not be parsed: {exc}") from exc
        if not isinstance(parsed, dict) or parsed.get("schema_version") != PS_CONTAINMENT_SCHEMA_VERSION:
            raise ContainmentIntegrityError("Containment state file has an invalid schema_version.")
        for dict_key in ("in_flight_processes", "persisted_barriers", "fenced_origins"):
            if not isinstance(parsed.get(dict_key), dict):
                raise ContainmentIntegrityError(f"Containment state field {dict_key!r} must be an object.")
        if not isinstance(parsed.get("incident_history"), list):
            raise ContainmentIntegrityError("Containment state field 'incident_history' must be a list.")
        return parsed

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        _ps_ensure_private_dir(self._root)
        if self._state_path.exists() and self._state_path.is_symlink():
            raise ContainmentIntegrityError(f"Refusing to overwrite symlinked state file: {self._state_path}")
        # Bound fenced_origins & incident_history so the state file never grows without limit.
        fenced = state.get("fenced_origins") or {}
        if len(fenced) > MAX_FENCED_ORIGINS:
            sorted_items = sorted(
                fenced.items(),
                key=lambda kv: float(kv[1].get("fenced_at", 0.0)) if isinstance(kv[1], Mapping) else 0.0,
            )
            state["fenced_origins"] = dict(sorted_items[-MAX_FENCED_ORIGINS:])
        history = state.get("incident_history") or []
        if len(history) > MAX_INCIDENT_HISTORY:
            state["incident_history"] = history[-MAX_INCIDENT_HISTORY:]

        tmp_path = self._root / f".state.{uuid.uuid4().hex[:12]}.tmp"
        encoded = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, self._state_path)
        # The file's own fsync makes its *contents* durable; the rename that
        # publishes it is durable only after the containing directory is synced.
        # Without this a crash can roll the manifest back to the previous
        # version, losing a fence or a hold that was already reported as written.
        self._fsync_state_dir()
        _ps_restrict_file(self._state_path)

    def _fsync_state_dir(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            dir_fd = os.open(self._root, flags)
        except OSError:  # pragma: no cover - platform without directory fds
            return
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover - some filesystems refuse this
            return
        finally:
            os.close(dir_fd)

    def record_process_started(self, record: SupervisedProcessRecord) -> None:
        with self._lock:
            state = self._read_state_unlocked()
            state["in_flight_processes"][record.supervision_id] = record.to_mapping()
            self._write_state_unlocked(state)

    def remove_in_flight_process(
        self,
        supervision_id: str,
        *,
        incident_entry: dict[str, Any] | None = None,
    ) -> SupervisedProcessRecord | None:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["in_flight_processes"].pop(supervision_id, None)
            if incident_entry is not None:
                state["incident_history"].append(incident_entry)
            if raw is not None or incident_entry is not None:
                self._write_state_unlocked(state)
            return SupervisedProcessRecord.from_mapping(raw) if isinstance(raw, Mapping) else None

    def list_in_flight_processes(self) -> list[SupervisedProcessRecord]:
        with self._lock:
            state = self._read_state_unlocked()
            return [
                SupervisedProcessRecord.from_mapping(v)
                for v in state["in_flight_processes"].values()
                if isinstance(v, Mapping)
            ]

    def fence_origin(
        self,
        *,
        reason: str,
        session_id: str = "",
        task_id: str | None = None,
        epoch: int | None = None,
        worker_id: str | None = None,
        supervision_id: str = "",
        quarantine_session: bool = False,
        now: float | None = None,
    ) -> int:
        ts = time.time() if now is None else float(now)
        keys: list[str] = []
        if worker_id:
            keys.append(f"worker:{worker_id}")
        if task_id and epoch is not None and epoch > 0:
            keys.append(f"epoch:{task_id}:{int(epoch)}")
        if supervision_id:
            keys.append(f"sup:{supervision_id}")
        if quarantine_session and session_id:
            keys.append(f"incident_session:{session_id}")
        if not keys:
            return 0
        with self._lock:
            state = self._read_state_unlocked()
            added = 0
            for key in keys:
                if key not in state["fenced_origins"]:
                    added += 1
                state["fenced_origins"][key] = FencedOriginRecord(
                    origin_key=key,
                    reason=str(reason)[:160],
                    session_id=session_id,
                    task_id=task_id,
                    epoch=epoch,
                    worker_id=worker_id,
                    fenced_at=ts,
                ).to_mapping()
            self._write_state_unlocked(state)
            return added

    def clear_session_quarantine(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            state = self._read_state_unlocked()
            removed = state["fenced_origins"].pop(f"incident_session:{session_id}", None)
            if removed is not None:
                self._write_state_unlocked(state)
                return True
            return False

    def is_origin_fenced(
        self,
        *,
        session_id: str = "",
        task_id: str | None = None,
        epoch: int | None = None,
        worker_id: str | None = None,
        supervision_id: str | None = "",
        current_boot_id: str | None = None,
    ) -> tuple[bool, str]:
        """Return ``(True, reason)`` if the caller's execution origin is stale, fenced, or quarantined."""
        if (
            worker_id
            and current_boot_id
            and worker_id.startswith("wrk-")
            and not worker_id.startswith(f"wrk-{current_boot_id}-")
        ):
            return (
                True,
                f"Stale worker `{worker_id}` belongs to a previous process boot and cannot admit new actions.",
            )
        active_sup = (supervision_id or current_supervision_id() or "").strip()
        with self._lock:
            state = self._read_state_unlocked()
            fenced = state["fenced_origins"]
            if active_sup and f"sup:{active_sup}" in fenced:
                rec = fenced[f"sup:{active_sup}"]
                return (
                    True,
                    f"Contained subprocess scope `{active_sup}` ({rec.get('reason', 'fenced')}) cannot admit new actions.",
                )
            if worker_id and f"worker:{worker_id}" in fenced:
                rec = fenced[f"worker:{worker_id}"]
                return (
                    True,
                    f"Stale or fenced worker `{worker_id}` ({rec.get('reason', 'fenced')}) cannot admit new actions.",
                )
            if task_id and epoch is not None and epoch > 0 and f"epoch:{task_id}:{int(epoch)}" in fenced:
                rec = fenced[f"epoch:{task_id}:{int(epoch)}"]
                return (
                    True,
                    f"Fenced task `{task_id}` epoch {int(epoch)} ({rec.get('reason', 'fenced')}) cannot admit new actions.",
                )
            if session_id and f"incident_session:{session_id}" in fenced:
                rec = fenced[f"incident_session:{session_id}"]
                return (
                    True,
                    f"Session `{session_id}` is under security containment ({rec.get('reason', 'incident')}) and cannot admit new actions.",
                )
        return False, ""

    def persist_barrier_hold(self, hold: PersistedBarrierHold) -> None:
        if not hold.barriers:
            return
        with self._lock:
            state = self._read_state_unlocked()
            existing_raw = state["persisted_barriers"].get(hold.hold_id)
            created_at = hold.created_at or time.time()
            if isinstance(existing_raw, Mapping) and existing_raw.get("created_at"):
                created_at = float(existing_raw["created_at"])
            updated = PersistedBarrierHold(
                hold_id=hold.hold_id,
                session_id=hold.session_id,
                task_id=hold.task_id,
                epoch=hold.epoch,
                worker_id=hold.worker_id,
                action_id=hold.action_id,
                supervision_id=hold.supervision_id,
                tool_name=hold.tool_name,
                footprint_scope=hold.footprint_scope,
                workspace_root=hold.workspace_root,
                barriers=hold.barriers,
                reason=hold.reason,
                lingering_pids=hold.lingering_pids,
                created_at=created_at,
                updated_at=hold.updated_at or time.time(),
            )
            state["persisted_barriers"][hold.hold_id] = updated.to_mapping()
            self._write_state_unlocked(state)

    def _refresh_hold_lingering_unlocked(
        self,
        state: dict[str, Any],
    ) -> tuple[list[PersistedBarrierHold], bool]:
        dirty = False
        holds: list[PersistedBarrierHold] = []
        for hold_id, raw in list(state["persisted_barriers"].items()):
            if not isinstance(raw, Mapping):
                continue
            hold = PersistedBarrierHold.from_mapping(raw)
            if hold.lingering_pids:
                still_alive = tuple(
                    (pid, fp)
                    for pid, fp in hold.lingering_pids
                    if is_same_live_process(pid, fp, probe_fn=self._probe_fn)
                )
                if still_alive != hold.lingering_pids:
                    dirty = True
                    hold = PersistedBarrierHold(
                        hold_id=hold.hold_id,
                        session_id=hold.session_id,
                        task_id=hold.task_id,
                        epoch=hold.epoch,
                        worker_id=hold.worker_id,
                        action_id=hold.action_id,
                        supervision_id=hold.supervision_id,
                        tool_name=hold.tool_name,
                        footprint_scope=hold.footprint_scope,
                        workspace_root=hold.workspace_root,
                        barriers=hold.barriers,
                        reason=hold.reason,
                        lingering_pids=still_alive,
                        created_at=hold.created_at,
                        updated_at=time.time(),
                    )
                    state["persisted_barriers"][hold_id] = hold.to_mapping()
            holds.append(hold)
        return holds, dirty

    def list_barrier_holds(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        workspace_root: Path | str | None = None,
    ) -> list[PersistedBarrierHold]:
        ws_str = ""
        if workspace_root is not None and str(workspace_root).strip():
            with contextlib.suppress(OSError):
                ws_str = str(Path(workspace_root).expanduser().resolve())
        with self._lock:
            state = self._read_state_unlocked()
            holds, dirty = self._refresh_hold_lingering_unlocked(state)
            if dirty:
                self._write_state_unlocked(state)
        filtered: list[PersistedBarrierHold] = []
        for h in holds:
            if session_id is not None and h.session_id != session_id:
                continue
            if task_id is not None and h.task_id != task_id:
                continue
            if ws_str and h.workspace_root != ws_str:
                continue
            filtered.append(h)
        return filtered

    def find_conflicting_holds(
        self,
        requested_barriers: Sequence[tuple[str, str]],
        *,
        action_class: str,
        candidate_action_id: str = "",
        allow_acknowledged_rollback: bool = False,
        allow_readonly_inspection: bool = False,
    ) -> tuple[list[PersistedBarrierHold], list[str]]:
        """Check ``requested_barriers`` against durable ``persisted_barriers``.

        Semantics:
        - If a hold still has live ``lingering_pids`` (an unkillable detached process is
          still running), EVERY overlapping barrier (including ``read_only`` and rollback)
          conflicts and fails closed.
        - Once all processes have exited (``lingering_pids == ()``), ``read_only``
          inspection tools and explicitly acknowledged ``rollback_edits`` may inspect/recover
          the workspace, and an identical ``candidate_action_id`` passes through to
          ``IntentLedger`` so ``reconcilable`` actions self-reconcile and ``non_idempotent``
          actions raise ``UncertainActionReplayError``. All other mutating/consequential
          actions touching the held barriers fail closed!
        """
        if not requested_barriers:
            return [], []
        with self._lock:
            state = self._read_state_unlocked()
            holds, dirty = self._refresh_hold_lingering_unlocked(state)
            if dirty:
                self._write_state_unlocked(state)

        conflicting_holds: list[PersistedBarrierHold] = []
        conflicting_targets: list[str] = []
        for hold in holds:
            has_live_lingering = bool(hold.lingering_pids)
            if not has_live_lingering:
                if action_class == "read_only" or allow_readonly_inspection:
                    continue
                if allow_acknowledged_rollback:
                    continue
                if candidate_action_id and hold.action_id and candidate_action_id == hold.action_id:
                    continue
            overlaps = _ps_barriers_conflict(hold.barriers, requested_barriers)
            if overlaps:
                conflicting_holds.append(hold)
                for tgt in overlaps:
                    if tgt not in conflicting_targets:
                        conflicting_targets.append(tgt)
        return conflicting_holds, conflicting_targets

    def find_resumption_holds(
        self,
        *,
        task_id: str,
        workspace_root: Path | str | None = None,
    ) -> list[PersistedBarrierHold]:
        """Return persisted holds blocking resumption on ``task_id`` or ``workspace_root``."""
        ws_str = ""
        if workspace_root is not None and str(workspace_root).strip():
            with contextlib.suppress(OSError):
                ws_str = str(Path(workspace_root).expanduser().resolve())
        probe_barriers: list[tuple[str, str]] = (
            [(f"workspace:{ws_str}", "exclusive")] if ws_str else []
        )
        with self._lock:
            state = self._read_state_unlocked()
            holds, dirty = self._refresh_hold_lingering_unlocked(state)
            if dirty:
                self._write_state_unlocked(state)

        blocking: list[PersistedBarrierHold] = []
        for h in holds:
            if h.task_id == task_id or h.session_id == task_id:
                blocking.append(h)
                continue
            if probe_barriers and _ps_barriers_conflict(h.barriers, probe_barriers):
                blocking.append(h)
        return blocking

    def release_hold(self, hold_id: str, *, force: bool = False) -> bool:
        """Release a persisted barrier hold once reconciled and zero lingering PIDs remain."""
        if not hold_id:
            return False
        with self._lock:
            state = self._read_state_unlocked()
            holds, dirty = self._refresh_hold_lingering_unlocked(state)
            _ = holds
            raw = state["persisted_barriers"].get(hold_id)
            if not isinstance(raw, Mapping):
                if dirty:
                    self._write_state_unlocked(state)
                return False
            hold = PersistedBarrierHold.from_mapping(raw)
            if hold.lingering_pids and not force:
                if dirty:
                    self._write_state_unlocked(state)
                pids_txt = ", ".join(str(p) for p, _ in hold.lingering_pids)
                raise ContainmentHoldActiveError(
                    f"Cannot release containment hold `{hold_id}` while detached/lingering "
                    f"process(es) remain alive: {pids_txt}."
                )
            state["persisted_barriers"].pop(hold_id, None)
            self._write_state_unlocked(state)
            return True

    def release_holds_for_action(self, action_id: str, *, force: bool = False) -> int:
        """Release all persisted barrier holds bound to ``action_id`` (if no lingering PIDs remain)."""
        if not action_id:
            return 0
        with self._lock:
            state = self._read_state_unlocked()
            holds, dirty = self._refresh_hold_lingering_unlocked(state)
            removed = 0
            for hold in holds:
                if hold.action_id != action_id and hold.hold_id != action_id:
                    continue
                if hold.lingering_pids and not force:
                    if dirty:
                        self._write_state_unlocked(state)
                    pids_txt = ", ".join(str(p) for p, _ in hold.lingering_pids)
                    raise ContainmentHoldActiveError(
                        f"Cannot reconcile action `{action_id}` while lingering process(es) "
                        f"remain alive: {pids_txt}."
                    )
                state["persisted_barriers"].pop(hold.hold_id, None)
                removed += 1
            if removed or dirty:
                self._write_state_unlocked(state)
            return removed

    def purge_session(self, session_id: str) -> int:
        """Remove persisted holds for a deleted session when no lingering processes remain."""
        if not session_id:
            return 0
        with self._lock:
            state = self._read_state_unlocked()
            holds, dirty = self._refresh_hold_lingering_unlocked(state)
            removed = 0
            for hold in holds:
                if hold.session_id == session_id and not hold.lingering_pids:
                    state["persisted_barriers"].pop(hold.hold_id, None)
                    removed += 1
            state["fenced_origins"].pop(f"incident_session:{session_id}", None)
            if removed or dirty:
                self._write_state_unlocked(state)
            return removed

    def list_holds(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        workspace_root: Path | str | None = None,
    ) -> list[PersistedBarrierHold]:
        return self.list_barrier_holds(
            session_id=session_id,
            task_id=task_id,
            workspace_root=workspace_root,
        )

    def record_incident(
        self,
        *,
        session_id: str = "",
        task_id: str | None = None,
        epoch: int | None = None,
        worker_id: str | None = None,
        reason: str = "security_incident",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        incident_id = f"inc-{uuid.uuid4().hex[:16]}"
        entry: dict[str, Any] = {
            "incident_id": incident_id,
            "ts": time.time(),
            "session_id": session_id,
            "task_id": task_id,
            "epoch": epoch,
            "worker_id": worker_id,
            "reason": str(reason)[:160],
            "details": dict(details) if isinstance(details, Mapping) else {},
        }
        with self._lock:
            state = self._read_state_unlocked()
            state["incident_history"].append(entry)
            self._write_state_unlocked(state)
        return entry

    def summary(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        workspace_root: Path | str | None = None,
    ) -> dict[str, Any]:
        eff_sid = session_id if session_id is not None else task_id
        ws_str = ""
        if workspace_root is not None and str(workspace_root).strip():
            with contextlib.suppress(OSError):
                ws_str = str(Path(workspace_root).expanduser().resolve())
        with self._lock:
            state = self._read_state_unlocked()
            holds, dirty = self._refresh_hold_lingering_unlocked(state)
            if dirty:
                self._write_state_unlocked(state)
            in_flight = [
                SupervisedProcessRecord.from_mapping(v)
                for v in state["in_flight_processes"].values()
                if isinstance(v, Mapping)
            ]
            fenced_count = len(state["fenced_origins"])
            quarantined = bool(
                eff_sid and f"incident_session:{eff_sid}" in state["fenced_origins"]
            )
            recent_incidents = list(state["incident_history"][-10:])
        if eff_sid is not None:
            in_flight = [
                r for r in in_flight if r.session_id == eff_sid or r.task_id == eff_sid
            ]
            holds = [
                h
                for h in holds
                if h.session_id == eff_sid
                or h.task_id == eff_sid
                or (task_id is not None and h.task_id == task_id)
                or (ws_str and h.workspace_root == ws_str)
            ]
            recent_incidents = [
                inc
                for inc in recent_incidents
                if isinstance(inc, Mapping)
                and (
                    inc.get("session_id") == eff_sid
                    or inc.get("task_id") == eff_sid
                    or (
                        isinstance(inc.get("record"), Mapping)
                        and (
                            inc["record"].get("session_id") == eff_sid
                            or inc["record"].get("task_id") == eff_sid
                        )
                    )
                )
            ]
        return {
            "schema_version": PS_CONTAINMENT_SCHEMA_VERSION,
            "quarantined": quarantined,
            "in_flight_processes": [r.to_mapping() for r in in_flight],
            "persisted_barriers": [h.to_mapping() for h in holds],
            "fenced_origin_count": fenced_count,
            "recent_incidents": recent_incidents,
        }


# ---------------------------------------------------------------------------
# Process Supervisor & Bounded 3-Stage Termination Coordinator
# ---------------------------------------------------------------------------


def _ps_default_send_signal(target: int, sig: int, *, process_group: bool) -> None:
    if target <= 1:
        return
    self_pid = os.getpid()
    self_pgid = os.getpgrp() if hasattr(os, "getpgrp") else -1
    if process_group:
        if target == self_pgid or not hasattr(os, "killpg"):
            return
        os.killpg(target, sig)
    else:
        if target == self_pid:
            return
        os.kill(target, sig)


@dataclass(slots=True)
class _PSLiveEntry:
    record: SupervisedProcessRecord
    proc: asyncio.subprocess.Process | None
    cancel_event: threading.Event
    contained_outcome: TerminationOutcome | None = None


class ProcessSupervisor:
    """Supervises subprocess groups, detached children, bounded cancellation, and restart recovery."""

    def __init__(
        self,
        store: ContainmentStore,
        *,
        probe_fn: Callable[[int], ProcessProbe | None] = probe_process,
        discover_fn: Callable[
            [SupervisedProcessRecord], tuple[list[ProcessProbe], list[ProcessProbe]]
        ] | None = None,
        signal_fn: Callable[[int, int, bool], None] | None = None,
        sync_container_cleanup_fn: Callable[[str], None] | None = None,
        async_container_cleanup_fn: Callable[[str], Awaitable[None]] | None = None,
        grace_timeout_s: float = DEFAULT_COOPERATIVE_GRACE_S,
        kill_timeout_s: float = DEFAULT_FORCE_KILL_WAIT_S,
    ) -> None:
        self._store = store
        self._probe_fn = probe_fn
        self._discover_fn = discover_fn or (
            lambda rec: discover_process_tree(rec, probe_fn=self._probe_fn)
        )
        self._signal_fn = signal_fn or (
            lambda target, sig, is_pg: _ps_default_send_signal(target, sig, process_group=is_pg)
        )
        self._sync_container_cleanup = sync_container_cleanup_fn
        self._async_container_cleanup = async_container_cleanup_fn
        self._grace_timeout_s = max(0.02, float(grace_timeout_s))
        self._kill_timeout_s = max(0.05, float(kill_timeout_s))
        self._lock = threading.RLock()
        self._live: dict[str, _PSLiveEntry] = {}
        self._contained_outcomes: dict[str, TerminationOutcome] = {}

    @property
    def store(self) -> ContainmentStore:
        return self._store

    def new_supervision_id(self) -> str:
        return f"sup-{uuid.uuid4().hex[:16]}"

    def prepare_spawn_env(
        self,
        base_env: Mapping[str, str],
        *,
        supervision_id: str,
        session_id: str = "",
        task_id: str | None = None,
        epoch: int | None = None,
        worker_id: str | None = None,
        current_boot_id: str = "",
    ) -> dict[str, str]:
        """Verify origin is not fenced/stale and inject ``M5_SUPERVISION_ID`` into child env."""
        if not _PS_SUPERVISION_ID_RE.match(supervision_id):
            raise ValueError(f"Invalid supervision_id: {supervision_id!r}")
        fenced, reason = self._store.is_origin_fenced(
            session_id=session_id,
            task_id=task_id,
            epoch=epoch,
            worker_id=worker_id,
            supervision_id=supervision_id,
            current_boot_id=current_boot_id,
        )
        if fenced:
            raise StaleOriginFencedError(reason)
        env = dict(base_env)
        env[SUPERVISION_ENV_VAR] = supervision_id
        return env

    @contextlib.contextmanager
    def supervision_context(self, supervision_id: str):
        tok = _PS_ACTIVE_SUPERVISION_ID.set(supervision_id)
        try:
            yield supervision_id
        finally:
            _PS_ACTIVE_SUPERVISION_ID.reset(tok)

    def register_process(
        self,
        proc: asyncio.subprocess.Process,
        *,
        supervision_id: str,
        tool_name: str,
        workspace_root: Path | None,
        container_name: str | None = None,
        session_id: str = "",
        task_id: str | None = None,
        epoch: int | None = None,
        worker_id: str | None = None,
        boot_id: str = "",
        action_id: str = "",
        barriers: Sequence[tuple[str, str]] = (),
        grant_id: str | None = None,
        capabilities: Sequence[str] = (),
        resource_scopes: Sequence[str] = (),
    ) -> SupervisedProcessRecord:
        pid = int(proc.pid or 0)
        if pid <= 1:
            raise ProcessSupervisionError(f"Refusing to register invalid subprocess PID: {pid}")
        pgid = pid
        if hasattr(os, "getpgid"):
            with contextlib.suppress(OSError):
                queried = os.getpgid(pid)
                if queried > 1:
                    pgid = queried
        probe = self._probe_fn(pid)
        start_fp = probe.start_fingerprint if probe is not None else ""
        uid = probe.uid if probe is not None else _ps_current_uid()
        ws_str = ""
        if workspace_root is not None:
            with contextlib.suppress(OSError):
                ws_str = str(workspace_root.resolve())
        eff_barriers = tuple(barriers)
        if not eff_barriers and ws_str and not tool_name.startswith("mcp__"):
            eff_barriers = ((f"workspace:{ws_str}", "exclusive"),)
        eff_scopes = tuple(str(s) for s in resource_scopes if str(s).strip())
        if not eff_scopes:
            eff_scopes = tuple(t for t, _ in eff_barriers)

        record = SupervisedProcessRecord(
            supervision_id=supervision_id,
            pid=pid,
            pgid=pgid,
            uid=uid,
            start_fingerprint=start_fp,
            container_name=container_name,
            tool_name=tool_name,
            session_id=session_id,
            task_id=task_id,
            epoch=epoch,
            worker_id=worker_id,
            boot_id=boot_id,
            action_id=action_id,
            workspace_root=ws_str,
            barriers=eff_barriers,
            started_at=time.time(),
            status="running",
            grant_id=(str(grant_id) if grant_id else None),
            capabilities=tuple(str(c) for c in capabilities if str(c).strip()),
            resource_scopes=eff_scopes,
        )
        self._store.record_process_started(record)
        with self._lock:
            self._live[supervision_id] = _PSLiveEntry(
                record=record,
                proc=proc,
                cancel_event=threading.Event(),
            )
        return record

    def get_contained_outcome(self, supervision_id: str) -> TerminationOutcome | None:
        with self._lock:
            entry = self._live.get(supervision_id)
            if entry is not None and entry.contained_outcome is not None:
                return entry.contained_outcome
            return self._contained_outcomes.get(supervision_id)

    def _signal_discovered(
        self,
        record: SupervisedProcessRecord,
        in_group: Sequence[ProcessProbe],
        detached: Sequence[ProcessProbe],
        sig: int,
    ) -> tuple[set[int], set[int]]:
        signaled: set[int] = set()
        detached_seen: set[int] = {p.pid for p in detached}
        self_pgid = os.getpgrp() if hasattr(os, "getpgrp") else -1

        if in_group and record.pgid > 1 and record.pgid != self_pgid:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                self._signal_fn(record.pgid, sig, True)
            signaled.update(p.pid for p in in_group)

        for p in (*in_group, *detached):
            if p.pgid > 1 and p.pgid != self_pgid and p.pgid == p.pid and p.pgid != record.pgid:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    self._signal_fn(p.pgid, sig, True)
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                self._signal_fn(p.pid, sig, False)
            signaled.add(p.pid)
        return signaled, detached_seen

    def _build_hold(
        self,
        record: SupervisedProcessRecord,
        *,
        reason: str,
        lingering_pids: tuple[tuple[int, str], ...] = (),
    ) -> PersistedBarrierHold:
        hold_id = record.action_id or f"hold-{record.supervision_id}"
        now_ts = time.time()
        return PersistedBarrierHold(
            hold_id=hold_id,
            session_id=record.session_id,
            task_id=record.task_id,
            epoch=record.epoch,
            worker_id=record.worker_id,
            action_id=record.action_id,
            supervision_id=record.supervision_id,
            tool_name=record.tool_name,
            workspace_root=record.workspace_root,
            barriers=record.barriers,
            reason=reason,
            lingering_pids=lingering_pids,
            created_at=now_ts,
            updated_at=now_ts,
        )

    def terminate_record_sync(
        self,
        record: SupervisedProcessRecord,
        *,
        reason: str,
        persist_hold: bool = True,
        fence_origin: bool = True,
        quarantine_session: bool = False,
        grace_timeout_s: float | None = None,
        kill_timeout_s: float | None = None,
    ) -> TerminationOutcome:
        """Execute synchronous 3-stage containment (`SIGTERM` -> grace -> `SIGKILL` -> verify)."""
        t0 = time.perf_counter()
        grace_s = self._grace_timeout_s if grace_timeout_s is None else max(0.01, float(grace_timeout_s))
        kill_s = self._kill_timeout_s if kill_timeout_s is None else max(0.02, float(kill_timeout_s))

        if fence_origin:
            self._store.fence_origin(
                reason=reason,
                session_id=record.session_id,
                task_id=record.task_id,
                epoch=record.epoch,
                worker_id=record.worker_id,
                supervision_id=record.supervision_id,
                quarantine_session=quarantine_session,
            )
        if persist_hold and record.barriers:
            self._store.persist_barrier_hold(self._build_hold(record, reason=reason))

        with self._lock:
            live_entry = self._live.get(record.supervision_id)
            if live_entry is not None:
                live_entry.cancel_event.set()

        in_group, detached = self._discover_fn(record)
        signaled_all: set[int] = set()
        detached_all: set[int] = set()
        cooperative_sent = False
        escalated_kill = False

        if in_group or detached:
            cooperative_sent = True
            sig_pids, det_pids = self._signal_discovered(record, in_group, detached, signal.SIGTERM)
            signaled_all.update(sig_pids)
            detached_all.update(det_pids)

            deadline_grace = time.monotonic() + grace_s
            while time.monotonic() < deadline_grace:
                rem_group, rem_det = self._discover_fn(record)
                if not rem_group and not rem_det:
                    break
                time.sleep(0.015)

            rem_group, rem_det = self._discover_fn(record)
            if rem_group or rem_det:
                escalated_kill = True
                sig_pids2, det_pids2 = self._signal_discovered(record, rem_group, rem_det, signal.SIGKILL)
                signaled_all.update(sig_pids2)
                detached_all.update(det_pids2)

        if record.container_name and self._sync_container_cleanup is not None:
            with contextlib.suppress(Exception):
                self._sync_container_cleanup(record.container_name)

        if escalated_kill:
            deadline_kill = time.monotonic() + kill_s
            while time.monotonic() < deadline_kill:
                rem_group, rem_det = self._discover_fn(record)
                if not rem_group and not rem_det:
                    break
                time.sleep(0.015)

        final_group, final_det = self._discover_fn(record)
        lingering = tuple((p.pid, p.start_fingerprint) for p in (*final_group, *final_det))
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        outcome = TerminationOutcome(
            supervision_id=record.supervision_id,
            reason=reason,
            cooperative_sent=cooperative_sent,
            escalated_kill=escalated_kill,
            signaled_pids=tuple(sorted(signaled_all)),
            detached_pids=tuple(sorted(detached_all)),
            lingering_pids=lingering,
            cleanly_reaped=(len(lingering) == 0),
            elapsed_ms=elapsed_ms,
        )

        if (persist_hold or lingering) and record.barriers:
            eff_reason = reason if not lingering else f"{reason}:lingering_detached_child"
            self._store.persist_barrier_hold(
                self._build_hold(record, reason=eff_reason, lingering_pids=lingering)
            )
        self._store.remove_in_flight_process(
            record.supervision_id,
            incident_entry={
                "ts": time.time(),
                "record": record.to_mapping(),
                "outcome": outcome.to_mapping(),
            },
        )
        with self._lock:
            entry = self._live.get(record.supervision_id)
            if entry is not None:
                entry.contained_outcome = outcome
            self._contained_outcomes[record.supervision_id] = outcome
            if len(self._contained_outcomes) > MAX_INCIDENT_HISTORY:
                oldest = next(iter(self._contained_outcomes))
                self._contained_outcomes.pop(oldest, None)
        return outcome

    async def terminate_record_async(
        self,
        record: SupervisedProcessRecord,
        *,
        reason: str,
        persist_hold: bool = True,
        fence_origin: bool = True,
        quarantine_session: bool = False,
        grace_timeout_s: float | None = None,
        kill_timeout_s: float | None = None,
    ) -> TerminationOutcome:
        """Execute async 3-stage containment while yielding to the asyncio loop for child reaping."""
        t0 = time.perf_counter()
        grace_s = self._grace_timeout_s if grace_timeout_s is None else max(0.01, float(grace_timeout_s))
        kill_s = self._kill_timeout_s if kill_timeout_s is None else max(0.02, float(kill_timeout_s))

        if fence_origin:
            self._store.fence_origin(
                reason=reason,
                session_id=record.session_id,
                task_id=record.task_id,
                epoch=record.epoch,
                worker_id=record.worker_id,
                supervision_id=record.supervision_id,
                quarantine_session=quarantine_session,
            )
        if persist_hold and record.barriers:
            self._store.persist_barrier_hold(self._build_hold(record, reason=reason))

        with self._lock:
            live_entry = self._live.get(record.supervision_id)
            if live_entry is not None:
                live_entry.cancel_event.set()

        in_group, detached = self._discover_fn(record)
        signaled_all: set[int] = set()
        detached_all: set[int] = set()
        cooperative_sent = False
        escalated_kill = False

        if in_group or detached:
            cooperative_sent = True
            sig_pids, det_pids = self._signal_discovered(record, in_group, detached, signal.SIGTERM)
            signaled_all.update(sig_pids)
            detached_all.update(det_pids)

            deadline_grace = time.monotonic() + grace_s
            while time.monotonic() < deadline_grace:
                await asyncio.sleep(0.015)
                rem_group, rem_det = self._discover_fn(record)
                if not rem_group and not rem_det:
                    break

            rem_group, rem_det = self._discover_fn(record)
            if rem_group or rem_det:
                escalated_kill = True
                sig_pids2, det_pids2 = self._signal_discovered(record, rem_group, rem_det, signal.SIGKILL)
                signaled_all.update(sig_pids2)
                detached_all.update(det_pids2)

        if record.container_name:
            if self._async_container_cleanup is not None:
                with contextlib.suppress(Exception):
                    await self._async_container_cleanup(record.container_name)
            elif self._sync_container_cleanup is not None:
                with contextlib.suppress(Exception):
                    self._sync_container_cleanup(record.container_name)

        if escalated_kill:
            deadline_kill = time.monotonic() + kill_s
            while time.monotonic() < deadline_kill:
                await asyncio.sleep(0.015)
                rem_group, rem_det = self._discover_fn(record)
                if not rem_group and not rem_det:
                    break

        final_group, final_det = self._discover_fn(record)
        lingering = tuple((p.pid, p.start_fingerprint) for p in (*final_group, *final_det))
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        outcome = TerminationOutcome(
            supervision_id=record.supervision_id,
            reason=reason,
            cooperative_sent=cooperative_sent,
            escalated_kill=escalated_kill,
            signaled_pids=tuple(sorted(signaled_all)),
            detached_pids=tuple(sorted(detached_all)),
            lingering_pids=lingering,
            cleanly_reaped=(len(lingering) == 0),
            elapsed_ms=elapsed_ms,
        )

        if (persist_hold or lingering) and record.barriers:
            eff_reason = reason if not lingering else f"{reason}:lingering_detached_child"
            self._store.persist_barrier_hold(
                self._build_hold(record, reason=eff_reason, lingering_pids=lingering)
            )
        self._store.remove_in_flight_process(
            record.supervision_id,
            incident_entry={
                "ts": time.time(),
                "record": record.to_mapping(),
                "outcome": outcome.to_mapping(),
            },
        )
        with self._lock:
            entry = self._live.get(record.supervision_id)
            if entry is not None:
                entry.contained_outcome = outcome
            self._contained_outcomes[record.supervision_id] = outcome
            if len(self._contained_outcomes) > MAX_INCIDENT_HISTORY:
                oldest = next(iter(self._contained_outcomes))
                self._contained_outcomes.pop(oldest, None)
        return outcome

    async def finalize_process_async(
        self,
        supervision_id: str,
        *,
        interrupted_reason: str = "",
    ) -> TerminationOutcome | None:
        """Finalize a supervised subprocess in ``finally``; sweep any detached children that outlived it."""
        with self._lock:
            entry = self._live.pop(supervision_id, None)
            prior_outcome = self._contained_outcomes.get(supervision_id)
        if entry is None:
            return prior_outcome
        if entry.contained_outcome is not None:
            return entry.contained_outcome
        if interrupted_reason:
            return await self.terminate_record_async(
                entry.record,
                reason=interrupted_reason,
                persist_hold=True,
                fence_origin=(interrupted_reason not in {"subprocess_timeout", "skill_script_timeout"}),
            )

        # Even when the leader exited normally (`returncode == 0`), sweep any orphaned
        # or `os.setsid()`-detached children so nothing outlives the acquired barrier!
        rem_group, rem_det = self._discover_fn(entry.record)
        if rem_group or rem_det:
            outcome = await self.terminate_record_async(
                entry.record,
                reason="detached_descendant_cleanup",
                persist_hold=False,
                fence_origin=False,
            )
            return outcome

        self._store.remove_in_flight_process(supervision_id)
        return None

    def _select_matching_records(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        epochs: Sequence[int] | None = None,
        worker_id: str | None = None,
    ) -> list[SupervisedProcessRecord]:
        epoch_set = {int(e) for e in epochs} if epochs else None
        candidates: dict[str, SupervisedProcessRecord] = {}
        with self._lock:
            for sup_id, entry in self._live.items():
                candidates[sup_id] = entry.record
        for persisted in self._store.list_in_flight_processes():
            candidates.setdefault(persisted.supervision_id, persisted)

        matched: list[SupervisedProcessRecord] = []
        for rec in candidates.values():
            if session_id is not None and rec.session_id != session_id and rec.task_id != session_id:
                continue
            if task_id is not None and rec.task_id != task_id and rec.session_id != task_id:
                continue
            if epoch_set is not None and (rec.epoch is None or rec.epoch not in epoch_set):
                continue
            if worker_id is not None and rec.worker_id != worker_id:
                continue
            matched.append(rec)
        return matched

    def contain_by_scope_sync(
        self,
        *,
        reason: str,
        session_id: str | None = None,
        task_id: str | None = None,
        epochs: Sequence[int] | None = None,
        worker_id: str | None = None,
        quarantine_session: bool = False,
        persist_hold: bool = True,
    ) -> list[TerminationOutcome]:
        """Synchronously fence matching origins and contain all matching in-flight subprocesses."""
        if epochs and task_id:
            for ep_num in epochs:
                self._store.fence_origin(
                    reason=reason,
                    session_id=session_id or "",
                    task_id=task_id,
                    epoch=int(ep_num),
                    worker_id=worker_id,
                    quarantine_session=quarantine_session,
                )
        else:
            self._store.fence_origin(
                reason=reason,
                session_id=session_id or "",
                task_id=task_id,
                worker_id=worker_id,
                quarantine_session=quarantine_session,
            )

        records = self._select_matching_records(
            session_id=session_id,
            task_id=task_id,
            epochs=epochs,
            worker_id=worker_id,
        )
        outcomes: list[TerminationOutcome] = []
        for rec in records:
            outcomes.append(
                self.terminate_record_sync(
                    rec,
                    reason=reason,
                    persist_hold=persist_hold,
                    fence_origin=True,
                    quarantine_session=quarantine_session,
                )
            )
        return outcomes

    async def contain_by_scope_async(
        self,
        *,
        reason: str,
        session_id: str | None = None,
        task_id: str | None = None,
        epochs: Sequence[int] | None = None,
        worker_id: str | None = None,
        quarantine_session: bool = False,
        persist_hold: bool = True,
    ) -> list[TerminationOutcome]:
        """Asynchronously fence matching origins and contain all matching in-flight subprocesses."""
        if epochs and task_id:
            for ep_num in epochs:
                self._store.fence_origin(
                    reason=reason,
                    session_id=session_id or "",
                    task_id=task_id,
                    epoch=int(ep_num),
                    worker_id=worker_id,
                    quarantine_session=quarantine_session,
                )
        else:
            self._store.fence_origin(
                reason=reason,
                session_id=session_id or "",
                task_id=task_id,
                worker_id=worker_id,
                quarantine_session=quarantine_session,
            )

        records = self._select_matching_records(
            session_id=session_id,
            task_id=task_id,
            epochs=epochs,
            worker_id=worker_id,
        )
        outcomes: list[TerminationOutcome] = []
        for rec in records:
            outcomes.append(
                await self.terminate_record_async(
                    rec,
                    reason=reason,
                    persist_hold=persist_hold,
                    fence_origin=True,
                    quarantine_session=quarantine_session,
                )
            )
        return outcomes

    @staticmethod
    def _ps_scope_overlaps(scope_a: str, scope_b: str) -> bool:
        if scope_a in {"*", "global:*"} or scope_b in {"*", "global:*"} or scope_a == scope_b:
            return True
        for wider, narrower in ((scope_a, scope_b), (scope_b, scope_a)):
            if wider.startswith("workspace:"):
                ws = wider.removeprefix("workspace:")
                if narrower.startswith(f"path:{ws}:") or narrower.startswith(f"dir:{ws}:"):
                    return True
            elif wider.startswith("dir:") and ":" in wider.removeprefix("dir:"):
                ws, rel = wider.removeprefix("dir:").rsplit(":", 1)
                rel = rel.strip("/")
                if narrower.startswith(f"path:{ws}:") or narrower.startswith(f"dir:{ws}:"):
                    n_rel = narrower.split(":", 2)[2].strip("/")
                    if n_rel == rel or n_rel.startswith(rel + "/"):
                        return True
            elif wider.startswith("mcp:") and narrower.startswith("mcp:"):
                w_parts = wider.split(":")
                n_parts = narrower.split(":")
                if len(w_parts) == 2 and (w_parts[1] == "*" or w_parts[1] == n_parts[1]):
                    return True
        return False

    def _select_capability_records(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        grant_ids: Collection[str] = (),
        capabilities: Collection[str] = (),
        resource_scopes: Collection[str] = (),
        tool_names: Collection[str] = (),
    ) -> list[SupervisedProcessRecord]:
        gid_set = {str(g) for g in grant_ids if str(g).strip()}
        cap_set = {str(c) for c in capabilities if str(c).strip()}
        scope_list = [str(s) for s in resource_scopes if str(s).strip()]
        tool_set = {str(t) for t in tool_names if str(t).strip()}
        if not gid_set and not cap_set and not scope_list and not tool_set:
            return []

        candidates: dict[str, SupervisedProcessRecord] = {}
        with self._lock:
            for sup_id, entry in self._live.items():
                candidates[sup_id] = entry.record
        for persisted in self._store.list_in_flight_processes():
            candidates.setdefault(persisted.supervision_id, persisted)

        matched: list[SupervisedProcessRecord] = []
        for rec in candidates.values():
            if (
                session_id is not None
                and session_id != "*"
                and rec.session_id != session_id
                and rec.task_id != session_id
            ):
                continue
            if task_id is not None and rec.task_id != task_id and rec.session_id != task_id:
                continue
            if gid_set and rec.grant_id and rec.grant_id in gid_set:
                matched.append(rec)
                continue
            if not cap_set and not scope_list and not tool_set:
                continue
            cap_ok = (not cap_set) or bool(set(rec.capabilities) & cap_set)
            tool_ok = bool(tool_set) and (rec.tool_name in tool_set)
            if cap_set and not cap_ok and not tool_ok:
                continue
            if tool_set and not cap_set and not tool_ok:
                continue
            rec_scopes = list(rec.resource_scopes) or [t for t, _ in rec.barriers]
            scope_ok = (not scope_list) or any(
                self._ps_scope_overlaps(r_sc, p_sc)
                for r_sc in scope_list
                for p_sc in (rec_scopes or ["global:*"])
            )
            if scope_ok:
                matched.append(rec)
        return matched

    def contain_by_capability_sync(
        self,
        *,
        reason: str,
        session_id: str | None = None,
        task_id: str | None = None,
        grant_ids: Collection[str] = (),
        capabilities: Collection[str] = (),
        resource_scopes: Collection[str] = (),
        tool_names: Collection[str] = (),
        persist_hold: bool = True,
    ) -> list[TerminationOutcome]:
        """Contain ONLY in-flight subprocesses matching the revoked grant/capability/resource scope.

        Fences only each terminated subprocess's ``supervision_id`` (not the
        entire worker/epoch/session), preserving unrelated live capabilities.
        """
        records = self._select_capability_records(
            session_id=session_id,
            task_id=task_id,
            grant_ids=grant_ids,
            capabilities=capabilities,
            resource_scopes=resource_scopes,
            tool_names=tool_names,
        )
        outcomes: list[TerminationOutcome] = []
        for rec in records:
            self._store.fence_origin(
                reason=reason,
                session_id=rec.session_id,
                task_id=rec.task_id,
                supervision_id=rec.supervision_id,
                quarantine_session=False,
            )
            outcomes.append(
                self.terminate_record_sync(
                    rec,
                    reason=reason,
                    persist_hold=persist_hold,
                    fence_origin=False,
                    quarantine_session=False,
                )
            )
        return outcomes

    async def contain_by_capability_async(
        self,
        *,
        reason: str,
        session_id: str | None = None,
        task_id: str | None = None,
        grant_ids: Collection[str] = (),
        capabilities: Collection[str] = (),
        resource_scopes: Collection[str] = (),
        tool_names: Collection[str] = (),
        persist_hold: bool = True,
    ) -> list[TerminationOutcome]:
        """Asynchronously contain ONLY in-flight subprocesses matching a revoked grant/capability/scope."""
        records = self._select_capability_records(
            session_id=session_id,
            task_id=task_id,
            grant_ids=grant_ids,
            capabilities=capabilities,
            resource_scopes=resource_scopes,
            tool_names=tool_names,
        )
        outcomes: list[TerminationOutcome] = []
        for rec in records:
            self._store.fence_origin(
                reason=reason,
                session_id=rec.session_id,
                task_id=rec.task_id,
                supervision_id=rec.supervision_id,
                quarantine_session=False,
            )
            outcomes.append(
                await self.terminate_record_async(
                    rec,
                    reason=reason,
                    persist_hold=persist_hold,
                    fence_origin=False,
                    quarantine_session=False,
                )
            )
        return outcomes

    def recover_on_startup(self, *, current_boot_id: str) -> StartupContainmentReport:
        """Sweep crashed in-flight subprocesses on startup, terminate survivors, and persist holds."""
        orphaned = self._store.list_in_flight_processes()
        swept_ids: list[str] = []
        terminated_pids: list[int] = []
        lingering_pids: list[int] = []
        hold_ids: list[str] = []
        fenced_added = 0

        for rec in orphaned:
            if rec.boot_id and rec.boot_id == current_boot_id and rec.supervision_id in self._live:
                continue
            swept_ids.append(rec.supervision_id)
            fenced_added += self._store.fence_origin(
                reason="crash_recovery_on_startup",
                session_id=rec.session_id,
                task_id=rec.task_id,
                epoch=rec.epoch,
                worker_id=rec.worker_id,
                supervision_id=rec.supervision_id,
            )
            outcome = self.terminate_record_sync(
                rec,
                reason="crash_interrupted_in_flight",
                persist_hold=True,
                fence_origin=False,
            )
            terminated_pids.extend(outcome.signaled_pids)
            lingering_pids.extend(pid for pid, _ in outcome.lingering_pids)
            if rec.barriers:
                hold_ids.append(rec.action_id or f"hold-{rec.supervision_id}")

        return StartupContainmentReport(
            swept_supervision_ids=tuple(swept_ids),
            terminated_pids=tuple(sorted(set(terminated_pids))),
            lingering_pids=tuple(sorted(set(lingering_pids))),
            persisted_hold_ids=tuple(hold_ids),
            fenced_origins_added=fenced_added,
        )


__all__ = [
    "ContainmentHoldActiveError",
    "ContainmentIntegrityError",
    "ContainmentStore",
    "DEFAULT_COOPERATIVE_GRACE_S",
    "DEFAULT_FORCE_KILL_WAIT_S",
    "FencedOriginRecord",
    "PS_CONTAINMENT_SCHEMA_VERSION",
    "PersistedBarrierHold",
    "ProcessProbe",
    "ProcessSupervisionError",
    "ProcessSupervisor",
    "SUPERVISION_ENV_VAR",
    "StartupContainmentReport",
    "StaleOriginFencedError",
    "SupervisedProcessRecord",
    "TerminationOutcome",
    "current_supervision_id",
    "discover_process_tree",
    "is_same_live_process",
    "probe_process",
]
