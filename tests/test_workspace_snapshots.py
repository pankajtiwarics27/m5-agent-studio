"""M5-P2-01 tests: pre-edit Git baseline, selective CAS snapshots, secret preservation, disk-full safe pause."""
from __future__ import annotations

import errno
import hashlib
import importlib.util
import sys
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio
import workspace_snapshots


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@m5.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "M5 Tester"], cwd=repo, check=True)


def _commit_all(repo: Path, message: str = "initial commit") -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _configure_isolated_studio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, dict[str, str]]:
    db_path = tmp_path / "studio_p2_01.db"
    harness_dir = tmp_path / ".harnesses"
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_dir)
    studio.init_db()
    token = studio.STUDIO_API_TOKEN or "m5-test-token-0123456789abcdef0123456789"
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", token)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    return token, {"Authorization": f"Bearer {token}"}


def test_capture_git_baseline_staged_unstaged_untracked_and_restore_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 + AC2 + AC3: Staged, unstaged, deleted, and untracked user work is preserved and restored without reset/clean."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tracked_clean.py").write_text("clean_v0 = True\n", encoding="utf-8")
    (repo / "staged_and_unstaged.py").write_text("state = 'v0_head'\n", encoding="utf-8")
    (repo / "unstaged_mod.py").write_text("mod = 'v0_head'\n", encoding="utf-8")
    (repo / "unstaged_deleted.py").write_text("delete_me = 'v0_head'\n", encoding="utf-8")
    head_sha = _commit_all(repo, "seed baseline")
    head_tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()

    # 1. Stage additions & modifications in Git index
    (repo / "staged_only.py").write_text("staged_new = 1\n", encoding="utf-8")
    (repo / "staged_and_unstaged.py").write_text("state = 'v1_staged'\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged_only.py", "staged_and_unstaged.py"], cwd=repo, check=True)

    # 2. Apply unstaged working-tree edits & deletion on top of the index
    (repo / "staged_and_unstaged.py").write_text("state = 'v2_unstaged_worktree'\n", encoding="utf-8")
    (repo / "unstaged_mod.py").write_text("mod = 'v1_user_unstaged'\n", encoding="utf-8")
    (repo / "unstaged_deleted.py").unlink()

    # 3. Create relevant untracked user files (including executable mode)
    (repo / "untracked_notes.md").write_text("# User Research Notes\nkeep me\n", encoding="utf-8")
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    exec_script = scripts_dir / "run_check.sh"
    exec_script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    exec_script.chmod(0o755)

    store = workspace_snapshots.WorkspaceSnapshotStore(tmp_path / ".harnesses")
    manifest = store.capture_baseline("hns-base0001", repo)

    assert manifest.git.is_git is True
    assert manifest.git.has_head is True
    assert manifest.git.head_commit == head_sha
    assert manifest.git.head_tree == head_tree
    assert manifest.git.clean_at_capture is False
    # Clean tracked file `tracked_clean.py` was NOT indiscriminately copied into CAS at baseline!
    clean_sha = hashlib.sha256(b"clean_v0 = True\n").hexdigest()
    assert not store.cas.has_blob(clean_sha)

    # 4. Simulate managed edits + lazy pre-edit capture + unrelated concurrent user scratch file
    store.capture_target_before_edit("hns-base0001", repo, "tracked_clean.py")
    assert store.cas.has_blob(clean_sha)
    w_clean = workspace_snapshots.atomic_write_confined_bytes(repo, "tracked_clean.py", b"clean_v0 = False  # agent\n")
    store.record_target_after_edit("hns-base0001", repo, "tracked_clean.py", w_clean)
    # Also pollute index by staging `tracked_clean.py` and overwriting `staged_only.py` in index
    subprocess.run(["git", "add", "tracked_clean.py"], cwd=repo, check=True)

    store.capture_target_before_edit("hns-base0001", repo, "staged_and_unstaged.py")
    w_su = workspace_snapshots.atomic_write_confined_bytes(repo, "staged_and_unstaged.py", b"state = 'agent_clobber'\n")
    store.record_target_after_edit("hns-base0001", repo, "staged_and_unstaged.py", w_su)

    store.capture_target_before_edit("hns-base0001", repo, "unstaged_deleted.py")
    w_del = workspace_snapshots.atomic_write_confined_bytes(repo, "unstaged_deleted.py", b"recreated_by_agent = 1\n")
    store.record_target_after_edit("hns-base0001", repo, "unstaged_deleted.py", w_del)

    store.capture_target_before_edit("hns-base0001", repo, "agent_created.py")
    w_new = workspace_snapshots.atomic_write_confined_bytes(repo, "agent_created.py", b"created = True\n")
    store.record_target_after_edit("hns-base0001", repo, "agent_created.py", w_new)

    # Unrelated user scratch file created out-of-band after baseline capture (must NOT be wiped by restore!)
    (repo / "user_out_of_band_scratch.txt").write_text("precious scratch\n", encoding="utf-8")

    # 5. Spy on subprocess.run during restore to prove ZERO destructive git reset/clean/checkout calls
    executed_git_cmds: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            executed_git_cmds.append([str(x) for x in cmd])
            assert workspace_snapshots.detect_destructive_git_command(" ".join(str(x) for x in cmd)) is None
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(workspace_snapshots.subprocess, "run", spy_run)
    report = store.restore_snapshot("hns-base0001", "baseline")

    assert report.verified is True
    assert "tracked_clean.py" in report.restored_files
    assert "staged_and_unstaged.py" in report.restored_files
    assert "agent_created.py" in report.removed_created_files
    assert "unstaged_deleted.py" in report.restored_deleted_files
    assert not any(c[1:2] in (["reset"], ["clean"], ["checkout"]) for c in executed_git_cmds)

    # Verify exact working-tree contents & modes
    assert (repo / "tracked_clean.py").read_text(encoding="utf-8") == "clean_v0 = True\n"
    assert (repo / "staged_only.py").read_text(encoding="utf-8") == "staged_new = 1\n"
    assert (repo / "staged_and_unstaged.py").read_text(encoding="utf-8") == "state = 'v2_unstaged_worktree'\n"
    assert (repo / "unstaged_mod.py").read_text(encoding="utf-8") == "mod = 'v1_user_unstaged'\n"
    assert not (repo / "unstaged_deleted.py").exists()
    assert not (repo / "agent_created.py").exists()
    assert (repo / "untracked_notes.md").read_text(encoding="utf-8") == "# User Research Notes\nkeep me\n"
    assert stat.S_IMODE(exec_script.stat().st_mode) == 0o755
    assert (repo / "user_out_of_band_scratch.txt").read_text(encoding="utf-8") == "precious scratch\n"

    # Verify exact staged Git index (`staged_and_unstaged.py` has v1_staged in index, `tracked_clean.py` unstaged!)
    staged_blob = subprocess.check_output(["git", "show", ":staged_and_unstaged.py"], cwd=repo, text=True)
    assert staged_blob == "state = 'v1_staged'\n"
    cached_names = subprocess.check_output(["git", "diff", "--cached", "--name-status"], cwd=repo, text=True)
    assert "A\tstaged_only.py" in cached_names
    assert "M\tstaged_and_unstaged.py" in cached_names
    assert "tracked_clean.py" not in cached_names


