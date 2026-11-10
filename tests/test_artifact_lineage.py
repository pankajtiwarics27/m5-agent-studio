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


# ---------------------------------------------------------------------------
# AP-006: versioned artifact identity, selective invalidation, confined
# previews, completeness and a privacy-preserving export/delete lifecycle.
# ---------------------------------------------------------------------------

CONFIG_A = "1" * 64
CONFIG_B = "2" * 64
INPUT_V1 = "v1"
INPUT_V2 = "v2"


def _store_root(registry: al.ArtifactRegistry) -> Path:
    return registry.state_path.parent.parent


def _record_versioned(
    registry: al.ArtifactRegistry,
    sha: str = BLOB_A,
    *,
    checks: tuple[str, ...] = ("render",),
    inputs: dict[str, str] | None = None,
    config: str = CONFIG_A,
    trust: str = "trusted",
    confidentiality: str = "internal",
) -> al.ProvenanceEntry:
    return registry.record_artifact(
        sha,
        source_kind="pipeline",
        source_ref="run-1",
        trust=trust,
        confidentiality=confidentiality,
        producer="workflow:site-builder",
        producer_version="1.4.0",
        config_sha256=config,
        inputs={"src/app.py": INPUT_V1} if inputs is None else inputs,
        required_checks=checks,
    )


@pytest.fixture()
def artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "site").mkdir(parents=True)
    (root / "site" / "index.html").write_text("<h1>built</h1>", encoding="utf-8")
    return root


def _digest_of(path: Path) -> str:
    return al.content_digest(path.read_bytes())


# --- AC1: bytes, input versions, producer/configuration and checks are identities ---


def test_identity_covers_the_producer_configuration_and_input_versions(
    registry: al.ArtifactRegistry,
) -> None:
    entry = _record_versioned(registry)

    assert entry.identity_sha256 == al.artifact_identity_sha256(
        BLOB_A,
        producer="workflow:site-builder",
        producer_version="1.4.0",
        config_sha256=CONFIG_A,
        inputs={"src/app.py": INPUT_V1},
    )
    # Same bytes, same producer, different configuration: a different version.
    other = _record_versioned(registry, config=CONFIG_B)
    assert other.identity_sha256 != entry.identity_sha256
    # Same bytes and configuration, different input version: still a different version.
    third = _record_versioned(registry, inputs={"src/app.py": INPUT_V2})
    assert third.identity_sha256 not in {entry.identity_sha256, other.identity_sha256}
    # One blob (content identity is deduplicated), three version identities.
    assert registry.summary()["artifact_count"] == 1


def test_identity_is_stable_across_input_ordering() -> None:
    assert al.artifact_identity_sha256(
        BLOB_A, inputs={"a.py": "1", "b.py": "2"}
    ) == al.artifact_identity_sha256(BLOB_A, inputs={"b.py": "2", "a.py": "1"})


def test_the_recorded_identity_is_stored_so_a_reader_can_verify_it(
    registry: al.ArtifactRegistry,
) -> None:
    entry = _record_versioned(registry)

    raw = json.loads(registry.state_path.read_text(encoding="utf-8"))
    recorded = raw["blobs"][BLOB_A]["entries"][entry.entry_id]
    assert raw["schema_version"] == al.AL_SCHEMA_VERSION
    assert recorded["identity_sha256"] == entry.identity_sha256
    assert recorded["producer"] == "workflow:site-builder"
    assert recorded["producer_version"] == "1.4.0"
    assert recorded["config_sha256"] == CONFIG_A
    assert recorded["inputs"] == {"src/app.py": INPUT_V1}
    assert recorded["required_checks"] == ["render"]


def test_identity_and_declared_checks_survive_a_restart(registry: al.ArtifactRegistry) -> None:
    entry = _record_versioned(registry)

    reopened = al.ArtifactRegistry(_store_root(registry))
    restored = reopened.entries_for(BLOB_A)[0]
    assert restored.identity_sha256 == entry.identity_sha256
    assert restored.inputs == {"src/app.py": INPUT_V1}
    assert restored.required_checks == ("render",)


