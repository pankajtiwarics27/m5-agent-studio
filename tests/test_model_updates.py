from __future__ import annotations

import pytest

from model_discovery import ModelCatalog, RuntimeAvailability
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance
from model_updates import ModelRevisionLedger, ModelUpdateError, RevisionAssessment


def _descriptor(
    model_id: str,
    digest: str,
    *,
    runtime: str = "ollama",
    verification: str = "unverified",
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=digest,
        source=runtime,
        revision=digest,
        runtime=runtime,
        limits=ModelLimits(context_tokens=4096),
        capabilities=("chat", "streaming"),
        provenance=ModelProvenance(runtime, model_id, verification),  # type: ignore[arg-type]
    )


def _catalog(*descriptors: ModelDescriptor, available: tuple[str, ...] = ("ollama",)) -> ModelCatalog:
    runtimes = tuple(
        RuntimeAvailability(runtime, runtime in available, len([d for d in descriptors if d.runtime == runtime]))
        for runtime in sorted({d.runtime for d in descriptors} | set(available))
    )
    return ModelCatalog(tuple(descriptors), runtimes)


def test_first_observed_revision_is_adopted_and_tracked() -> None:
    ledger = ModelRevisionLedger()
    events = ledger.observe([_descriptor("llama:3", "sha256:v1")])
    assert [event.kind for event in events] == ["adopted"]
    assert ledger.active_descriptor("llama:3").digest == "sha256:v1"
    assessment = ledger.assess("llama:3")
    assert isinstance(assessment, RevisionAssessment)
    assert assessment.active_digest == "sha256:v1"
    assert assessment.pending_digests == () and assessment.history_digests == ()
    assert ledger.status_for(_descriptor("llama:3", "sha256:v1")) == "active"
    assert ledger.status_for(_descriptor("llama:3", "sha256:other")) == "unknown"


def test_updates_wait_for_adoption_and_stop_drift_when_pinned() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("llama:3", "sha256:v1")])

    events = ledger.observe([_descriptor("llama:3", "sha256:v1"), _descriptor("llama:3", "sha256:v2")])
    assert [(event.kind, event.new_digest) for event in events] == [("update_available", "sha256:v2")]
    assert ledger.active_descriptor("llama:3").digest == "sha256:v1"

    pinned = ledger.pin("llama:3")
    assert pinned.pinned_digest == "sha256:v1"
    events = ledger.observe([_descriptor("llama:3", "sha256:v3")])
    assert [(event.kind, event.reason) for event in events] == [
        ("update_available", "pinned to sha256:v1; update not adopted")
    ]
    assert ledger.active_descriptor("llama:3").digest == "sha256:v1"

    with pytest.raises(ModelUpdateError, match="pinned to sha256:v1"):
        ledger.adopt_update("llama:3", digest="sha256:v3")

    ledger.unpin("llama:3")
    event = ledger.adopt_update("llama:3", digest="sha256:v3")
    assert (event.kind, event.previous_digest, event.new_digest) == ("adopted", "sha256:v1", "sha256:v3")
    after = ledger.assess("llama:3")
    assert after.history_digests == ("sha256:v1",)
    assert after.pending_digests == ("sha256:v2",)  # v2 stayed pending, never auto-adopted


def test_pending_order_drives_default_adoption() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("m", "v1")])
    ledger.observe([_descriptor("m", "v2")])
    ledger.observe([_descriptor("m", "v3")])
    event = ledger.adopt_update("m")  # default: most recently observed pending
    assert event.new_digest == "v3"
    assessment = ledger.assess("m")
    assert assessment.active_digest == "v3"
    assert assessment.history_digests == ("v1",)
    assert assessment.pending_digests == ("v2",)


