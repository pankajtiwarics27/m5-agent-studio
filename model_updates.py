"""Model revision tracking with pinning, rollback, and safe invalidation.

AS-057 turns AS-053 discovery output into a per-(runtime, model_id) revision
lifecycle: newly observed digests are recorded, an upgrade can be adopted
explicitly or auto-adopted, a revision can be *pinned* so drift is refused,
*rolled back* to the most recently active revision, and *invalidated* safely
— an invalidated digest never serves as active state, is skipped by rollback
fallbacks, and cannot be adopted until an operator clears the invalidation
with a reason.

The ledger is pure state: it executes nothing, loads no model, contacts no
runtime, and stores no wall-clock timestamps. Events form a deterministic,
bounded audit trail (sequence numbers, trimmed oldest-first with an overflow
count). Status lookups (``status_for``) let dependent artifacts — AS-056
evaluation scores, reassembled transcripts, caches — detect that a pinned
descriptor identity became superseded, missing, or invalidated.

Recover-first asymmetry is deliberate: pinning refuses automatic and explicit
forward promotion, but rollback and invalidation always work — safety valves
must not require un-pinning first. Any pin whose digest is no longer active
is released automatically, so a pin never claims a stale guarantee.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Union

from model_discovery import ModelCatalog
from model_registry import ModelDescriptor

RevisionState = Literal["active", "superseded", "observed", "missing", "invalidated", "unknown"]

EventKind = Literal[
    "adopted",
    "adoption_refused",
    "update_available",
    "pinned",
    "unpinned",
    "rolled_back",
    "missing",
    "restored",
    "invalidated",
    "invalidation_cleared",
]

_DEFAULT_HISTORY_LIMIT = 16
_DEFAULT_EVENT_LIMIT = 256


class ModelUpdateError(ValueError):
    """Raised when a revision-lifecycle operation is invalid or refused."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    result = value.strip()
    if not result:
        raise ModelUpdateError(f"{field} must not be empty.")
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ModelUpdateError(f"{field} must not contain control characters.")
    return result


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelUpdateError(f"{field} must be a positive integer.")
    return value


@dataclass(frozen=True, slots=True)
class RevisionEvent:
    """One deterministic audit record of a lifecycle transition."""

    sequence: int
    kind: EventKind
    runtime: str
    model_id: str
    previous_digest: str | None
    new_digest: str | None
    reason: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "runtime": self.runtime,
            "model_id": self.model_id,
            "previous_digest": self.previous_digest,
            "new_digest": self.new_digest,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RevisionAssessment:
    """The current lifecycle view for one (runtime, model_id) identity."""

    runtime: str
    model_id: str
    active_digest: str | None
    pinned_digest: str | None
    missing: bool
    pending_digests: tuple[str, ...]
    history_digests: tuple[str, ...]
    invalidated: tuple[tuple[str, str], ...]

    @property
    def has_active(self) -> bool:
        return self.active_digest is not None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "model_id": self.model_id,
            "active_digest": self.active_digest,
            "pinned_digest": self.pinned_digest,
            "missing": self.missing,
            "pending_digests": list(self.pending_digests),
            "history_digests": list(self.history_digests),
            "invalidated": [[digest, reason] for digest, reason in self.invalidated],
        }


def _usable(descriptor: ModelDescriptor) -> bool:
    return descriptor.provenance.verification != "rejected"


@dataclass(slots=True)
class _KeyState:
    active: ModelDescriptor | None = None
    pending: dict[str, ModelDescriptor] = field(default_factory=dict)
    history: list[ModelDescriptor] = field(default_factory=list)
    pinned: str | None = None
    missing: bool = False
    invalidated: dict[str, str] = field(default_factory=dict)