def test_subdirectory_unborn_head_and_non_git_workspaces_preserved(tmp_path: Path) -> None:
    """Task 1 + AC1 + AC2: Unborn HEAD repos, subdirectory workspaces, and non-Git folders restore faithfully."""
    store = workspace_snapshots.WorkspaceSnapshotStore(tmp_path / ".harnesses")

    # Case A: Unborn HEAD Git repository with staged + untracked files
    unborn = tmp_path / "unborn_repo"
    _init_git_repo(unborn)
    (unborn / "staged_init.py").write_text("x = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged_init.py"], cwd=unborn, check=True)
    (unborn / "local_draft.txt").write_text("draft\n", encoding="utf-8")
    m_unborn = store.capture_baseline("hns-unborn01", unborn)
    assert m_unborn.git.is_git is True
    assert m_unborn.git.has_head is False
    assert len(m_unborn.staged_changes) == 1
    (unborn / "staged_init.py").write_text("x = 999\n", encoding="utf-8")
    rep_unborn = store.restore_snapshot("hns-unborn01", "baseline")
    assert rep_unborn.verified is True
    assert (unborn / "staged_init.py").read_text(encoding="utf-8") == "x = 10\n"

    # Case B: Subdirectory workspace inside larger monorepo
    mono = tmp_path / "monorepo"
    _init_git_repo(mono)
    sub_pkg = mono / "packages" / "service"
    sub_pkg.mkdir(parents=True)
    (sub_pkg / "svc.py").write_text("v = 1\n", encoding="utf-8")
    _commit_all(mono, "init monorepo")
    (sub_pkg / "svc.py").write_text("v = 2  # staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "packages/service/svc.py"], cwd=mono, check=True)
    (sub_pkg / "svc.py").write_text("v = 3  # unstaged\n", encoding="utf-8")
    m_sub = store.capture_baseline("hns-subdir01", sub_pkg)
    assert m_sub.git.workspace_rel_prefix == "packages/service"
    (sub_pkg / "svc.py").write_text("v = 999  # agent\n", encoding="utf-8")
    subprocess.run(["git", "add", "packages/service/svc.py"], cwd=mono, check=True)
    rep_sub = store.restore_snapshot("hns-subdir01", "baseline", target_root=sub_pkg)
    assert rep_sub.verified is True
    assert (sub_pkg / "svc.py").read_text(encoding="utf-8") == "v = 3  # unstaged\n"
    staged_sub = subprocess.check_output(["git", "show", ":packages/service/svc.py"], cwd=mono, text=True)
    assert staged_sub == "v = 2  # staged\n"

    # Case C: Non-Git plain directory workspace
    plain = tmp_path / "plain_dir"
    plain.mkdir()
    (plain / "notes.txt").write_text("hello plain\n", encoding="utf-8")
    (plain / ".env").write_text("PLAIN_SECRET=dont_copy_me\n", encoding="utf-8")
    m_plain = store.capture_baseline("hns-plain01", plain)
    assert m_plain.git.is_git is False
    assert [u.rel_path for u in m_plain.untracked_files] == ["notes.txt"]
    assert [p.rel_path for p in m_plain.preserved_files] == [".env"]
    (plain / "notes.txt").write_text("mutated\n", encoding="utf-8")
    rep_plain = store.restore_snapshot("hns-plain01", "baseline")
    assert rep_plain.verified is True
    assert (plain / "notes.txt").read_text(encoding="utf-8") == "hello plain\n"


def test_ignored_sensitive_files_preserved_in_place_without_cas_copying(tmp_path: Path) -> None:
    """Task 2 + Task 3 + AC1 + AC2: Sensitive/ignored files are never copied into CAS and remain intact."""
    repo = tmp_path / "sec_repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text(".env\nsecrets/\nnode_modules/\nlocal.ignored.cfg\n", encoding="utf-8")
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
    _commit_all(repo, "init")

    env_secret = "OPENAI_API_KEY=sk-live-super-secret-never-copy-887766"
    tls_secret = "-----BEGIN PRIVATE KEY-----\nunique-tls-material-554433"
    unignored_secret = "DATABASE_URL=postgres://admin:unignored-secret-221100@db/prod"

    (repo / ".env").write_text(env_secret + "\n", encoding="utf-8")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "prod.key").write_text(tls_secret + "\n", encoding="utf-8")
    (repo / ".env.local").write_text(unignored_secret + "\n", encoding="utf-8")  # NOT in .gitignore!
    (repo / ".env.example").write_text("OPENAI_API_KEY=example\n", encoding="utf-8")  # Safe template
    (repo / "local.ignored.cfg").write_text("feature_x=true\n", encoding="utf-8")
    (repo / "node_modules" / "leftpad").mkdir(parents=True)
    (repo / "node_modules" / "leftpad" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    harness_root = tmp_path / ".harnesses"
    store = workspace_snapshots.WorkspaceSnapshotStore(harness_root)
    manifest = store.capture_baseline("hns-secret01", repo)

    preserved_paths = {p.rel_path: p for p in manifest.preserved_files}
    assert ".env" in preserved_paths
    assert "secrets/prod.key" in preserved_paths
    assert ".env.local" in preserved_paths
    assert "local.ignored.cfg" in preserved_paths
    assert all(p.stored_in_cas is False for p in preserved_paths.values())

    # Safe template `.env.example` IS snapshotted into CAS; `node_modules` is skipped completely
    assert [u.rel_path for u in manifest.untracked_files] == [".env.example"]
    assert not any("node_modules" in p for p in preserved_paths)

    # Scan EVERY file under `.harnesses/` to prove zero secret plaintext leaked to CAS or manifests
    for stored_file in harness_root.rglob("*"):
        if stored_file.is_file():
            raw = stored_file.read_bytes()
            assert env_secret.encode("utf-8") not in raw
            assert tls_secret.encode("utf-8") not in raw
            assert unignored_secret.encode("utf-8") not in raw

    # Pre-edit target capture refuses sensitive paths fail-closed
    with pytest.raises(workspace_snapshots.SnapshotConflictError, match="sensitive"):
        store.capture_target_before_edit("hns-secret01", repo, ".env")

    # Mutate `main.py`, restore, and verify preserved secrets are verified in-place
    store.capture_target_before_edit("hns-secret01", repo, "main.py")
    w_sha = workspace_snapshots.atomic_write_confined_bytes(repo, "main.py", b"print('edited')\n")
    store.record_target_after_edit("hns-secret01", repo, "main.py", w_sha)

    report = store.restore_snapshot("hns-secret01", "baseline")
    assert report.verified is True
    assert set(report.preserved_verified) >= {".env", "secrets/prod.key", ".env.local", "local.ignored.cfg"}
    assert (repo / ".env").read_text(encoding="utf-8") == env_secret + "\n"
    assert (repo / "secrets" / "prod.key").read_text(encoding="utf-8") == tls_secret + "\n"


def test_cas_deduplication_permissions_and_tamper_detection_fails_closed(tmp_path: Path) -> None:
    """Task 2 + AC2: CAS deduplicates identical blobs (0600) and aborts restore before touching files if corrupt."""
    repo = tmp_path / "cas_repo"
    _init_git_repo(repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo, "init")

    shared_payload = "identical user content across two files\n"
    (repo / "dup_a.txt").write_text(shared_payload, encoding="utf-8")
    (repo / "dup_b.txt").write_text(shared_payload, encoding="utf-8")

    store = workspace_snapshots.WorkspaceSnapshotStore(tmp_path / ".harnesses")
    manifest = store.capture_baseline("hns-cas00001", repo)
    assert manifest.cas_blobs_written == 1
    digest = manifest.untracked_files[0].cas_sha256
    assert digest is not None
    blob_path = store.cas._blob_path(digest)
    assert stat.S_IMODE(blob_path.stat().st_mode) == 0o600

    # Concurrent user modification on an agent-created file is protected from unlink
    store.capture_target_before_edit("hns-cas00001", repo, "agent_file.py")
    w_sha = workspace_snapshots.atomic_write_confined_bytes(repo, "agent_file.py", b"agent_v1\n")
    store.record_target_after_edit("hns-cas00001", repo, "agent_file.py", w_sha)
    (repo / "agent_file.py").write_text("user_customized_after_agent\n", encoding="utf-8")
    with pytest.raises(workspace_snapshots.SnapshotConflictError, match="modified outside recorded agent edit"):
        store.restore_snapshot("hns-cas00001", "baseline")
    assert (repo / "agent_file.py").read_text(encoding="utf-8") == "user_customized_after_agent\n"

    # Corrupt CAS blob and verify restore aborts BEFORE mutating workspace
    (repo / "agent_file.py").write_text("agent_v1\n", encoding="utf-8")
    (repo / "dup_a.txt").write_text("changed_on_disk\n", encoding="utf-8")
    blob_path.write_bytes(b"corrupted-cas-bytes")
    with pytest.raises(workspace_snapshots.SnapshotIntegrityError, match="SHA-256 verification"):
        store.restore_snapshot("hns-cas00001", "baseline")
    assert (repo / "dup_a.txt").read_text(encoding="utf-8") == "changed_on_disk\n"


def test_destructive_git_commands_blocked_in_validator_and_shell_guard() -> None:
    """AC3: Destructive git reset/clean/checkout commands fail closed while safe invocations remain allowed."""
    blocked = [
        "git reset --hard",
        "git reset --hard HEAD~1",
        "git -C /tmp/repo reset --merge",
        "git clean -fd",
        "git clean -xfd",
        "git clean --force",
        "echo ok && git clean -f",
        "git checkout --force",
        "git checkout -f main",
        "git checkout -- .",
        "git restore .",
    ]
    for cmd in blocked:
        reason = workspace_snapshots.detect_destructive_git_command(cmd)
        assert reason is not None, f"Expected {cmd!r} to be blocked"
        assert studio._is_shell_blocked(cmd) is not None

    allowed = [
        "git status --short",
        "git diff --cached",
        "git clean -n -d",
        "git clean --dry-run -fd",
        "git checkout -b feature/safe-branch",
        "git reset HEAD staged_file.py",
        "git restore --staged staged_file.py",
    ]
    for cmd in allowed:
        assert workspace_snapshots.detect_destructive_git_command(cmd) is None, f"Expected {cmd!r} to be allowed"
        assert studio._is_shell_blocked(cmd) is None


def test_harness_turn_and_edit_handle_disk_full_with_safe_pause_and_preserve_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 4 + AC1: Disk-full during pre-edit CAS snapshot or write triggers safe pause with zero user data loss."""
    _token, headers = _configure_isolated_studio(monkeypatch, tmp_path)
    repo = tmp_path / "disk_repo"
    _init_git_repo(repo)
    (repo / "module.py").write_text("value = 'committed'\n", encoding="utf-8")
    _commit_all(repo, "init")
    # Uncommitted user work that MUST survive disk-full untouched
    user_precious = "value = 'user_uncommitted_precious_work'\n"
    (repo / "module.py").write_text(user_precious, encoding="utf-8")

    async def fake_skills(skill_ids: list[str], prompt: str) -> tuple[str, list[dict[str, Any]]]:
        return "", []

    async def fake_unload(client_obj: Any, keep_model_id: str) -> dict[str, Any]:
        return {"unloaded": []}

    monkeypatch.setattr(studio, "build_skills_context", fake_skills)
    monkeypatch.setattr(studio, "_unload_other_omlx_models", fake_unload)

    with TestClient(studio.app, base_url="http://127.0.0.1:3000") as client:
        c_resp = client.post(
            "/api/harness/sessions",
            headers=headers,
            json={"title": "Disk Full Guard", "workspace_dir": str(repo), "policy": "autonomous"},
        )
        assert c_resp.status_code == 200
        sid = c_resp.json()["session"]["id"]

        # Simulate disk-full (`ENOSPC`) as soon as `write_file` attempts atomic write after pre-turn snapshot
        real_atomic_write = studio._atomic_write_workspace_text

        def fail_enospc_write(root: Path, raw_path: str | Path, text: str, *, mode: int | None = None) -> None:
            raise workspace_snapshots.SnapshotDiskFullError(
                "Simulated ENOSPC while writing workspace file",
                operation="atomic_write_workspace_text",
                target_path=str(raw_path),
                errno_code=errno.ENOSPC,
            )

        async def stream_write_attempt(*args: Any, **kwargs: Any):
            yield {
                "type": "content",
                "text": (
                    '<tool_call>{"name": "write_file", "arguments": '
                    '{"path": "module.py", "content": "value = \'clobbered\'\\n"}}</tool_call>'
                ),
            }
            yield {"type": "usage", "decode_tps": 55.0, "prefill_tps": 180.0, "ttft": 0.04, "completion_tokens": 12, "cached_tokens": 0}

        monkeypatch.setattr(studio, "_atomic_write_workspace_text", fail_enospc_write)
        monkeypatch.setattr(studio, "stream_any_model", stream_write_attempt)

        r_turn = client.post(
            "/api/harness/run",
            headers=headers,
            json={"session_id": sid, "prompt": "Overwrite module.py", "max_steps": 3},
        )
        assert r_turn.status_code == 200
        sse_body = r_turn.text
        assert "event: paused" in sse_body
        assert '"error_code": "disk_full"' in sse_body
        assert '"state": "paused"' in sse_body

        # User's uncommitted file is 100% preserved and zero temp files remain!
        assert (repo / "module.py").read_text(encoding="utf-8") == user_precious
        assert not list(repo.glob("*.tmp"))

        state_resp = client.get(f"/api/harness/sessions/{sid}/state", headers=headers)
        assert state_resp.status_code == 200
        state_json = state_resp.json()
        assert state_json["task_status"] == "paused"
        assert state_json["workspace_baseline"] is not None
        assert state_json["workspace_baseline"]["counts"]["unstaged_changes"] == 1

        # Also verify pre-turn snapshot disk-full (`DEFAULT_SNAPSHOT_MIN_FREE_BYTES` impossibly high) pauses before step 1!
        res_prep_early = client.post(f"/api/harness/sessions/{sid}/resume", headers=headers)
        assert res_prep_early.status_code == 200
        monkeypatch.setattr(studio, "DEFAULT_SNAPSHOT_MIN_FREE_BYTES", 10**18)
        r_pre_full = client.post(
            "/api/harness/run",
            headers=headers,
            json={"session_id": sid, "prompt": "Try with 0 free snapshot space", "resume": True, "max_steps": 2},
        )
        assert r_pre_full.status_code == 200
        assert "event: paused" in r_pre_full.text
        assert '"error_code": "disk_full"' in r_pre_full.text
        assert (repo / "module.py").read_text(encoding="utf-8") == user_precious

        # Restore normal disk writes & space floor, resume, edit `module.py`, then restore baseline via HTTP
        monkeypatch.setattr(studio, "DEFAULT_SNAPSHOT_MIN_FREE_BYTES", 0)
        monkeypatch.setattr(studio, "_atomic_write_workspace_text", real_atomic_write)
        call_count = 0

        async def stream_write_then_finish(*args: Any, **kwargs: Any):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield {
                    "type": "content",
                    "text": (
                        '<tool_call>{"name": "write_file", "arguments": '
                        '{"path": "module.py", "content": "value = \'agent_updated\'\\n"}}</tool_call>'
                    ),
                }
            else:
                yield {"type": "content", "text": "Updated module.py."}
            yield {"type": "usage", "decode_tps": 55.0, "prefill_tps": 180.0, "ttft": 0.04, "completion_tokens": 10, "cached_tokens": 0}

        monkeypatch.setattr(studio, "stream_any_model", stream_write_then_finish)
        res_prep = client.post(f"/api/harness/sessions/{sid}/resume", headers=headers)
        assert res_prep.status_code == 200

        r_turn2 = client.post(
            "/api/harness/run",
            headers=headers,
            json={"session_id": sid, "prompt": "Continue", "resume": True, "max_steps": 4},
        )
        assert r_turn2.status_code == 200
        assert (repo / "module.py").read_text(encoding="utf-8") == "value = 'agent_updated'\n"

        # Restore baseline via HTTP endpoint and verify user's uncommitted work returns byte-for-byte!
        rest_resp = client.post(f"/api/harness/sessions/{sid}/snapshots/baseline/restore", headers=headers)
        assert rest_resp.status_code == 200, rest_resp.text
        rest_json = rest_resp.json()
        assert rest_json["report"]["verified"] is True
        assert "module.py" in rest_json["report"]["restored_files"]
        assert (repo / "module.py").read_text(encoding="utf-8") == user_precious


def test_worktree_apply_refuses_clobbering_uncommitted_user_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 + AC2: Applying an isolated worktree refuses silent overwrite of uncommitted user edits in main workspace."""
    _token, headers = _configure_isolated_studio(monkeypatch, tmp_path)
    repo = tmp_path / "wt_repo"
    _init_git_repo(repo)
    (repo / "shared.py").write_text("x = 0\n", encoding="utf-8")
    (repo / "clean_target.py").write_text("clean = 0\n", encoding="utf-8")
    _commit_all(repo, "init")

    with TestClient(studio.app, base_url="http://127.0.0.1:3000") as client:
        c_resp = client.post(
            "/api/harness/sessions",
            headers=headers,
            json={
                "title": "Worktree Conflict Safety",
                "workspace_dir": str(repo),
                "use_worktree": True,
                "policy": "autonomous",
            },
        )
        assert c_resp.status_code == 200, c_resp.text
        sess = c_resp.json()["session"]
        sid = sess["id"]
        wt_dir = Path(sess["worktree_dir"])
        assert wt_dir != repo

        # Agent modifies `shared.py` in worktree while user modifies `shared.py` in main `repo`
        (wt_dir / "shared.py").write_text("x = 1  # worktree agent\n", encoding="utf-8")
        (repo / "shared.py").write_text("x = 2  # user uncommitted work\n", encoding="utf-8")

        apply_conflict = client.post(f"/api/harness/sessions/{sid}/apply", headers=headers)
        assert apply_conflict.status_code == 409
        conflict_data = apply_conflict.json()
        assert conflict_data["status"] == "conflict"
        assert conflict_data["conflicts"][0]["path"] == "shared.py"
        assert (repo / "shared.py").read_text(encoding="utf-8") == "x = 2  # user uncommitted work\n"

        # Revert `shared.py` in worktree and modify `clean_target.py` only -> clean apply succeeds
        subprocess.run(["git", "checkout", "--", "shared.py"], cwd=wt_dir, check=True)
        (wt_dir / "clean_target.py").write_text("clean = 42  # worktree applied\n", encoding="utf-8")

        apply_ok = client.post(f"/api/harness/sessions/{sid}/apply", headers=headers)
        assert apply_ok.status_code == 200, apply_ok.text
        assert (repo / "clean_target.py").read_text(encoding="utf-8") == "clean = 42  # worktree applied\n"
        assert (repo / "shared.py").read_text(encoding="utf-8") == "x = 2  # user uncommitted work\n"

        # Restore main workspace (`target="workspace"`) to initial baseline
        restore_ws = client.post(
            f"/api/harness/sessions/{sid}/snapshots/baseline/restore",
            headers=headers,
            json={"target": "workspace"},
        )
        assert restore_ws.status_code == 200, restore_ws.text
        assert (repo / "clean_target.py").read_text(encoding="utf-8") == "clean = 0\n"


def test_standalone_studio_bundle_workspace_snapshots_and_sandbox_boundary_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone `studio.py` bundles `sandbox_boundary` and `workspace_snapshots` without sibling files."""
    repo_root = Path(__file__).resolve().parents[1]
    bundle_copy = tmp_path / "isolated_bundle" / "studio.py"
    bundle_copy.parent.mkdir(parents=True)
    bundle_copy.write_bytes((repo_root / "studio.py").read_bytes())

    clean_sys_path = [p for p in sys.path if Path(p or ".").resolve() != repo_root]
    monkeypatch.setattr(sys, "path", clean_sys_path)

    spec = importlib.util.spec_from_file_location("studio_standalone_p2_01", bundle_copy)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        mod.DB_PATH = tmp_path / "standalone_p2_01.db"
        mod.HARNESS_STORE_DIR = tmp_path / ".standalone_harnesses"
        mod.STUDIO_API_TOKEN = "m5-standalone-token-0123456789abcdef0123456789"
        mod.STUDIO_AUTH_DISABLED = False
        mod.init_db()

        # Verify inlined sandbox_boundary symlink escape confinement via _resolve_confined_path
        ws = tmp_path / "ws"
        outside = tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        (ws / "escape_link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PermissionError):
            mod._resolve_confined_path(ws, "escape_link/secret.txt")
        (ws / "escape_link").unlink()

        _init_git_repo(ws)
        (ws / "hello.py").write_text("v = 'baseline'\n", encoding="utf-8")
        _commit_all(ws, "init")
        (ws / "hello.py").write_text("v = 'user_dirty'\n", encoding="utf-8")
        (ws / ".env").write_text("STANDALONE_SECRET=keep_out_of_cas_777\n", encoding="utf-8")

        headers = {"Authorization": "Bearer m5-standalone-token-0123456789abcdef0123456789"}
        with TestClient(mod.app, base_url="http://127.0.0.1:3000") as client:
            c_resp = client.post(
                "/api/harness/sessions",
                headers=headers,
                json={"title": "Standalone Snapshot", "workspace_dir": str(ws), "policy": "autonomous"},
            )
            assert c_resp.status_code == 200
            sid = c_resp.json()["session"]["id"]

            snaps = client.get(f"/api/harness/sessions/{sid}/snapshots", headers=headers).json()
            assert snaps["baseline_snapshot_id"] is not None
            assert snaps["snapshots"][0]["counts"]["preserved_sensitive_files"] == 1

            # Mutate via managed write_file tool inside standalone bundle, then restore baseline
            import asyncio
            import httpx

            async def _run_edit() -> dict[str, Any]:
                async with httpx.AsyncClient() as hc:
                    return await mod._execute_harness_tool(
                        hc,
                        {"id": sid, "worktree_dir": str(ws), "policy": "autonomous"},
                        "write_file",
                        {"path": "hello.py", "content": "v = 'agent_standalone'\n"},
                    )

            res = asyncio.run(_run_edit())
            assert res["ok"] is True
            assert (ws / "hello.py").read_text(encoding="utf-8") == "v = 'agent_standalone'\n"

            r_rest = client.post(f"/api/harness/sessions/{sid}/snapshots/baseline/restore", headers=headers)
            assert r_rest.status_code == 200
            assert r_rest.json()["report"]["verified"] is True
            assert (ws / "hello.py").read_text(encoding="utf-8") == "v = 'user_dirty'\n"
            assert (ws / ".env").read_text(encoding="utf-8") == "STANDALONE_SECRET=keep_out_of_cas_777\n"
    finally:
        sys.modules.pop(spec.name, None)
