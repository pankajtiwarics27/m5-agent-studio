"""Tests for workspace quarantine and trusted reconstruction (`M5-P5-01`)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import workspace_quarantine as wq


def _workspace(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('baseline')\n", encoding="utf-8")
    (root / "notes.md").write_text("trusted notes\n", encoding="utf-8")
    return root


def _mutate(root: Path) -> None:
    """Simulate a contaminated session: edit a known file, drop in an unknown one."""
    (root / "src" / "app.py").write_text("print('accepted change')\n", encoding="utf-8")
    (root / "src" / "surprise.py").write_text("exfiltrate()\n", encoding="utf-8")


def _frozen_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_freeze_preserves_every_byte_and_does_not_touch_the_source(tmp_path: Path) -> None:
    live = _mutate_state = _workspace(tmp_path / "live")
    _mutate(live)
    before = _frozen_bytes(live)

    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="suspected contamination")

    tree = wq.quarantine_dir(tmp_path / "store", manifest.quarantine_id) / wq.WQ_TREE_NAME
    frozen = _frozen_bytes(tree)
    assert frozen == before
    assert _frozen_bytes(live) == before, "freezing must never modify the affected workspace"
    assert manifest.paths() == sorted(before)
    assert {entry.rel_path: entry.sha256 for entry in manifest.files} == {
        rel: __import__("hashlib").sha256(data).hexdigest() for rel, data in before.items()
    }
    assert manifest.tree_sha256


def test_frozen_tree_is_read_only_so_the_evidence_cannot_be_edited(tmp_path: Path) -> None:
    live = _workspace(tmp_path / "live")
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="freeze")
    record = wq.quarantine_dir(tmp_path / "store", manifest.quarantine_id)
    tree = record / wq.WQ_TREE_NAME

    assert stat.S_IMODE((tree / "src" / "app.py").stat().st_mode) & 0o222 == 0
    assert stat.S_IMODE(tree.stat().st_mode) & 0o222 == 0
    # The record itself stays owner-writable so it can be released; the evidence cannot.
    with pytest.raises(PermissionError):
        (tree / "src" / "app.py").write_text("tampered", encoding="utf-8")


def test_symlinks_are_recorded_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("host secret\n", encoding="utf-8")
    live = _workspace(tmp_path / "live")
    os.symlink(outside, live / "link.txt")

    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="symlink probe")
    entry = manifest.get("link.txt")
    assert entry is not None and entry.kind == "symlink"
    assert entry.link_target == str(outside)
    tree = wq.quarantine_dir(tmp_path / "store", manifest.quarantine_id) / wq.WQ_TREE_NAME
    assert not (tree / "link.txt").exists(), "a symlink's target must not be copied into the evidence"
    assert b"host secret" not in b"".join(_frozen_bytes(tree).values())


def test_plan_classifies_unknown_changes_as_ambiguous_not_accepted(tmp_path: Path) -> None:
    baseline = _workspace(tmp_path / "baseline")
    live = _workspace(tmp_path / "live")
    _mutate(live)
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="contamination")

    plan = wq.plan_reconstruction(manifest, baseline_root=baseline, accepted_paths=["src/app.py"])

    assert plan.accepted == ("src/app.py",)
    assert plan.ambiguous == ("src/surprise.py",)
    assert "notes.md" in plan.unchanged


def test_reconstruction_never_promotes_an_unaccepted_change(tmp_path: Path) -> None:
    baseline = _workspace(tmp_path / "baseline")
    live = _workspace(tmp_path / "live")
    _mutate(live)
    unknown = (live / "src" / "surprise.py").read_bytes()
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="contamination")

    result = wq.reconstruct_workspace(
        manifest,
        store_root=tmp_path / "store",
        baseline_root=baseline,
        destination=tmp_path / "recovered",
        accepted_paths=["src/app.py"],
        isolation_qualified=False,
    )

    recovered = Path(result.destination)
    assert (recovered / "src" / "app.py").read_text(encoding="utf-8") == "print('accepted change')\n"
    assert (recovered / "notes.md").read_text(encoding="utf-8") == "trusted notes\n"
    assert not (recovered / "src" / "surprise.py").exists()
    # ...but the unknown change is preserved, not destroyed.
    withheld = Path(result.ambiguous_destination)
    assert withheld.name.endswith(wq.WQ_AMBIGUOUS_SUFFIX)
    assert (withheld / "src" / "surprise.py").read_bytes() == unknown
    assert "src/surprise.py" in (withheld / "AMBIGUOUS.md").read_text(encoding="utf-8")
    assert result.restored == ("src/app.py",)
    assert result.ambiguous == ("src/surprise.py",)
    # The original workspace still holds the contaminated bytes.
    assert (live / "src" / "surprise.py").read_bytes() == unknown


def test_a_truncated_ambiguous_change_is_still_preserved(tmp_path: Path) -> None:
    baseline = _workspace(tmp_path / "baseline")
    live = _workspace(tmp_path / "live")
    (live / "src" / "app.py").write_text("", encoding="utf-8")
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="emptied file")

    result = wq.reconstruct_workspace(
        manifest,
        store_root=tmp_path / "store",
        baseline_root=baseline,
        destination=tmp_path / "recovered",
        accepted_paths=[],
        isolation_qualified=False,
    )

    assert result.restored == ()
    assert result.ambiguous == ("src/app.py",)
    assert Path(result.ambiguous_destination, "src", "app.py").read_bytes() == b""


def test_accepting_a_path_that_was_never_preserved_is_refused(tmp_path: Path) -> None:
    baseline = _workspace(tmp_path / "baseline")
    live = _workspace(tmp_path / "live")
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="nothing changed")

    with pytest.raises(wq.WorkspaceQuarantineIntegrityError):
        wq.plan_reconstruction(manifest, baseline_root=baseline, accepted_paths=["src/never-existed.py"])
    with pytest.raises(wq.WorkspaceQuarantineError):
        wq.plan_reconstruction(manifest, baseline_root=baseline, accepted_paths=["../escape"])


def test_reconstruction_refuses_to_overwrite_existing_user_work(tmp_path: Path) -> None:
    baseline = _workspace(tmp_path / "baseline")
    live = _workspace(tmp_path / "live")
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="contamination")
    destination = tmp_path / "recovered"
    destination.mkdir()
    (destination / "mine.txt").write_text("user work\n", encoding="utf-8")

    with pytest.raises(wq.WorkspaceQuarantineError):
        wq.reconstruct_workspace(
            manifest,
            store_root=tmp_path / "store",
            baseline_root=baseline,
            destination=destination,
            accepted_paths=[],
            isolation_qualified=False,
        )
    assert (destination / "mine.txt").read_text(encoding="utf-8") == "user work\n"


def test_a_modified_quarantine_is_refused_rather_than_reconstructed(tmp_path: Path) -> None:
    baseline = _workspace(tmp_path / "baseline")
    live = _workspace(tmp_path / "live")
    _mutate(live)
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="contamination")
    tree = wq.quarantine_dir(tmp_path / "store", manifest.quarantine_id) / wq.WQ_TREE_NAME
    frozen = tree / "src" / "app.py"
    os.chmod(frozen, 0o600)
    frozen.write_text("tampered evidence\n", encoding="utf-8")

    with pytest.raises(wq.WorkspaceQuarantineIntegrityError, match="does not match"):
        wq.reconstruct_workspace(
            manifest,
            store_root=tmp_path / "store",
            baseline_root=baseline,
            destination=tmp_path / "recovered",
            accepted_paths=["src/app.py"],
            isolation_qualified=False,
        )


def test_host_compromise_without_qualified_isolation_is_unresolvable_locally(tmp_path: Path) -> None:
    baseline = _workspace(tmp_path / "baseline")
    live = _workspace(tmp_path / "live")
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="host compromise")

    result = wq.reconstruct_workspace(
        manifest,
        store_root=tmp_path / "store",
        baseline_root=baseline,
        destination=tmp_path / "recovered",
        accepted_paths=[],
        isolation_qualified=False,
        host_compromise=True,
    )
    assert result.recoverability == "unresolvable_locally"
    assert result.isolated_verification == "unqualified_isolation"

    assert wq.classify_recoverability(host_compromise=True, isolation_qualified=True) == "recoverable_locally"
    assert wq.classify_recoverability(host_compromise=False, isolation_qualified=False) == "recoverable_locally"


def test_active_quarantine_is_reported_until_released(tmp_path: Path) -> None:
    live = _workspace(tmp_path / "live")
    store = tmp_path / "store"
    assert wq.find_active_quarantine(store, live) is None

    manifest = wq.freeze_workspace(live, store_root=store, reason="contamination")
    active = wq.find_active_quarantine(store, live)
    assert active is not None and active.quarantine_id == manifest.quarantine_id
    assert wq.find_active_quarantine(store, tmp_path / "baseline") is None

    wq.release_quarantine(store, manifest.quarantine_id, note="replacement workspace accepted")
    assert wq.find_active_quarantine(store, live) is None
    # Releasing lifts the freeze; it does not delete the preserved bytes.
    assert wq.load_manifest(store, manifest.quarantine_id).paths() == manifest.paths()


def test_manifest_record_is_private_and_persisted_atomically(tmp_path: Path) -> None:
    live = _workspace(tmp_path / "live")
    manifest = wq.freeze_workspace(live, store_root=tmp_path / "store", reason="contamination")
    record = wq.quarantine_dir(tmp_path / "store", manifest.quarantine_id)
    assert stat.S_IMODE(record.stat().st_mode) == 0o700
    assert stat.S_IMODE((record / wq.WQ_MANIFEST_NAME).stat().st_mode) == 0o600
    assert not [p.name for p in record.iterdir() if p.name.endswith(".tmp")]

    reloaded = wq.load_manifest(tmp_path / "store", manifest.quarantine_id)
    assert reloaded.to_mapping() == manifest.to_mapping()
    assert reloaded.schema_version == wq.WQ_SCHEMA_VERSION


def test_duplicate_quarantine_id_is_refused(tmp_path: Path) -> None:
    live = _workspace(tmp_path / "live")
    store = tmp_path / "store"
    wq.freeze_workspace(live, store_root=store, reason="first", quarantine_id="q-fixed")
    with pytest.raises(wq.WorkspaceQuarantineError):
        wq.freeze_workspace(live, store_root=store, reason="second", quarantine_id="q-fixed")


def test_manifest_with_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    live = _workspace(tmp_path / "live")
    store = tmp_path / "store"
    manifest = wq.freeze_workspace(live, store_root=store, reason="contamination")
    path = wq.quarantine_dir(store, manifest.quarantine_id) / wq.WQ_MANIFEST_NAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = wq.WQ_SCHEMA_VERSION + 1
    os.chmod(path, 0o600)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(wq.WorkspaceQuarantineIntegrityError):
        wq.load_manifest(store, manifest.quarantine_id)


def test_freeze_refuses_a_symlinked_workspace_root(tmp_path: Path) -> None:
    live = _workspace(tmp_path / "live")
    link = tmp_path / "link-to-live"
    os.symlink(live, link)
    with pytest.raises(wq.WorkspaceQuarantineError):
        wq.freeze_workspace(link, store_root=tmp_path / "store", reason="symlinked root")


def test_quarantine_freezes_managed_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The freeze is enforced by the harness, not merely recorded."""
    import app

    workspace = _workspace(tmp_path / "ws")
    store = tmp_path / "store"
    monkeypatch.setattr(app, "HARNESS_STORE_DIR", store)

    root_res, parts = app._workspace_relative_parts(workspace, "notes.md")
    assert app._workspace_quarantined_denial(root_res, "/".join(parts)) is None

    manifest = wq.freeze_workspace(workspace, store_root=store, reason="suspected contamination")
    denial = app._workspace_quarantined_denial(root_res, "/".join(parts))
    assert denial is not None
    assert denial["ok"] is False
    assert denial["error_code"] == "workspace_quarantined"
    assert denial["quarantine_id"] == manifest.quarantine_id
    assert "quarantined" in denial["output"]

    wq.release_quarantine(store, manifest.quarantine_id)
    assert app._workspace_quarantined_denial(root_res, "/".join(parts)) is None