class ModelRevisionLedger:
    """Tracks observed revisions per (runtime, model_id) with bounded audits."""

    def __init__(
        self,
        *,
        auto_adopt_updates: bool = False,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
        event_limit: int = _DEFAULT_EVENT_LIMIT,
    ) -> None:
        if not isinstance(auto_adopt_updates, bool):
            raise TypeError("auto_adopt_updates must be a boolean.")
        self._auto_adopt = auto_adopt_updates
        self._history_limit = _positive_int(history_limit, "history_limit")
        self._event_limit = _positive_int(event_limit, "event_limit")
        self._events: list[RevisionEvent] = []
        self._dropped_events = 0
        self._sequence = 0
        self._state: dict[tuple[str, str], _KeyState] = {}

    # -- audit trail -------------------------------------------------------

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    def events(self, *, model_id: str | None = None) -> tuple[RevisionEvent, ...]:
        if model_id is None:
            return tuple(self._events)
        wanted = _text(model_id, "model_id")
        return tuple(event for event in self._events if event.model_id == wanted)

    def _record(self, kind: EventKind, runtime: str, model_id: str, previous: str | None, new: str | None, reason: str) -> RevisionEvent:
        self._sequence += 1
        event = RevisionEvent(self._sequence, kind, runtime, model_id, previous, new, reason)
        self._events.append(event)
        while len(self._events) > self._event_limit:
            self._events.pop(0)
            self._dropped_events += 1
        return event

    # -- state access ------------------------------------------------------

    def _key_state(self, runtime: str, model_id: str) -> _KeyState:
        key = (runtime, model_id)
        state = self._state.get(key)
        if state is None:
            state = _KeyState()
            self._state[key] = state
        return state

    def _resolve(self, model_id: str, runtime: str | None) -> tuple[_KeyState, tuple[str, str]]:
        wanted = _text(model_id, "model_id")
        if runtime is not None:
            normalized = _text(runtime, "runtime")
            key = (normalized, wanted)
            state = self._state.get(key)
            if state is None:
                raise ModelUpdateError(f"model {wanted} has no observed revisions in runtime {normalized}; run observe first.")
            return state, key
        matches = sorted(key for key in self._state if key[1] == wanted)
        if not matches:
            raise ModelUpdateError(f"model {wanted} has no observed revisions; run observe first.")
        if len(matches) > 1:
            runtimes = ", ".join(runtime_name for runtime_name, _model in matches)
            raise ModelUpdateError(f"model {wanted} was observed in multiple runtimes ({runtimes}); specify runtime.")
        return self._state[matches[0]], matches[0]

    # -- discovery merge ---------------------------------------------------

    def observe(self, observation: Union[ModelCatalog, Sequence[ModelDescriptor], None] = None) -> tuple[RevisionEvent, ...]:
        """Merge one discovery result into the ledger and report new events.

        A runtime that was consulted and reported available, yet no longer
        lists the active digest, marks the revision *missing* (transient
        outage — never destructive). A runtime absent from the observation
        entirely marks nothing: absence of information is not removal.
        """
        if isinstance(observation, ModelCatalog):
            descriptors = observation.descriptors
            consulted = {entry.runtime for entry in observation.runtimes}
            available = {entry.runtime for entry in observation.runtimes if entry.available}
        elif observation is None:
            raise ModelUpdateError("observe requires a catalog or descriptor sequence.")
        else:
            if isinstance(observation, (str, bytes)) or not isinstance(observation, Sequence):
                raise TypeError("observation must be a ModelCatalog or a sequence of ModelDescriptor values.")
            for descriptor in observation:
                if not isinstance(descriptor, ModelDescriptor):
                    raise TypeError("observation sequences must contain ModelDescriptor values.")
            descriptors = tuple(observation)
            consulted = {descriptor.runtime for descriptor in descriptors}
            available = consulted

        before = self._sequence
        seen: dict[tuple[str, str], dict[str, ModelDescriptor]] = {}
        for descriptor in descriptors:
            seen.setdefault((descriptor.runtime, descriptor.model_id), {})[descriptor.digest] = descriptor

        for key in sorted(seen):
            runtime, model_id = key
            state = self._key_state(runtime, model_id)
            for digest, descriptor in seen[key].items():
                if digest == (state.active.digest if state.active is not None else None):
                    state.pending.pop(digest, None)
                    if state.missing:
                        state.missing = False
                        self._record("restored", runtime, model_id, digest, digest, "active revision observed again")
                    continue
                state.pending[digest] = descriptor
                if digest in state.invalidated:
                    continue  # recorded as evidence; adoption refused until cleared
                if state.active is None:
                    if not _usable(descriptor):
                        self._record("adoption_refused", runtime, model_id, None, digest, "provenance rejected; first adoption refused")
                    else:
                        state.active = descriptor
                        state.pending.pop(digest, None)
                        self._record("adopted", runtime, model_id, None, digest, "first revision observed")
                elif state.pinned is not None:
                    self._record("update_available", runtime, model_id, state.active.digest, digest, f"pinned to {state.pinned}; update not adopted")
                elif not self._auto_adopt:
                    self._record("update_available", runtime, model_id, state.active.digest, digest, "new revision observed; explicit adoption required")
                elif not _usable(descriptor):
                    self._record("adoption_refused", runtime, model_id, state.active.digest, digest, "provenance rejected; automatic adoption refused")
                else:
                    self._promote(runtime, model_id, digest, reason="automatic update adoption")

        for key, state in self._state.items():
            runtime, model_id = key
            if state.active is None or state.missing or runtime not in consulted or key in seen or runtime not in available:
                continue
            state.missing = True
            self._record("missing", runtime, model_id, state.active.digest, None, "consulted runtime no longer reports the active revision")

        return tuple(event for event in self._events if event.sequence > before)

    # -- transitions ---------------------------------------------------------

    def _promote(self, runtime: str, model_id: str, digest: str, *, reason: str) -> None:
        state = self._key_state(runtime, model_id)
        descriptor = state.pending.get(digest)
        if descriptor is None:
            raise ModelUpdateError(f"digest {digest} was not observed for model {model_id}.")
        if digest in state.invalidated:
            raise ModelUpdateError("digest is invalidated; clear the invalidation with a reason before adopting.")
        if state.pinned is not None:
            raise ModelUpdateError(f"model {model_id} is pinned to {state.pinned}; unpin before adopting an update.")
        previous = state.active
        if previous is not None and previous.digest == digest:
            raise ModelUpdateError("digest is already the active revision.")
        if previous is not None:
            state.history = ([previous] + state.history)[: self._history_limit]
        state.active = descriptor
        state.pending.pop(digest, None)
        state.missing = False
        self._record("adopted", runtime, model_id, previous.digest if previous is not None else None, digest, reason)

    def adopt_update(self, model_id: str, *, runtime: str | None = None, digest: str | None = None, expected_previous_digest: str | None = None) -> RevisionEvent:
        """Explicitly promote an observed revision; pinned identities refuse.

        ``expected_previous_digest`` guards against acting on stale knowledge:
        the adoption fails if the active revision is no longer what the caller
        last saw.
        """
        state, (rt, mid) = self._resolve(model_id, runtime)
        if state.pinned is not None:
            raise ModelUpdateError(f"model {mid} is pinned to {state.pinned}; unpin before adopting an update.")
        if digest is None:
            if not state.pending:
                raise ModelUpdateError("no pending revision to adopt; run observe first.")
            digest = next(reversed(state.pending))
        else:
            digest = _text(digest, "digest")
            if digest not in state.pending:
                raise ModelUpdateError(f"digest {digest} was not observed for model {mid}.")
        expected = _text(expected_previous_digest, "expected_previous_digest") if expected_previous_digest is not None else None
        if expected is not None and (state.active is None or state.active.digest != expected):
            raise ModelUpdateError("the active revision changed since the caller's observation; re-check and retry.")
        if not _usable(state.pending[digest]):
            raise ModelUpdateError("refusing to adopt a revision with rejected provenance.")
        self._promote(rt, mid, digest, reason="explicit adoption")
        return self._events[-1]

    def pin(self, model_id: str, *, runtime: str | None = None) -> RevisionAssessment:
        """Freeze the active revision: automatic and explicit promotion refuse."""
        state, (rt, mid) = self._resolve(model_id, runtime)
        if state.active is None:
            raise ModelUpdateError("cannot pin a model without an active revision.")
        if state.pinned is not None:
            raise ModelUpdateError(f"model {mid} is already pinned to {state.pinned}.")
        state.pinned = state.active.digest
        self._record("pinned", rt, mid, None, state.active.digest, "active revision pinned; updates refused until unpin")
        return self.assess(mid, runtime=rt)

    def unpin(self, model_id: str, *, runtime: str | None = None) -> RevisionAssessment:
        state, (rt, mid) = self._resolve(model_id, runtime)
        if state.pinned is None:
            raise ModelUpdateError(f"model {mid} is not pinned.")
        pinned = state.pinned
        state.pinned = None
        self._record("unpinned", rt, mid, pinned, None, "pin released")
        return self.assess(mid, runtime=rt)

    def rollback(self, model_id: str, *, runtime: str | None = None) -> RevisionEvent:
        """Restore the most recently active revision; always available.

        History entries that are invalidated are skipped — rollback is a
        recovery path, never a way back into quarantined state. The displaced
        revision stays observable (moved back to pending) so re-adoption later
        remains an explicit, auditable act.
        """
        state, (rt, mid) = self._resolve(model_id, runtime)
        history = state.history
        target: ModelDescriptor | None = None
        while history:
            candidate = history.pop(0)
            if candidate.digest in state.invalidated:
                continue
            target = candidate
            break
        if target is None:
            raise ModelUpdateError(f"model {mid} has no usable previously active revision to roll back to.")
        previous = state.active
        state.active = target
        state.missing = False
        if previous is not None and previous.digest != target.digest:
            state.pending.setdefault(previous.digest, previous)
        if state.pinned is not None and state.pinned != target.digest:
            released = state.pinned
            state.pinned = None
            reason = f"rolled back to {target.digest}; pin on {released} released with active state"
        else:
            reason = f"rolled back to {target.digest}"
        return self._record("rolled_back", rt, mid, previous.digest if previous is not None else None, target.digest, reason)

    def invalidate(self, model_id: str, *, runtime: str | None = None, digest: str | None = None, reason: str) -> RevisionEvent:
        """Quarantine one revision; if it was active, fall back to the newest
        valid history entry, or refuse to serve any active revision at all."""
        state, (rt, mid) = self._resolve(model_id, runtime)
        explanation = _text(reason, "reason")
        target = _text(digest, "digest") if digest is not None else (state.active.digest if state.active is not None else None)
        if target is None:
            raise ModelUpdateError("cannot invalidate: the model has no active revision and no digest was given.")
        state.invalidated[target] = explanation
        state.pending.pop(target, None)
        if state.pinned == target:
            state.pinned = None
        self._record("invalidated", rt, mid, state.active.digest if state.active is not None else None, target, explanation)
        if state.active is not None and state.active.digest == target:
            state.active = None
            state.missing = False
            while state.history:
                candidate = state.history.pop(0)
                if candidate.digest in state.invalidated:
                    continue
                state.active = candidate
                if state.pinned is not None and state.pinned != candidate.digest:
                    state.pinned = None
                self._record("adopted", rt, mid, target, candidate.digest, "invalidated revision replaced by rollback fallback")
                break
        return self._events[-1]

    def clear_invalidation(self, model_id: str, digest: str, *, runtime: str | None = None, reason: str) -> RevisionAssessment:
        state, (rt, mid) = self._resolve(model_id, runtime)
        target = _text(digest, "digest")
        explanation = _text(reason, "reason")
        if target not in state.invalidated:
            raise ModelUpdateError(f"no invalidation exists for {mid} digest {target}.")
        del state.invalidated[target]
        self._record("invalidation_cleared", rt, mid, target, None, f"invalidation cleared: {explanation}")
        return self.assess(mid, runtime=rt)

    # -- queries -------------------------------------------------------------

    def assess(self, model_id: str, *, runtime: str | None = None) -> RevisionAssessment:
        state, (rt, mid) = self._resolve(model_id, runtime)
        active = state.active
        return RevisionAssessment(
            runtime=rt,
            model_id=mid,
            active_digest=active.digest if active is not None else None,
            pinned_digest=state.pinned,
            missing=active is not None and state.missing,
            pending_digests=tuple(digest for digest in state.pending if digest not in state.invalidated),
            history_digests=tuple(item.digest for item in state.history if item.digest not in state.invalidated),
            invalidated=tuple(sorted(state.invalidated.items())),
        )

    def active_descriptor(self, model_id: str, *, runtime: str | None = None) -> ModelDescriptor:
        state, (_rt, mid) = self._resolve(model_id, runtime)
        if state.active is None:
            raise ModelUpdateError(f"model {mid} has no active revision.")
        return state.active

    def status_for(self, descriptor: ModelDescriptor) -> RevisionState:
        """Lifecycle state of one descriptor — the staleness hook for consumers.

        ``superseded`` and ``invalidated`` are exactly the states in which an
        AS-056 score or a derived cache keyed to this descriptor must not be
        presented as current.
        """
        if not isinstance(descriptor, ModelDescriptor):
            raise TypeError("descriptor must be a ModelDescriptor value.")
        state = self._state.get((descriptor.runtime, descriptor.model_id))
        if state is None:
            return "unknown"
        digest = descriptor.digest
        if digest in state.invalidated:
            return "invalidated"
        if state.active is not None and state.active.digest == digest:
            return "missing" if state.missing else "active"
        if any(item.digest == digest for item in state.history):
            return "superseded"
        if digest in state.pending:
            return "observed"
        return "unknown"
