"""M5-P2-01 tests: pre-edit Git baseline and selective user-work snapshots.

Acceptance criteria covered:

1. *No user changes silently lost* -- staged, unstaged, untracked, and deleted
   work is captured into content-addressed storage before a managed edit, a
   re-edit after the user changes the file captures the new bytes, and an edit
   whose user work cannot be snapshotted is refused with the file untouched.
2. *Restore baseline and snapshot verified* -- `verify()` reconstructs every
   captured path into a scratch directory and re-hashes it, tampered manifests
   and missing blobs are detected, and the HTTP verify endpoint reports it.
3. *No destructive reset/clean* -- the Git runner refuses `reset`, `clean`,
   `checkout`, `stash`, `restore`, `rm`, and friends, capture uses only read-only
   plumbing, and the worktree/index are identical before and after a capture.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from fastapi.testclient import TestClient
import pytest

import app as studio
import runtime_persistence as rp
import workspace_baseline as wb

TOKEN = "t" * 40
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=False)


def _init_repo(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    assert _git(ws, "init", "-q").returncode == 0
    _git(ws, "config", "user.email", "baseline@example.com")
    _git(ws, "config", "user.name", "Baseline Test")
    (ws / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    (ws / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "docs").mkdir()
    (ws / "docs" / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "initial")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    _init_repo(ws)
    return ws


@pytest.fixture()
def dirty_repo(repo: Path) -> Path:
    """A repo carrying every kind of uncommitted user work at once."""
    # Commit the module that will be deleted first, so no later staging is swept in.
    (repo / "old_module.py").write_text("LEGACY = True\n", encoding="utf-8")
    _git(repo, "add", "old_module.py")
    _git(repo, "commit", "-q", "-m", "add legacy module")
    (repo / "old_module.py").unlink()

    (repo / "app.py").write_text("def add(a, b):\n    return a + b + 1  # user edit\n", encoding="utf-8")
    (repo / "staged.py").write_text("staged user work\n", encoding="utf-8")
    _git(repo, "add", "staged.py")
    (repo / "scratch_notes.md").write_text("untracked user notes\n", encoding="utf-8")
    (repo / ".env").write_text("API_TOKEN=not-a-real-secret\n", encoding="utf-8")
    (repo / "debug.log").write_text("ignored log line\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "artifact.bin").write_text("ignored artifact\n", encoding="utf-8")
    return repo


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "m5_p2_01.sqlite3"
    workspace = tmp_path / "studio-workspace"
    workspace.mkdir(exist_ok=True)
    harness_store = tmp_path / "harness_store"
    harness_store.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_store)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", {"http://testserver", "https://testserver"})
    monkeypatch.delenv("STUDIO_BASELINE_DIR", raising=False)
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._PENDING_APPROVALS.clear()
    studio.init_db()
    studio._set_default_projects_dir(str(workspace), create_if_missing=True)
    return workspace


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _new_session(workspace: Path, title: str = "P2-01 task") -> dict[str, Any]:
    return studio.create_harness_session_db(
        title=title,
        workspace_dir=str(workspace),
        use_worktree=False,
        policy="standard",
        lead_model="local-model",
        critic_model="critic-model",
    )


def _running_session(workspace: Path, task_id: str) -> dict[str, Any]:
    """Create a session plus a `running` root task so managed edits are admitted."""
    session = _new_session(workspace)
    rp.RuntimePersistenceStore(studio.STORAGE, studio.DB_PATH).create_task(
        task_id, policy="standard", state="running", now=100.0
    )
    return {"id": session["id"], "task_id": task_id, "worktree_dir": str(workspace), "policy": "standard"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. Capture: staged, unstaged, untracked, deleted, ignored, sensitive
# ---------------------------------------------------------------------------


def test_capture_preserves_staged_unstaged_untracked_and_deleted_work(dirty_repo: Path, tmp_path: Path) -> None:
    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    record = store.capture(dirty_repo, session_id="s-capture", reason="pre_edit")

    assert record.git_repo is True
    assert len(record.head_commit) == 40
    assert record.staged_entries >= 2

    by_path = {f.path: f for f in record.files}
    # Unstaged user edit, staged addition, untracked note, and the deletion.
    assert by_path["app.py"].change == "modified"
    assert by_path["app.py"].digest == _sha256_text((dirty_repo / "app.py").read_text(encoding="utf-8"))
    assert by_path["staged.py"].change.startswith("staged")
    assert by_path["staged.py"].captured is True
    assert by_path["scratch_notes.md"].change == "untracked"
    assert by_path["scratch_notes.md"].captured is True
    # A deletion keeps the committed bytes, from the index or HEAD object.
    assert by_path["old_module.py"].change == "deleted"
    assert by_path["old_module.py"].captured is True
    assert by_path["old_module.py"].blob_source in {"index", "head"}

    verification = store.verify(dirty_repo, record.baseline_id, scratch_dir=tmp_path / "restore")
    assert verification.ok is True
    assert set(verification.verified_paths) == {"app.py", "staged.py", "scratch_notes.md", "old_module.py"}
    restored = tmp_path / "restore"
    # Byte-exact reconstruction of the pre-edit user work, including the deletion.
    assert (restored / "app.py").read_text(encoding="utf-8") == (dirty_repo / "app.py").read_text(encoding="utf-8")
    assert (restored / "staged.py").read_text(encoding="utf-8") == "staged user work\n"
    assert (restored / "scratch_notes.md").read_text(encoding="utf-8") == "untracked user notes\n"
    assert (restored / "old_module.py").read_text(encoding="utf-8") == "LEGACY = True\n"


def test_capture_never_copies_ignored_or_secret_files_unless_explicitly_protected(
    dirty_repo: Path, tmp_path: Path
) -> None:
    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    record = store.capture(dirty_repo, session_id="s-secrets")

    paths = {f.path for f in record.files}
    # Ignored files are not enumerated or copied in bulk.
    assert "debug.log" not in paths
    assert "build/artifact.bin" not in paths
    # A secret-named untracked file is recorded as metadata only, never copied.
    env_entry = next(f for f in record.files if f.path == ".env")
    assert env_entry.captured is False
    assert env_entry.sensitive is True
    assert env_entry.capture_reason == "sensitive_not_copied"
    assert env_entry.digest == _sha256_text("API_TOKEN=not-a-real-secret\n")
    assert list((tmp_path / "baselines").rglob(".env")) == []
    assert env_entry.path in store.verify(dirty_repo, record.baseline_id).unrestorable_paths

    # An operator can opt one specific ignored path in; only that path is copied.
    protected = store.capture(dirty_repo, session_id="s-secrets", protected_ignored=["debug.log"])
    debug_entry = next(f for f in protected.files if f.path == "debug.log")
    assert debug_entry.captured is True
    assert store.read_blob(dirty_repo, str(debug_entry.digest)) == b"ignored log line\n"
    assert "build/artifact.bin" not in {f.path for f in protected.files}


def test_focus_paths_are_captured_even_when_untracked_or_outside_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.txt").write_text("user draft\n", encoding="utf-8")
    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")

    record = store.capture(plain, session_id="s-focus", focus_paths=["notes.txt"])
    assert record.git_repo is False
    entry = record.file("notes.txt")
    assert entry is not None and entry.captured is True and entry.digest == _sha256_text("user draft\n")
    verification = store.verify(plain, record.baseline_id, scratch_dir=tmp_path / "restore-plain")
    assert verification.ok is True
    assert (tmp_path / "restore-plain" / "notes.txt").read_text(encoding="utf-8") == "user draft\n"


def test_capture_refuses_escaping_focus_paths_and_does_not_follow_symlinks(repo: Path, tmp_path: Path) -> None:
    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    for bad in ("../outside.txt", "/etc/passwd", "nested/../../escape.txt"):
        with pytest.raises(wb.BaselineUnsafePathError):
            store.capture(repo, session_id="s-escape", focus_paths=[bad])

    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not leak\n", encoding="utf-8")
    os.symlink(outside, repo / "linked.txt")
    record = store.capture(repo, session_id="s-link", focus_paths=["linked.txt"])
    entry = record.file("linked.txt")
    assert entry is not None
    assert entry.captured is False
    assert entry.capture_reason == "symlink_not_followed"
    assert "do not leak" not in json.dumps(record.to_dict())
    assert list((tmp_path / "baselines").rglob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 2. Verification, tampering, and storage exhaustion
# ---------------------------------------------------------------------------


def test_verify_detects_tampered_manifest_and_missing_blob(dirty_repo: Path, tmp_path: Path) -> None:
    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    record = store.capture(dirty_repo, session_id="s-tamper")
    assert store.verify(dirty_repo, record.baseline_id).ok is True

    manifest_path = store.baseline_dir(dirty_repo, record.baseline_id) / "baseline.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["files"][0]["digest"] = "0" * 64
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(wb.BaselineCorruptError):
        store.load(dirty_repo, record.baseline_id)

    # Rewriting a blob so it no longer matches its name is also detected.
    fresh = store.capture(dirty_repo, session_id="s-tamper")
    target = next(f for f in fresh.files if f.restorable and f.digest)
    blob_path = store._blob_path(dirty_repo, str(target.digest))
    blob_path.write_bytes(b"substituted bytes\n")
    verification = store.verify(dirty_repo, fresh.baseline_id)
    assert verification.ok is False
    assert target.path in verification.digest_mismatches


def test_latest_is_the_newest_capture_not_the_highest_baseline_id(dirty_repo: Path, tmp_path: Path) -> None:
    """Baseline ids are random, so ordering must come from `captured_at`."""
    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    records = [store.capture(dirty_repo, session_id="s-order") for _ in range(3)]

    listed = store.list_baselines(dirty_repo, limit=3)
    assert [r.baseline_id for r in listed] == [r.baseline_id for r in reversed(records)]
    assert store.latest(dirty_repo, session_id="s-order").baseline_id == records[-1].baseline_id  # type: ignore[union-attr]
    assert [r.captured_at for r in listed] == sorted((r.captured_at for r in listed), reverse=True)
    # Sub-second captures must remain distinguishable: the whole point of the fix.
    assert len({r.captured_at for r in records}) == len(records)


def test_storage_exhaustion_is_reported_rather_than_half_captured(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    # Pre-flight headroom check: refuse before writing anything.
    monkeypatch.setattr(store, "_free_bytes", lambda: 1024)
    with pytest.raises(wb.BaselineStorageExhausted) as excinfo:
        store.capture(dirty_repo, session_id="s-full")
    assert "free disk space" in str(excinfo.value).lower()
    assert list((tmp_path / "baselines").rglob("baseline.json")) == []

    # A mid-write ENOSPC is mapped to the same fail-closed error.
    monkeypatch.undo()
    store2 = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    real_write = store2._atomic_write

    def _enospc(path: Path, data: bytes, *, mode: int = 0o600) -> None:
        if path.name == "baseline.json":
            raise wb.BaselineStorageExhausted("No space left while writing the baseline store.")
        real_write(path, data, mode=mode)

    monkeypatch.setattr(store2, "_atomic_write", _enospc)
    with pytest.raises(wb.BaselineStorageExhausted):
        store2.capture(dirty_repo, session_id="s-full-2")

    # The errno mapping itself is covered directly too.
    with pytest.raises(wb.BaselineStorageExhausted):
        wb.WorkspaceBaselineStore._raise_for_storage_error(OSError(errno.ENOSPC, "No space left"), "baseline store")
    with pytest.raises(wb.BaselineError):
        wb.WorkspaceBaselineStore._raise_for_storage_error(OSError(errno.EACCES, "denied"), "baseline store")


# ---------------------------------------------------------------------------
# 3. No destructive Git, and the tree is untouched by capture
# ---------------------------------------------------------------------------


def test_git_runner_refuses_destructive_subcommands(dirty_repo: Path, tmp_path: Path) -> None:
    git = wb._Git(dirty_repo)
    for forbidden in sorted(wb.FORBIDDEN_GIT_SUBCOMMANDS):
        with pytest.raises(wb.GitCommandRefused):
            git.run(forbidden, "--help")
    with pytest.raises(wb.GitCommandRefused):
        git.run("some-unknown-subcommand")
    with pytest.raises(wb.GitCommandRefused):
        git.run()
    # The read-only plumbing it does use still works.
    code, out = git.run("rev-parse", "--is-inside-work-tree")
    assert code == 0 and out.strip() == b"true"


def test_capture_and_verify_run_only_read_only_plumbing_and_leave_the_tree_identical(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    real_run = subprocess.run

    def _spy(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]).endswith("git"):
            seen.append([str(a) for a in argv[1:]])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(wb.subprocess, "run", _spy)
    status_before = _git(dirty_repo, "status", "--porcelain=v1").stdout
    index_before = (dirty_repo / ".git" / "index").read_bytes()

    store = wb.WorkspaceBaselineStore(tmp_path / "baselines")
    record = store.capture(dirty_repo, session_id="s-readonly", focus_paths=["app.py"])
    store.verify(dirty_repo, record.baseline_id, scratch_dir=tmp_path / "restore")

    assert seen, "expected the capture to inspect the repository through Git"
    for argv in seen:
        assert argv, "every Git invocation must name a subcommand"
        assert argv[0] in wb.ALLOWED_GIT_SUBCOMMANDS, f"{argv[0]} is not read-only plumbing"
        assert argv[0] not in wb.FORBIDDEN_GIT_SUBCOMMANDS
    assert [a for a in seen if a[0] in {"reset", "clean", "checkout", "stash", "restore", "rm"}] == []
    # Neither the worktree nor the index changed.
    assert _git(dirty_repo, "status", "--porcelain=v1").stdout == status_before
    assert (dirty_repo / ".git" / "index").read_bytes() == index_before
    assert (dirty_repo / "old_module.py").exists() is False  # the user's deletion is preserved


# ---------------------------------------------------------------------------
# 4. Managed edits: automatic capture, refresh, and fail-closed denials
# ---------------------------------------------------------------------------


def test_managed_edit_captures_baseline_and_refreshes_after_a_user_change(
    repo: Path, isolated_env: Path, tmp_path: Path
) -> None:
    session = _running_session(repo, "task-baseline-edit")
    store = studio._baseline_store(repo)

    first_edit = asyncio.run(
        studio._execute_harness_tool(
            None, session, "edit_file",
            {"path": "app.py", "old_text": "return a + b", "new_text": "return a + b"},
        )
    )
    assert first_edit["ok"] is True
    first = store.latest(repo, session_id=str(session["id"]))
    assert first is not None
    captured_digest = first.file("app.py").digest  # type: ignore[union-attr]
    assert captured_digest == _sha256_text((repo / "app.py").read_text(encoding="utf-8"))

    # The user edits the file again; the next managed edit must capture *that*.
    user_text = "def add(a, b):\n    return a + b + 42  # user changed again\n"
    (repo / "app.py").write_text(user_text, encoding="utf-8")
    second_edit = asyncio.run(
        studio._execute_harness_tool(
            None, session, "write_file", {"path": "app.py", "content": "def add(a, b):\n    return a + b\n"}
        )
    )
    assert second_edit["ok"] is True
    latest = store.latest(repo, session_id=str(session["id"]))
    assert latest is not None and latest.baseline_id != first.baseline_id
    refreshed = latest.file("app.py")
    assert refreshed is not None and refreshed.digest == _sha256_text(user_text)

    # With the newest baseline in hand, the user's intermediate work is restorable.
    verification = store.verify(repo, latest.baseline_id, scratch_dir=tmp_path / "restore")
    assert verification.ok is True
    assert (tmp_path / "restore" / "app.py").read_text(encoding="utf-8") == user_text

    # A third edit of an unchanged file does not churn a new baseline.
    third_edit = asyncio.run(
        studio._execute_harness_tool(
            None, session, "edit_file",
            {"path": "app.py", "old_text": "return a + b", "new_text": "return a + b"},
        )
    )
    assert third_edit["ok"] is True
    assert store.latest(repo, session_id=str(session["id"])).baseline_id == latest.baseline_id  # type: ignore[union-attr]


def test_managed_edit_is_refused_when_user_work_cannot_be_snapshotted(
    repo: Path, isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_session(repo, "task-too-large")
    (repo / "big.txt").write_text("x" * 4096, encoding="utf-8")
    original = studio._baseline_store

    def _capped_store(workspace_root: Path) -> wb.WorkspaceBaselineStore:
        store = original(workspace_root)
        store.max_file_bytes = 1024
        return store

    monkeypatch.setattr(studio, "_baseline_store", _capped_store)
    before = (repo / "big.txt").read_text(encoding="utf-8")
    denied = asyncio.run(
        studio._execute_harness_tool(None, session, "write_file", {"path": "big.txt", "content": "clobbered\n"})
    )
    assert denied["ok"] is False
    assert denied.get("baseline_blocked") is True
    assert "not snapshotted" in denied["output"]
    assert (repo / "big.txt").read_text(encoding="utf-8") == before  # user work untouched
    events = [e for e in studio._read_harness_events(str(session["id"])) if e.get("type") == "baseline/path_unrestorable"]
    assert events and events[0]["path"] == "big.txt"

    # A brand-new file has no user work to lose, so the same limit does not block it.
    created = asyncio.run(
        studio._execute_harness_tool(None, session, "write_file", {"path": "new_file.py", "content": "print(1)\n"})
    )
    assert created["ok"] is True


def test_disk_full_during_capture_fails_closed_and_pauses_the_task(
    repo: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_session(repo, "task-disk-full")
    before = (repo / "app.py").read_text(encoding="utf-8")

    class _FullStore(wb.WorkspaceBaselineStore):
        def latest(self, workspace: Path, *, session_id: str | None = None):  # type: ignore[override]
            return None

        def capture(self, workspace: Path, **kwargs: Any):  # type: ignore[override]
            raise wb.BaselineStorageExhausted("No space left while writing the baseline store.")

    paused: list[str] = []
    monkeypatch.setattr(studio, "_baseline_store", lambda _root: _FullStore(repo.parent / "baselines"))
    monkeypatch.setattr(studio, "_pause_for_baseline_failure", lambda sid, reason: paused.append(reason))

    denied = asyncio.run(
        studio._execute_harness_tool(None, session, "write_file", {"path": "app.py", "content": "clobbered\n"})
    )
    assert denied["ok"] is False
    assert denied.get("baseline_blocked") is True
    assert "Free disk space" in denied["output"]
    assert (repo / "app.py").read_text(encoding="utf-8") == before
    assert paused == ["baseline_storage_exhausted"]
    events = [e for e in studio._read_harness_events(str(session["id"])) if e.get("type") == "baseline/storage_exhausted"]
    assert len(events) == 1 and events[0]["path"] == "app.py"


# ---------------------------------------------------------------------------
# 5. HTTP surface
# ---------------------------------------------------------------------------


def test_baseline_http_endpoints_capture_list_and_verify(repo: Path, isolated_env: Path, tmp_path: Path) -> None:
    session = _new_session(repo)
    sid = str(session["id"])
    (repo / "app.py").write_text("def add(a, b):\n    return a + b + 5\n", encoding="utf-8")
    (repo / "scratch.md").write_text("untracked\n", encoding="utf-8")

    with TestClient(studio.app) as client:
        captured = client.post(f"/api/harness/sessions/{sid}/baseline", headers=_headers(), json={"reason": "pre_edit"})
        assert captured.status_code == 200
        body = captured.json()
        assert body["status"] == "captured"
        assert body["verification"]["ok"] is True
        baseline_id = body["baseline"]["baseline_id"]
        assert "scratch.md" in body["baseline"]["restorable_paths"]
        assert body["baseline"]["head_commit"]

        listed = client.get(f"/api/harness/sessions/{sid}/baseline", headers=_headers())
        assert listed.status_code == 200
        assert listed.json()["latest"]["baseline_id"] == baseline_id
        assert listed.json()["baselines"]

        verified = client.post(f"/api/harness/sessions/{sid}/baseline/{baseline_id}/verify", headers=_headers())
        assert verified.status_code == 200
        assert verified.json()["ok"] is True
        assert verified.json()["verification"]["restored_paths"]

        unauthenticated = client.get(f"/api/harness/sessions/{sid}/baseline")
        assert unauthenticated.status_code == 401
        missing = client.post(
            f"/api/harness/sessions/{sid}/baseline/bl-000000000000/verify", headers=_headers()
        )
        assert missing.status_code == 404
        unknown_session = client.get("/api/harness/sessions/hns-missing/baseline", headers=_headers())
        assert unknown_session.status_code == 404


def test_baseline_http_endpoints_reject_a_tampered_manifest(repo: Path, isolated_env: Path) -> None:
    session = _new_session(repo)
    sid = str(session["id"])
    with TestClient(studio.app) as client:
        baseline_id = client.post(
            f"/api/harness/sessions/{sid}/baseline", headers=_headers(), json={}
        ).json()["baseline"]["baseline_id"]
        store = studio._baseline_store(repo)
        manifest = store.baseline_dir(repo, baseline_id) / "baseline.json"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["total_bytes"] = raw.get("total_bytes", 0) + 4096
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        tampered = client.post(f"/api/harness/sessions/{sid}/baseline/{baseline_id}/verify", headers=_headers())
        assert tampered.status_code == 409
        assert "manifest" in tampered.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 6. Standalone artifact
# ---------------------------------------------------------------------------


def test_standalone_bundle_inlines_the_baseline_store() -> None:
    studio_source = (ROOT / "studio.py").read_text(encoding="utf-8")
    assert "class WorkspaceBaselineStore" in studio_source
    assert "BaselineStorageExhausted" in studio_source
    assert "workspace_baseline = sys.modules[__name__]" in studio_source
    assert "_ensure_pre_edit_baseline" in studio_source
    assert sys.modules.get("workspace_baseline") is not None
