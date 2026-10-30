"""Durable, cursor-addressable task event stream for harness sessions (M5-P1-07).

The runtime already writes one append-only JSONL log per harness session under
``.harnesses/sessions/<session_id>.jsonl``. That log is the source the UI
re-renders after a reload, so it has to behave like a durable event stream
rather than a debug trace:

* **Monotonic, gap-detectable sequence numbers.** Every record carries a ``seq``
  that strictly increases per session. A reader that remembers ``seq = N`` can
  ask for ``> N`` and can *prove* whether it missed anything: a pruned history,
  a counter rollback after a restore, or a crash between counter allocation and
  append all show up as a discontinuity instead of silently vanishing.
* **At-most-once terminal records.** Terminal turn outcomes and approval
  decisions can be appended with a ``dedupe_key``. Re-appending the same key
  returns the record that is already durable instead of writing a second one, so
  a retried or concurrently resumed turn cannot emit two "turn completed" or two
  "approval decided" records.
* **Bounded storage.** Logs are pruned to a byte budget, keeping the newest
  events. Pruning is visible to readers through ``retained_from_seq`` so an old
  cursor is answered with ``snapshot_required`` instead of a half history.
* **Explicit truncation.** A torn or non-JSON line is skipped and counted; it is
  never invented or repaired into a fake event.

Durability contract: appends use ``O_APPEND`` + ``fsync`` of the record, and the
sequence sidecar (``<session>.seq``) is replaced atomically. The store assumes a
single application process owns a session at a time, which matches the runtime's
own worker-lease model; concurrent appends from separate processes are not
supported and are not claimed to be.

This module is standard-library only and does no I/O outside the store directory
it is given. It never invents events, never deletes a session directory, and
never mutates a record after it is written.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TASK_EVENT_STREAM_VERSION = 1

DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 1000
#: Roll the log over once it grows past this size, keeping the newest events.
DEFAULT_MAX_LOG_BYTES = 8 * 1024 * 1024
DEFAULT_RETAIN_EVENTS = 4000
#: How often a live stream emits a heartbeat frame while idle.
STREAM_HEARTBEAT_SECONDS = 10.0

#: Turn outcomes that end a turn; the newest one decides the reconstructed state.
TERMINAL_TURN_EVENTS = frozenset({"turn/end"})
#: Turn-end states that mean the turn reached a final outcome.
TERMINAL_TURN_STATES = frozenset({"completed", "cancelled", "failed"})

_APPEND_LOCK = threading.Lock()
_DEDUPE_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
_DEDUPE_CACHE_SESSIONS: dict[str, str] = {}


class TaskEventError(Exception):
    """Raised when a session id or cursor cannot address the event store safely."""


def _safe_session_id(session_id: str) -> str:
    """Return a store-safe session id or raise.

    Session ids become file names, so anything that can escape the store
    directory is rejected rather than sanitized (a sanitized id would silently
    point at a different session).
    """
    sid = (session_id or "").strip()
    if not sid or len(sid) > 128:
        raise TaskEventError("Task event session id must be 1-128 characters.")
    if ".." in sid or "/" in sid or "\\" in sid:
        raise TaskEventError(f"Unsafe task event session id: {session_id!r}")
    if not all(ch.isalnum() or ch in "-_." for ch in sid):
        raise TaskEventError(f"Unsafe task event session id: {session_id!r}")
    return sid


@dataclass(frozen=True)
class TaskEventSnapshot:
    """Event-derived view of a session, used for reload reconstruction."""

    session_id: str
    latest_seq: int
    retained_from_seq: int
    event_count: int
    turns_started: int
    turns_completed: int
    last_turn_state: str
    terminal: bool
    undecided_approvals: tuple[str, ...]
    approval_decisions: tuple[tuple[str, str], ...]
    last_event_type: str
    last_event_ts: float
    skipped_records: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "stream_version": TASK_EVENT_STREAM_VERSION,
            "latest_seq": self.latest_seq,
            "retained_from_seq": self.retained_from_seq,
            "event_count": self.event_count,
            "turns_started": self.turns_started,
            "turns_completed": self.turns_completed,
            "last_turn_state": self.last_turn_state,
            "terminal": self.terminal,
            "undecided_approvals": list(self.undecided_approvals),
            "approval_decisions": [
                {"approval_id": aid, "decision": decision} for aid, decision in self.approval_decisions
            ],
            "last_event_type": self.last_event_type,
            "last_event_ts": self.last_event_ts,
            "skipped_records": self.skipped_records,
        }


@dataclass(frozen=True)
class TaskEventPage:
    """One cursor read over a session's event log."""

    session_id: str
    events: tuple[dict[str, Any], ...]
    next_cursor: int
    latest_seq: int
    retained_from_seq: int
    has_more: bool
    truncated: bool
    snapshot_required: bool
    resync_reason: str
    missing_ranges: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "stream_version": TASK_EVENT_STREAM_VERSION,
            "events": list(self.events),
            "next_cursor": self.next_cursor,
            "latest_seq": self.latest_seq,
            "retained_from_seq": self.retained_from_seq,
            "has_more": self.has_more,
            "truncated": self.truncated,
            "snapshot_required": self.snapshot_required,
            "resync_reason": self.resync_reason,
            "missing_ranges": [{"from_seq": start, "to_seq": end} for start, end in self.missing_ranges],
        }