def test_rollback_restores_previous_and_skips_quarantined_history() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("m", "v1")])
    ledger.observe([_descriptor("m", "v2")])
    ledger.adopt_update("m", digest="v2")
    event = ledger.rollback("m")
    assert (event.kind, event.previous_digest, event.new_digest) == ("rolled_back", "v2", "v1")
    assert ledger.active_descriptor("m").digest == "v1"
    assert ledger.status_for(_descriptor("m", "v2")) == "observed"  # displaced back into pending, re-adoptable

    with pytest.raises(ModelUpdateError, match="no usable previously active revision"):
        ledger.rollback("m")

    # A quarantined history entry is never a rollback target.
    ledger.observe([_descriptor("m", "v0")])
    ledger.adopt_update("m", digest="v0")  # history now [v1, v2... no v2 was never displaced]
    assert ledger.assess("m").history_digests == ("v1",)
    ledger.invalidate("m", digest="v1", reason="bad artifact")  # v1 quarantined in history
    assert ledger.status_for(_descriptor("m", "v1")) == "invalidated"
    assert ledger.assess("m").history_digests == ()  # hidden from assessment, still skipped on rollback
    ledger.observe([_descriptor("m", "v3")])
    ledger.adopt_update("m", digest="v3")  # history [v0, (v1 quarantined)]
    event = ledger.rollback("m")
    assert event.new_digest == "v0"  # skipped the quarantined v1


def test_quarantined_revision_blocks_adoption_until_cleared() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("m", "v1")])
    ledger.observe([_descriptor("m", "v2")])

    event = ledger.invalidate("m", digest="v2", reason="supply chain advisory")
    assert (event.kind, event.new_digest, event.reason) == ("invalidated", "v2", "supply chain advisory")
    with pytest.raises(ModelUpdateError, match="not observed"):
        ledger.adopt_update("m", digest="v2")  # dropped from pending by quarantine
    assert ledger.assess("m").invalidated == (("v2", "supply chain advisory"),)

    ledger.clear_invalidation("m", "v2", reason="advisory retracted after verification")
    assert ledger.assess("m").invalidated == ()
    with pytest.raises(ModelUpdateError, match="no pending revision"):
        ledger.adopt_update("m")  # clearing does not resurrect the pending record; re-observe needed
    ledger.observe([_descriptor("m", "v2")])
    assert ledger.adopt_update("m", digest="v2").new_digest == "v2"

    # Quarantining the just-adopted (active) revision falls back to the prior one.
    ledger.invalidate("m", digest="v2", reason="second advisory")
    with pytest.raises(ModelUpdateError, match="not observed"):
        ledger.adopt_update("m", digest="v2")
    assert ledger.active_descriptor("m").digest == "v1"  # rollback fallback applied


def test_invalidating_the_active_revision_falls_back_without_destroying_state() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("m", "v1")])
    ledger.observe([_descriptor("m", "v2")])
    ledger.adopt_update("m", digest="v2")
    ledger.pin("m")

    event = ledger.invalidate("m", reason="disk corruption confirmed")
    assert event.kind == "adopted"  # the fallback promotion is the newest event
    assert any(
        entry.kind == "invalidated" and entry.reason == "disk corruption confirmed" for entry in ledger.events()
    )
    assessment = ledger.assess("m")
    assert assessment.active_digest == "v1"  # fallback applied
    assert assessment.pinned_digest is None  # pin on the quarantined digest released automatically
    assert ledger.status_for(_descriptor("m", "v2")) == "invalidated"
    assert ledger.status_for(_descriptor("m", "v1")) == "active"

    # No history to fall back to: serving stops rather than keeping bad state.
    solo = ModelRevisionLedger()
    solo.observe([_descriptor("x", "v1")])
    solo.invalidate("x", reason="revoked")
    with pytest.raises(ModelUpdateError, match="no active revision"):
        solo.active_descriptor("x")
    assert solo.assess("x").active_digest is None
    assert solo.status_for(_descriptor("x", "v1")) == "invalidated"
    # The quarantined digest stays refused even after re-observation.
    solo.observe([_descriptor("x", "v1")])
    assert solo.assess("x").active_digest is None