def test_unreadable_quarantine_state_denies_instead_of_admitting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app

    workspace = _workspace(tmp_path / "ws")
    monkeypatch.setattr(app, "HARNESS_STORE_DIR", tmp_path / "store")

    def explode(*_args, **_kwargs):
        raise OSError("store unavailable")

    monkeypatch.setattr(wq, "find_active_quarantine", explode)
    root_res, parts = app._workspace_relative_parts(workspace, "notes.md")
    denial = app._workspace_quarantined_denial(root_res, "/".join(parts))
    assert denial is not None and denial["error_code"] == "workspace_quarantined"


def test_a_quarantined_workspace_refuses_real_tool_writes_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: an unknown modification is not silently trusted, and the freeze holds."""
    import asyncio

    import app as studio
    import runtime_persistence as rp

    harness_dir = tmp_path / ".harnesses"
    workspace = _workspace(tmp_path / "workspace")
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_dir)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    studio.init_db()
    rp.RuntimePersistenceStore(studio.STORAGE, studio.DB_PATH).create_task(
        "task-p5-01", policy="standard", state="running", now=100.0
    )
    session = {"id": "sess-p5-01", "task_id": "task-p5-01", "worktree_dir": str(workspace), "policy": "standard"}

    def write(content: str) -> dict[str, object]:
        return asyncio.run(
            studio._execute_harness_tool(None, session, "write_file", {"path": "notes.md", "content": content})
        )

    assert write("accepted\n")["ok"] is True
    accepted_bytes = (workspace / "notes.md").read_bytes()

    manifest = wq.freeze_workspace(workspace, store_root=harness_dir, reason="suspected contamination")
    refusal = write("should never land\n")
    assert refusal["ok"] is False
    assert refusal["error_code"] == "workspace_quarantined"
    assert (workspace / "notes.md").read_bytes() == accepted_bytes

    wq.release_quarantine(harness_dir, manifest.quarantine_id, note="reconstruction accepted")
    assert write("after release\n")["ok"] is True
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "after release\n"
