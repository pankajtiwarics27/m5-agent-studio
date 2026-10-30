"""M5-P2-02 tests: version-aware managed edits, conflict checks, durable edit transactions.

The behaviours pinned here are the story's acceptance criteria:

* an edit whose expected content version no longer holds **conflicts** instead of
  overwriting a concurrent user change (AC1);
* a caller-supplied ``expected_sha256`` is enforced exactly — matching writes,
  mismatching and malformed values are refused (AC2);
* paths outside the workspace, through a symlink, or in the secret set are
  rejected before anything is written (AC3);
* every applied edit is recorded durably with its pre/post digests.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import app as studio
import edit_transactions
import runtime_persistence as rp
import workspace_snapshots


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A disposable harness store + running task + non-Git workspace."""
    db_path = tmp_path / "studio_p2_02.db"
    harness_dir = tmp_path / ".harnesses"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_dir)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    studio.init_db()
    store = rp.RuntimePersistenceStore(studio.STORAGE, studio.DB_PATH)
    store.create_task("task-p2-02", policy="standard", state="running", now=100.0)
    return workspace


def _sess(workspace: Path, session_id: str = "sess-p2-02") -> dict[str, Any]:
    return {
        "id": session_id,
        "task_id": "task-p2-02",
        "worktree_dir": str(workspace),
        "policy": "standard",
    }


def _run(workspace: Path, tool: str, args: dict[str, Any], session_id: str = "sess-p2-02") -> dict[str, Any]:
    return asyncio.run(studio._execute_harness_tool(None, _sess(workspace, session_id), tool, args))


# ---------------------------------------------------------------------------
# 1. Expected-version parsing and the durable stores
# ---------------------------------------------------------------------------


def test_parse_expected_sha256_accepts_documented_forms_and_refuses_the_rest() -> None:
    digest = "A" * 64
    assert edit_transactions.parse_expected_sha256(None) is None
    assert edit_transactions.parse_expected_sha256("") is None
    assert edit_transactions.parse_expected_sha256("   ") is None
    assert edit_transactions.parse_expected_sha256(digest) == "a" * 64
    assert edit_transactions.parse_expected_sha256(f"sha256:{digest}") == "a" * 64
    assert edit_transactions.parse_expected_sha256(f"  sha256:{digest.lower()}  ") == "a" * 64

    for bad in ("deadbeef", "a" * 63, "a" * 65, "sha256:", "md5:" + "a" * 32, "../etc/passwd", 123, ["a" * 64]):
        with pytest.raises(edit_transactions.EditVersionError):
            edit_transactions.parse_expected_sha256(bad)