def test_auto_adopt_mode_still_refuses_pinned_and_rejected_provenance() -> None:
    ledger = ModelRevisionLedger(auto_adopt_updates=True)
    ledger.observe([_descriptor("m", "v1")])
    assert ledger.active_descriptor("m").digest == "v1"
    events = ledger.observe([_descriptor("m", "v2")])
    assert [(event.kind, event.new_digest) for event in events] == [("adopted", "v2")]
    assert ledger.assess("m").history_digests == ("v1",)

    ledger.pin("m")
    events = ledger.observe([_descriptor("m", "v3")])
    assert [event.kind for event in events] == ["update_available"]
    ledger.unpin("m")

    # Rejected provenance is never adopted automatically...
    events = ledger.observe([_descriptor("m", "v4", verification="rejected")])
    assert [(event.kind, event.new_digest) for event in events] == [("adoption_refused", "v4")]
    assert ledger.active_descriptor("m").digest == "v2"
    # ...nor as the first revision of a new model.
    events = ledger.observe([_descriptor("brand-new", "v1", verification="rejected"), _descriptor("m", "v2")])
    assert [(event.kind, event.reason) for event in events] == [
        ("adoption_refused", "provenance rejected; first adoption refused")
    ]
    assert ledger.status_for(_descriptor("brand-new", "v1", verification="rejected")) == "observed"
    assert ledger.active_descriptor("m").digest == "v2"  # m stays untouched by the refusal


def test_optimistic_guard_and_validation_gates() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("m", "v1"), _descriptor("m", "v2")])
    with pytest.raises(ModelUpdateError, match="active revision changed"):
        ledger.adopt_update("m", digest="v2", expected_previous_digest="wrong")
    assert ledger.adopt_update("m", digest="v2", expected_previous_digest="v1").new_digest == "v2"
    with pytest.raises(ModelUpdateError, match="was not observed"):
        ledger.adopt_update("m", digest="v2")  # already consumed by the adoption above

    with pytest.raises(ModelUpdateError, match="not pinned"):
        ledger.unpin("m")
    ledger.pin("m")
    with pytest.raises(ModelUpdateError, match="already pinned"):
        ledger.pin("m")
    with pytest.raises(ModelUpdateError, match="reason must not be empty"):
        ledger.invalidate("m", reason="   ")
    with pytest.raises(TypeError):
        ledger.invalidate("m", reason=42)  # type: ignore[arg-type]
    with pytest.raises(ModelUpdateError, match="no invalidation exists"):
        ledger.clear_invalidation("m", "nope", reason="nothing to clear")


def test_missing_is_non_destructive_and_restores_cleanly() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("m", "v1")])

    events = ledger.observe(_catalog())  # ollama consulted, available, reports nothing
    assert [event.kind for event in events] == ["missing"]
    assert ledger.active_descriptor("m").digest == "v1"  # state preserved for recovery
    assert ledger.assess("m").missing is True
    assert ledger.status_for(_descriptor("m", "v1")) == "missing"

    events = ledger.observe(_catalog(_descriptor("m", "v1")))
    assert [event.kind for event in events] == ["restored"]
    assert ledger.assess("m").missing is False

    # A runtime that is merely offline proves nothing.
    events = ledger.observe(_catalog(available=()))
    assert events == ()
    assert ledger.assess("m").missing is False


def test_only_consulted_runtimes_can_mark_state() -> None:
    ledger = ModelRevisionLedger()
    llama = _descriptor("m", "v1", runtime="llama.cpp")
    ledger.observe([llama])
    catalog = ModelCatalog((), (RuntimeAvailability("ollama", True, 0),))
    events = ledger.observe(catalog)
    assert events == ()  # llama.cpp was not consulted at all
    assert ledger.status_for(llama) == "active"


def test_runtimes_are_tracked_independently_for_the_same_model_id() -> None:
    ledger = ModelRevisionLedger()
    ledger.observe([_descriptor("m", "v-ollama", runtime="ollama"), _descriptor("m", "v-llama", runtime="llama.cpp")])
    assert ledger.active_descriptor("m", runtime="ollama").digest == "v-ollama"
    assert ledger.active_descriptor("m", runtime="llama.cpp").digest == "v-llama"
    with pytest.raises(ModelUpdateError, match=r"multiple runtimes \(llama.cpp, ollama\)"):
        ledger.assess("m")
    ledger.observe([_descriptor("m", "v2-ollama", runtime="ollama")])
    assert ledger.adopt_update("m", runtime="ollama", digest="v2-ollama").new_digest == "v2-ollama"
    assert ledger.active_descriptor("m", runtime="llama.cpp").digest == "v-llama"  # other runtime untouched
    assert ledger.status_for(_descriptor("m", "v-ollama")) == "superseded"


