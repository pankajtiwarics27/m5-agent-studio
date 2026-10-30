"""M5-P2-03 tests: multi-file rollback and action reconciliation.

The behaviours pinned here are the story's acceptance criteria:

* a partially applied multi-file edit is undone from the durable batch journal and
  the bytes it replaced — never by a destructive Git reset — while every file the
  batch did not touch, and every later user edit outside the written regions,
  survives (AC1);
* a rollback refuses to overwrite a file that changed after the batch wrote it,
  and a refused rollback writes nothing at all (AC2);
* when an unknown-outcome managed edit exists for the task, or the batch cannot be
  undone, the batch is left in an explicit ``unresolved`` state with per-path
  reasons instead of guessing (AC3);
* an interruption part-way through the write or part-way through the rollback is
  resumable from durable state.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app as studio
import edit_rollback
import runtime_persistence as rp
import workspace_snapshots


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Engine-level helpers (journal + CAS + engine, no HTTP/tool layer)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_env(tmp_path: Path) -> SimpleNamespace:
    store = tmp_path / "store"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    journal = edit_rollback.EditBatchJournal(store)
    cas = workspace_snapshots.ContentAddressedBlobStore(store / "edits" / "blobs")
    engine = edit_rollback.EditRollbackEngine(journal, cas)
    return SimpleNamespace(store=store, workspace=workspace, journal=journal, cas=cas, engine=engine)


def _apply_step(
    env: SimpleNamespace,
    batch_id: str,
    rel_path: str,
    new_text: str,
    *,
    tool: str = "write_file",
    edit_id: str = "",
) -> edit_rollback.EditStep:
    """Mimic the runtime: read the pre-image, record the step, then write atomically."""
    root = env.workspace
    pre = workspace_snapshots.read_confined_target(root, rel_path)
    post = new_text.encode("utf-8")
    pre_blob = env.cas.put_bytes(pre)[0] if pre is not None else None
    post_blob = env.cas.put_bytes(post)[0]
    pre_mode = None
    if pre is not None:
        pre_mode = stat.S_IMODE((root / rel_path).stat().st_mode)
    _, step = env.journal.record_step(
        batch_id,
        rel_path=rel_path,
        tool=tool,
        pre_sha256=_sha(pre) if pre is not None else None,
        post_sha256=_sha(post),
        pre_blob=pre_blob,
        post_blob=post_blob,
        pre_mode=pre_mode,
        edit_id=edit_id,
    )
    workspace_snapshots.atomic_write_confined_bytes(root, rel_path, post, mode=pre_mode)
    return step


# ---------------------------------------------------------------------------
# 1. The inverse patch itself
# ---------------------------------------------------------------------------


def test_inverse_merge_reverts_the_written_region_and_keeps_user_edits() -> None:
    pre = "header\nvalue = 1\nfooter\n"
    post = "header\nvalue = 2\nfooter\n"
    user = "user-one\nheader\nvalue = 2\nfooter\nuser-two\n"

    merged, reason = edit_rollback.inverse_merge(user, pre, post)

    assert reason == ""
    assert merged == "user-one\nheader\nvalue = 1\nfooter\nuser-two\n"


def test_inverse_merge_refuses_when_the_written_region_changed_too() -> None:
    pre = "value = 1\n"
    post = "value = 2\n"
    user = "value = 3\n"

    merged, reason = edit_rollback.inverse_merge(user, pre, post)

    assert merged is None
    assert "changed since" in reason


def test_inverse_merge_keeps_a_user_insertion_around_the_written_region() -> None:
    pre = "a\nb\n"
    post = "a\nb\nc\n"

    merged, reason = edit_rollback.inverse_merge("z\na\nb\nc\n", pre, post)

    assert reason == ""
    assert merged == "z\na\nb\n"


def test_inverse_merge_refuses_when_the_user_inserted_inside_the_written_region() -> None:
    pre = "a\n"
    post = "a\nb\nc\n"

    merged, reason = edit_rollback.inverse_merge("a\nb\nuser\nc\n", pre, post)

    assert merged is None
    assert "changed since" in reason


# ---------------------------------------------------------------------------
# 2. The durable batch journal
# ---------------------------------------------------------------------------


def test_journal_records_steps_privately_and_keeps_the_first_pre_image(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace, task_id="task-1")
    first = _apply_step(env, batch.batch_id, "a.py", "a = 2\n")
    second = _apply_step(env, batch.batch_id, "a.py", "a = 3\n")

    reloaded = env.journal.load(batch.batch_id)
    assert len(reloaded.steps) == 1
    step = reloaded.steps[0]
    assert step.pre_sha256 == first.pre_sha256 == _sha(b"a = 1\n")
    assert step.post_sha256 == second.post_sha256 == _sha(b"a = 3\n")
    assert step.note == "edited again inside the same batch"

    batch_path = env.journal.batch_path(batch.batch_id)
    assert stat.S_IMODE(batch_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(batch_path.parent.stat().st_mode) == 0o700
    assert json.loads(batch_path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_journal_refuses_unsafe_ids_and_unknown_states(engine_env: SimpleNamespace) -> None:
    env = engine_env
    batch = env.journal.open_batch("sess-1", env.workspace)
    with pytest.raises(edit_rollback.EditRollbackError):
        env.journal.batch_path("../../escape")
    with pytest.raises(edit_rollback.EditBatchNotFound):
        env.journal.load("eb-doesnotexist")
    with pytest.raises(edit_rollback.EditRollbackError):
        env.journal.close_batch(batch.batch_id, state="not-a-state")
    with pytest.raises(edit_rollback.EditRollbackError):
        env.journal.record_step(
            batch.batch_id,
            rel_path="a.py",
            tool="write_file",
            pre_sha256=None,
            post_sha256="not-a-digest",
            pre_blob=None,
        )


def test_mark_step_is_durable_and_rejects_a_missing_path(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")

    env.journal.mark_step(batch.batch_id, "a.py", state="skipped", note="write refused")

    step = env.journal.load(batch.batch_id).steps[0]
    assert step.state == "skipped"
    assert step.note == "write refused"
    with pytest.raises(edit_rollback.EditRollbackError):
        env.journal.mark_step(batch.batch_id, "missing.py", state="skipped")


# ---------------------------------------------------------------------------
# 3. Planning and applying a rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_replaced_files_and_removes_created_ones(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "keep.txt").write_text("untouched\n")
    (env.workspace / "a.py").write_text("a = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace, task_id="task-1")
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")
    _apply_step(env, batch.batch_id, "new.py", "created = True\n")

    plan = env.engine.plan(batch.batch_id, env.workspace)
    assert plan.safe
    assert {decision.rel_path: decision.decision for decision in plan.decisions} == {
        "a.py": edit_rollback.DECISION_REVERT,
        "new.py": edit_rollback.DECISION_DELETE,
    }
    # Planning is read-only.
    assert (env.workspace / "a.py").read_text() == "a = 2\n"
    assert (env.workspace / "new.py").exists()

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "rolled_back"
    assert result.ok
    assert (env.workspace / "a.py").read_text() == "a = 1\n"
    assert not (env.workspace / "new.py").exists()
    assert (env.workspace / "keep.txt").read_text() == "untouched\n"
    assert env.journal.load(batch.batch_id).state == "rolled_back"


def test_dry_run_reports_the_plan_without_writing(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")

    result = env.engine.execute(batch.batch_id, env.workspace, dry_run=True)

    assert result.dry_run and result.ok
    assert (env.workspace / "a.py").read_text() == "a = 2\n"
    assert env.journal.load(batch.batch_id).state == "open"


def test_rollback_refuses_to_overwrite_a_later_user_edit(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    (env.workspace / "b.py").write_text("b = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")
    _apply_step(env, batch.batch_id, "b.py", "b = 2\n")
    # The user rewrites one of the batch's files afterwards.
    (env.workspace / "a.py").write_text("a = 'user edit'\n")

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "unresolved"
    assert not result.ok
    assert result.failed == ()
    assert any("a.py" in reason for reason in result.reasons)
    # A refused rollback performs no writes at all — not even on the safe path.
    assert (env.workspace / "a.py").read_text() == "a = 'user edit'\n"
    assert (env.workspace / "b.py").read_text() == "b = 2\n"
    assert env.journal.load(batch.batch_id).state == "unresolved"


def test_rollback_merges_around_user_edits_in_unwritten_regions(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "doc.txt").write_text("intro\nbody = 1\noutro\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "doc.txt", "intro\nbody = 2\noutro\n")
    (env.workspace / "doc.txt").write_text("intro\nbody = 2\noutro\nappended by user\n")

    plan = env.engine.plan(batch.batch_id, env.workspace)
    assert [decision.decision for decision in plan.decisions] == [edit_rollback.DECISION_REVERT_MERGED]

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "rolled_back"
    assert (env.workspace / "doc.txt").read_text() == "intro\nbody = 1\noutro\nappended by user\n"


def test_rollback_refuses_when_a_created_file_was_edited_afterwards(engine_env: SimpleNamespace) -> None:
    env = engine_env
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "new.py", "created = 1\n")
    (env.workspace / "new.py").write_text("created = 1\nuser_line = 2\n")

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "unresolved"
    assert (env.workspace / "new.py").read_text() == "created = 1\nuser_line = 2\n"


def test_rollback_refuses_when_the_file_was_deleted_afterwards(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")
    (env.workspace / "a.py").unlink()

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "unresolved"
    assert not (env.workspace / "a.py").exists()


# ---------------------------------------------------------------------------
# 4. Interrupted edits and interrupted rollbacks
# ---------------------------------------------------------------------------


def test_interrupted_multi_file_edit_is_recoverable(engine_env: SimpleNamespace) -> None:
    """The crash leaves one written file, one recorded-but-never-written step."""
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    (env.workspace / "b.py").write_text("b = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")
    # Recorded intent for the second file, then the process died before the write.
    pre = (env.workspace / "b.py").read_bytes()
    env.journal.record_step(
        batch.batch_id,
        rel_path="b.py",
        tool="write_file",
        pre_sha256=_sha(pre),
        post_sha256=_sha(b"b = 2\n"),
        pre_blob=env.cas.put_bytes(pre)[0],
        post_blob=env.cas.put_bytes(b"b = 2\n")[0],
    )

    plan = env.engine.plan(batch.batch_id, env.workspace)
    decisions = {decision.rel_path: decision.decision for decision in plan.decisions}
    assert decisions == {"a.py": edit_rollback.DECISION_REVERT, "b.py": edit_rollback.DECISION_ALREADY_REVERTED}

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "rolled_back"
    assert (env.workspace / "a.py").read_text() == "a = 1\n"
    assert (env.workspace / "b.py").read_text() == "b = 1\n"


def test_resumed_rollback_only_redoes_the_steps_still_applied(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    (env.workspace / "b.py").write_text("b = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")
    _apply_step(env, batch.batch_id, "b.py", "b = 2\n")

    # Simulate the crash: a.py was already restored and durably marked, b.py was not.
    workspace_snapshots.atomic_write_confined_bytes(env.workspace, "a.py", b"a = 1\n")
    env.journal.mark_step(batch.batch_id, "a.py", state="rolled_back", note="restored before the crash")

    plan = env.engine.plan(batch.batch_id, env.workspace)
    assert [(d.rel_path, d.decision) for d in plan.decisions] == [("b.py", edit_rollback.DECISION_REVERT)]

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "rolled_back"
    assert result.applied == ("b.py",)
    assert (env.workspace / "a.py").read_text() == "a = 1\n"
    assert (env.workspace / "b.py").read_text() == "b = 1\n"


def test_blocked_reasons_abort_the_rollback_before_any_write(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")

    result = env.engine.execute(
        batch.batch_id,
        env.workspace,
        blocked_reasons=("an unresolved managed edit may still be writing",),
    )

    assert result.state == "unresolved"
    assert result.reasons == ("an unresolved managed edit may still be writing",)
    assert (env.workspace / "a.py").read_text() == "a = 2\n"
    notes = env.journal.load(batch.batch_id).notes
    assert any("unresolved managed edit" in note for note in notes)


def test_rollback_refuses_a_symlinked_target(engine_env: SimpleNamespace) -> None:
    env = engine_env
    (env.workspace / "a.py").write_text("a = 1\n")
    batch = env.journal.open_batch("sess-1", env.workspace)
    _apply_step(env, batch.batch_id, "a.py", "a = 2\n")
    # The written file is replaced by a symlink pointing outside the workspace.
    (env.workspace / "a.py").unlink()
    outside = env.store / "outside.py"
    outside.write_text("secret\n")
    (env.workspace / "a.py").symlink_to(outside)

    result = env.engine.execute(batch.batch_id, env.workspace)

    assert result.state == "unresolved"
    assert outside.read_text() == "secret\n"
    assert (env.workspace / "a.py").is_symlink()


# ---------------------------------------------------------------------------
# 5. Application wiring: tool surface, journal from real edits, reconciliation
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "studio_p2_03.db"
    harness_dir = tmp_path / ".harnesses"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_dir)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    studio.init_db()
    store = rp.RuntimePersistenceStore(studio.STORAGE, studio.DB_PATH)
    store.create_task("task-p2-03", policy="standard", state="running", now=100.0)
    return workspace


def _sess(workspace: Path, session_id: str = "sess-p2-03") -> dict[str, Any]:
    return {
        "id": session_id,
        "task_id": "task-p2-03",
        "worktree_dir": str(workspace),
        "policy": "standard",
    }


def _run(workspace: Path, tool: str, args: dict[str, Any], session_id: str = "sess-p2-03") -> dict[str, Any]:
    return asyncio.run(studio._execute_harness_tool(None, _sess(workspace, session_id), tool, args))


def _write(workspace: Path, path: str, content: str) -> dict[str, Any]:
    return _run(workspace, "write_file", {"path": path, "content": content})


def test_managed_edits_are_journalled_and_roll_back_through_the_tool_surface(isolated_workspace: Path) -> None:
    workspace = isolated_workspace
    # `a.py` predates the batch (so it has a pre-image to restore); `b.py` is ours.
    (workspace / "a.py").write_text("base = 0\n")
    assert _write(workspace, "a.py", "a = 1\n")["ok"]
    assert _run(workspace, "edit_file", {"path": "a.py", "old_text": "a = 1", "new_text": "a = 2"})["ok"]
    assert _write(workspace, "b.py", "created = True\n")["ok"]
    (workspace / "user.txt").write_text("mine\n")

    listing = _run(workspace, "list_edit_batches", {})
    assert listing["ok"]
    assert len(listing["batches"]) == 1
    batch = listing["batches"][0]
    assert batch["state"] == "open"
    assert batch["paths"] == ["a.py", "b.py"]
    batch_id = batch["batch_id"]

    planned = _run(workspace, "rollback_edits", {"batch_id": batch_id})
    assert planned["ok"]
    assert planned["rollback"]["dry_run"] is True
    assert (workspace / "a.py").read_text() == "a = 2\n"

    applied = _run(workspace, "rollback_edits", {"batch_id": batch_id, "dry_run": False})

    assert applied["ok"]
    assert applied["rollback"]["state"] == "rolled_back"
    assert (workspace / "a.py").read_text() == "base = 0\n"
    assert not (workspace / "b.py").exists()
    assert (workspace / "user.txt").read_text() == "mine\n"
    assert studio._read_harness_events("sess-p2-03")[-1]["type"] == "edit/rollback"


def test_rollback_tool_refuses_a_batch_from_another_session(isolated_workspace: Path) -> None:
    workspace = isolated_workspace
    assert _write(workspace, "a.py", "a = 1\n")["ok"]
    batch_id = _run(workspace, "list_edit_batches", {})["batches"][0]["batch_id"]

    denied = _run(workspace, "rollback_edits", {"batch_id": batch_id, "dry_run": False}, session_id="sess-other")

    assert not denied["ok"]
    assert denied["error_code"] == "edit_rollback_refused"
    assert (workspace / "a.py").read_text() == "a = 1\n"


def test_rollback_tool_reports_an_unresolved_batch_without_writing(isolated_workspace: Path) -> None:
    workspace = isolated_workspace
    assert _write(workspace, "a.py", "a = 1\n")["ok"]
    batch_id = _run(workspace, "list_edit_batches", {})["batches"][0]["batch_id"]
    (workspace / "a.py").write_text("a = 'user rewrite'\n")

    refused = _run(workspace, "rollback_edits", {"batch_id": batch_id, "dry_run": False})

    assert not refused["ok"]
    assert refused["rollback"]["state"] == "unresolved"
    assert (workspace / "a.py").read_text() == "a = 'user rewrite'\n"


def test_reconciliation_blocks_rollback_while_a_managed_edit_is_unresolved(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = isolated_workspace
    assert _write(workspace, "a.py", "a = 1\n")["ok"]
    batch_id = _run(workspace, "list_edit_batches", {})["batches"][0]["batch_id"]
    record = SimpleNamespace(
        action_id="act-uncertain",
        tool_name="edit_file",
        status="uncertain",
        error_code="ambiguity_on_dispatch",
        updated_at=1.0,
    )
    real_ledger = studio.get_durable_action_ledger()

    class _LedgerWithUnresolvedAction:
        """Real ledger, with one unknown-outcome managed edit reported for the task."""

        def list_task_actions(self, *_a: Any, **_k: Any) -> list[Any]:
            return [record]

        def __getattr__(self, name: str) -> Any:
            return getattr(real_ledger, name)

    monkeypatch.setattr(studio, "get_durable_action_ledger", lambda *a, **k: _LedgerWithUnresolvedAction())

    blocked = _run(workspace, "rollback_edits", {"batch_id": batch_id, "dry_run": False})

    assert not blocked["ok"]
    assert blocked["rollback"]["state"] == "unresolved"
    assert (workspace / "a.py").read_text() == "a = 1\n"
    assert blocked["reconciliation"]["blocked_reasons"]
    assert "act-uncertain" in blocked["output"]

    acknowledged = _run(
        workspace,
        "rollback_edits",
        {"batch_id": batch_id, "dry_run": False, "acknowledge_unresolved": True},
    )

    assert acknowledged["ok"]
    assert acknowledged["reconciliation"]["acknowledged_action_ids"] == ["act-uncertain"]
    events = [line for line in studio._read_harness_events("sess-p2-03") if line["type"] == "edit/rollback"]
    assert events[-1]["acknowledged_action_ids"] == ["act-uncertain"]


def test_reconciliation_fails_closed_when_the_ledger_is_unreadable(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = isolated_workspace
    assert _write(workspace, "a.py", "a = 1\n")["ok"]
    batch_id = _run(workspace, "list_edit_batches", {})["batches"][0]["batch_id"]

    real_ledger = studio.get_durable_action_ledger()

    class _UnreadableLedger:
        """Every other ledger call works; only the reconciliation read fails."""

        def list_task_actions(self, *_a: Any, **_k: Any) -> list[Any]:
            raise RuntimeError("ledger unavailable")

        def __getattr__(self, name: str) -> Any:
            return getattr(real_ledger, name)

    monkeypatch.setattr(studio, "get_durable_action_ledger", lambda *a, **k: _UnreadableLedger())

    report = _run(workspace, "reconcile_task_actions", {})
    assert report["ok"]
    assert report["blocked_reasons"]

    refused = _run(workspace, "rollback_edits", {"batch_id": batch_id, "dry_run": False})
    assert not refused["ok"]
    assert (workspace / "a.py").read_text() == "a = 1\n"


def test_reconcile_tool_reports_a_clean_task(isolated_workspace: Path) -> None:
    workspace = isolated_workspace
    report = _run(workspace, "reconcile_task_actions", {})
    assert report["ok"]
    assert report["unresolved_actions"] == []
    assert report["blocked_reasons"] == []
    assert "no managed edit" in report["output"]


def test_rollback_is_denied_by_plan_policy(isolated_workspace: Path) -> None:
    workspace = isolated_workspace
    assert _write(workspace, "a.py", "a = 1\n")["ok"]
    batch_id = _run(workspace, "list_edit_batches", {})["batches"][0]["batch_id"]

    sess = _sess(workspace)
    sess["policy"] = "plan"
    denied = asyncio.run(studio._execute_harness_tool(None, sess, "rollback_edits", {"batch_id": batch_id}))

    assert not denied["ok"]
    assert "plan" in denied["output"]


def test_failed_write_is_not_left_as_an_applied_step(isolated_workspace: Path) -> None:
    workspace = isolated_workspace
    assert _write(workspace, "a.py", "a = 1\n")["ok"]

    def _conflict(*_a: Any, **_k: Any) -> str:
        raise workspace_snapshots.SnapshotConflictError("raced")

    original = studio._atomic_write_workspace_text
    studio._atomic_write_workspace_text = _conflict
    try:
        refused = _write(workspace, "b.py", "b = 1\n")
    finally:
        studio._atomic_write_workspace_text = original
    assert not refused["ok"]
    assert not (workspace / "b.py").exists()

    batch_id = _run(workspace, "list_edit_batches", {})["batches"][0]["batch_id"]
    steps = {step.rel_path: step for step in studio._edit_batch_journal().load(batch_id).steps}
    assert steps["a.py"].state == "applied"
    assert steps["b.py"].state == "skipped"

    rolled_back = _run(workspace, "rollback_edits", {"batch_id": batch_id, "dry_run": False})
    assert rolled_back["ok"]
    assert not (workspace / "a.py").exists()


def test_sensitive_paths_are_never_journalled(isolated_workspace: Path) -> None:
    workspace = isolated_workspace
    result = _run(workspace, "write_file", {"path": ".env", "content": "TOKEN=1\n"})
    assert not result["ok"]
    batches = studio._edit_batch_journal().list_batches("sess-p2-03")
    assert all(".env" not in step.rel_path for batch in batches for step in batch.steps)


def test_rollback_module_globals_do_not_collide_with_the_bundle_namespace() -> None:
    """The standalone bundle concatenates inlined modules into one namespace.

    A private helper that shares a name with another inlined module silently
    replaces it (a group-less `_SHA256_RE` in `edit_rollback` once broke
    `edit_transactions.parse_expected_sha256` in the bundle while every
    source-tree test passed), so collisions are refused here rather than in
    production.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    inlined = (
        "runtime_persistence",
        "side_effect_ledger",
        "execution_budget",
        "workflow_checkpoints",
        "workflow_state",
        "mcp_client",
        "context_store",
        "onboarding_preflight",
        "failure_injection",
        "execution_spine",
        "task_events",
        "sandbox_boundary",
        "edit_transactions",
        "workspace_snapshots",
    )

    def top_level_names(path: Path) -> set[str]:
        names: set[str] = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names

    mine = top_level_names(root / "edit_rollback.py")
    others: dict[str, str] = {}
    for module in inlined:
        for name in top_level_names(root / f"{module}.py"):
            others.setdefault(name, module)
    collisions = {name: others[name] for name in mine if name in others}
    assert collisions == {}, f"edit_rollback globals collide inside the standalone namespace: {collisions}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