def _valid_record(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("seq"), int)
        and not isinstance(record.get("seq"), bool)
        and int(record["seq"]) > 0
        and isinstance(record.get("type"), str)
    )


class TaskEventLog:
    """Append-only per-session event log with cursors, dedupe, and pruning."""

    def __init__(
        self,
        store_dir: Path,
        *,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        retain_events: int = DEFAULT_RETAIN_EVENTS,
    ) -> None:
        self.store_dir = Path(store_dir)
        self.max_log_bytes = max(64 * 1024, int(max_log_bytes))
        self.retain_events = max(16, int(retain_events))

    # -- paths ------------------------------------------------------------

    def log_path(self, session_id: str) -> Path:
        return self.store_dir / f"{_safe_session_id(session_id)}.jsonl"

    def cursor_path(self, session_id: str) -> Path:
        return self.store_dir / f"{_safe_session_id(session_id)}.seq"

    # -- reading ----------------------------------------------------------

    def load(self, session_id: str) -> tuple[list[dict[str, Any]], int]:
        """Return ``(valid_records, skipped_line_count)`` in file order."""
        path = self.log_path(session_id)
        if not path.exists():
            return [], 0
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return [], 0
        records: list[dict[str, Any]] = []
        skipped = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if _valid_record(parsed):
                records.append(parsed)
            else:
                skipped += 1
        return records, skipped

    def all_events(self, session_id: str) -> list[dict[str, Any]]:
        """Every retained event, in sequence order (backward-compatible list view)."""
        records, _ = self.load(session_id)
        return sorted(records, key=lambda rec: int(rec["seq"]))

    def latest_seq(self, session_id: str) -> int:
        records, _ = self.load(session_id)
        if not records:
            return 0
        return max(int(rec["seq"]) for rec in records)

    def retained_from_seq(self, session_id: str) -> int:
        records, _ = self.load(session_id)
        if not records:
            return 0
        return min(int(rec["seq"]) for rec in records)

    def read(self, session_id: str, *, cursor: int = 0, limit: int = DEFAULT_READ_LIMIT) -> TaskEventPage:
        """Return the events after ``cursor`` plus an explicit resync verdict."""
        safe_start = max(0, int(cursor))
        safe_limit = max(1, min(int(limit), MAX_READ_LIMIT))
        records, _ = self.load(session_id)
        records.sort(key=lambda rec: int(rec["seq"]))
        latest = int(records[-1]["seq"]) if records else 0
        retained = int(records[0]["seq"]) if records else 0

        truncated = bool(records) and retained > 1

        def _empty(reason: str, missing: tuple[tuple[int, int], ...] = ()) -> TaskEventPage:
            return TaskEventPage(
                session_id=session_id,
                events=(),
                next_cursor=safe_start,
                latest_seq=latest,
                retained_from_seq=retained,
                has_more=False,
                truncated=truncated,
                snapshot_required=bool(reason),
                resync_reason=reason,
                missing_ranges=missing,
            )

        if safe_start > latest:
            # The reader is ahead of the store: a different database, a restored
            # backup, or a brand-new store. Only a snapshot can re-anchor it.
            return _empty("cursor_ahead")
        if safe_start > 0 and records and safe_start + 1 < retained:
            # A reader that had already applied events is missing pruned history:
            # never let it continue as if the log were complete. A reader at
            # cursor 0 has no prior state to contradict, so it gets the retained
            # tail (with `truncated` set) instead of a refusal.
            return _empty("events_pruned", ((safe_start + 1, retained - 1),))

        selected = [rec for rec in records if int(rec["seq"]) > safe_start][:safe_limit]
        missing: list[tuple[int, int]] = []
        # A fresh reader (cursor 0) starts at the oldest retained event: history
        # pruned before that is `truncated`, not an unrecoverable gap in what the
        # reader was promised. Gaps after that point are real.
        expected = safe_start + 1 if safe_start > 0 else (retained or 1)
        for rec in selected:
            seq = int(rec["seq"])
            if seq > expected:
                missing.append((expected, seq - 1))
            expected = seq + 1
        if not selected and safe_start < latest:
            missing.append((safe_start + 1, latest))
        next_cursor = int(selected[-1]["seq"]) if selected else safe_start
        return TaskEventPage(
            session_id=session_id,
            events=tuple(selected),
            next_cursor=next_cursor,
            latest_seq=latest,
            retained_from_seq=retained,
            has_more=bool(selected) and next_cursor < latest,
            truncated=truncated,
            snapshot_required=bool(missing),
            resync_reason="sequence_gap" if missing else "",
            missing_ranges=tuple(missing),
        )

    # -- writing ----------------------------------------------------------

    def _next_seq(self, session_id: str, records: Iterable[dict[str, Any]]) -> int:
        """Allocate the next sequence number, durably and monotonically."""
        cursor_file = self.cursor_path(session_id)
        recorded = 0
        if cursor_file.exists():
            try:
                recorded = int(cursor_file.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                recorded = 0
        existing = max((int(rec["seq"]) for rec in records), default=0)
        return max(recorded, existing) + 1

    def _write_cursor(self, session_id: str, seq: int) -> None:
        cursor_file = self.cursor_path(session_id)
        tmp = cursor_file.with_name(cursor_file.name + ".tmp")
        tmp.write_text(str(seq), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, cursor_file)

    def _dedupe_index(self, session_id: str, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        key = str(self.log_path(session_id).resolve())
        cached = _DEDUPE_CACHE.get(key)
        if cached is None or _DEDUPE_CACHE_SESSIONS.get(key) != session_id:
            cached = {}
            for rec in records:
                dedupe_key = rec.get("dedupe_key")
                if isinstance(dedupe_key, str) and dedupe_key and dedupe_key not in cached:
                    cached[dedupe_key] = rec
            _DEDUPE_CACHE[key] = cached
            _DEDUPE_CACHE_SESSIONS[key] = session_id
        return cached

    def find_by_dedupe_key(self, session_id: str, dedupe_key: str) -> dict[str, Any] | None:
        if not dedupe_key:
            return None
        records, _ = self.load(session_id)
        return self._dedupe_index(session_id, records).get(dedupe_key)

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
        ts: float | None = None,
    ) -> dict[str, Any]:
        """Append one event and return the durable record.

        With a ``dedupe_key`` the append is idempotent: if a record with that key
        is already retained, it is returned unchanged and nothing is written.
        """
        safe_id = _safe_session_id(session_id)
        if not isinstance(event_type, str) or not event_type.strip():
            raise TaskEventError("Task event type must be a non-empty string.")
        body = dict(payload or {})
        body.pop("seq", None)
        body.pop("ts", None)
        body.pop("type", None)
        body.pop("dedupe_key", None)
        with _APPEND_LOCK:
            self.store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            records, _ = self.load(safe_id)
            if dedupe_key:
                existing = self._dedupe_index(safe_id, records).get(dedupe_key)
                if existing is not None:
                    return dict(existing)
            seq = self._next_seq(safe_id, records)
            record: dict[str, Any] = {**body, "seq": seq, "ts": round(time.time() if ts is None else ts, 3), "type": event_type}
            if dedupe_key:
                record["dedupe_key"] = dedupe_key
            self._write_cursor(safe_id, seq)
            line = json.dumps(record, ensure_ascii=False) + "\n"
            fd = os.open(self.log_path(safe_id), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            if dedupe_key:
                self._dedupe_index(safe_id, records)[dedupe_key] = record
            self._maybe_prune(safe_id)
            return record

    def _maybe_prune(self, session_id: str) -> None:
        path = self.log_path(session_id)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= self.max_log_bytes:
            return
        self.prune(session_id, retain=self.retain_events)

    def prune(self, session_id: str, *, retain: int = DEFAULT_RETAIN_EVENTS) -> int:
        """Keep the newest ``retain`` events; return how many were dropped."""
        safe_id = _safe_session_id(session_id)
        records, _ = self.load(safe_id)
        keep = max(1, int(retain))
        if len(records) <= keep:
            return 0
        dropped = records[: -keep]
        kept = records[-keep:]
        path = self.log_path(safe_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in kept),
            encoding="utf-8",
        )
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        # The dedupe index must forget pruned keys; rebuild it from kept records.
        _DEDUPE_CACHE.pop(str(path.resolve()), None)
        _DEDUPE_CACHE_SESSIONS.pop(str(path.resolve()), None)
        self._dedupe_index(safe_id, kept)
        return len(dropped)

    # -- reconstruction ---------------------------------------------------

    def reconstruct(self, session_id: str) -> TaskEventSnapshot:
        """Recompute session facts from the durable log alone."""
        safe_id = _safe_session_id(session_id)
        records, skipped = self.load(safe_id)
        records.sort(key=lambda rec: int(rec["seq"]))
        latest = int(records[-1]["seq"]) if records else 0
        retained = int(records[0]["seq"]) if records else 0
        turns_started = 0
        turns_completed = 0
        last_turn_state = ""
        undecided: list[str] = []
        decisions: dict[str, str] = {}
        for rec in records:
            event_type = rec.get("type")
            if event_type == "turn/start":
                turns_started += 1
            elif event_type == "turn/end":
                state = str(rec.get("state") or "")
                if state:
                    last_turn_state = state
                if state == "completed":
                    turns_completed += 1
            elif event_type == "approval/request":
                approval_id = rec.get("approval_id")
                if isinstance(approval_id, str) and approval_id and approval_id not in decisions:
                    if approval_id not in undecided:
                        undecided.append(approval_id)
            elif event_type == "approval/decision":
                approval_id = rec.get("approval_id")
                if isinstance(approval_id, str) and approval_id:
                    decisions[approval_id] = str(rec.get("decision") or "")
                    if approval_id in undecided:
                        undecided.remove(approval_id)
        return TaskEventSnapshot(
            session_id=safe_id,
            latest_seq=latest,
            retained_from_seq=retained,
            event_count=len(records),
            turns_started=turns_started,
            turns_completed=turns_completed,
            last_turn_state=last_turn_state,
            terminal=last_turn_state in TERMINAL_TURN_STATES,
            undecided_approvals=tuple(undecided),
            approval_decisions=tuple(decisions.items()),
            last_event_type=str(records[-1].get("type") or "") if records else "",
            last_event_ts=float(records[-1].get("ts") or 0.0) if records else 0.0,
            skipped_records=skipped,
        )

    def approval_decision(self, session_id: str, approval_id: str) -> str:
        """Return the durable decision for an approval id, or ``""`` if undecided."""
        if not approval_id:
            return ""
        for approval, decision in self.reconstruct(session_id).approval_decisions:
            if approval == approval_id:
                return decision
        return ""


def parse_cursor(value: Any, *, default: int = 0) -> int:
    """Parse a cursor from a query string, header, or JSON value without raising."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


__all__ = [
    "DEFAULT_MAX_LOG_BYTES",
    "DEFAULT_READ_LIMIT",
    "DEFAULT_RETAIN_EVENTS",
    "MAX_READ_LIMIT",
    "STREAM_HEARTBEAT_SECONDS",
    "TASK_EVENT_STREAM_VERSION",
    "TaskEventError",
    "TaskEventLog",
    "TaskEventPage",
    "TaskEventSnapshot",
    "parse_cursor",
]