def test_malformed_identities_are_refused(registry: al.ArtifactRegistry) -> None:
    with pytest.raises(al.ArtifactLineageError):
        registry.record_artifact(BLOB_A, source_kind="pipeline", source_ref="r", config_sha256="not-a-digest")
    with pytest.raises(al.ArtifactLineageError):
        registry.record_artifact(BLOB_A, source_kind="pipeline", source_ref="r", inputs={"src/app.py": "  "})
    with pytest.raises(al.ArtifactLineageError):
        registry.record_artifact(BLOB_A, source_kind="pipeline", source_ref="r", inputs="src/app.py")  # type: ignore[arg-type]
    with pytest.raises(al.ArtifactLineageError):
        al.artifact_identity_sha256("not-a-digest")


# --- AC2: an input change invalidates affected descendants, not unrelated work ---


def test_an_input_version_change_invalidates_only_its_consumers(
    registry: al.ArtifactRegistry,
) -> None:
    _record_versioned(registry, BLOB_A)
    registry.record_artifact(
        BLOB_B, source_kind="pipeline", source_ref="run-9", trust="trusted", inputs={"src/other.py": INPUT_V1}
    )

    result = registry.invalidate_for_inputs({"src/app.py": INPUT_V2}, reason="src/app.py edited")

    assert result.invalidated == (BLOB_A,)
    assert result.changed_inputs == {"src/app.py": INPUT_V2}
    assert all(entry.invalidated for entry in registry.entries_for(BLOB_A))
    assert registry.check_operation(BLOB_A, "preview").allowed is False
    # The unrelated artifact was not deleted, invalidated, or made unusable.
    assert not any(entry.invalidated for entry in registry.entries_for(BLOB_B))
    assert registry.check_operation(BLOB_B, "preview").allowed is True


def test_an_unchanged_input_version_invalidates_nothing(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry)

    result = registry.invalidate_for_inputs({"src/app.py": INPUT_V1}, reason="re-recorded")

    assert result.invalidated == ()
    assert registry.check_operation(BLOB_A, "preview").allowed is True


def test_input_invalidation_cascades_to_derivatives_and_spares_siblings(
    registry: al.ArtifactRegistry,
) -> None:
    _record_versioned(registry, BLOB_A)
    registry.record_artifact(
        BLOB_B, source_kind="pipeline", source_ref="bundle", trust="trusted", derived_from=[BLOB_A]
    )
    registry.record_artifact(BLOB_C, source_kind="pipeline", source_ref="independent", trust="trusted")

    result = registry.invalidate_for_inputs({"src/app.py": INPUT_V2}, reason="input moved")

    assert set(result.invalidated) == {BLOB_A, BLOB_B}
    assert "via" in (registry.entries_for(BLOB_B)[0].invalidation_reason)
    assert registry.check_operation(BLOB_C, "preview").allowed is True


def test_an_empty_input_change_is_refused(registry: al.ArtifactRegistry) -> None:
    with pytest.raises(al.ArtifactLineageError):
        registry.invalidate_for_inputs({}, reason="nothing changed")


# --- AC3: traversal, symlink escape, digest mismatch and unsafe previews ---


def test_a_reference_inside_the_root_is_confined(artifact_root: Path) -> None:
    confined = al.confine_reference(artifact_root, "site/index.html")

    assert confined.relative_path == "site/index.html"
    assert confined.content_sha256 == _digest_of(artifact_root / "site" / "index.html")
    assert confined.size == (artifact_root / "site" / "index.html").stat().st_size
    absolute = al.confine_reference(artifact_root, str(artifact_root / "site" / "index.html"))
    assert absolute.relative_path == confined.relative_path