def test_edit_ledger_records_ordered_private_transactions(tmp_path: Path) -> None:
    ledger = edit_transactions.EditTransactionLedger(tmp_path / "store")
    first = ledger.record(
        session_id="sess-a",
        rel_path="module.py",
        tool="write_file",
        pre_sha256=None,
        post_sha256=_sha("v1\n"),
        bytes_written=3,
        mode=0o644,
    )
    second = ledger.record(
        session_id="sess-a",
        rel_path="module.py",
        tool="edit_file",
        pre_sha256=_sha("v1\n"),
        post_sha256=_sha("v2\n"),
        expected_sha256=_sha("v1\n"),
        observed_sha256=_sha("v1\n"),
        bytes_written=3,
        task_id="task-1",
    )

    assert (first.seq, second.seq) == (1, 2)
    assert first.pre_existed is False and second.pre_existed is True
    assert second.expected_sha256 == second.observed_sha256 == _sha("v1\n")
    assert first.edit_id != second.edit_id

    history = ledger.list_transactions("sess-a")
    assert [row.seq for row in history] == [2, 1]
    assert ledger.latest_for_path("sess-a", "module.py").edit_id == second.edit_id  # type: ignore[union-attr]

    path = ledger.ledger_path("sess-a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["schema_version"] == edit_transactions.EDIT_LEDGER_VERSION
    assert record["tool"] == "write_file"


def test_edit_ledger_is_durable_across_reload_and_reports_torn_lines(tmp_path: Path) -> None:
    root = tmp_path / "store"
    edit_transactions.EditTransactionLedger(root).record(
        session_id="sess-b",
        rel_path="a.py",
        tool="write_file",
        post_sha256=_sha("a\n"),
    )
    # A torn append (crash mid-write) is reported, never repaired into a fake record.
    with (root / "edits" / "sess-b.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"seq": 2, "session_id": "sess-b", "rel_pa')

    reopened = edit_transactions.EditTransactionLedger(root)
    assert [row.seq for row in reopened.list_transactions("sess-b")] == [1]
    assert reopened.count_unreadable_lines("sess-b") == 1
    # The next append continues the sequence instead of reusing it.
    assert reopened.record(session_id="sess-b", rel_path="a.py", tool="write_file", post_sha256=_sha("b\n")).seq == 2


def test_edit_ledger_refuses_unsafe_session_and_path_values(tmp_path: Path) -> None:
    ledger = edit_transactions.EditTransactionLedger(tmp_path / "store")
    for bad_session in ("", "../escape", "a/b", "x" * 200, ".."):
        with pytest.raises(edit_transactions.EditVersionError):
            ledger.record(session_id=bad_session, rel_path="a.py", tool="write_file", post_sha256=_sha("a\n"))
    for bad_path in ("", "/etc/passwd", "../outside.py", "a/../../b.py", "C:\\Windows\\system32"):
        with pytest.raises(edit_transactions.EditVersionError):
            ledger.record(session_id="sess-c", rel_path=bad_path, tool="write_file", post_sha256=_sha("a\n"))
    with pytest.raises(edit_transactions.EditVersionError):
        ledger.record(session_id="sess-c", rel_path="a.py", tool="write_file", post_sha256="not-a-digest")


def test_observed_version_index_is_durable_bounded_and_session_scoped(tmp_path: Path) -> None:
    index = edit_transactions.ObservedVersionIndex(tmp_path / "store", max_entries=3)
    index.mark_observed("sess-a", "a.py", _sha("a\n"))
    index.mark_observed("sess-b", "a.py", _sha("b\n"))
    assert index.expected_for("sess-a", "a.py") == _sha("a\n")
    assert index.expected_for("sess-b", "a.py") == _sha("b\n")
    assert index.expected_for("sess-c", "a.py") is None

    for name in ("b.py", "c.py", "d.py"):
        index.mark_observed("sess-a", name, _sha(name))
    entries = index.entries("sess-a")
    assert len(entries) == 3
    assert "a.py" not in entries  # oldest evicted, never silently wrong

    index.forget("sess-a", "b.py")
    assert index.expected_for("sess-a", "b.py") is None
    # Durable: a fresh instance (new process) reads the same index.
    assert edit_transactions.ObservedVersionIndex(tmp_path / "store").expected_for("sess-a", "c.py") == _sha("c.py")


# ---------------------------------------------------------------------------
# 2. Compare-and-swap write primitive
# ---------------------------------------------------------------------------


def test_atomic_write_confined_bytes_enforces_the_expected_digest(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "app.py"
    target.write_text("v1\n", encoding="utf-8")

    written = workspace_snapshots.atomic_write_confined_bytes(root, "app.py", b"v2\n", expected_sha256=_sha("v1\n"))
    assert written == _sha("v2\n")
    assert target.read_text(encoding="utf-8") == "v2\n"

    # A stale expectation refuses the write and leaves both the file and the tree clean.
    with pytest.raises(workspace_snapshots.SnapshotConflictError) as excinfo:
        workspace_snapshots.atomic_write_confined_bytes(root, "app.py", b"clobber\n", expected_sha256=_sha("v1\n"))
    assert "expected content" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == "v2\n"
    assert sorted(p.name for p in root.iterdir()) == ["app.py"]


def test_atomic_write_confined_bytes_conflicts_on_missing_target_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    with pytest.raises(workspace_snapshots.SnapshotConflictError):
        workspace_snapshots.atomic_write_confined_bytes(root, "missing.txt", b"x\n", expected_sha256=_sha("x\n"))
    assert not (root / "missing.txt").exists()

    # A symlink leaf is refused outright: a managed edit may not be redirected.
    with pytest.raises(workspace_snapshots.SnapshotConflictError):
        workspace_snapshots.atomic_write_confined_bytes(root, "link.txt", b"x\n")
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert (root / "link.txt").is_symlink()


# ---------------------------------------------------------------------------
# 3. Tool-level version awareness (AC1 + AC2)
# ---------------------------------------------------------------------------


def test_write_file_enforces_the_expected_version_and_records_the_transaction(
    isolated_workspace: Path,
) -> None:
    target = isolated_workspace / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")

    stale = _run(
        isolated_workspace,
        "write_file",
        {"path": "module.py", "content": "value = 2\n", "expected_sha256": _sha("value = 0\n")},
    )
    assert stale["ok"] is False
    assert stale["error_code"] == "stale_version"
    assert stale["expected_sha256"] == _sha("value = 0\n")
    assert stale["current_sha256"] == _sha("value = 1\n")
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert studio._edit_ledger().list_transactions("sess-p2-02") == []

    applied = _run(
        isolated_workspace,
        "write_file",
        {"path": "module.py", "content": "value = 2\n", "expected_sha256": _sha("value = 1\n")},
    )
    assert applied["ok"] is True
    assert applied["sha256"] == _sha("value = 2\n")
    assert applied["previous_sha256"] == _sha("value = 1\n")
    transaction = applied["edit_transaction"]
    assert transaction["pre_sha256"] == _sha("value = 1\n")
    assert transaction["post_sha256"] == _sha("value = 2\n")
    assert transaction["expected_sha256"] == _sha("value = 1\n")
    assert transaction["tool"] == "write_file"
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    recorded = studio._edit_ledger().latest_for_path("sess-p2-02", "module.py")
    assert recorded is not None and recorded.post_sha256 == _sha("value = 2\n")
    # The written bytes are now the version the agent holds, so the next edit needs no re-read.
    assert studio._observed_versions().expected_for("sess-p2-02", "module.py") == _sha("value = 2\n")


def test_write_file_rejects_a_malformed_expected_hash_without_writing(isolated_workspace: Path) -> None:
    target = isolated_workspace / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    result = _run(
        isolated_workspace,
        "write_file",
        {"path": "module.py", "content": "clobber\n", "expected_sha256": "deadbeef"},
    )
    assert result["ok"] is False
    assert result["error_code"] == "invalid_expected_sha256"
    assert "64 hexadecimal" in result["output"]
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_read_file_reports_the_version_and_a_concurrent_user_edit_conflicts(isolated_workspace: Path) -> None:
    target = isolated_workspace / "module.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    read = _run(isolated_workspace, "read_file", {"path": "module.py"})
    assert read["ok"] is True
    assert read["sha256"] == _sha(target.read_text(encoding="utf-8"))
    assert f"sha256: {read['sha256']}" in read["output"]

    # The user saves the file while the agent is reasoning about the version it read.
    user_bytes = "def add(a, b):\n    return a + b + 42  # user change\n"
    target.write_text(user_bytes, encoding="utf-8")

    conflicted = _run(
        isolated_workspace,
        "edit_file",
        {"path": "module.py", "old_text": "return a + b", "new_text": "return a + b + 1"},
    )
    assert conflicted["ok"] is False
    assert conflicted["error_code"] == "stale_version"
    assert conflicted["conflict_source"] == "observed_read"
    assert conflicted["current_sha256"] == _sha(user_bytes)
    assert "read_file" in conflicted["output"]
    assert target.read_text(encoding="utf-8") == user_bytes  # the user's edit survives
    assert studio._edit_ledger().list_transactions("sess-p2-02") == []

    # Re-reading is the documented recovery: the agent's next edit is accepted.
    assert _run(isolated_workspace, "read_file", {"path": "module.py"})["ok"] is True
    applied = _run(
        isolated_workspace,
        "edit_file",
        {"path": "module.py", "old_text": "return a + b + 42", "new_text": "return a + b + 43"},
    )
    assert applied["ok"] is True
    assert "return a + b + 43" in target.read_text(encoding="utf-8")


def test_user_edit_after_our_last_write_conflicts_even_without_a_re_read(isolated_workspace: Path) -> None:
    target = isolated_workspace / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    assert _run(isolated_workspace, "write_file", {"path": "module.py", "content": "value = 2\n"})["ok"] is True

    user_bytes = "value = 99  # user edit after the agent wrote\n"
    target.write_text(user_bytes, encoding="utf-8")

    conflicted = _run(isolated_workspace, "write_file", {"path": "module.py", "content": "value = 3\n"})
    assert conflicted["ok"] is False
    assert conflicted["error_code"] == "stale_version"
    # Our own write is the version the agent holds, so that expectation is what fails.
    assert conflicted["conflict_source"] == "observed_read"
    assert target.read_text(encoding="utf-8") == user_bytes

    # Without any observed version the durable edit history still protects the user:
    # the last bytes this runtime wrote are the baseline for the next managed edit.
    studio._observed_versions().clear("sess-p2-02")
    across_restart = _run(isolated_workspace, "write_file", {"path": "module.py", "content": "value = 4\n"})
    assert across_restart["ok"] is False
    assert across_restart["conflict_source"] == "last_write"
    assert across_restart["expected_sha256"] == _sha("value = 2\n")
    assert target.read_text(encoding="utf-8") == user_bytes


def test_edit_race_between_the_version_check_and_the_replace_still_conflicts(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compare-and-swap guard, not just the up-front check, has to hold."""
    target = isolated_workspace / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    assert _run(isolated_workspace, "read_file", {"path": "module.py"})["ok"] is True

    real_read = studio._read_workspace_text

    def read_then_user_saves(root: Path, path: Path, **kwargs: Any) -> tuple[str, os.stat_result]:
        text, info = real_read(root, path, **kwargs)
        # A user save lands after we read but before the atomic replace.
        (isolated_workspace / "module.py").write_text("value = 77  # saved mid-edit\n", encoding="utf-8")
        return text, info

    monkeypatch.setattr(studio, "_read_workspace_text", read_then_user_saves)

    result = _run(
        isolated_workspace,
        "edit_file",
        {"path": "module.py", "old_text": "value = 1", "new_text": "value = 2"},
    )
    assert result["ok"] is False
    assert result["error_code"] == "stale_version"
    assert result["conflict_source"] == "edit_race"
    assert result["current_sha256"] == _sha("value = 77  # saved mid-edit\n")
    assert target.read_text(encoding="utf-8") == "value = 77  # saved mid-edit\n"
    assert studio._edit_ledger().list_transactions("sess-p2-02") == []
    assert [p.name for p in isolated_workspace.iterdir() if p.name.startswith(".m5-")] == []


def test_first_write_of_an_unread_path_is_still_allowed(isolated_workspace: Path) -> None:
    created = _run(isolated_workspace, "write_file", {"path": "new_module.py", "content": "created = True\n"})
    assert created["ok"] is True
    assert created["previous_sha256"] is None
    assert created["edit_transaction"]["pre_existed"] is False
    assert (isolated_workspace / "new_module.py").read_text(encoding="utf-8") == "created = True\n"


# ---------------------------------------------------------------------------
# 4. Unauthorized paths (AC3)
# ---------------------------------------------------------------------------


def test_managed_edits_reject_escaping_absolute_traversal_and_symlink_paths(
    isolated_workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (isolated_workspace / "escape_link").symlink_to(outside)

    attempts = [
        {"path": str(outside), "content": "clobbered\n"},
        {"path": "../outside.txt", "content": "clobbered\n"},
        {"path": "escape_link", "content": "clobbered\n"},
        {"path": "/etc/passwd", "content": "clobbered\n"},
    ]
    for args in attempts:
        for tool in ("write_file", "edit_file"):
            payload = dict(args)
            if tool == "edit_file":
                payload.update({"old_text": "outside", "new_text": "clobbered"})
            result = _run(isolated_workspace, tool, payload)
            assert result["ok"] is False, (tool, args)
            assert result["error_code"] in {"unauthorized_path", "unsafe_path"}, (tool, args, result)
            assert "Refused" in result["output"] or "escape" in result["output"], (tool, args, result)

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert (isolated_workspace / "escape_link").is_symlink()
    assert studio._edit_ledger().list_transactions("sess-p2-02", limit=50) == []


def test_symlink_inside_the_workspace_is_still_refused(isolated_workspace: Path) -> None:
    """A link that stays inside the workspace is refused too: we edit files, never link targets."""
    real = isolated_workspace / "real.py"
    real.write_text("value = 1\n", encoding="utf-8")
    (isolated_workspace / "alias.py").symlink_to(real)

    result = _run(isolated_workspace, "write_file", {"path": "alias.py", "content": "clobbered\n"})
    assert result["ok"] is False
    assert result["error_code"] in {"unsafe_path", "unauthorized_path"}
    assert real.read_text(encoding="utf-8") == "value = 1\n"
    assert (isolated_workspace / "alias.py").is_symlink()


def test_managed_edits_still_refuse_secret_paths(isolated_workspace: Path) -> None:
    (isolated_workspace / ".env").write_text("API_TOKEN=do-not-touch\n", encoding="utf-8")
    result = _run(isolated_workspace, "write_file", {"path": ".env", "content": "API_TOKEN=clobbered\n"})
    assert result["ok"] is False
    assert result["error_code"] == "unauthorized_path"
    assert (isolated_workspace / ".env").read_text(encoding="utf-8") == "API_TOKEN=do-not-touch\n"


# ---------------------------------------------------------------------------
# 5. Durability and the standalone bundle
# ---------------------------------------------------------------------------


def test_edit_history_and_observed_versions_survive_a_restart(isolated_workspace: Path) -> None:
    assert _run(isolated_workspace, "write_file", {"path": "module.py", "content": "value = 1\n"})["ok"] is True

    # Fresh store objects simulate a new process: nothing is cached in memory.
    ledger = edit_transactions.EditTransactionLedger(studio.HARNESS_STORE_DIR)
    observed = edit_transactions.ObservedVersionIndex(studio.HARNESS_STORE_DIR)
    latest = ledger.latest_for_path("sess-p2-02", "module.py")
    assert latest is not None and latest.post_sha256 == _sha("value = 1\n")
    assert observed.expected_for("sess-p2-02", "module.py") == _sha("value = 1\n")

    # Because the version survived, an unread edit of the same unchanged file applies.
    second = _run(isolated_workspace, "edit_file", {"path": "module.py", "old_text": "1", "new_text": "2"})
    assert second["ok"] is True
    assert [row.seq for row in ledger.list_transactions("sess-p2-02")] == [2, 1]


def test_edit_events_are_recorded_on_the_task_stream(isolated_workspace: Path) -> None:
    target = isolated_workspace / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    assert _run(isolated_workspace, "read_file", {"path": "module.py"})["ok"] is True
    target.write_text("value = 5\n", encoding="utf-8")
    assert _run(isolated_workspace, "write_file", {"path": "module.py", "content": "value = 6\n"})["ok"] is False
    assert _run(
        isolated_workspace,
        "write_file",
        {"path": "module.py", "content": "value = 6\n", "expected_sha256": _sha("value = 5\n")},
    )["ok"] is True

    events = studio._read_harness_events("sess-p2-02")
    types = [event.get("type") for event in events]
    assert "edit/conflict" in types
    assert "edit/applied" in types
    applied = next(event for event in events if event.get("type") == "edit/applied")
    assert applied["post_sha256"] == _sha("value = 6\n")
    assert applied["path"] == "module.py"
    assert applied["edit_id"].startswith("ed-")


def test_standalone_bundle_inlines_the_edit_transaction_ledger() -> None:
    root = Path(__file__).resolve().parents[1]
    bundle = root / "studio.py"
    if not bundle.exists():
        pytest.skip("studio.py has not been built in this checkout")
    spec = importlib.util.spec_from_file_location("m5_studio_standalone_p2_02", bundle)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    assert hasattr(module, "EditTransactionLedger")
    assert hasattr(module, "ObservedVersionIndex")
    assert hasattr(module, "parse_expected_sha256")
