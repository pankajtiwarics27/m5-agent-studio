"""Tests for artifact lineage: provenance separation, invalidation, retention (`M5-P5-02`).

The three acceptance criteria, pinned individually:

* **deduplicated content does not merge trust histories** — two sightings of one blob
  stay two entries, and every answer is the most restrictive one;
* **stale evidence is not accepted** — evidence recorded against a digest or a security
  generation that has since moved is refused, and the refusal is recorded;
* **active recovery artifacts are protected from GC** — a frozen quarantine (and an
  active task's artifacts) survive retention, while untouched content is collectable.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import artifact_lineage as al
import workspace_quarantine as wq

BLOB_A = "a" * 64
BLOB_B = "b" * 64
BLOB_C = "c" * 64


@pytest.fixture()
def registry(tmp_path: Path) -> al.ArtifactRegistry:
    return al.ArtifactRegistry(tmp_path / "store")


# ---------------------------------------------------------------------------
# AC1: deduplicated content does not merge trust histories
# ---------------------------------------------------------------------------


def test_the_same_bytes_from_two_sources_keep_two_trust_histories(registry: al.ArtifactRegistry) -> None:
    trusted = registry.record_artifact(
        BLOB_A, source_kind="baseline", source_ref="HEAD", trust="trusted", confidentiality="public"
    )
    untrusted = registry.record_artifact(
        BLOB_A, source_kind="tool_output", source_ref="tool-7", trust="untrusted", confidentiality="secret"
    )

    entries = registry.entries_for(BLOB_A)
    assert [entry.entry_id for entry in entries] == [trusted.entry_id, untrusted.entry_id]
    # One blob (content identity is deduplicated) ...
    assert registry.summary()["artifact_count"] == 1
    # ... but two independent histories, and the restrictive answer wins.
    assert registry.effective_trust(BLOB_A) == "untrusted"
    assert registry.effective_confidentiality(BLOB_A) == "secret"


def test_a_later_trusted_sighting_cannot_launder_an_earlier_untrusted_one(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="web_fetch", source_ref="evil.example", trust="untrusted")
    registry.record_artifact(BLOB_A, source_kind="baseline", source_ref="HEAD", trust="trusted")
    registry.record_artifact(BLOB_A, source_kind="baseline", source_ref="HEAD~1", trust="trusted")

    assert len(registry.entries_for(BLOB_A)) == 3
    assert registry.effective_trust(BLOB_A) == "untrusted"


def test_unknown_content_is_trusted_by_nothing(registry: al.ArtifactRegistry) -> None:
    assert registry.effective_trust("d" * 64) == "untrusted"
    assert registry.effective_confidentiality("d" * 64) == "secret"
    verdict = registry.check_operation("d" * 64, "prompt")
    assert verdict.allowed is False and verdict.code == "unknown_artifact"


def test_operation_restrictions_are_deny_wins(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="baseline", source_ref="HEAD", allowed_operations=("read", "prompt"))
    assert registry.check_operation(BLOB_A, "prompt").allowed is True

    registry.record_artifact(BLOB_A, source_kind="tool_output", source_ref="t", denied_operations=("prompt",))
    verdict = registry.check_operation(BLOB_A, "prompt")
    assert verdict.allowed is False and verdict.code == "operation_denied"

    # An entry that allows only a subset restricts the whole artifact: no entry may be
    # bypassed by consulting a friendlier one.
    registry.record_artifact(BLOB_B, source_kind="baseline", source_ref="HEAD", allowed_operations=("read",))
    registry.record_artifact(BLOB_B, source_kind="mirror", source_ref="m", allowed_operations=("read", "export"))
    narrowed = registry.check_operation(BLOB_B, "export")
    assert narrowed.allowed is False and narrowed.code == "operation_not_allowed"


def test_invalidating_one_sighting_invalidates_the_artifact(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="baseline", source_ref="HEAD", trust="trusted")
    registry.record_artifact(BLOB_A, source_kind="tool_output", source_ref="t", trust="untrusted")

    affected = registry.invalidate(BLOB_A, reason="source file changed after capture")

    assert affected == (BLOB_A,)
    assert all(entry.invalidated for entry in registry.entries_for(BLOB_A))
    verdict = registry.check_operation(BLOB_A, "read")
    assert verdict.allowed is False and verdict.code == "invalidated_artifact"
    # Invalidation is recorded, not destructive: the bytes' history is still readable.
    assert len(registry.entries_for(BLOB_A)) == 2


def test_invalidation_cascades_to_derived_artifacts(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="source", source_ref="a.py")
    registry.record_artifact(BLOB_B, source_kind="derived", source_ref="report.json", derived_from=(BLOB_A,))
    registry.record_artifact(BLOB_C, source_kind="derived", source_ref="summary.md", derived_from=(BLOB_B,))

    affected = registry.invalidate(BLOB_A, reason="security finding")

    assert set(affected) == {BLOB_A, BLOB_B, BLOB_C}
    # Derivation does not lower the recorded trust label; it invalidates the artifact,
    # which is what every consumer actually checks.
    assert registry.check_operation(BLOB_C, "read").code == "invalidated_artifact"


def test_a_parent_we_never_saw_is_a_stub_not_a_trusted_origin(registry: al.ArtifactRegistry) -> None:
    parent = "e" * 64
    registry.record_artifact(BLOB_B, source_kind="derived", source_ref="out.json", derived_from=(parent,))
    assert registry.entries_for(parent) == ()
    assert registry.effective_trust(parent) == "untrusted"


# ---------------------------------------------------------------------------
# AC2: stale evidence is not accepted
# ---------------------------------------------------------------------------


def test_evidence_is_accepted_while_its_inputs_are_unchanged(registry: al.ArtifactRegistry) -> None:
    record = registry.record_evidence(
        kind="verification",
        subject="patch",
        source_files={"app.py": BLOB_A},
        task_id="t1",
        security_generation=4,
    )
    verdict = registry.accept_evidence(
        record.evidence_id, current_files={"app.py": BLOB_A}, security_generation=4
    )
    assert verdict.accepted is True and verdict.code == "accepted"


def test_evidence_is_refused_when_a_source_file_changed(registry: al.ArtifactRegistry) -> None:
    record = registry.record_evidence(kind="verification", subject="patch", source_files={"app.py": BLOB_A})
    verdict = registry.accept_evidence(record.evidence_id, current_files={"app.py": BLOB_B})

    assert verdict.accepted is False
    assert verdict.code == "source_changed"
    assert "app.py" in verdict.reason
    # The refusal is durable: the record is now stale, so a later check cannot flip it
    # back to valid by re-reading the (changed) file system.
    assert registry.evidence(record.evidence_id).stale is True
    assert registry.accept_evidence(record.evidence_id).code == "already_stale"


def test_evidence_is_refused_when_a_source_file_disappeared(registry: al.ArtifactRegistry) -> None:
    record = registry.record_evidence(kind="verification", subject="patch", source_files={"app.py": BLOB_A})
    verdict = registry.accept_evidence(record.evidence_id, current_files={})
    assert verdict.accepted is False and verdict.code == "source_missing"


def test_evidence_is_refused_after_the_security_generation_moves(registry: al.ArtifactRegistry) -> None:
    record = registry.record_evidence(
        kind="verification", subject="patch", source_files={"app.py": BLOB_A}, security_generation=3
    )
    verdict = registry.accept_evidence(
        record.evidence_id, current_files={"app.py": BLOB_A}, security_generation=4
    )
    assert verdict.accepted is False
    assert verdict.code == "security_generation_changed"
    assert "3 -> 4" in verdict.reason


def test_evidence_is_refused_once_its_subject_artifact_is_invalidated(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="source", source_ref="a.py")
    # Evidence recorded against content that is *already* invalidated must be refused at
    # the point of use, not merely flagged when the invalidation happened to run later.
    registry.invalidate(BLOB_A, reason="contaminated input")
    record = registry.record_evidence(kind="verification", subject="patch", content_sha256=BLOB_A)

    verdict = registry.accept_evidence(record.evidence_id)
    assert verdict.accepted is False and verdict.code == "subject_invalidated"


def test_invalidating_an_artifact_marks_later_evidence_stale_in_place(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="source", source_ref="a.py")
    record = registry.record_evidence(kind="verification", subject="patch", content_sha256=BLOB_A)

    registry.invalidate(BLOB_A, reason="contaminated input")

    assert registry.evidence(record.evidence_id).stale is True
    assert registry.accept_evidence(record.evidence_id).code == "already_stale"


def test_accepting_evidence_does_not_require_a_known_generation(registry: al.ArtifactRegistry) -> None:
    """"Not checked" must never be reported as "unchanged"."""
    record = registry.record_evidence(kind="verification", subject="patch", source_files={"a.py": BLOB_A})
    assert record.security_generation is None
    assert registry.accept_evidence(record.evidence_id, current_files={"a.py": BLOB_A}).accepted is True


# ---------------------------------------------------------------------------
# AC3: active recovery artifacts are protected from GC
# ---------------------------------------------------------------------------


def _age(registry: al.ArtifactRegistry, sha: str, seconds: float) -> None:
    """Backdate an artifact so the retention window can be tested without sleeping."""
    state = json.loads(registry.state_path.read_text(encoding="utf-8"))
    state["blobs"][sha]["first_seen"] -= seconds
    os.chmod(registry.state_path, 0o600)
    registry.state_path.write_text(json.dumps(state), encoding="utf-8")


def test_retention_collects_only_what_nothing_depends_on(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="tool_output", source_ref="t", trust="unverified")
    registry.record_artifact(BLOB_B, source_kind="tool_output", source_ref="t", task_id="active-task")
    for sha in (BLOB_A, BLOB_B):
        _age(registry, sha, 10_000)

    plan = registry.plan_retention(retain_seconds=60, active_task_ids=["active-task"])

    assert plan.evictable == (BLOB_A,)
    assert "active-task" in plan.protected[BLOB_B]

    result = registry.apply_retention(plan)
    assert result.evicted == (BLOB_A,)
    assert registry.entries_for(BLOB_A) == ()
    assert registry.entries_for(BLOB_B)


def test_a_frozen_quarantine_is_never_collected(tmp_path: Path) -> None:
    """The recovery path's own bytes survive retention until the quarantine is released."""
    store = tmp_path / "store"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src").write_text("contested bytes\n", encoding="utf-8")

    manifest = wq.freeze_workspace(workspace, store_root=store, reason="suspected contamination")
    registry = al.ArtifactRegistry(store)
    _age(registry, manifest.tree_sha256, 10_000)

    # Two independent mechanisms protect it: the pin recorded by freeze_workspace, and the
    # re-derivation from the quarantine store that a collector is expected to pass in.
    plan = registry.plan_retention(retain_seconds=0)
    assert manifest.tree_sha256 not in plan.evictable
    assert "quarantine" in plan.protected[manifest.tree_sha256]

    unpinned = al.ArtifactRegistry(store)
    unpinned.unpin(f"quarantine:{manifest.quarantine_id}")
    from_store = unpinned.plan_retention(
        retain_seconds=0, protected_refs=wq.recovery_protection(store)
    )
    assert manifest.tree_sha256 in from_store.protected, (
        "an unreleased quarantine must protect its artifact even without the explicit pin"
    )

    wq.release_quarantine(store, manifest.quarantine_id, note="reconstruction accepted")
    assert wq.recovery_protection(store) == {}, "releasing lifts the protection"

    still_secret = al.ArtifactRegistry(store).plan_retention(
        retain_seconds=0, protected_refs=wq.recovery_protection(store)
    )
    assert manifest.tree_sha256 not in still_secret.evictable, (
        "workspace bytes stay confidential even after the pin is lifted"
    )
    after_release = al.ArtifactRegistry(store).plan_retention(
        retain_seconds=0, protected_refs=wq.recovery_protection(store), allow_secret_eviction=True
    )
    assert manifest.tree_sha256 in after_release.evictable, (
        "after release the pinned recovery artifact is collectable by an explicit secret eviction"
    )