def test_bounded_history_and_event_overflow() -> None:
    ledger = ModelRevisionLedger(history_limit=2, event_limit=4)
    ledger.observe([_descriptor("m", "v1")])
    for digest in ("v2", "v3", "v4", "v5"):
        ledger.observe([_descriptor("m", digest)])
        ledger.adopt_update("m", digest=digest)
    assessment = ledger.assess("m")
    assert assessment.active_digest == "v5"
    assert assessment.history_digests == ("v4", "v3")  # bounded; older entries dropped
    assert len(ledger.events()) == 4
    assert ledger.dropped_events == 5
    assert {event.kind for event in ledger.events()} == {"update_available", "adopted"}
    # Sequence numbers keep proving the trim, even when the log is shorter.
    assert max(event.sequence for event in ledger.events()) == 9

    with pytest.raises(ModelUpdateError, match="history_limit must be a positive integer"):
        ModelRevisionLedger(history_limit=0)
    with pytest.raises(ModelUpdateError, match="event_limit must be a positive integer"):
        ModelRevisionLedger(event_limit=-2)
    with pytest.raises(TypeError, match="auto_adopt_updates"):
        ModelRevisionLedger(auto_adopt_updates="yes")  # type: ignore[arg-type]


def test_observe_input_validation() -> None:
    ledger = ModelRevisionLedger()
    with pytest.raises(ModelUpdateError, match="requires a catalog"):
        ledger.observe()
    with pytest.raises(TypeError, match="ModelCatalog or a sequence"):
        ledger.observe("ollama")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ModelDescriptor values"):
        ledger.observe(["ollama"])  # type: ignore[list-item]
    with pytest.raises(ModelUpdateError, match="no observed revisions"):
        ledger.assess("never-seen")
    with pytest.raises(ModelUpdateError, match="no observed revisions in runtime ollama"):
        ledger.pin("never-seen", runtime="ollama")
    with pytest.raises(TypeError, match="descriptor must be a ModelDescriptor"):
        ledger.status_for("nope")  # type: ignore[arg-type]


def test_catalog_drives_the_ledger_end_to_end() -> None:
    """AS-053 catalog -> AS-057 lifecycle: discover, pin the shipped revision,
    take an update, roll back when it regresses, and quarantine it — with the
    event sequence proving every transition and stable audit identities."""
    ledger = ModelRevisionLedger()
    week1 = _catalog(_descriptor("team-model", "sha256:one"), _descriptor("other", "sha256:keep"))
    assert [event.kind for event in ledger.observe(week1)] == ["adopted", "adopted"]

    ledger.pin("team-model")
    week2 = _catalog(_descriptor("team-model", "sha256:two"), _descriptor("other", "sha256:keep"))
    assert [event.kind for event in ledger.observe(week2)] == ["update_available"]
    assert ledger.active_descriptor("team-model").digest == "sha256:one"

    ledger.unpin("team-model")
    assert ledger.adopt_update("team-model", digest="sha256:two", expected_previous_digest="sha256:one").kind == "adopted"
    assert ledger.rollback("team-model").new_digest == "sha256:one"
    assert ledger.invalidate("team-model", digest="sha256:two", reason="regression + broken tokenizer").kind == "invalidated"
    assert ledger.status_for(_descriptor("team-model", "sha256:two")) == "invalidated"
    assert ledger.assess("team-model").active_digest == "sha256:one"
    assert ledger.assess("other").active_digest == "sha256:keep"  # neighbors untouched

    kinds = [event.kind for event in ledger.events(model_id="team-model")]
    assert kinds == ["adopted", "pinned", "update_available", "unpinned", "adopted", "rolled_back", "invalidated"]
    mapping = ledger.assess("team-model").to_mapping()
    assert mapping["invalidated"] == [["sha256:two", "regression + broken tokenizer"]]
    sequences = [event.sequence for event in ledger.events()]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