def test_parent_traversal_is_refused(artifact_root: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("outside the root\n", encoding="utf-8")

    for reference in ("../secret.txt", "site/../../secret.txt", ""):
        with pytest.raises(al.ArtifactReferenceError):
            al.confine_reference(artifact_root, reference)


def test_an_absolute_reference_outside_the_root_is_refused(artifact_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.html"
    outside.write_text("outside the root\n", encoding="utf-8")

    with pytest.raises(al.ArtifactReferenceError, match="outside"):
        al.confine_reference(artifact_root, str(outside))


def test_a_symlinked_file_is_refused(artifact_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.html"
    outside.write_text("bytes the root does not own\n", encoding="utf-8")
    (artifact_root / "site" / "link.html").symlink_to(outside)

    with pytest.raises(al.ArtifactReferenceError, match="symlink"):
        al.confine_reference(artifact_root, "site/link.html")


def test_a_symlinked_directory_component_is_refused(artifact_root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.html").write_text("x\n", encoding="utf-8")
    (artifact_root / "linked").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(al.ArtifactReferenceError, match="symlink"):
        al.confine_reference(artifact_root, "linked/x.html")


def test_a_mismatched_hash_is_refused(artifact_root: Path) -> None:
    with pytest.raises(al.ArtifactReferenceError, match="digest"):
        al.confine_reference(artifact_root, "site/index.html", expected_sha256=BLOB_A)


def test_a_directory_is_not_an_artifact_reference(artifact_root: Path) -> None:
    with pytest.raises(al.ArtifactReferenceError, match="regular files"):
        al.confine_reference(artifact_root, "site")


def test_a_confined_preview_is_allowed_and_reports_completeness(
    registry: al.ArtifactRegistry, artifact_root: Path
) -> None:
    digest = _digest_of(artifact_root / "site" / "index.html")
    _record_versioned(registry, digest)

    pending = registry.check_preview(digest, artifact_root, "site/index.html")
    assert pending.allowed is True
    assert pending.complete is False, "a preview may be shown, but not as a finished artifact"
    assert pending.reference is not None and pending.reference.relative_path == "site/index.html"

    registry.record_check(digest, "render", outcome="passed")
    done = registry.check_preview(digest, artifact_root, "site/index.html")
    assert done.allowed is True and done.complete is True


def test_an_unsafe_or_unowned_preview_is_refused(
    registry: al.ArtifactRegistry, artifact_root: Path, tmp_path: Path
) -> None:
    digest = _digest_of(artifact_root / "site" / "index.html")
    unknown = registry.check_preview(BLOB_C, artifact_root, "site/index.html")
    assert unknown.allowed is False and unknown.code == "unknown_artifact"

    _record_versioned(registry, digest, trust="untrusted")
    untrusted = registry.check_preview(digest, artifact_root, "site/index.html")
    assert untrusted.allowed is False and untrusted.code == "untrusted_content"

    escaped = al.ArtifactRegistry(tmp_path / "second")
    _record_versioned(escaped, digest)
    outside = escaped.check_preview(digest, artifact_root, "../secret.html")
    assert outside.allowed is False and outside.code == "unsafe_reference"

    secret = al.ArtifactRegistry(tmp_path / "third")
    _record_versioned(secret, digest, confidentiality="secret")
    refused = secret.check_preview(digest, artifact_root, "site/index.html")
    assert refused.allowed is False and refused.code == "confidential_content"
    assert secret.check_preview(digest, artifact_root, "site/index.html", allow_secret=True).allowed is True


def test_a_preview_of_an_invalidated_artifact_is_refused(
    registry: al.ArtifactRegistry, artifact_root: Path
) -> None:
    digest = _digest_of(artifact_root / "site" / "index.html")
    _record_versioned(registry, digest)
    registry.invalidate(digest, reason="build output superseded")

    verdict = registry.check_preview(digest, artifact_root, "site/index.html")

    assert verdict.allowed is False and verdict.code == "invalidated_artifact"


def test_a_preview_whose_bytes_changed_is_refused(
    registry: al.ArtifactRegistry, artifact_root: Path
) -> None:
    target = artifact_root / "site" / "index.html"
    recorded = _digest_of(target)
    _record_versioned(registry, recorded)
    target.write_text("<h1>edited after the build</h1>", encoding="utf-8")

    # The recorded identity no longer describes the bytes on disk, so the preview is a
    # digest mismatch rather than a silently-changed artifact.
    verdict = registry.check_preview(recorded, artifact_root, "site/index.html")
    assert verdict.allowed is False and verdict.code == "unsafe_reference"
    assert "digest" in verdict.reason

    # A caller may not escape the check by presenting the new bytes as a known artifact.
    assert registry.check_preview(_digest_of(target), artifact_root, "site/index.html").allowed is False


def test_a_confined_file_artifact_records_the_confined_identity(
    registry: al.ArtifactRegistry, artifact_root: Path
) -> None:
    entry = registry.record_file_artifact(
        artifact_root,
        "site/index.html",
        trust="trusted",
        producer="workflow:site-builder",
        required_checks=("render",),
    )

    assert entry.source_kind == "workspace_file"
    assert entry.source_ref == "site/index.html"
    assert entry.content_sha256 == _digest_of(artifact_root / "site" / "index.html")
    with pytest.raises(al.ArtifactReferenceError):
        registry.record_file_artifact(artifact_root, "../escape.html")


# --- AC4: partial output and missing checks cannot look accepted ---


def test_an_artifact_without_declared_checks_is_never_complete(registry: al.ArtifactRegistry) -> None:
    registry.record_artifact(BLOB_A, source_kind="pipeline", source_ref="run", trust="trusted")

    status = registry.artifact_status(BLOB_A)

    assert status.state == al.ARTIFACT_PARTIAL
    assert status.complete is False
    assert status.code == "checks_not_declared"
    with pytest.raises(al.ArtifactIncompleteError):
        registry.require_complete(BLOB_A)


def test_missing_and_failed_checks_are_reported_by_name(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry, checks=("render", "lint"))
    registry.record_check(BLOB_A, "render", outcome="failed", detail="unclosed tag")

    status = registry.artifact_status(BLOB_A)

    assert status.state == al.ARTIFACT_PARTIAL
    assert status.missing_checks == ("lint",)
    assert status.failed_checks == ("render",)
    assert status.code == "checks_failed"
    with pytest.raises(al.ArtifactIncompleteError):
        registry.require_complete(BLOB_A)


def test_a_pass_on_one_check_cannot_cover_a_failure_on_another(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry, checks=("render", "lint"))
    registry.record_check(BLOB_A, "render", outcome="passed")
    registry.record_check(BLOB_A, "lint", outcome="failed")

    assert registry.artifact_status(BLOB_A).complete is False


def test_a_re_run_supersedes_the_outcome_but_keeps_the_history(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry)
    registry.record_check(BLOB_A, "render", outcome="failed")
    assert registry.artifact_status(BLOB_A).failed_checks == ("render",)

    registry.record_check(BLOB_A, "render", outcome="passed")

    status = registry.require_complete(BLOB_A)
    assert status.state == al.ARTIFACT_COMPLETE and status.complete is True
    assert [check.outcome for check in registry.checks_for(BLOB_A)] == ["failed", "passed"]


def test_checks_after_invalidation_cannot_resurrect_the_artifact(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry)
    registry.invalidate(BLOB_A, reason="input contaminated")
    registry.record_check(BLOB_A, "render", outcome="passed")

    status = registry.artifact_status(BLOB_A)

    assert status.state == al.ARTIFACT_INVALIDATED
    assert status.complete is False
    with pytest.raises(al.ArtifactIncompleteError):
        registry.require_complete(BLOB_A)


def test_an_unknown_artifact_or_outcome_is_refused(registry: al.ArtifactRegistry) -> None:
    with pytest.raises(al.UnknownArtifactError):
        registry.record_check(BLOB_B, "render", outcome="passed")
    _record_versioned(registry)
    with pytest.raises(al.ArtifactLineageError):
        registry.record_check(BLOB_A, "render", outcome="probably-fine")
    assert registry.artifact_status(BLOB_C).state == al.ARTIFACT_UNKNOWN


# --- AC5: export/delete preserve privacy and scoped reference history ---


def test_export_refuses_secret_content_until_it_is_explicitly_allowed(
    registry: al.ArtifactRegistry,
) -> None:
    _record_versioned(registry, confidentiality="secret")
    registry.record_check(BLOB_A, "render", outcome="passed")

    with pytest.raises(al.ArtifactExportError):
        registry.export_artifact(BLOB_A)

    export = registry.export_artifact(BLOB_A, allow_secret=True, requested_by="task-7")
    assert export.complete is True
    assert export.confidentiality == "secret"
    assert export.identity_sha256 == registry.entries_for(BLOB_A)[0].identity_sha256
    recorded = registry.exports()[-1]
    assert recorded["export_id"] == export.export_id
    assert recorded["requested_by"] == "task-7"
    assert registry.summary()["export_count"] == 1


def test_export_marks_partial_output_and_refuses_invalidated_output(
    registry: al.ArtifactRegistry,
) -> None:
    _record_versioned(registry)

    partial = registry.export_artifact(BLOB_A)
    assert partial.complete is False
    assert partial.state == al.ARTIFACT_PARTIAL
    assert partial.missing_checks == ("render",)

    registry.invalidate(BLOB_A, reason="build superseded")
    with pytest.raises(al.ArtifactExportError):
        registry.export_artifact(BLOB_A)


def test_deletion_needs_confirmation_a_reason_and_no_live_dependants(
    registry: al.ArtifactRegistry,
) -> None:
    _record_versioned(registry)
    with pytest.raises(al.ArtifactDeletionError, match="confirmation"):
        registry.delete_artifact(BLOB_A, reason="cleanup")
    with pytest.raises(al.ArtifactDeletionError, match="reason"):
        registry.delete_artifact(BLOB_A, reason="   ", confirmed=True)

    registry.pin("review-pin", kind="review", ref=BLOB_A)
    with pytest.raises(al.ArtifactDeletionError, match="pinned"):
        registry.delete_artifact(BLOB_A, reason="cleanup", confirmed=True)
    registry.unpin("review-pin")

    registry.record_artifact(BLOB_B, source_kind="pipeline", source_ref="bundle", derived_from=[BLOB_A])
    with pytest.raises(al.ArtifactDeletionError, match="derived"):
        registry.delete_artifact(BLOB_A, reason="cleanup", confirmed=True)

    registry.invalidate(BLOB_B, reason="derivative superseded")

    # Nothing current depends on it any more, so the deletion is allowed.
    deletion = registry.delete_artifact(BLOB_A, reason="superseded build", confirmed=True)
    assert deletion.content_sha256 == BLOB_A
    assert registry.entries_for(BLOB_A) == ()


def test_valid_evidence_blocks_deletion(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry)
    record = registry.record_evidence(kind="verification", subject="site", content_sha256=BLOB_A)

    with pytest.raises(al.ArtifactDeletionError, match="Evidence"):
        registry.delete_artifact(BLOB_A, reason="cleanup", confirmed=True)

    registry.accept_evidence(record.evidence_id, current_files={})  # still valid
    with pytest.raises(al.ArtifactDeletionError, match="Evidence"):
        registry.delete_artifact(BLOB_A, reason="cleanup", confirmed=True)


def test_deletion_leaves_a_scoped_tombstone_without_content_or_source_reference(
    registry: al.ArtifactRegistry,
) -> None:
    entry = _record_versioned(registry)
    registry.record_check(BLOB_A, "render", outcome="failed", detail="unclosed tag")

    deletion = registry.delete_artifact(BLOB_A, reason="owner withdrew the draft", confirmed=True)

    tombstone = registry.tombstones()[BLOB_A]
    assert tombstone["identity_sha256"] == entry.identity_sha256 == deletion.identity_sha256
    assert tombstone["reason"] == "owner withdrew the draft"
    preserved = tombstone["preserved_references"][0]
    assert preserved["entry_id"] == entry.entry_id
    assert preserved["source_kind"] == "pipeline"
    assert preserved["required_checks"] == ["render"]
    # Privacy: the free-form source reference and the content are not retained.
    assert "source_ref" not in preserved
    assert "run-1" not in json.dumps(tombstone)
    # Audit: the check history survives the deletion.
    assert [check.outcome for check in registry.checks_for(BLOB_A)] == ["failed"]
    assert registry.summary()["tombstone_count"] == 1


def test_deleting_secret_content_needs_an_explicit_allowance(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry, confidentiality="secret")

    with pytest.raises(al.ArtifactDeletionError):
        registry.delete_artifact(BLOB_A, reason="cleanup", confirmed=True)

    registry.delete_artifact(BLOB_A, reason="cleanup", confirmed=True, allow_secret=True)
    assert registry.entries_for(BLOB_A) == ()


# --- Migration: a populated v1 store upgrades without losing anything ---------


def _rewrite_as_v1(registry: al.ArtifactRegistry) -> None:
    """Turn the store into a genuine v1 document: no v2-only fields at all."""
    raw = json.loads(registry.state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    for blob in raw["blobs"].values():
        for item in blob["entries"].values():
            for key in (
                "producer",
                "producer_version",
                "config_sha256",
                "inputs",
                "required_checks",
                "identity_sha256",
            ):
                item.pop(key, None)
    raw.pop("checks", None)
    raw.pop("exports", None)
    raw.pop("tombstones", None)
    registry.state_path.write_text(json.dumps(raw), encoding="utf-8")


def test_a_populated_v1_store_is_upgraded_without_losing_records(
    registry: al.ArtifactRegistry,
) -> None:
    entry = registry.record_artifact(
        BLOB_A, source_kind="baseline", source_ref="HEAD", trust="trusted", task_id="task-1"
    )
    evidence = registry.record_evidence(
        kind="verification", subject="patch", content_sha256=BLOB_A, source_files={"app.py": BLOB_A}
    )
    registry.pin("keep-me", kind="review", ref=BLOB_B)
    _rewrite_as_v1(registry)

    upgraded = al.ArtifactRegistry(_store_root(registry))
    restored = upgraded.entries_for(BLOB_A)

    assert [item.entry_id for item in restored] == [entry.entry_id]
    assert upgraded.effective_trust(BLOB_A) == "trusted"
    assert upgraded.pins()["keep-me"]["ref"] == BLOB_B
    assert upgraded.evidence(evidence.evidence_id).status == al.EVIDENCE_VALID
    assert upgraded.accept_evidence(evidence.evidence_id, current_files={"app.py": BLOB_A}).accepted is True
    # A v1 artifact has no declared checks, so it is explicitly not complete.
    assert upgraded.artifact_status(BLOB_A).code == "checks_not_declared"

    upgraded.record_artifact(BLOB_C, source_kind="baseline", source_ref="HEAD", required_checks=("render",))
    written = json.loads(upgraded.state_path.read_text(encoding="utf-8"))
    assert written["schema_version"] == al.AL_SCHEMA_VERSION
    assert entry.entry_id in written["blobs"][BLOB_A]["entries"]
    assert written["pins"]["keep-me"]["ref"] == BLOB_B
    assert evidence.evidence_id in written["evidence"]


def test_a_newer_store_schema_is_refused_rather_than_downgraded(registry: al.ArtifactRegistry) -> None:
    _record_versioned(registry)
    raw = json.loads(registry.state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = al.AL_SCHEMA_VERSION + 1
    registry.state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(al.ArtifactLineageIntegrityError):
        al.ArtifactRegistry(_store_root(registry)).entries_for(BLOB_A)