def test_evidence_still_in_force_pins_its_subject(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="source", source_ref="a.py")
    record = registry.record_evidence(kind="verification", subject="patch", content_sha256=BLOB_A)
    _age(registry, BLOB_A, 10_000)

    plan = registry.plan_retention(retain_seconds=60)
    assert BLOB_A not in plan.evictable
    assert record.evidence_id in plan.protected[BLOB_A]

    registry.invalidate(BLOB_A, reason="input contaminated")  # evidence goes stale with it
    stale_plan = registry.plan_retention(retain_seconds=60)
    assert BLOB_A in stale_plan.evictable, "stale evidence must not keep bytes alive forever"


def test_confidential_content_is_kept_unless_explicitly_allowed(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="secret_store", source_ref="k", confidentiality="secret")
    _age(registry, BLOB_A, 10_000)
    assert registry.plan_retention(retain_seconds=0).evictable == ()
    assert registry.plan_retention(retain_seconds=0, allow_secret_eviction=True).evictable == (BLOB_A,)


def test_the_retention_grace_window_keeps_recent_artifacts(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="tool_output", source_ref="t")
    assert registry.plan_retention(retain_seconds=3600).evictable == ()


def test_apply_retention_never_removes_a_protected_artifact(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="tool_output", source_ref="t")
    plan = registry.plan_retention(retain_seconds=3600)
    result = registry.apply_retention(plan)  # nothing was evictable
    assert result.evicted == () and BLOB_A in result.kept


