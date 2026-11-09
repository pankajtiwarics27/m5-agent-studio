"""Dormant LC-02 journal/queue kernel; intents are NOT native-operation authority.

Explicit construction is local disk I/O. Import performs no I/O. There is no
transport, background worker, automatic retry of an effect, or app integration.
The future consumer must supply authenticated/qualified evidence, session/grant
checks and LC-03/07 admission before acting on any returned reservation/intent.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypeVar

import runtime_persistence
from local_runtime_lifecycle import ModelInstanceObservation, ObservationStamp, RuntimeIdentity, RuntimeInventory

if os.name == "posix":
    import fcntl

_SCHEMA = 4
_APPLICATION_ID = 0x4D354C43
_MAX_INTEGER = 2**63 - 1
_T = TypeVar("_T")


class CoordinatorError(RuntimeError):
    """Bounded category only: never echo storage, model, or upstream bodies."""


def _label(value: object) -> None:
    if (not isinstance(value, str) or not value or len(value) > 512
            or value.strip() != value or not value.isprintable()):
        raise CoordinatorError("invalid_reference")


def _integer(value: object, *, positive: bool = False) -> None:
    if type(value) is not int or value < int(positive) or value > _MAX_INTEGER:
        raise CoordinatorError("invalid_integer")


def owner_namespace_path(home: Path, platform_name: str) -> Path:
    """Pure path selection, never a workspace/session-specific default."""
    if platform_name == "Darwin":
        return home / "Library/Application Support/M5 Agent Studio/lifecycle"
    if platform_name == "Linux":
        return home / ".local/state/m5-agent-studio/lifecycle"
    raise CoordinatorError("locking_platform_unsupported")


@dataclass(frozen=True, slots=True)
class ModelTarget:
    runtime_id: str
    artifact_id: str
    artifact_digest: str
    profile_fingerprint: str

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            _label(value)


def _encode(target: ModelTarget | None) -> str | None:
    return None if target is None else json.dumps(asdict(target), sort_keys=True, separators=(",", ":"))


def _decode(raw: str | None) -> ModelTarget | None:
    return None if raw is None else ModelTarget(**json.loads(raw))


@dataclass(frozen=True, slots=True)
class TransitionStep:
    operation_id: str
    domain_id: str
    generation: int
    owner_epoch: int
    kind: Literal["drain", "unload", "load", "verify"]
    source: ModelTarget | None
    source_instance_id: str | None
    target: ModelTarget

    def __post_init__(self) -> None:
        for value in (self.operation_id, self.domain_id):
            _label(value)
        for integer_value in (self.generation, self.owner_epoch):
            _integer(integer_value, positive=True)
        if self.kind not in ("drain", "unload", "load", "verify") or not isinstance(self.target, ModelTarget):
            raise CoordinatorError("invalid_step")
        if self.source is not None and not isinstance(self.source, ModelTarget):
            raise CoordinatorError("invalid_step")
        if (self.source is None) != (self.source_instance_id is None):
            raise CoordinatorError("invalid_step")
        if self.source_instance_id is not None:
            _label(self.source_instance_id)


@dataclass(frozen=True, slots=True)
class ReleaseStep:
    """Unload-only intent: real source/hold, no target and no load authority."""
    operation_id: str
    request_id: str
    domain_id: str
    generation: int
    owner_epoch: int
    kind: Literal["drain", "unload"]
    source: ModelTarget
    source_instance_id: str
    memory_id: str

    def __post_init__(self) -> None:
        for value in (self.operation_id, self.request_id, self.domain_id, self.source_instance_id, self.memory_id):
            _label(value)
        for integer_value in (self.generation, self.owner_epoch):
            _integer(integer_value, positive=True)
        if self.kind not in ("drain", "unload") or not isinstance(self.source, ModelTarget):
            raise CoordinatorError("invalid_release_step")


@dataclass(frozen=True, slots=True)
class RequestLease:
    lease_id: str
    request_id: str
    domain_id: str
    generation: int
    owner_epoch: int
    target: ModelTarget
    instance_id: str

    def __post_init__(self) -> None:
        for value in (self.lease_id, self.request_id, self.domain_id, self.instance_id):
            _label(value)
        for integer_value in (self.generation, self.owner_epoch):
            _integer(integer_value, positive=True)
        if not isinstance(self.target, ModelTarget):
            raise CoordinatorError("invalid_lease")


@dataclass(frozen=True, slots=True)
class LoadLease:
    """Exclusive explicit-load completion slot, never inference authority."""
    lease_id: str
    request_id: str
    domain_id: str
    generation: int
    owner_epoch: int
    target: ModelTarget
    instance_id: str

    def __post_init__(self) -> None:
        for value in (self.lease_id, self.request_id, self.domain_id, self.instance_id):
            _label(value)
        for integer_value in (self.generation, self.owner_epoch):
            _integer(integer_value, positive=True)
        if not isinstance(self.target, ModelTarget):
            raise CoordinatorError("invalid_lease")


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    owner_epoch: int
    generation: int
    operation_id: str | None
    purpose: str

    def __post_init__(self) -> None:
        _integer(self.owner_epoch, positive=True)
        _integer(self.generation)
        if self.operation_id is not None:
            _label(self.operation_id)
        if self.purpose not in ("reconcile", "unload", "verify", "load_control"):
            raise CoordinatorError("invalid_evidence_scope")
        if self.purpose != "reconcile" and self.operation_id is None:
            raise CoordinatorError("invalid_evidence_scope")


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    """Trusted-consumer attestations, not self-authenticating native proofs.

    Native producers remain unqualified. Empty inventory alone is insufficient:
    caller must establish terminality of ALL outstanding effects and headroom.
    Epoch fencing cannot cancel physical work. References contain no raw bodies.
    """

    inventories: tuple[RuntimeInventory, ...]
    clock_id: str
    now: float
    max_age: float
    reference: str
    scope: EvidenceScope
    operations_terminal: bool = False
    headroom_verified: bool = False
    owned_instance_ids: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _label(self.clock_id)
        _label(self.reference)
        if not isinstance(self.inventories, tuple) or not all(isinstance(x, RuntimeInventory) for x in self.inventories):
            raise CoordinatorError("invalid_evidence")
        if (not isinstance(self.owned_instance_ids, tuple)
                or any(not isinstance(x, tuple) or len(x) != 2 for x in self.owned_instance_ids)):
            raise CoordinatorError("invalid_ownership_evidence")
        for runtime_id, instance_id in self.owned_instance_ids:
            _label(runtime_id)
            _label(instance_id)
        if not isinstance(self.scope, EvidenceScope):
            raise CoordinatorError("invalid_evidence_scope")
        ObservationStamp().freshness(now=self.now, clock_id=self.clock_id, max_age=self.max_age)
        # LC-01 freshness validation rejects nonfinite/bool/invalid clocks.
        for item in self.inventories:
            item.stamp.freshness(now=self.now, clock_id=self.clock_id, max_age=self.max_age)


@dataclass(frozen=True, slots=True)
class DomainSnapshot:
    state: str
    generation: int
    resident: ModelTarget | None
    resident_instance_id: str | None
    operation_stage: str | None
    operation_status: str | None
    queued: int
    active_requests: int


class _Storage:
    def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
        conn = sqlite3.connect(database, timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE owner(singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL, last_now INTEGER NOT NULL)",
        "CREATE TABLE domains(id TEXT PRIMARY KEY, bindings TEXT NOT NULL, generation INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('reconciling','idle','switching')), resident TEXT, resident_instance_id TEXT, operation_id TEXT)",
        "CREATE TABLE requests(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, domain TEXT NOT NULL REFERENCES domains(id), target TEXT NOT NULL, deadline INTEGER NOT NULL, purpose TEXT NOT NULL CHECK(purpose IN ('inference','load','unload')), status TEXT NOT NULL CHECK(status IN ('queued','admitted','completed','cancelled','expired','interrupted')), cancel_requested INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE operations(id TEXT PRIMARY KEY, domain TEXT NOT NULL REFERENCES domains(id), generation INTEGER NOT NULL, owner_epoch INTEGER NOT NULL, source TEXT, source_instance_id TEXT, target TEXT, stage TEXT NOT NULL CHECK(stage IN ('drain','unload','load','verify')), dispatched INTEGER NOT NULL DEFAULT 0, deadline INTEGER, status TEXT NOT NULL CHECK(status IN ('active','uncertain','complete','reconciled')), goal TEXT NOT NULL DEFAULT 'acquire' CHECK(goal IN ('acquire','release')), request_id TEXT REFERENCES requests(id), source_memory_id TEXT REFERENCES memory_reservations(id), CHECK((goal='acquire' AND target IS NOT NULL AND request_id IS NULL AND source_memory_id IS NULL) OR (goal='release' AND target IS NULL AND source IS NOT NULL AND source_instance_id IS NOT NULL AND stage IN ('drain','unload') AND request_id IS NOT NULL AND source_memory_id IS NOT NULL)))",
        "CREATE UNIQUE INDEX one_pending_operation ON operations(domain) WHERE status IN ('active','uncertain')",
        "CREATE TABLE leases(id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL REFERENCES requests(id), domain TEXT NOT NULL REFERENCES domains(id), generation INTEGER NOT NULL, owner_epoch INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','released','interrupted')))",
        "CREATE UNIQUE INDEX one_active_request ON leases(domain) WHERE status='active'",
        "CREATE TABLE journal(seq INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, operation_id TEXT, event TEXT NOT NULL, reference TEXT, owner_epoch INTEGER NOT NULL, generation INTEGER NOT NULL)",
        "CREATE TABLE memory_domains(domain TEXT PRIMARY KEY REFERENCES domains(id), limit_bytes INTEGER NOT NULL CHECK(limit_bytes>0), reserve_bytes INTEGER NOT NULL CHECK(reserve_bytes>0), revision INTEGER NOT NULL CHECK(revision>=0), state TEXT NOT NULL CHECK(state IN ('reconciling','ready','paused')), policy_ref TEXT NOT NULL)",
        "CREATE UNIQUE INDEX one_shared_memory_domain ON memory_domains((1))",
        "CREATE TABLE memory_reservations(id TEXT PRIMARY KEY, domain TEXT NOT NULL REFERENCES memory_domains(domain), work_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('model','action')), peak_bytes INTEGER NOT NULL CHECK(peak_bytes>0), binding TEXT NOT NULL, owner_epoch INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('reserved','dispatched','uncertain','released')), UNIQUE(domain,work_id))",
        "CREATE UNIQUE INDEX one_memory_model ON memory_reservations(domain) WHERE kind='model' AND status!='released'",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO owner VALUES(1,0,0)")
    conn.execute(f"PRAGMA application_id={_APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version={_SCHEMA}")


class LifecycleCoordinator:
    """One process per canonical owner namespace, one persistent DB for all domains.

    All cooperating Studio instances must bind the SAME machine/owner namespace;
    arbitrary independent namespaces/other clients are outside this guarantee.
    No workspace/custom database argument exists. Unsupported locks fail closed.
    """

    def __init__(self, namespace_root: Path, *, queue_limit: int = 32, observation_max_age_s: float = 5.0) -> None:
        _integer(queue_limit, positive=True)
        if queue_limit > 1024:
            raise CoordinatorError("invalid_queue_limit")
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise CoordinatorError("locking_platform_unsupported")
        ObservationStamp().freshness(now=0, clock_id=None, max_age=observation_max_age_s)
        self._max_observation_age = observation_max_age_s
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        self._fd: int | None = None
        self._closed = False
        self._storage = _Storage()
        self._queue_limit = queue_limit
        self._epoch = 0
        self.observer_clock_id = uuid.uuid4().hex
        try:
            if namespace_root.is_symlink():
                raise CoordinatorError("unsafe_namespace")
            namespace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root = namespace_root.resolve(strict=True)
            self._root_stat = self.root.stat()
            if (not stat.S_ISDIR(self._root_stat.st_mode) or self._root_stat.st_uid != os.getuid()
                    or stat.S_IMODE(self._root_stat.st_mode) & 0o077):
                raise CoordinatorError("unsafe_namespace")
            self._fd = os.open(self.root / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self._lock_stat = os.fstat(self._fd)
            self._private_regular(self._lock_stat)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CoordinatorError("namespace_busy") from None
            self.database = self.root / "journal.sqlite3"
            created = False
            try:
                db_fd = os.open(self.database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(db_fd)
                created = True
            except FileExistsError:
                pass
            self._db_stat = self.database.lstat()
            self._private_regular(self._db_stat)
            if created:
                runtime_persistence.run_write_transaction(self._storage, self.database, _initialize_schema)
            self._bootstrap()
        except BaseException as exc:
            self.close()
            if isinstance(exc, CoordinatorError) or not isinstance(exc, Exception):
                raise
            raise CoordinatorError("journal_unavailable") from None

    @staticmethod
    def _private_regular(info: os.stat_result) -> None:
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise CoordinatorError("unsafe_namespace")

    def _owned(self) -> None:
        if self._closed or self._fd is None or os.getpid() != self._pid:
            raise CoordinatorError("owner_fenced")
        root, lock, database = self.root.stat(), (self.root / "owner.lock").lstat(), self.database.lstat()
        if ((root.st_dev, root.st_ino) != (self._root_stat.st_dev, self._root_stat.st_ino)
                or (lock.st_dev, lock.st_ino) != (self._lock_stat.st_dev, self._lock_stat.st_ino)
                or (database.st_dev, database.st_ino) != (self._db_stat.st_dev, self._db_stat.st_ino)
                or stat.S_IMODE(root.st_mode) & 0o077):
            raise CoordinatorError("owner_fenced")
        self._private_regular(lock)
        self._private_regular(database)

    def _bootstrap(self) -> None:
        def preflight(conn: sqlite3.Connection) -> None:
            if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA:
                raise CoordinatorError("journal_schema_unsupported")
            if conn.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
                raise CoordinatorError("journal_identity_invalid")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CoordinatorError("journal_invalid")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CoordinatorError("journal_invalid")
            owner = conn.execute("SELECT epoch,last_now FROM owner WHERE singleton=1").fetchone()
            if owner is None:
                raise CoordinatorError("journal_invalid")
            _integer(owner["epoch"])
            _integer(owner["last_now"])
            for row in conn.execute("SELECT * FROM domains"):
                _label(row["id"])
                bindings = tuple(RuntimeIdentity(**raw) for raw in json.loads(row["bindings"]))
                if (not bindings or len({x.runtime_id for x in bindings}) != len(bindings)
                        or any(x.memory_domain_id != row["id"] for x in bindings)):
                    raise CoordinatorError("journal_invalid")
                resident = _decode(row["resident"])
                if (resident is None) != (row["resident_instance_id"] is None):
                    raise CoordinatorError("journal_invalid")
                if row["resident_instance_id"] is not None:
                    _label(row["resident_instance_id"])
                _integer(row["generation"])
            for row in conn.execute("SELECT * FROM operations"):
                source, target = _decode(row["source"]), _decode(row["target"])
                if (source is None) != (row["source_instance_id"] is None):
                    raise CoordinatorError("journal_invalid")
                if row["goal"] == "acquire":
                    if target is None or row["request_id"] is not None or row["source_memory_id"] is not None:
                        raise CoordinatorError("journal_invalid")
                elif row["goal"] == "release":
                    request = conn.execute("SELECT * FROM requests WHERE id=?", (row["request_id"],)).fetchone()
                    memory = conn.execute("SELECT * FROM memory_reservations WHERE id=?", (row["source_memory_id"],)).fetchone()
                    if (target is not None or source is None or row["stage"] not in ("drain", "unload")
                            or request is None or request["purpose"] != "unload" or request["domain"] != row["domain"] or request["target"] != row["source"]
                            or memory is None or memory["kind"] != "model" or memory["domain"] != row["domain"]
                            or json.loads(memory["binding"]).get("profile", {}).get("target") != asdict(source)):
                        raise CoordinatorError("journal_invalid")
                else:
                    raise CoordinatorError("journal_invalid")
            for row in conn.execute("SELECT target,deadline,purpose FROM requests"):
                if _decode(row["target"]) is None or row["purpose"] not in ("inference", "load", "unload"):
                    raise CoordinatorError("journal_invalid")
                _integer(row["deadline"])
            for memory_domain in conn.execute("SELECT * FROM memory_domains"):
                for name in ("limit_bytes", "reserve_bytes"):
                    _integer(memory_domain[name], positive=True)
                _integer(memory_domain["revision"])
                _label(memory_domain["policy_ref"])
            for reservation in conn.execute("SELECT * FROM memory_reservations"):
                for name in ("id", "work_id"):
                    _label(reservation[name])
                _integer(reservation["peak_bytes"], positive=True)
                _integer(reservation["owner_epoch"], positive=True)
                from shared_memory_admission import MemoryDenied, _lease
                try:
                    _lease(reservation)
                except MemoryDenied:
                    raise CoordinatorError("journal_invalid") from None
                if reservation["owner_epoch"] > owner["epoch"]:
                    raise CoordinatorError("journal_invalid")
        runtime_persistence.run_read(self._storage, self.database, preflight)

        def claim(conn: sqlite3.Connection) -> None:
            epoch = conn.execute("SELECT epoch FROM owner WHERE singleton=1").fetchone()[0] + 1
            _integer(epoch, positive=True)
            conn.execute("UPDATE owner SET epoch=? WHERE singleton=1", (epoch,))
            conn.execute("UPDATE domains SET state='reconciling'")
            for memory_domain in conn.execute("SELECT revision FROM memory_domains"):
                _integer(memory_domain["revision"] + 1)
            conn.execute("UPDATE memory_domains SET state='reconciling',revision=revision+1")
            conn.execute("UPDATE memory_reservations SET status='uncertain' WHERE status!='released'")
            conn.execute("UPDATE operations SET status='uncertain' WHERE status='active'")
            conn.execute("UPDATE leases SET status='interrupted' WHERE status='active'")
            conn.execute("UPDATE requests SET status='interrupted' WHERE status='admitted'")
            self._epoch = epoch
            for row in conn.execute("SELECT id,operation_id FROM domains").fetchall():
                self._event(conn, row["id"], "owner_recovered", row["operation_id"])
        runtime_persistence.run_write_transaction(self._storage, self.database, claim)

    def close(self) -> None:
        if os.getpid() != self._pid:
            # Never acquire an inherited mutex potentially held by another thread.
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._closed = True
            return
        with self._mutex:
            if self._fd is not None:
                # A forked child must not unlock its parent's shared description.
                if os.getpid() == self._pid:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None
            self._closed = True

    def __enter__(self) -> LifecycleCoordinator:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _call(self, fn: Callable[[sqlite3.Connection], _T], *, now_ms: int | None = None) -> _T:
        if os.getpid() != self._pid:
            raise CoordinatorError("owner_fenced")
        with self._mutex:
            try:
                self._owned()
                def transaction(conn: sqlite3.Connection) -> _T:
                    row = conn.execute("SELECT epoch,last_now FROM owner WHERE singleton=1").fetchone()
                    if row is None or row["epoch"] != self._epoch:
                        raise CoordinatorError("owner_fenced")
                    if now_ms is not None:
                        _integer(now_ms)
                        if now_ms < row["last_now"]:
                            raise CoordinatorError("clock_regression")
                        conn.execute("UPDATE owner SET last_now=? WHERE singleton=1", (now_ms,))
                    return fn(conn)
                return runtime_persistence.run_write_transaction(self._storage, self.database, transaction)
            except CoordinatorError:
                raise
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                raise CoordinatorError("journal_unavailable") from None

    @staticmethod
    def _event(conn: sqlite3.Connection, domain: str, event: str, op: str | None = None, ref: str | None = None) -> None:
        epoch = conn.execute("SELECT epoch FROM owner WHERE singleton=1").fetchone()[0]
        generation = conn.execute("SELECT generation FROM domains WHERE id=?", (domain,)).fetchone()[0]
        conn.execute("INSERT INTO journal(domain,operation_id,event,reference,owner_epoch,generation) VALUES(?,?,?,?,?,?)", (domain, op, event, ref, epoch, generation))

    @staticmethod
    def _domain(conn: sqlite3.Connection, domain: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM domains WHERE id=?", (domain,)).fetchone()
        if row is None:
            raise CoordinatorError("domain_unknown")
        return row

    def register_domain(self, domain_id: str, runtimes: tuple[RuntimeIdentity, ...], *, now_ms: int) -> None:
        _label(domain_id)
        if (not isinstance(runtimes, tuple) or not runtimes or not all(isinstance(x, RuntimeIdentity) for x in runtimes)
                or any(x.memory_domain_id != domain_id for x in runtimes)
                or len({x.runtime_id for x in runtimes}) != len(runtimes)):
            raise CoordinatorError("invalid_domain_binding")
        bindings = json.dumps([asdict(x) for x in sorted(runtimes, key=lambda x: x.runtime_id)], sort_keys=True)
        def register(conn: sqlite3.Connection) -> None:
            old = conn.execute("SELECT bindings FROM domains WHERE id=?", (domain_id,)).fetchone()
            if old is not None:
                if old[0] != bindings:
                    raise CoordinatorError("domain_binding_changed")
                return
            conn.execute("INSERT INTO domains VALUES(?,?,0,'reconciling',NULL,NULL,NULL)", (domain_id, bindings))
            self._event(conn, domain_id, "registered")
        self._call(register, now_ms=now_ms)

    @staticmethod
    def _binding(row: sqlite3.Row, runtime_id: str) -> RuntimeIdentity:
        for raw in json.loads(row["bindings"]):
            if raw["runtime_id"] == runtime_id:
                return RuntimeIdentity(**raw)
        raise CoordinatorError("runtime_unbound")

    def enqueue(self, request_id: str, domain_id: str, target: ModelTarget, *, deadline_ms: int, now_ms: int) -> str:
        return self._enqueue(request_id, domain_id, target, purpose="inference", deadline_ms=deadline_ms, now_ms=now_ms)

    def enqueue_load(self, request_id: str, domain_id: str, target: ModelTarget, *, deadline_ms: int, now_ms: int) -> str:
        """Queue explicit-load intent, not a fake inference request or native grant."""
        return self._enqueue(request_id, domain_id, target, purpose="load", deadline_ms=deadline_ms, now_ms=now_ms)

    def enqueue_unload(self, request_id: str, domain_id: str, source: ModelTarget, *, deadline_ms: int, now_ms: int) -> str:
        """Queue exact owned-source release. The request reference is NOT a new target."""
        return self._enqueue(request_id, domain_id, source, purpose="unload", deadline_ms=deadline_ms, now_ms=now_ms)

    def _enqueue(self, request_id: str, domain_id: str, target: ModelTarget, *, purpose: Literal["inference", "load", "unload"], deadline_ms: int, now_ms: int) -> str:
        _label(request_id)
        _integer(deadline_ms)
        _integer(now_ms)
        if not isinstance(target, ModelTarget):
            raise CoordinatorError("invalid_request")
        def enqueue(conn: sqlite3.Connection) -> str:
            domain = self._domain(conn, domain_id)
            binding = self._binding(domain, target.runtime_id)
            if (binding.location != "local" or binding.egress != "local-only"
                    or binding.ownership_mode not in ("coordinated", "isolated")):
                raise CoordinatorError("control_binding_unqualified")
            conn.execute("UPDATE requests SET status='expired' WHERE domain=? AND status='queued' AND deadline<=?", (domain_id, now_ms))
            previous = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if previous is not None:
                if (previous["domain"], previous["target"], previous["deadline"], previous["purpose"]) != (domain_id, _encode(target), deadline_ms, purpose):
                    raise CoordinatorError("idempotency_conflict")
                return str(previous["status"])
            if deadline_ms <= now_ms:
                raise CoordinatorError("invalid_request")
            count = conn.execute("SELECT COUNT(*) FROM requests WHERE domain=? AND status='queued'", (domain_id,)).fetchone()[0]
            if count >= self._queue_limit:
                raise CoordinatorError("queue_full")
            conn.execute("INSERT INTO requests(id,domain,target,deadline,purpose,status) VALUES(?,?,?,?,?,'queued')", (request_id, domain_id, _encode(target), deadline_ms, purpose))
            self._event(conn, domain_id, {"load": "load_enqueued", "unload": "unload_enqueued"}.get(purpose, "enqueued"))
            return "queued"
        return self._call(enqueue, now_ms=now_ms)

    def cancel(self, request_id: str, *, now_ms: int) -> str:
        _label(request_id)
        def cancel(conn: sqlite3.Connection) -> str:
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise CoordinatorError("request_unknown")
            if row["status"] == "queued":
                conn.execute("UPDATE requests SET status='cancelled' WHERE id=?", (request_id,))
                self._event(conn, row["domain"], "cancelled")
                return "cancelled"
            if row["status"] == "admitted":
                if not row["cancel_requested"]:
                    conn.execute("UPDATE requests SET cancel_requested=1 WHERE id=?", (request_id,))
                    self._event(conn, row["domain"], "request_cancel_requested")
                return "pending_terminal"
            return str(row["status"])
        return self._call(cancel, now_ms=now_ms)

    def claim_next(self, domain_id: str, *, now_ms: int, step_timeout_ms: int = 60_000, request_id: str | None = None) -> TransitionStep | ReleaseStep | RequestLease | LoadLease | None:
        """Claim one domain stage/request; optionally fence to the FIFO waiter.

        request_id is an internal consumer fence, not authority. It never skips
        another queued owner or advances an operation for a different target.
        Native permits and independent completion evidence are still required.
        """
        if request_id is not None:
            _label(request_id)
        _integer(step_timeout_ms, positive=True)
        _integer(now_ms)
        _integer(now_ms + step_timeout_ms)
        def claim(conn: sqlite3.Connection) -> TransitionStep | ReleaseStep | RequestLease | LoadLease | None:
            domain = self._domain(conn, domain_id)
            conn.execute("UPDATE requests SET status='expired' WHERE domain=? AND status='queued' AND deadline<=?", (domain_id, now_ms))
            # Optional consumer fence: select/check FIFO ownership in the SAME
            # write transaction as claim, not a racy pre-query in an app actor.
            head = None
            if request_id is not None:
                head = conn.execute("SELECT * FROM requests WHERE domain=? AND status='queued' ORDER BY seq LIMIT 1", (domain_id,)).fetchone()
                if head is None or head["id"] != request_id:
                    return None
            if domain["state"] == "reconciling":
                return None
            if domain["operation_id"] is not None:
                operation = conn.execute("SELECT * FROM operations WHERE id=?", (domain["operation_id"],)).fetchone()
                if operation["status"] != "active":
                    return None  # Completed release still fences pending settlement.
                if operation["goal"] == "release":
                    if head is not None and (head["id"] != operation["request_id"] or head["purpose"] != "unload"):
                        return None
                elif head is not None and (operation["target"] != head["target"] or head["purpose"] == "unload"):
                    return None
                if operation["dispatched"]:
                    if now_ms >= operation["deadline"]:
                        self._uncertain(conn, domain_id, operation["id"], "step_timeout")
                    return None
            else:
                if conn.execute("SELECT 1 FROM leases WHERE domain=? AND status='active'", (domain_id,)).fetchone():
                    return None
                request = conn.execute("SELECT * FROM requests WHERE domain=? AND status='queued' ORDER BY seq LIMIT 1", (domain_id,)).fetchone()
                if request is None:
                    return None
                if request["purpose"] == "unload":
                    # Absent/mismatched/unowned sources need a separate qualified
                    # absence/recovery policy, not synthetic success or eviction.
                    if request["target"] != domain["resident"] or domain["resident_instance_id"] is None:
                        return None
                    memory = conn.execute("SELECT * FROM memory_reservations WHERE domain=? AND kind='model' AND status='dispatched' AND owner_epoch=?", (domain_id, self._epoch)).fetchone()
                    policy = conn.execute("SELECT state FROM memory_domains WHERE domain=?", (domain_id,)).fetchone()
                    if (memory is None or policy is None or policy["state"] != "ready"
                            or json.loads(memory["binding"]).get("profile", {}).get("target") != json.loads(request["target"])):
                        return None
                    operation_id = uuid.uuid4().hex
                    generation = domain["generation"] + 1
                    _integer(generation)
                    conn.execute("INSERT INTO operations(id,domain,generation,owner_epoch,source,source_instance_id,target,stage,status,goal,request_id,source_memory_id) VALUES(?,?,?,?,?,?,NULL,'drain','active','release',?,?)", (operation_id, domain_id, generation, self._epoch, domain["resident"], domain["resident_instance_id"], request["id"], memory["id"]))
                    conn.execute("UPDATE domains SET generation=?,state='switching',operation_id=? WHERE id=?", (generation, operation_id, domain_id))
                    conn.execute("UPDATE operations SET dispatched=1,deadline=? WHERE id=?", (now_ms + step_timeout_ms, operation_id))
                    self._event(conn, domain_id, "dispatch_release_drain", operation_id)
                    source = _decode(domain["resident"])
                    assert source is not None
                    return ReleaseStep(operation_id, request["id"], domain_id, generation, self._epoch, "drain", source, domain["resident_instance_id"], memory["id"])
                if request["target"] == domain["resident"]:
                    target = _decode(request["target"])
                    assert target is not None
                    cls = LoadLease if request["purpose"] == "load" else RequestLease
                    lease = cls(uuid.uuid4().hex, request["id"], domain_id, domain["generation"], self._epoch, target, domain["resident_instance_id"])
                    conn.execute("INSERT INTO leases VALUES(?,?,?,?,?,'active')", (lease.lease_id, lease.request_id, domain_id, lease.generation, self._epoch))
                    conn.execute("UPDATE requests SET status='admitted' WHERE id=?", (request["id"],))
                    self._event(conn, domain_id, "load_reserved" if request["purpose"] == "load" else "request_reserved")
                    return lease
                operation_id = uuid.uuid4().hex
                generation = domain["generation"] + 1
                _integer(generation)
                stage = "load" if domain["resident"] is None else "drain"
                conn.execute("INSERT INTO operations(id,domain,generation,owner_epoch,source,source_instance_id,target,stage,dispatched,deadline,status) VALUES(?,?,?,?,?,?,?,?,0,NULL,'active')", (operation_id, domain_id, generation, self._epoch, domain["resident"], domain["resident_instance_id"], request["target"], stage))
                conn.execute("UPDATE domains SET generation=?,state='switching',operation_id=? WHERE id=?", (generation, operation_id, domain_id))
                operation = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            conn.execute("UPDATE operations SET dispatched=1,deadline=? WHERE id=?", (now_ms + step_timeout_ms, operation["id"]))
            self._event(conn, domain_id, "dispatch_" + operation["stage"], operation["id"])
            if operation["goal"] == "release":
                source = _decode(operation["source"])
                assert source is not None
                return ReleaseStep(operation["id"], operation["request_id"], domain_id, operation["generation"], self._epoch, operation["stage"], source, operation["source_instance_id"], operation["source_memory_id"])
            target = _decode(operation["target"])
            assert target is not None
            return TransitionStep(operation["id"], domain_id, operation["generation"], self._epoch, operation["stage"], _decode(operation["source"]), operation["source_instance_id"], target)
        return self._call(claim, now_ms=now_ms)

    def _transition_operation(self, conn: sqlite3.Connection, step: TransitionStep, *, allow_uncertain: bool = False) -> sqlite3.Row | None:
        if type(step) is not TransitionStep:
            return None
        row = conn.execute("SELECT * FROM operations WHERE id=?", (step.operation_id,)).fetchone()
        if (row is None or row["goal"] != "acquire" or step.owner_epoch != self._epoch or row["owner_epoch"] != self._epoch
                or row["domain"] != step.domain_id or row["generation"] != step.generation
                or row["stage"] != step.kind or not row["dispatched"]
                or row["status"] not in (("active", "uncertain") if allow_uncertain else ("active",))
                or _decode(row["source"]) != step.source or row["source_instance_id"] != step.source_instance_id
                or _decode(row["target"]) != step.target):
            return None
        return row

    def _active_step(self, conn: sqlite3.Connection, step: TransitionStep, now_ms: int) -> sqlite3.Row | None:
        row = self._transition_operation(conn, step)
        if row is None:
            return None
        if now_ms >= row["deadline"]:
            self._uncertain(conn, step.domain_id, step.operation_id, "late_completion")
            return None
        return row

    def _release_operation(self, conn: sqlite3.Connection, step: ReleaseStep) -> sqlite3.Row | None:
        if type(step) is not ReleaseStep:
            return None
        row = conn.execute("SELECT * FROM operations WHERE id=?", (step.operation_id,)).fetchone()
        domain = self._domain(conn, step.domain_id)
        if (row is None or row["goal"] != "release" or row["target"] is not None
                or row["domain"] != step.domain_id or row["generation"] != step.generation
                or row["owner_epoch"] != self._epoch or step.owner_epoch != self._epoch
                or row["request_id"] != step.request_id or row["source_memory_id"] != step.memory_id
                or row["source"] != _encode(step.source) or row["source_instance_id"] != step.source_instance_id
                or domain["generation"] != step.generation or domain["operation_id"] != step.operation_id):
            return None
        return row

    def _active_release(self, conn: sqlite3.Connection, step: ReleaseStep, now_ms: int, *, finishing: bool = False) -> sqlite3.Row | None:
        """Native release gate, separate from acquire and inference authority.

        Only an already-sent stage may finish after its request was cancelled or
        expired; the final control status remains cancelled/expired, not success.
        """
        row = self._release_operation(conn, step)
        if row is None or row["status"] != "active" or row["stage"] != step.kind or not row["dispatched"]:
            return None
        if now_ms >= row["deadline"]:
            self._uncertain(conn, step.domain_id, step.operation_id, "late_native_release")
            return None
        domain = self._domain(conn, step.domain_id)
        request = conn.execute("SELECT * FROM requests WHERE id=?", (step.request_id,)).fetchone()
        memory = conn.execute("SELECT * FROM memory_reservations WHERE id=?", (step.memory_id,)).fetchone()
        policy = conn.execute("SELECT state FROM memory_domains WHERE domain=?", (step.domain_id,)).fetchone()
        if (domain["state"] != "switching" or domain["resident"] != _encode(step.source) or domain["resident_instance_id"] != step.source_instance_id
                or request is None or request["domain"] != step.domain_id or request["purpose"] != "unload" or request["target"] != _encode(step.source)
                or request["status"] not in (("queued", "cancelled", "expired") if finishing else ("queued",))
                or (not finishing and (request["cancel_requested"] or request["deadline"] <= now_ms))
                or memory is None or memory["status"] != "dispatched" or memory["kind"] != "model" or memory["domain"] != step.domain_id or memory["owner_epoch"] != self._epoch
                or json.loads(memory["binding"]).get("profile", {}).get("target") != asdict(step.source)
                or policy is None or policy["state"] != "ready"
                or conn.execute("SELECT 1 FROM leases WHERE domain=? AND status='active'", (step.domain_id,)).fetchone()):
            return None
        return row

    def mark_release_uncertain(self, step: ReleaseStep, *, reference: str, now_ms: int) -> bool:
        """Fence even a physically completed release whose settlement failed."""
        _label(reference)
        def mark(conn: sqlite3.Connection) -> bool:
            row = self._release_operation(conn, step)
            if row is None or row["status"] not in ("active", "complete"):
                return False
            conn.execute("UPDATE operations SET status='uncertain' WHERE id=?", (step.operation_id,))
            self._uncertain(conn, step.domain_id, step.operation_id, reference)
            return True
        return self._call(mark, now_ms=now_ms)

    def complete_release(self, step: ReleaseStep, *, reference: str, now_ms: int, evidence: ReconciliationEvidence | None = None) -> bool:
        """Physical stage proof only. Keep the barrier until real memory settlement."""
        _label(reference)
        def complete(conn: sqlite3.Connection) -> bool:
            row = self._release_operation(conn, step)
            if row is None or row["status"] != "active" or row["stage"] != step.kind or not row["dispatched"]:
                return False
            if now_ms >= row["deadline"]:
                self._uncertain(conn, step.domain_id, step.operation_id, "late_release_completion")
                return False
            domain = self._domain(conn, step.domain_id)
            if domain["state"] != "switching":
                return False
            if conn.execute("SELECT 1 FROM leases WHERE domain=? AND status='active'", (step.domain_id,)).fetchone():
                raise CoordinatorError("requests_not_drained")
            if step.kind == "drain":
                conn.execute("UPDATE operations SET stage='unload',dispatched=0,deadline=NULL WHERE id=?", (step.operation_id,))
            else:
                if evidence is None:
                    raise CoordinatorError("release_unverified")
                self._verify_evidence(domain, evidence, None, now_ms, "unload")
                conn.execute("UPDATE operations SET status='complete' WHERE id=?", (step.operation_id,))
                conn.execute("UPDATE domains SET resident=NULL,resident_instance_id=NULL WHERE id=?", (step.domain_id,))
            self._event(conn, step.domain_id, "complete_release_" + step.kind, step.operation_id, reference)
            return True
        return self._call(complete, now_ms=now_ms)

    def finish_unload(self, step: ReleaseStep, *, reference: str, now_ms: int) -> bool:
        """Retire the barrier ONLY after the bound real model hold was settled.

        True means barrier retirement, not a canonical completed API command:
        cancelled/expired requests keep those statuses. No native consumer exists.
        """
        _label(reference)
        def finish(conn: sqlite3.Connection) -> bool:
            row = self._release_operation(conn, step)
            if row is None or row["status"] != "complete" or row["stage"] != "unload" or step.kind != "unload":
                return False
            domain = self._domain(conn, step.domain_id)
            request = conn.execute("SELECT * FROM requests WHERE id=?", (step.request_id,)).fetchone()
            memory = conn.execute("SELECT * FROM memory_reservations WHERE id=?", (step.memory_id,)).fetchone()
            policy = conn.execute("SELECT state FROM memory_domains WHERE domain=?", (step.domain_id,)).fetchone()
            if (domain["state"] != "switching" or domain["resident"] is not None or domain["resident_instance_id"] is not None
                    or request is None or request["purpose"] != "unload" or request["domain"] != step.domain_id or request["target"] != _encode(step.source)
                    or request["status"] not in ("queued", "cancelled", "expired")
                    or memory is None or memory["status"] != "released" or memory["kind"] != "model" or memory["owner_epoch"] != self._epoch or memory["domain"] != step.domain_id
                    or json.loads(memory["binding"]).get("profile", {}).get("target") != asdict(step.source)
                    or policy is None or policy["state"] != "ready"
                    or conn.execute("SELECT 1 FROM memory_reservations WHERE domain=? AND kind='model' AND status!='released'", (step.domain_id,)).fetchone()):
                return False
            status = "cancelled" if request["cancel_requested"] else request["status"] if request["status"] in ("cancelled", "expired") else "expired" if request["deadline"] <= now_ms else "completed"
            conn.execute("UPDATE requests SET status=? WHERE id=?", (status, step.request_id))
            conn.execute("UPDATE domains SET state='idle',operation_id=NULL WHERE id=?", (step.domain_id,))
            self._event(conn, step.domain_id, "release_settled", step.operation_id, reference)
            return True
        return self._call(finish, now_ms=now_ms)

    def _uncertain(self, conn: sqlite3.Connection, domain: str, op: str | None, ref: str) -> None:
        conn.execute("UPDATE domains SET state='reconciling' WHERE id=?", (domain,))
        if op is not None:
            conn.execute("UPDATE operations SET status='uncertain' WHERE id=? AND status='active'", (op,))
        self._event(conn, domain, "uncertain", op, ref)

    def mark_uncertain(self, step: TransitionStep, *, reference: str, now_ms: int) -> bool:
        _label(reference)
        def mark(conn: sqlite3.Connection) -> bool:
            row = self._active_step(conn, step, now_ms)
            if row is None:
                return False
            self._uncertain(conn, step.domain_id, step.operation_id, reference)
            return True
        return self._call(mark, now_ms=now_ms)

    def note_pending(self, step: TransitionStep, *, now_ms: int) -> bool:
        """Record an ACK/202 without changing stage, reservation or deadline."""
        def note(conn: sqlite3.Connection) -> bool:
            if self._active_step(conn, step, now_ms) is None:
                return False
            self._event(conn, step.domain_id, "pending_ack", step.operation_id)
            return True
        return self._call(note, now_ms=now_ms)

    def _verify_evidence(self, domain: sqlite3.Row, evidence: ReconciliationEvidence, target: ModelTarget | None, now_ms: int, purpose: str, *, scope_id: str | None = None) -> str | None:
        if not isinstance(evidence, ReconciliationEvidence) or evidence.operations_terminal is not True or evidence.headroom_verified is not True:
            raise CoordinatorError("release_unverified")
        expected_scope = EvidenceScope(self._epoch, domain["generation"], scope_id if scope_id is not None else domain["operation_id"], purpose)
        if (evidence.scope != expected_scope or evidence.clock_id != self.observer_clock_id
                or evidence.now != now_ms / 1000):
            raise CoordinatorError("evidence_scope_mismatch")
        expected = {x["runtime_id"]: RuntimeIdentity(**x) for x in json.loads(domain["bindings"])}
        actual = {x.runtime.runtime_id: x for x in evidence.inventories}
        if len(actual) != len(evidence.inventories) or set(actual) != set(expected):
            raise CoordinatorError("inventory_incomplete")
        loaded: list[tuple[str, ModelInstanceObservation]] = []
        for runtime_id, inventory in actual.items():
            if (inventory.runtime != expected[runtime_id] or inventory.reachability != "reachable"
                    or inventory.completeness != "complete"
                    or inventory.stamp.freshness(now=evidence.now, clock_id=evidence.clock_id, max_age=min(evidence.max_age, self._max_observation_age)) != "fresh"):
                raise CoordinatorError("inventory_unverified")
            for item in inventory.instances:
                if item.active_requests != 0 or item.state not in ("loaded", "absent"):
                    raise CoordinatorError("runtime_not_quiescent")
                if item.state == "absent":
                    if item.resident_memory_bytes not in (None, 0):
                        raise CoordinatorError("release_unverified")
                else:
                    loaded.append((runtime_id, item))
        if target is None:
            if loaded:
                raise CoordinatorError("resident_conflict")
        else:
            if len(loaded) != 1:
                raise CoordinatorError("resident_conflict")
            runtime_id, item = loaded[0]
            assert isinstance(item, ModelInstanceObservation)
            if (runtime_id != target.runtime_id or item.artifact_id != target.artifact_id
                    or item.artifact_digest != target.artifact_digest
                    or item.load_profile_fingerprint != target.profile_fingerprint
                    or item.instance_id is None or item.effective_context_tokens is None):
                raise CoordinatorError("target_unverified")
            if (runtime_id, item.instance_id) not in evidence.owned_instance_ids:
                raise CoordinatorError("ownership_unverified")
            return item.instance_id
        return None

    def complete_step(self, step: TransitionStep, *, reference: str, now_ms: int, evidence: ReconciliationEvidence | None = None) -> bool:
        """Confirm a terminal stage. Unload/verify require domain-wide evidence."""
        _label(reference)
        def complete(conn: sqlite3.Connection) -> bool:
            if self._active_step(conn, step, now_ms) is None:
                return False
            domain = self._domain(conn, step.domain_id)
            instance_id = None
            if step.kind in ("unload", "verify"):
                if evidence is None:
                    raise CoordinatorError("release_unverified")
                instance_id = self._verify_evidence(domain, evidence, None if step.kind == "unload" else step.target, now_ms, step.kind)
            if step.kind == "drain" and conn.execute("SELECT 1 FROM leases WHERE domain=? AND status='active'", (step.domain_id,)).fetchone():
                raise CoordinatorError("requests_not_drained")
            if step.kind == "verify":
                conn.execute("UPDATE operations SET status='complete' WHERE id=?", (step.operation_id,))
                conn.execute("UPDATE domains SET state='idle',resident=?,resident_instance_id=?,operation_id=NULL WHERE id=?", (_encode(step.target), instance_id, step.domain_id))
            else:
                next_stage = {"drain": "unload", "unload": "load", "load": "verify"}[step.kind]
                conn.execute("UPDATE operations SET stage=?,dispatched=0,deadline=NULL WHERE id=?", (next_stage, step.operation_id))
                if step.kind == "unload":
                    conn.execute("UPDATE domains SET resident=NULL,resident_instance_id=NULL WHERE id=?", (step.domain_id,))
            self._event(conn, step.domain_id, "complete_" + step.kind, step.operation_id, reference)
            return True
        return self._call(complete, now_ms=now_ms)

    def reconcile(self, domain_id: str, evidence: ReconciliationEvidence, *, now_ms: int, adopt: ModelTarget | None = None) -> None:
        def reconcile(conn: sqlite3.Connection) -> None:
            domain = self._domain(conn, domain_id)
            if domain["state"] != "reconciling":
                raise CoordinatorError("reconciliation_not_required")
            if adopt is not None:
                known = [domain["resident"]]
                if domain["operation_id"]:
                    old = conn.execute("SELECT source,target FROM operations WHERE id=?", (domain["operation_id"],)).fetchone()
                    known.extend(old)
                if _encode(adopt) not in known:
                    raise CoordinatorError("external_resident_unowned")
            instance_id = self._verify_evidence(domain, evidence, adopt, now_ms, "reconcile")
            if domain["operation_id"]:
                conn.execute("UPDATE operations SET status='reconciled' WHERE id=?", (domain["operation_id"],))
            conn.execute("UPDATE leases SET status='interrupted' WHERE domain=? AND status='active'", (domain_id,))
            conn.execute("UPDATE requests SET status='interrupted' WHERE domain=? AND status='admitted'", (domain_id,))
            conn.execute("UPDATE domains SET state='idle',resident=?,resident_instance_id=?,operation_id=NULL WHERE id=?", (_encode(adopt), instance_id, domain_id))
            self._event(conn, domain_id, "reconciled", domain["operation_id"], evidence.reference)
        self._call(reconcile, now_ms=now_ms)

    def _active_lease(self, conn: sqlite3.Connection, lease: RequestLease | LoadLease, purpose: Literal["inference", "load"]) -> sqlite3.Row | None:
        expected = RequestLease if purpose == "inference" else LoadLease
        if type(lease) is not expected:
            return None
        row = conn.execute("SELECT * FROM leases WHERE id=?", (lease.lease_id,)).fetchone()
        request = conn.execute("SELECT * FROM requests WHERE id=?", (lease.request_id,)).fetchone()
        domain = self._domain(conn, lease.domain_id)
        if (row is None or request is None or request["purpose"] != purpose or request["status"] != "admitted"
                or request["domain"] != lease.domain_id or request["target"] != _encode(lease.target)
                or row["status"] != "active" or row["owner_epoch"] != self._epoch
                or lease.owner_epoch != self._epoch or row["request_id"] != lease.request_id
                or row["domain"] != lease.domain_id or row["generation"] != lease.generation
                or domain["generation"] != lease.generation or domain["resident"] != _encode(lease.target)
                or domain["resident_instance_id"] != lease.instance_id):
            return None
        return request

    def mark_request_uncertain(self, lease: RequestLease, *, reference: str, now_ms: int) -> bool:
        """Hold the domain after an inference disconnect; do not free its slot."""
        return self._mark_lease_uncertain(lease, "inference", reference=reference, now_ms=now_ms)

    def mark_load_uncertain(self, lease: LoadLease, *, reference: str, now_ms: int) -> bool:
        """Retain an interrupted control slot; do not infer native completion."""
        return self._mark_lease_uncertain(lease, "load", reference=reference, now_ms=now_ms)

    def _mark_lease_uncertain(self, lease: RequestLease | LoadLease, purpose: Literal["inference", "load"], *, reference: str, now_ms: int) -> bool:
        _label(reference)
        def mark(conn: sqlite3.Connection) -> bool:
            if self._active_lease(conn, lease, purpose) is None:
                return False
            self._uncertain(conn, lease.domain_id, None, reference)
            return True
        return self._call(mark, now_ms=now_ms)

    def finish_request(self, lease: RequestLease, *, reference: str, now_ms: int) -> bool:
        """Caller-confirmed terminal inference releases ONLY its request slot."""
        _label(reference)
        def finish(conn: sqlite3.Connection) -> bool:
            if self._active_lease(conn, lease, "inference") is None:
                return False
            conn.execute("UPDATE leases SET status='released' WHERE id=?", (lease.lease_id,))
            conn.execute("UPDATE requests SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'completed' END WHERE id=?", (lease.request_id,))
            self._event(conn, lease.domain_id, "request_finished", ref=reference)
            return True
        return self._call(finish, now_ms=now_ms)

    def finish_load(self, lease: LoadLease, evidence: ReconciliationEvidence, *, reference: str, now_ms: int) -> bool:
        """Proof-based CONTROL slot completion, not per-request fit or memory settlement.

        A cancelled/expired admitted control still needs independent terminal
        proof to release its slot; it is never reported as a completed request.
        A consumer must separately enforce installed qualification and LC03.
        """
        _label(reference)
        return self._call(lambda conn: self._finish_load(conn, lease, evidence, reference=reference, now_ms=now_ms), now_ms=now_ms)

    def _finish_load(self, conn: sqlite3.Connection, lease: LoadLease, evidence: ReconciliationEvidence, *, reference: str, now_ms: int) -> bool:
        """Internal atomic seam; native/memory consumer owns this SAME write."""
        _label(reference)
        request = self._active_lease(conn, lease, "load")
        if request is None:
            return False
        domain = self._domain(conn, lease.domain_id)
        if domain["state"] != "idle" or domain["operation_id"] is not None:
            return False
        instance = self._verify_evidence(domain, evidence, lease.target, now_ms, "load_control", scope_id=lease.lease_id)
        if instance != lease.instance_id:
            raise CoordinatorError("target_unverified")
        status = "cancelled" if request["cancel_requested"] else "expired" if request["deadline"] <= now_ms else "completed"
        conn.execute("UPDATE leases SET status='released' WHERE id=?", (lease.lease_id,))
        conn.execute("UPDATE requests SET status=? WHERE id=?", (status, lease.request_id))
        self._event(conn, lease.domain_id, "load_finished", ref=reference)
        return True

    def evidence_scope(self, domain_id: str) -> EvidenceScope:
        """Scope for a NEW observation; not a proof generator or acceptance."""
        def scope(conn: sqlite3.Connection) -> EvidenceScope:
            domain = self._domain(conn, domain_id)
            purpose = "reconcile"
            if domain["state"] != "reconciling":
                op = conn.execute("SELECT stage,dispatched,status FROM operations WHERE id=?", (domain["operation_id"],)).fetchone()
                if op is None:
                    control = conn.execute("SELECT l.id FROM leases l JOIN requests r ON r.id=l.request_id WHERE l.domain=? AND l.status='active' AND r.purpose='load' AND r.status='admitted' AND l.owner_epoch=? AND l.generation=?", (domain_id, self._epoch, domain["generation"])).fetchone()
                    if control is None:
                        raise CoordinatorError("observation_not_requested")
                    return EvidenceScope(self._epoch, domain["generation"], control["id"], "load_control")
                if not op["dispatched"] or op["status"] != "active" or op["stage"] not in ("unload", "verify"):
                    raise CoordinatorError("observation_not_requested")
                purpose = op["stage"]
            return EvidenceScope(self._epoch, domain["generation"], domain["operation_id"], purpose)
        return self._call(scope)

    def snapshot(self, domain_id: str) -> DomainSnapshot:
        def snapshot(conn: sqlite3.Connection) -> DomainSnapshot:
            domain = self._domain(conn, domain_id)
            op = conn.execute("SELECT stage,status FROM operations WHERE id=?", (domain["operation_id"],)).fetchone()
            queued = conn.execute("SELECT COUNT(*) FROM requests WHERE domain=? AND status='queued'", (domain_id,)).fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM leases WHERE domain=? AND status='active'", (domain_id,)).fetchone()[0]
            return DomainSnapshot(domain["state"], domain["generation"], _decode(domain["resident"]), domain["resident_instance_id"], None if op is None else op["stage"], None if op is None else op["status"], queued, active)
        return self._call(snapshot)