# ---------------------------------------------------------------------------
# Store contract details beyond the shared conformance suite
# ---------------------------------------------------------------------------


def test_provenance_survives_a_restart(tmp_path: Path) -> None:
    store = tmp_path / "store"
    original = al.ArtifactRegistry(store)
    entry = original.record_artifact(
        BLOB_A, source_kind="baseline", source_ref="HEAD", trust="trusted", task_id="task-1"
    )

    reopened = al.ArtifactRegistry(store)
    assert [e.entry_id for e in reopened.entries_for(BLOB_A)] == [entry.entry_id]
    assert reopened.effective_trust(BLOB_A) == "trusted"
    assert reopened.summary()["revision"] >= 1


def test_recording_rejects_a_digest_that_is_not_sha256(registry: al.ArtifactRegistry) -> None:
    with pytest.raises(al.ArtifactLineageError):
        registry.record_artifact("not-a-digest", source_kind="baseline", source_ref="HEAD")
    with pytest.raises(al.ArtifactLineageError):
        registry.record_artifact(BLOB_A, source_kind="baseline", source_ref="HEAD", trust="probably-fine")


def test_state_file_is_private_and_written_without_leftovers(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="baseline", source_ref="HEAD")
    assert stat.S_IMODE(registry.state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(registry.state_path.parent.stat().st_mode) == 0o700
    assert [p.name for p in registry.state_path.parent.iterdir() if p.name != "state.json"] == []


def test_a_re_used_evidence_id_is_refused(registry: al.ArtifactRegistry) -> None:
    registry.record_evidence(kind="verification", subject="p", evidence_id="ev-fixed")
    with pytest.raises(al.ArtifactLineageError):
        registry.record_evidence(kind="verification", subject="p", evidence_id="ev-fixed")


def test_content_digest_is_the_identity_of_the_bytes() -> None:
    assert al.content_digest(b"one") == al.content_digest(b"one")
    assert al.content_digest(b"one") != al.content_digest(b"two")


# ---------------------------------------------------------------------------
# The live gate: evidence gathered under one security generation is not accepted
# under another
# ---------------------------------------------------------------------------


def _machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    import app as studio
    import runtime_persistence as rp
    import workflow_state as ws

    db_path = tmp_path / "p5_02.sqlite"
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", tmp_path / "store")
    studio.init_db()
    sm = ws.DurableTaskStateMachine(studio.STORAGE, db_path)
    rp.RuntimePersistenceStore(studio.STORAGE, db_path).create_task(
        "task-p5-02", policy="standard", state="running", now=100.0
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.py").write_text("result = 1\n", encoding="utf-8")
    sm.set_verification_spec(
        "task-p5-02",
        ws.TaskVerificationSpec(
            require_managed_edit=False,
            required_modified_files=("out.py",),
            test_command=(sys.executable, "-c", "print('1 passed')"),
        ),
    )
    return sm, workspace


def test_completion_is_verified_while_the_security_generation_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sm, workspace = _machine(tmp_path, monkeypatch)
    report = sm.verify_task_completion(
        "task-p5-02", workspace_dir=workspace, security_generation=lambda: 7
    )
    assert report.verified is True, report.failure_detail


def test_completion_is_refused_when_the_generation_moves_mid_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 on the real completion gate: the evidence is tied to one security configuration."""
    sm, workspace = _machine(tmp_path, monkeypatch)
    generations = iter([7, 8])  # read once before the run, once before the verdict

    report = sm.verify_task_completion(
        "task-p5-02", workspace_dir=workspace, security_generation=lambda: next(generations)
    )

    assert report.verified is False
    assert report.failure_code == "security_configuration_changed_during_verification"
    assert "7 -> 8" in report.failure_detail


def test_an_unknown_generation_is_never_reported_as_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workflow_state as ws

    sm, workspace = _machine(tmp_path, monkeypatch)

    def unavailable() -> int | None:
        raise RuntimeError("security store unreadable")

    assert ws._capture_security_generation(unavailable) is None
    assert ws._capture_security_generation(lambda: None) is None
    assert ws._capture_security_generation(None) is None
    # With no generation to compare, the run is simply not gated — and says nothing.
    report = sm.verify_task_completion(
        "task-p5-02", workspace_dir=workspace, security_generation=unavailable
    )
    assert report.verified is True, report.failure_detail
