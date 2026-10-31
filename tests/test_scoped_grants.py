"""Tests for M5-P3-04: Scoped grants, expiry, event-driven revocation, capability containment, and subprocess env hygiene."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import stat
import subprocess
import sys
import textwrap
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

import app as studio
import process_supervision
import scoped_grants


class _FakeClock:
    def __init__(self, initial: float = 1_700_000_000.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


@pytest.fixture()
def isolated_grant_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _FakeClock]:
    kv_secrets: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        studio.keyring,
        "set_password",
        lambda svc, acct, val: kv_secrets.__setitem__((svc, acct), str(val)),
    )
    monkeypatch.setattr(
        studio.keyring,
        "get_password",
        lambda svc, acct: kv_secrets.get((svc, acct)),
    )
    monkeypatch.setattr(
        studio.keyring,
        "delete_password",
        lambda svc, acct: kv_secrets.pop((svc, acct), None),
    )

    home = tmp_path / "home"
    ws = home / "workspace"
    store_dir = home / ".harnesses" / "sessions"
    db_path = home / "studio.db"
    ws.mkdir(parents=True)
    store_dir.mkdir(parents=True)
    (ws / "src").mkdir()
    (ws / "docs").mkdir()
    (ws / "README.md").write_text("# Demo\n", encoding="utf-8")
    (ws / "src" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ws / "src" / "b.py").write_text("OTHER = 2\n", encoding="utf-8")
    (ws / "src" / "protected.py").write_text("SAFE = True\n", encoding="utf-8")
    (ws / "docs" / "secret_spec.md").write_text("confidential architecture\n", encoding="utf-8")

    clock = _FakeClock()
    monkeypatch.setattr(studio, "BASE_DIR", home)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_MCP_STDIO", True)

    studio._PENDING_APPROVALS.clear()
    studio._RESOLVED_APPROVALS.clear()
    studio._RESOLVED_APPROVALS_TS.clear()
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._reset_execution_profile_controller(None)
    studio._reset_process_supervisor(None)
    custom_store = scoped_grants.ScopedGrantStore(store_dir, clock=clock)
    studio._reset_scoped_grant_store(custom_store)
    studio.init_db()

    yield ws, clock

    studio._reset_scoped_grant_store(None)
    studio._reset_process_supervisor(None)
    studio._reset_execution_profile_controller(None)


def test_grant_persistence_permissions_integrity_and_no_auto_renewal(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """Task 1 + AC 1 + AC 2: persisted grant scope, policy revision, finite expiry, fail-closed integrity, and zero auto-renewal."""
    ws, clock = isolated_grant_runtime
    store = studio.get_scoped_grant_store()

    # Permanent / non-positive / NaN / excessive TTLs are rejected at issuance.
    for bad_ttl in (0.0, -5.0, float("inf"), float("nan"), scoped_grants.SG_MAX_GRANT_TTL_SECONDS + 1.0):
        with pytest.raises(scoped_grants.InvalidGrantSpecError):
            store.issue_grant(
                session_id="sess-1",
                capabilities=("workspace_write", "workspace_read"),
                resource_scopes=("path:src/a.py",),
                ttl_seconds=bad_ttl,
                workspace_root=ws,
            )

    # Confirm ScopedGrantStore exposes no automatic or manual renewal/extension API (`AC 2`).
    for forbidden_method in ("renew", "renew_grant", "extend", "extend_grant", "refresh", "refresh_grant"):
        assert not hasattr(store, forbidden_method)

    issued = store.issue_grant(
        session_id="sess-1",
        task_id="sess-1",
        epoch=1,
        capabilities=("workspace_write", "workspace_read"),
        resource_scopes=("path:src/a.py",),
        tool_names=("write_file", "edit_file"),
        ttl_seconds=120.0,
        max_uses=2,
        current_policy="standard",
        workspace_root=ws,
    )
    assert issued.status == "active"
    assert issued.session_policy_revision == 1
    assert issued.global_policy_revision == 1
    original_expiry = issued.expires_at
    assert original_expiry == pytest.approx(clock.now + 120.0)

    # Verify owner-only file & directory permissions on disk (`0700` dir, `0600` file).
    dir_mode = stat.S_IMODE(store.state_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(store.state_path.stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600

    # Simulate process restart: a fresh ScopedGrantStore reloads exact scope, revisions, and expiry.
    reloaded = scoped_grants.ScopedGrantStore(store.state_path.parent.parent, clock=clock)
    loaded_rec = reloaded.get_grant(issued.grant_id)
    assert loaded_rec == issued

    # Advance 50s and consume 1 use: `expires_at` MUST remain identical (no sliding-window renewal!).
    clock.advance(50.0)
    action_caps = scoped_grants.resolve_tool_capabilities("write_file")
    action_scopes = scoped_grants.resolve_tool_resource_scopes(
        "write_file", {"path": "src/a.py"}, workspace_root=ws
    )
    v1 = reloaded.validate_and_consume(
        grant_id=issued.grant_id,
        session_id="sess-1",
        task_id="sess-1",
        epoch=1,
        tool_name="write_file",
        args={"path": "src/a.py", "content": "VALUE = 2\n"},
        action_capabilities=action_caps,
        action_scopes=action_scopes,
    )
    assert v1.allowed is True
    assert v1.grant is not None
    assert v1.grant.uses_consumed == 1
    assert v1.grant.expires_at == original_expiry  # Strictly unchanged (`AC 2`)!

    # Out-of-scope file `src/b.py` is denied without burning the remaining valid use on `src/a.py`.
    wrong_scopes = scoped_grants.resolve_tool_resource_scopes(
        "write_file", {"path": "src/b.py"}, workspace_root=ws
    )
    v_wrong = reloaded.validate_and_consume(
        grant_id=issued.grant_id,
        session_id="sess-1",
        task_id="sess-1",
        epoch=1,
        tool_name="write_file",
        args={"path": "src/b.py", "content": "NOPE\n"},
        action_capabilities=action_caps,
        action_scopes=wrong_scopes,
    )
    assert v_wrong.allowed is False
    assert v_wrong.error_code == "grant_scope_mismatch"

    # Advance past original `expires_at` (+71s -> total 121s > 120s): expired grant blocks admission (`AC 1`).
    clock.advance(71.0)
    v_exp = reloaded.validate_and_consume(
        grant_id=issued.grant_id,
        session_id="sess-1",
        task_id="sess-1",
        epoch=1,
        tool_name="write_file",
        args={"path": "src/a.py", "content": "VALUE = 3\n"},
        action_capabilities=action_caps,
        action_scopes=action_scopes,
    )
    assert v_exp.allowed is False
    assert v_exp.error_code == "grant_expired"

    # Corrupted state file fails closed with GrantStoreIntegrityError (never silently allowing).
    store.state_path.write_text("{corrupted-json", encoding="utf-8")
    with pytest.raises(scoped_grants.GrantStoreIntegrityError):
        reloaded.find_matching_grant(
            session_id="sess-1",
            task_id="sess-1",
            epoch=1,
            tool_name="write_file",
            args={"path": "src/a.py"},
            workspace_root=ws,
        )


def test_admission_enforces_scoped_grant_expiry_max_uses_and_model_untrusted_keys(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """End-to-end `_execute_harness_tool` admission with scoped grants, expiry, max_uses, and untrusted model keys."""
    ws, clock = isolated_grant_runtime
    sess = studio.create_harness_session_db(
        "grant-e2e", str(ws), False, "standard", "local", "critic"
    )
    sid = sess["id"]
    store = studio.get_scoped_grant_store()

    grant = store.issue_grant(
        session_id=sid,
        capabilities=("code_execution", "workspace_write", "workspace_read"),
        resource_scopes=("workspace:.",),
        tool_names=("ctx_execute_python",),
        ttl_seconds=60.0,
        max_uses=2,
        current_policy="standard",
        workspace_root=ws,
    )

    async def _exercise() -> None:
        async with httpx.AsyncClient() as client:
            # 1. `read_file` is already allowed in `standard` policy and MUST NOT burn `max_uses`!
            r_read = await studio._execute_harness_tool(
                client, sess, "read_file", {"path": "src/a.py"}, approved=False
            )
            assert r_read["ok"] is True
            assert store.get_grant(grant.grant_id).uses_consumed == 0  # type: ignore[union-attr]

            # 2. Unapproved `ctx_execute_python` in `standard` policy automatically matches & consumes 1 use.
            r_w1 = await studio._execute_harness_tool(
                client,
                sess,
                "ctx_execute_python",
                {"code": "from pathlib import Path; Path('src/a.py').write_text('VALUE = 10\\n')"},
                approved=False,
            )
            assert r_w1["ok"] is True, r_w1
            after_1 = store.get_grant(grant.grant_id)
            assert after_1 is not None and after_1.uses_consumed == 1
            assert after_1.expires_at == grant.expires_at

            # 3. `run_bash` is NOT in `grant.tool_names`; model trying to smuggle `grant_id`
            #    and `approved: True` inside untrusted tool `args` is rejected!
            r_smuggle = await studio._execute_harness_tool(
                client,
                sess,
                "run_bash",
                {
                    "command": "echo PWN > src/b.py",
                    "grant_id": grant.grant_id,
                    "approved": True,
                },
                approved=False,
                grant_source="model_output",
            )
            assert r_smuggle["ok"] is False
            assert (ws / "src" / "b.py").read_text(encoding="utf-8") == "OTHER = 2\n"

            # 4. Second valid execution consumes the 2nd (final) use -> status transitions to `consumed`.
            r_w2 = await studio._execute_harness_tool(
                client,
                sess,
                "ctx_execute_python",
                {"code": "from pathlib import Path; Path('src/a.py').write_text('VALUE = 20\\n')"},
                approved=False,
            )
            assert r_w2["ok"] is True, r_w2
            after_2 = store.get_grant(grant.grant_id)
            assert after_2 is not None and after_2.status == "consumed" and after_2.uses_consumed == 2

            # 5. Third attempt has NO automatic renewal: explicit `grant_id` fails closed with `grant_consumed`!
            r_w3 = await studio._execute_harness_tool(
                client,
                sess,
                "ctx_execute_python",
                {"code": "print('nope')"},
                approved=True,
                grant_source="scoped_grant",
                grant_id=grant.grant_id,
            )
            assert r_w3["ok"] is False
            assert r_w3.get("error_code") == "grant_consumed"

            # 6. Issue a 10s grant, advance clock 11s -> blocked with `grant_expired` (`AC 1` & `AC 2`).
            short_grant = store.issue_grant(
                session_id=sid,
                capabilities=("code_execution", "workspace_write", "workspace_read"),
                resource_scopes=("workspace:.",),
                tool_names=("ctx_execute_python",),
                ttl_seconds=10.0,
                current_policy="standard",
                workspace_root=ws,
            )
            clock.advance(11.0)
            r_expired_explicit = await studio._execute_harness_tool(
                client,
                sess,
                "ctx_execute_python",
                {"code": "print('late')"},
                approved=True,
                grant_source="scoped_grant",
                grant_id=short_grant.grant_id,
            )
            assert r_expired_explicit["ok"] is False
            assert r_expired_explicit.get("error_code") == "grant_expired"

            r_expired_implicit = await studio._execute_harness_tool(
                client,
                sess,
                "ctx_execute_python",
                {"code": "print('late')"},
                approved=False,
            )
            assert r_expired_implicit["ok"] is False

    asyncio.run(_exercise())


def test_revocation_cannot_be_bypassed_through_any_other_tool(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """AC 3: Revoking a capability/resource blocks managed, rollback, bash, python, and skill tools alike."""
    ws, _clock = isolated_grant_runtime
    # Even under `autonomous`, a revoked capability/resource MUST block every tool that touches or overlaps it!
    sess = studio.create_harness_session_db(
        "anti-bypass", str(ws), False, "autonomous", "local", "critic"
    )
    sid = sess["id"]
    store = studio.get_scoped_grant_store()

    helper_py = ws / "touch_protected.py"
    helper_py.write_text(
        "from pathlib import Path\nPath('src/protected.py').write_text('BYPASSED\\n')\n",
        encoding="utf-8",
    )

    # First create a clean edit on `src/protected.py` so `rollback_edits` has a batch targeting it,
    # then restore and revoke `workspace_write` on `path:src/protected.py`.
    async def _verify_anti_bypass() -> None:
        async with httpx.AsyncClient() as client:
            seed = await studio._execute_harness_tool(
                client,
                sess,
                "write_file",
                {"path": "src/protected.py", "content": "SAFE = 'v2'\n"},
                approved=True,
            )
            assert seed["ok"] is True

            # Issue & immediately revoke a grant on `path:src/protected.py` (`block_scope=True`).
            g = store.issue_grant(
                session_id=sid,
                capabilities=("workspace_write",),
                resource_scopes=("path:src/protected.py",),
                ttl_seconds=300.0,
                current_policy="autonomous",
                workspace_root=ws,
            )
            rev_rec, rule = store.revoke_grant(
                g.grant_id, session_id=sid, reason="lock_protected_module", block_scope=True
            )
            assert rev_rec is not None and rev_rec.status == "revoked"
            assert rule is not None

            # 1. `write_file` on `src/protected.py` -> DENIED (`capability_revoked`)
            r_wf = await studio._execute_harness_tool(
                client,
                sess,
                "write_file",
                {"path": "src/protected.py", "content": "BYPASS_WF\n"},
                approved=True,
            )
            assert r_wf["ok"] is False
            assert r_wf.get("error_code") == "capability_revoked"

            # 2. `edit_file` on `src/protected.py` -> DENIED (`capability_revoked`)
            r_ef = await studio._execute_harness_tool(
                client,
                sess,
                "edit_file",
                {"path": "src/protected.py", "old_text": "SAFE = 'v2'", "new_text": "BYPASS_EF"},
                approved=True,
            )
            assert r_ef["ok"] is False
            assert r_ef.get("error_code") == "capability_revoked"

            # 3. `rollback_edits` targeting `src/protected.py` -> DENIED (`capability_revoked`)
            r_rb = await studio._execute_harness_tool(
                client,
                sess,
                "rollback_edits",
                {"paths": ["src/protected.py"]},
                approved=True,
            )
            assert r_rb["ok"] is False
            assert r_rb.get("error_code") == "capability_revoked"

            # 4. `run_bash` (conservative workspace write overlaps `path:src/protected.py`) -> DENIED!
            r_bash = await studio._execute_harness_tool(
                client,
                sess,
                "run_bash",
                {"command": "python3 touch_protected.py"},
                approved=True,
            )
            assert r_bash["ok"] is False
            assert r_bash.get("error_code") == "capability_revoked"

            # 5. `ctx_execute_python` -> DENIED!
            r_py = await studio._execute_harness_tool(
                client,
                sess,
                "ctx_execute_python",
                {"code": "open('src/protected.py', 'w').write('BYPASS_PY\\n')"},
                approved=True,
            )
            assert r_py["ok"] is False
            assert r_py.get("error_code") == "capability_revoked"

            # 6. `skill_execute_script` -> DENIED!
            r_skill = await studio._run_authorized_tool(
                "skill_execute_script",
                {"script": str(helper_py), "args": [], "cwd": str(ws)},
                "autonomous",
                approved=True,
                grant_source="skill_opt_in",
                session_id=sid,
                workspace_root=ws,
                executor=lambda: studio._execute_skill_script_inner(
                    helper_py, [], cwd=ws, timeout=5.0
                ),
            )
            assert r_skill["ok"] is False
            assert r_skill.get("error_code") == "capability_revoked"

            # Confirm `src/protected.py` was NEVER modified by any tool!
            assert (ws / "src" / "protected.py").read_text(encoding="utf-8") == "SAFE = 'v2'\n"

            # Meanwhile, disjoint `write_file("src/b.py")` AND `read_file("src/protected.py")` still succeed!
            r_disjoint = await studio._execute_harness_tool(
                client,
                sess,
                "write_file",
                {"path": "src/b.py", "content": "OTHER = 99\n"},
                approved=True,
            )
            assert r_disjoint["ok"] is True
            r_read_ok = await studio._execute_harness_tool(
                client,
                sess,
                "read_file",
                {"path": "src/protected.py"},
                approved=False,
            )
            assert r_read_ok["ok"] is True

            # Now revoke `workspace_read` on `docs/secret_spec.md` and verify neither `read_file` nor `grep_search` can read it!
            store.revoke_scope(
                session_id=sid,
                capabilities=("workspace_read",),
                resource_scopes=("path:docs/secret_spec.md",),
                reason="confidential_doc_revoked",
                workspace_root=ws,
            )
            r_secret_read = await studio._execute_harness_tool(
                client, sess, "read_file", {"path": "docs/secret_spec.md"}, approved=True
            )
            assert r_secret_read["ok"] is False
            assert r_secret_read.get("error_code") == "capability_revoked"

            r_secret_grep = await studio._execute_harness_tool(
                client, sess, "grep_search", {"pattern": "confidential"}, approved=True
            )
            assert r_secret_grep["ok"] is False
            assert r_secret_grep.get("error_code") == "capability_revoked"

            # Disjoint `read_file("README.md")` still succeeds!
            r_readme = await studio._execute_harness_tool(
                client, sess, "read_file", {"path": "README.md"}, approved=False
            )
            assert r_readme["ok"] is True

    asyncio.run(_verify_anti_bypass())


def test_event_driven_revocation_on_policy_patch_epoch_fencing_cancel_and_mcp_disable(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """Task 2: Policy updates, lease epoch fencing, task cancellation, and MCP disable/delete immediately revoke grants."""
    ws, _clock = isolated_grant_runtime
    token = "t" * 40
    studio.STUDIO_API_TOKEN = token
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(studio.app, base_url="http://127.0.0.1:8787") as tc:
        # 1. Policy revision change via PATCH /api/harness/sessions/{sid}
        res_create = tc.post(
            "/api/harness/sessions",
            headers=headers,
            json={"title": "pol-rev", "workspace_dir": str(ws), "policy": "standard"},
        )
        assert res_create.status_code == 200
        sid = res_create.json()["session"]["id"]

        res_issue = tc.post(
            f"/api/harness/sessions/{sid}/grants",
            headers=headers,
            json={
                "capabilities": ["workspace_write", "workspace_read"],
                "resource_scopes": ["path:src/a.py"],
                "ttl_seconds": 300,
            },
        )
        assert res_issue.status_code == 200
        gid_pol = res_issue.json()["grant"]["grant_id"]
        assert res_issue.json()["grant"]["session_policy_revision"] == 1

        # Patch policy standard -> plan -> revokes `gid_pol` and bumps policy revision to 2.
        res_patch = tc.patch(
            f"/api/harness/sessions/{sid}",
            headers=headers,
            json={"policy": "plan"},
        )
        assert res_patch.status_code == 200
        g_summary = tc.get(f"/api/harness/sessions/{sid}/grants", headers=headers).json()
        assert g_summary["session_policy_revision"] == 2
        assert g_summary["active_grants"] == []
        rev_g = studio.get_scoped_grant_store().get_grant(gid_pol)
        assert rev_g is not None and rev_g.status == "revoked"
        assert "policy_revision_changed" in rev_g.revoked_reason

        # 2. Epoch fencing revokes epoch-bound grants automatically.
        sm = studio.get_task_state_machine()
        sm.ensure_root_task(sid, session_id=sid, title="pol-rev", workspace_dir=str(ws), policy="standard")
        coord = studio.get_worker_lease_coordinator(ws)
        claim1 = coord.acquire_or_takeover(sid, "wrk-1", lease_ttl_s=30.0)
        ep1 = claim1.epoch_record.epoch

        g_epoch = studio.get_scoped_grant_store().issue_grant(
            session_id=sid,
            task_id=sid,
            epoch=ep1,
            capabilities=("workspace_write",),
            resource_scopes=("path:src/a.py",),
            ttl_seconds=300.0,
            current_policy="plan",
            workspace_root=ws,
        )
        assert studio.get_scoped_grant_store().get_grant(g_epoch.grant_id).status == "active"  # type: ignore[union-attr]

        coord.fence_and_halt(sid, target_state="paused", reason="lease_takeover")
        rev_ep = studio.get_scoped_grant_store().get_grant(g_epoch.grant_id)
        assert rev_ep is not None and rev_ep.status == "revoked"
        assert "epoch_fenced" in rev_ep.revoked_reason

        # 2b. HTTP `/grants/revoke` and `/cancel` revoke grants immediately.
        res_g2 = tc.post(
            f"/api/harness/sessions/{sid}/grants",
            headers=headers,
            json={
                "capabilities": ["workspace_write"],
                "resource_scopes": ["path:src/b.py"],
                "ttl_seconds": 120,
            },
        )
        assert res_g2.status_code == 200
        gid_cancel = res_g2.json()["grant"]["grant_id"]
        res_cancel = tc.post(
            f"/api/harness/sessions/{sid}/cancel",
            headers=headers,
            json={"reason": "user_cancelled"},
        )
        assert res_cancel.status_code == 200
        after_cancel = studio.get_scoped_grant_store().get_grant(gid_cancel)
        assert after_cancel is not None and after_cancel.status == "revoked"

        res_rev_api = tc.post(
            f"/api/harness/sessions/{sid}/grants/revoke",
            headers=headers,
            json={
                "capabilities": ["workspace_write"],
                "resource_scopes": ["path:src/b.py"],
                "reason": "api_scope_lock",
            },
        )
        assert res_rev_api.status_code == 200
        assert res_rev_api.json()["revocation_rule"]["reason"] == "api_scope_lock"

        # 3. MCP server disable / delete bumps global_policy_revision and revokes `mcp:{alias}` grants.
        mcp_script = ws / "echo_mcp.py"
        mcp_script.write_text("import sys\n", encoding="utf-8")
        studio.upsert_mcp_server(
            {
                "alias": "demo_mcp",
                "name": "Demo MCP",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(mcp_script)],
                "approval_policy": "ask",
                "enabled": True,
            }
        )
        g_mcp = studio.get_scoped_grant_store().issue_grant(
            session_id=sid,
            capabilities=("external_mcp",),
            resource_scopes=("mcp:demo_mcp:ping",),
            ttl_seconds=300.0,
            current_policy="plan",
        )
        prev_global_rev = g_mcp.global_policy_revision
        studio.upsert_mcp_server({"alias": "demo_mcp", "enabled": False})
        after_mcp = studio.get_scoped_grant_store().get_grant(g_mcp.grant_id)
        assert after_mcp is not None and after_mcp.status == "revoked"
        _, new_global_rev = studio.get_scoped_grant_store().get_revisions(sid)
        assert new_global_rev == prev_global_rev + 1


def test_capability_specific_in_flight_containment_preserves_unrelated_capabilities(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """Task 3: Revoking one capability/grant contains matching live subprocesses (including detached children)
    without killing unrelated capabilities or quarantining the whole session."""
    ws, _clock = isolated_grant_runtime
    sess = studio.create_harness_session_db(
        "cap-contain", str(ws), False, "standard", "local", "critic"
    )
    sid = sess["id"]
    store = studio.get_scoped_grant_store()
    supervisor = studio.get_process_supervisor()

    # Slow stdio MCP server + slow `run_bash` subprocess with detached child.
    slow_mcp = ws / "slow_mcp_server.py"
    slow_mcp_pid_file = ws / "slow_mcp.pid"
    slow_mcp.write_text(
        textwrap.dedent(
            f"""\
            import json, os, pathlib, sys, time
            pathlib.Path({str(slow_mcp_pid_file)!r}).write_text(str(os.getpid()))
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method", "")
                if method == "initialize":
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"protocolVersion": "2024-11-05", "capabilities": {{}}, "serverInfo": {{"name": "slow", "version": "1"}}}}}}) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/list":
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"tools": [{{"name": "hang", "description": "hang", "inputSchema": {{"type": "object"}}}}]}}}}) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/call":
                    time.sleep(30.0)
            """
        ),
        encoding="utf-8",
    )

    bash_detached = ws / "spawn_detached_worker.py"
    detached_pid_file = ws / "detached_worker.pid"
    leader_pid_file = ws / "leader_worker.pid"
    bash_detached.write_text(
        textwrap.dedent(
            f"""\
            import os, pathlib, subprocess, sys, time
            pathlib.Path({str(leader_pid_file)!r}).write_text(str(os.getpid()))
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            pathlib.Path({str(detached_pid_file)!r}).write_text(str(child.pid))
            time.sleep(30.0)
            """
        ),
        encoding="utf-8",
    )

    async def _run_containment_scenario() -> None:
        async with httpx.AsyncClient() as client:
            studio.upsert_mcp_server(
                {
                    "alias": "slowmcp",
                    "name": "Slow MCP",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(slow_mcp)],
                    "approval_policy": "ask",
                    "enabled": True,
                }
            )
            await studio.sync_mcp_server_tools(client, "slowmcp")
            slow_mcp_pid_file.unlink(missing_ok=True)

            mcp_grant = store.issue_grant(
                session_id=sid,
                capabilities=("external_mcp",),
                resource_scopes=("mcp:slowmcp:hang",),
                ttl_seconds=300.0,
                current_policy="standard",
            )
            exec_grant = store.issue_grant(
                session_id=sid,
                capabilities=("code_execution", "workspace_read", "workspace_write"),
                resource_scopes=("workspace:.",),
                ttl_seconds=300.0,
                current_policy="standard",
                workspace_root=ws,
            )

            # Launch BOTH in-flight operations concurrently in the SAME session `sid`!
            mcp_task = asyncio.create_task(
                studio.execute_mcp_tool_call(
                    client,
                    "slowmcp",
                    "hang",
                    {},
                    cwd=None,
                    policy="standard",
                    approved=False,  # Admitted via `mcp_grant`!
                    session_id=sid,
                )
            )
            bash_task = asyncio.create_task(
                studio._execute_harness_tool(
                    client,
                    sess,
                    "run_bash",
                    {"command": "python3 spawn_detached_worker.py"},
                    approved=False,  # Admitted via `exec_grant`!
                )
            )

            def _read_pid(path: Path) -> int:
                """Read a pid the child may still be writing."""
                for _ in range(250):
                    try:
                        text = path.read_text(encoding="utf-8").strip()
                        if text:
                            return int(text)
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.02)
                raise AssertionError(f"{path.name} never held a pid")

            def _registered_mcp_records() -> list[Any]:
                """In-flight supervisor records owned by the stdio MCP session."""
                return [
                    rec
                    for rec in supervisor.store.list_in_flight_processes()
                    if rec.status == "running"
                    and rec.session_id == sid
                    and "external_mcp" in rec.capabilities
                ]

            # Wait until both child trees have recorded their PIDs *and* the
            # supervisor has registered the stdio MCP record. The pid file is
            # written by the child at startup, which can happen before the
            # parent reaches `_register_active_subprocess` (that runs only once
            # `create_subprocess_exec` returns). Containing in that window
            # matches zero records and fails with `assert 0 == 1` for a reason
            # that has nothing to do with capability scoping, so wait for the
            # registry - the same source `contain_by_capability_async` reads.
            deadline = time.monotonic() + 5.0
            mcp_records: list[Any] = []
            while time.monotonic() < deadline:
                mcp_records = _registered_mcp_records()
                if (
                    mcp_records
                    and slow_mcp_pid_file.exists()
                    and leader_pid_file.exists()
                    and detached_pid_file.exists()
                ):
                    break
                await asyncio.sleep(0.02)

            assert slow_mcp_pid_file.exists(), "slow_mcp did not start"
            assert detached_pid_file.exists(), "detached bash worker did not start"
            mcp_pid = _read_pid(slow_mcp_pid_file)
            leader_pid = _read_pid(leader_pid_file)
            det_pid = _read_pid(detached_pid_file)
            assert [rec.pid for rec in mcp_records] == [mcp_pid], (
                "slow_mcp must be registered for containment under its own pid"
            )

            # Step A: Revoke ONLY `mcp_grant` (`external_mcp` on `mcp:slowmcp:hang`) and run capability containment!
            rev_mcp, _ = store.revoke_grant(mcp_grant.grant_id, session_id=sid, block_scope=True)
            assert rev_mcp is not None
            mcp_outcomes = await supervisor.contain_by_capability_async(
                reason="revoke_slowmcp_only",
                session_id=sid,
                grant_ids=(mcp_grant.grant_id,),
                capabilities=("external_mcp",),
                resource_scopes=("mcp:slowmcp:hang",),
                persist_hold=False,
            )
            assert len(mcp_outcomes) == 1
            with pytest.raises(Exception):
                await asyncio.wait_for(mcp_task, timeout=5.0)
            assert process_supervision.probe_process(mcp_pid) is None

            # CRITICAL (`Task 3`): Unrelated `run_bash` leader + detached child in the SAME session MUST STILL BE ALIVE!
            assert not bash_task.done()
            assert process_supervision.probe_process(leader_pid) is not None
            assert process_supervision.probe_process(det_pid) is not None

            # Step B: Now revoke `exec_grant` -> terminates BOTH `leader_pid` AND `os.setsid()` `det_pid`!
            store.revoke_grant(exec_grant.grant_id, session_id=sid, block_scope=True)
            exec_outcomes = await supervisor.contain_by_capability_async(
                reason="revoke_exec_grant",
                session_id=sid,
                grant_ids=(exec_grant.grant_id,),
                capabilities=exec_grant.capabilities,
                resource_scopes=exec_grant.resource_scopes,
                persist_hold=True,
            )
            assert len(exec_outcomes) == 1
            bash_res = await asyncio.wait_for(bash_task, timeout=5.0)
            assert bash_res["ok"] is False
            assert process_supervision.probe_process(leader_pid) is None
            assert process_supervision.probe_process(det_pid) is None

            # Session itself is NOT quarantined for unrevoked `read_file` inspection!
            r_inspect = await studio._execute_harness_tool(
                client, sess, "read_file", {"path": "README.md"}, approved=False
            )
            assert r_inspect["ok"] is True

    asyncio.run(_run_containment_scenario())


def test_secrets_and_injection_vars_excluded_from_arbitrary_subprocesses(
    isolated_grant_runtime: tuple[Path, _FakeClock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 4: Arbitrary subprocesses never inherit host secrets, Studio/provider/MCP credentials, or LD_PRELOAD/PYTHONPATH."""
    ws, _clock = isolated_grant_runtime

    studio_tok = "studio-secret-bearer-token-9988776655443322"
    prov_secret = "sk-proj-super-secret-provider-credential-12345"
    alpha_mcp_secret = "mcp-alpha-scoped-token-value-abcdef"
    beta_mcp_secret = "mcp-beta-other-server-secret-zyxwvu"

    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", studio_tok)
    monkeypatch.setenv("STUDIO_API_TOKEN", studio_tok)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-host-env-openai-secret-0000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-secret-1111")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_host_secret_token_2222")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-host-secret-3333")
    monkeypatch.setenv("MY_CUSTOM_WEBHOOK_SECRET", "webhook-secret-4444")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/malicious_preload.so")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/malicious_dyld.dylib")
    monkeypatch.setenv("PYTHONPATH", "/tmp/hijack_pythonpath")
    monkeypatch.setenv("BASH_ENV", "/tmp/hijack_bashenv.sh")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o ProxyCommand=evil")
    # Even if an attacker planted a registered secret value inside an otherwise-benign `TERM` var,
    # value-level scrubbing strips it!
    monkeypatch.setenv("TERM", f"xterm-{prov_secret}")

    # Persist a cloud provider credential + two MCP servers in SQLite/keyring.
    stored_prov_ref = studio._keyring_store("cloud:openai:api-key", prov_secret)
    studio.runtime_persistence.run_write_transaction(
        studio.STORAGE,
        studio.DB_PATH,
        lambda conn: conn.execute(
            """
            INSERT OR REPLACE INTO cloud_providers
            (alias, name, wire, base_url, api_key, models_json, enabled, updated_at)
            VALUES ('openai', 'OpenAI', 'openai', 'https://api.openai.com/v1', ?, '[]', 1, ?)
            """,
            (stored_prov_ref, time.time()),
        ),
    )
    studio.upsert_mcp_server(
        {
            "alias": "alpha_mcp",
            "name": "Alpha MCP",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "env": {"ALPHA_MCP_TOKEN": alpha_mcp_secret},
            "enabled": True,
        }
    )
    studio.upsert_mcp_server(
        {
            "alias": "beta_mcp",
            "name": "Beta MCP",
            "transport": "http",
            "url": "https://example.com/mcp",
            "auth_token": beta_mcp_secret,
            "enabled": True,
        }
    )

    dump_script = ws / "dump_env.py"
    dump_script.write_text(
        "import json, os\nprint(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )

    sess = studio.create_harness_session_db(
        "env-hygiene", str(ws), False, "autonomous", "local", "critic"
    )

    all_forbidden_values = (
        studio_tok,
        prov_secret,
        alpha_mcp_secret,
        beta_mcp_secret,
        "sk-host-env-openai-secret-0000",
        "sk-ant-host-secret-1111",
        "ghp_host_secret_token_2222",
        "aws-host-secret-3333",
        "webhook-secret-4444",
        "malicious_preload.so",
        "malicious_dyld.dylib",
        "hijack_pythonpath",
        "hijack_bashenv.sh",
    )
    forbidden_keys = {
        "STUDIO_API_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "MY_CUSTOM_WEBHOOK_SECRET",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "BASH_ENV",
        "GIT_SSH_COMMAND",
    }

    async def _verify_subprocesses() -> None:
        async with httpx.AsyncClient() as client:
            # 1. `run_bash` child environment:
            r_bash = await studio._execute_harness_tool(
                client, sess, "run_bash", {"command": "python3 dump_env.py"}, approved=True
            )
            assert r_bash["ok"] is True
            bash_env = json.loads(r_bash["output"].strip().splitlines()[-1])

            # 2. `ctx_execute_python` child environment:
            r_py = await studio._execute_harness_tool(
                client,
                sess,
                "ctx_execute_python",
                {"code": "import json, os; print(json.dumps(dict(os.environ)))"},
                approved=True,
            )
            assert r_py["ok"] is True
            py_env = json.loads(r_py["output"].strip().splitlines()[-1])

            # 3. `skill_execute_script` child environment:
            r_skill = await studio._run_authorized_tool(
                "skill_execute_script",
                {"script": str(dump_script), "args": [], "cwd": str(ws)},
                "auto_approve",
                approved=True,
                grant_source="skill_opt_in",
                session_id=sess["id"],
                workspace_root=ws,
                executor=lambda: studio._execute_skill_script_inner(
                    dump_script, [], cwd=ws, timeout=5.0
                ),
            )
            assert r_skill["ok"] is True
            skill_env = json.loads(r_skill["output"].strip().splitlines()[-1])

            for label, child_env in (("bash", bash_env), ("python", py_env), ("skill", skill_env)):
                assert not (set(child_env) & forbidden_keys), f"{label} leaked keys: {set(child_env) & forbidden_keys}"
                serialized = json.dumps(child_env)
                for sec_val in all_forbidden_values:
                    assert sec_val not in serialized, f"{label} leaked secret value {sec_val!r}"
                assert child_env.get("M5_SUPERVISION_ID", "").startswith("sup-")

    asyncio.run(_verify_subprocesses())

    # 4. Scoped MCP stdio env for `alpha_mcp` keeps ONLY `ALPHA_MCP_TOKEN`, stripping Studio/provider/beta secrets & LD_PRELOAD!
    mcp_env = studio._mcp_stdio_env(
        "alpha_mcp",
        {
            "ALPHA_MCP_TOKEN": alpha_mcp_secret,
            "LEAKED_STUDIO": studio_tok,
            "LEAKED_BETA": beta_mcp_secret,
            "LD_PRELOAD": "/tmp/evil.so",
            "STUDIO_API_TOKEN": studio_tok,
        },
    )
    assert mcp_env["ALPHA_MCP_TOKEN"] == alpha_mcp_secret
    assert "STUDIO_API_TOKEN" not in mcp_env
    assert "LD_PRELOAD" not in mcp_env
    assert "LEAKED_STUDIO" not in mcp_env
    assert "LEAKED_BETA" not in mcp_env

    # 5. Git worktree helper env strips GIT_SSH_COMMAND, secrets, and LD_PRELOAD while forcing non-interactive mode.
    git_env = studio._git_subprocess_env()
    assert git_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_SSH_COMMAND" not in git_env
    assert "OPENAI_API_KEY" not in git_env
    assert "LD_PRELOAD" not in git_env


def test_interactive_approval_persists_one_shot_exact_args_finite_grant(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """`/api/harness/approve` persists a 1-use exact-args finite-TTL grant; argument tampering or second replay fails closed."""
    ws, clock = isolated_grant_runtime
    token = "a" * 40
    studio.STUDIO_API_TOKEN = token
    headers = {"Authorization": f"Bearer {token}"}

    sess = studio.create_harness_session_db(
        "apr-grant", str(ws), False, "standard", "local", "critic"
    )
    sid = sess["id"]
    pending = studio._create_pending_tool_approval(
        sid, 1, "write_file", {"path": "src/a.py", "content": "APPROVED_ONCE\n"}
    )
    aid = pending["approval_id"]

    with TestClient(studio.app, base_url="http://127.0.0.1:8787") as tc:
        res = tc.post(
            "/api/harness/approve",
            headers=headers,
            json={
                "approval_id": aid,
                "session_id": sid,
                "decision": "approve",
                "ttl_seconds": 30.0,
            },
        )
        assert res.status_code == 200

    persisted = studio.get_scoped_grant_store().get_grant(aid)
    assert persisted is not None
    assert persisted.max_uses == 1
    assert persisted.expires_at == pytest.approx(clock.now + 30.0)

    async def _check_one_shot() -> None:
        async with httpx.AsyncClient() as client:
            # Tampered arguments with the approved `approval_id` -> DENIED (`grant_scope_mismatch`)!
            r_tampered = await studio._execute_harness_tool(
                client,
                sess,
                "write_file",
                {"path": "src/a.py", "content": "TAMPERED_PAYLOAD\n"},
                approved=True,
                grant_source="operator_approval",
                approval_id=aid,
            )
            assert r_tampered["ok"] is False
            assert r_tampered.get("error_code") == "grant_scope_mismatch"

            # Exact approved arguments -> SUCCEEDS once!
            r_exact = await studio._execute_harness_tool(
                client,
                sess,
                "write_file",
                {"path": "src/a.py", "content": "APPROVED_ONCE\n"},
                approved=True,
                grant_source="operator_approval",
                approval_id=aid,
            )
            assert r_exact["ok"] is True

            # Second execution with the same `approval_id` -> DENIED (`grant_consumed`)!
            r_replay = await studio._execute_harness_tool(
                client,
                sess,
                "write_file",
                {"path": "src/a.py", "content": "APPROVED_ONCE\n"},
                approved=True,
                grant_source="operator_approval",
                approval_id=aid,
            )
            assert r_replay["ok"] is False
            assert r_replay.get("error_code") == "grant_consumed"

    asyncio.run(_check_one_shot())


def test_standalone_studio_bundle_enforces_scoped_grants_and_revocation(
    tmp_path: Path,
) -> None:
    """Verify single-file `studio.py` bundle in `python -I` enforces scoped grants, expiry, and anti-bypass revocation."""
    probe_code = textwrap.dedent(
        f"""\
        import asyncio, os, pathlib, sys
        sys.path.insert(0, {str(Path.cwd())!r})
        import httpx
        import studio

        root = pathlib.Path({str(tmp_path)!r})
        ws = root / "ws"
        ws.mkdir()
        (ws / "target.txt").write_text("init\\n")
        studio.BASE_DIR = root
        studio.WORKSPACE_ROOT = ws
        studio.PROJECTS_DIR = root
        studio.DB_PATH = root / "studio.db"
        studio.HARNESS_STORE_DIR = root / ".harnesses" / "sessions"
        studio.HARNESS_STORE_DIR.mkdir(parents=True)
        studio._reset_scoped_grant_store(None)
        studio._reset_process_supervisor(None)
        studio._reset_execution_profile_controller(None)
        studio.init_db()

        sess = studio.create_harness_session_db("bundle", str(ws), False, "standard", "local", "critic")
        g = studio.get_scoped_grant_store().issue_grant(
            session_id=sess["id"],
            capabilities=("workspace_write", "workspace_read"),
            resource_scopes=("path:target.txt",),
            ttl_seconds=60.0,
            max_uses=1,
            current_policy="standard",
            workspace_root=ws,
        )

        async def main():
            async with httpx.AsyncClient() as client:
                r1 = await studio._execute_harness_tool(
                    client, sess, "write_file", {{"path": "target.txt", "content": "ok\\n"}}, approved=False
                )
                assert r1["ok"] is True, r1
                studio.get_scoped_grant_store().revoke_scope(
                    session_id=sess["id"],
                    capabilities=("workspace_write",),
                    resource_scopes=("path:target.txt",),
                    workspace_root=ws,
                )
                r2 = await studio._execute_harness_tool(
                    client, sess, "edit_file", {{"path": "target.txt", "old_text": "ok", "new_text": "no"}}, approved=True
                )
                assert r2["ok"] is False and r2.get("error_code") == "capability_revoked", r2

        asyncio.run(main())
        print("STANDALONE_OK")
        """
    )
    res = subprocess.run(
        [sys.executable, "-I", "-c", probe_code],
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
    assert "STANDALONE_OK" in res.stdout


def test_path_canonicalization_symlink_alias_and_concurrent_single_use_grant(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """Adversarial checks: dot-segments/symlinks cannot bypass file revocations, single-use grants are race-free, and Git config env injection keys are scrubbed."""
    ws, _clock = isolated_grant_runtime
    (ws / "src" / "sub").mkdir(parents=True, exist_ok=True)
    protected = ws / "src" / "protected.py"
    protected.write_text("SAFE = 1\n", encoding="utf-8")
    alias = ws / "src" / "alias_to_protected.py"
    alias.symlink_to(protected)

    sess = studio.create_harness_session_db(
        "adversarial-paths", str(ws), False, "autonomous", "local", "critic"
    )
    sid = sess["id"]
    store = studio.get_scoped_grant_store()
    store.revoke_scope(
        session_id=sid,
        capabilities=("workspace_write",),
        resource_scopes=("path:src/protected.py",),
        workspace_root=ws,
        reason="protect_canonical_target",
    )

    async def _attempt_aliases() -> None:
        async with httpx.AsyncClient() as client:
            for candidate_path in (
                "./src//protected.py",
                "src/./protected.py",
                "src/sub/../protected.py",
                "src/alias_to_protected.py",
            ):
                res = await studio._execute_harness_tool(
                    client,
                    sess,
                    "write_file",
                    {"path": candidate_path, "content": "PWNED\n"},
                    approved=True,
                )
                assert res["ok"] is False, f"Bypass succeeded via {candidate_path!r}: {res}"
                assert res.get("error_code") == "capability_revoked"

    asyncio.run(_attempt_aliases())
    assert protected.read_text(encoding="utf-8") == "SAFE = 1\n"

    # Concurrent 8-thread race on a max_uses=1 grant admits AT MOST 1 caller!
    g_once = store.issue_grant(
        session_id=sid,
        capabilities=("code_execution",),
        resource_scopes=("workspace:.",),
        ttl_seconds=60.0,
        max_uses=1,
        current_policy="autonomous",
        workspace_root=ws,
    )
    import concurrent.futures
    import threading

    barrier = threading.Barrier(8)

    def _race_consume(_idx: int) -> scoped_grants.GrantValidationOutcome:
        barrier.wait(timeout=5.0)
        return store.validate_and_consume(
            grant_id=g_once.grant_id,
            session_id=sid,
            task_id=None,
            epoch=None,
            tool_name="run_bash",
            args={"command": "echo hi"},
            action_capabilities=("code_execution",),
            action_scopes=(f"workspace:{ws.resolve()}",),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(_race_consume, range(8)))
    assert sum(1 for d in decisions if d.allowed) == 1
    assert sum(1 for d in decisions if not d.allowed and d.error_code == "grant_consumed") == 7

    # Git config injection env vars (`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0`) are scrubbed!
    scrubbed = scoped_grants.sanitize_subprocess_env(
        {
            "HOME": str(ws),
            "GIT_CONFIG_PARAMETERS": "'core.sshCommand=evil'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "/tmp/evil",
        },
        interpreter_dir="/usr/bin",
        extra={
            "GIT_CONFIG_PARAMETERS": "'core.sshCommand=evil'",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
        },
        allow_scoped_mcp_env=True,
        include_git_non_interactive=True,
    )
    assert "GIT_CONFIG_PARAMETERS" not in scrubbed
    assert "GIT_CONFIG_COUNT" not in scrubbed
    assert "GIT_CONFIG_KEY_0" not in scrubbed
    assert "GIT_CONFIG_VALUE_0" not in scrubbed



@pytest.mark.parametrize("key", sorted(scoped_grants.SG_FORBIDDEN_INJECTION_ENV_KEYS))
def test_every_forbidden_injection_key_is_stripped_from_a_configured_env(key: str) -> None:
    """`extra` (an MCP server's `env_json`, a container env) must not reintroduce a hook.

    The `extra` path deliberately allows secret-named keys so a stdio MCP server
    receives its own credentials; loader/startup hooks are the exception and have to
    be refused *whatever* the caller configured.
    """
    env = scoped_grants.sanitize_subprocess_env(
        {"PATH": "/usr/bin", "LANG": "C.UTF-8"},
        interpreter_dir="/usr/bin",
        extra={key: "/tmp/evil"},
        allow_scoped_mcp_env=True,
    )
    assert key not in env


@pytest.mark.parametrize(
    "key",
    [
        # Siblings of keys that were already blocked. Blocking RUBYOPT but not
        # RUBYLIB (and NODE_OPTIONS but not NODE_PATH, JAVA_TOOL_OPTIONS but not
        # _JAVA_OPTIONS) left the same hook reachable under another name.
        "RUBYLIB",
        "NODE_PATH",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "PERL5DB",
    ],
)
def test_loader_hook_siblings_are_blocked_too(key: str) -> None:
    assert scoped_grants.is_secret_env_key(key) is True
    env = scoped_grants.sanitize_subprocess_env(
        {key: "/tmp/evil", "PATH": "/usr/bin"},
        interpreter_dir="/usr/bin",
        extra={key: "/tmp/evil"},
        allow_scoped_mcp_env=True,
    )
    assert key not in env


def test_registered_secret_values_never_reach_a_child_environment() -> None:
    """Allowlisted keys are still dropped when their *value* carries a known secret."""
    secret = "tok-supersecret-value"
    env = scoped_grants.sanitize_subprocess_env(
        {
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "MY_ALLOWED_VAR": f"prefix-{secret}-suffix",
            "OPENAI_API_KEY": "sk-live-abcdef",
            "STUDIO_API_TOKEN": secret,
        },
        interpreter_dir="/usr/bin",
        known_secret_values=[secret],
        extra_allowed_host_keys=("MY_ALLOWED_VAR",),
    )
    assert "MY_ALLOWED_VAR" not in env
    assert "OPENAI_API_KEY" not in env
    assert "STUDIO_API_TOKEN" not in env
    assert all(secret not in v for v in env.values())


def test_mid_spawn_grant_revocation_contains_stdio_mcp_before_register_process(
    isolated_grant_runtime: tuple[Path, _FakeClock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministically reproduce the Python 3.13 spawn-to-registration race:
    the OS child has already forked and written its PID file while
    ``asyncio.create_subprocess_exec`` is still suspended before returning
    ``proc`` to ``_mcp_stdio_rpc``. Revoking the grant and calling
    ``contain_by_capability_async`` in that window must return 1 outcome, reap
    the child, send zero RPC bytes, and preserve unrelated session subprocesses.
    """
    ws, _clock = isolated_grant_runtime
    sess = studio.create_harness_session_db(
        "mid-spawn-race", str(ws), False, "standard", "local", "critic"
    )
    sid = sess["id"]
    store = studio.get_scoped_grant_store()
    supervisor = studio.get_process_supervisor()

    slow_mcp = ws / "race_mcp_server.py"
    rpc_log = ws / "race_mcp_rpc.log"
    slow_mcp.write_text(
        textwrap.dedent(
            f"""\
            import json, pathlib, sys, time
            log_path = pathlib.Path({str(rpc_log)!r})
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method", "")
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(method + "\\n")
                if method == "initialize":
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"protocolVersion": "2024-11-05", "capabilities": {{}}, "serverInfo": {{"name": "race", "version": "1"}}}}}}) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/list":
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"tools": [{{"name": "hang", "description": "hang", "inputSchema": {{"type": "object"}}}}]}}}}) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/call":
                    time.sleep(30.0)
            """
        ),
        encoding="utf-8",
    )

    bash_worker = ws / "unrelated_bash_worker.py"
    bash_pid_file = ws / "unrelated_bash.pid"
    bash_worker.write_text(
        textwrap.dedent(
            f"""\
            import os, pathlib, time
            pathlib.Path({str(bash_pid_file)!r}).write_text(str(os.getpid()))
            time.sleep(30.0)
            """
        ),
        encoding="utf-8",
    )

    async def _scenario() -> None:
        async with httpx.AsyncClient() as client:
            studio.upsert_mcp_server(
                {
                    "alias": "racemcp",
                    "name": "Race MCP",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(slow_mcp)],
                    "approval_policy": "ask",
                    "enabled": True,
                }
            )
            await studio.sync_mcp_server_tools(client, "racemcp")
            rpc_log.unlink(missing_ok=True)

            mcp_grant = store.issue_grant(
                session_id=sid,
                capabilities=("external_mcp",),
                resource_scopes=("mcp:racemcp:hang",),
                ttl_seconds=300.0,
                current_policy="standard",
            )
            exec_grant = store.issue_grant(
                session_id=sid,
                capabilities=("code_execution", "workspace_read", "workspace_write"),
                resource_scopes=("workspace:.",),
                ttl_seconds=300.0,
                current_policy="standard",
                workspace_root=ws,
            )

            orig_exec = asyncio.create_subprocess_exec
            child_forked = asyncio.Event()
            allow_exec_return = asyncio.Event()
            captured_pids: list[int] = []

            async def _delayed_exec(*args: object, **kwargs: object):
                proc = await orig_exec(*args, **kwargs)
                if any(str(a) == str(slow_mcp) for a in args):
                    assert proc.pid is not None
                    captured_pids.append(proc.pid)
                    child_forked.set()
                    await allow_exec_return.wait()
                return proc

            monkeypatch.setattr(studio.asyncio, "create_subprocess_exec", _delayed_exec)

            bash_task = asyncio.create_task(
                studio._execute_harness_tool(
                    client,
                    sess,
                    "run_bash",
                    {"command": "python3 unrelated_bash_worker.py"},
                    approved=False,
                )
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not bash_pid_file.exists():
                await asyncio.sleep(0.02)
            assert bash_pid_file.exists()
            unrelated_pid = int(bash_pid_file.read_text().strip())

            mcp_task = asyncio.create_task(
                studio.execute_mcp_tool_call(
                    client,
                    "racemcp",
                    "hang",
                    {},
                    cwd=None,
                    policy="standard",
                    approved=False,
                    session_id=sid,
                )
            )

            # Wait until OS child is alive in kernel, while create_subprocess_exec
            # is still suspended BEFORE returning `proc` to `_mcp_stdio_rpc`.
            await asyncio.wait_for(child_forked.wait(), timeout=5.0)
            assert len(captured_pids) == 1
            mcp_pid = captured_pids[0]
            assert process_supervision.probe_process(mcp_pid) is not None

            # Fire grant revocation + containment right inside the spawn window!
            rev_rec, _ = store.revoke_grant(mcp_grant.grant_id, session_id=sid, block_scope=True)
            assert rev_rec is not None
            contain_task = asyncio.create_task(
                supervisor.contain_by_capability_async(
                    reason="revoke_mid_spawn",
                    session_id=sid,
                    grant_ids=(mcp_grant.grant_id,),
                    capabilities=("external_mcp",),
                    resource_scopes=("mcp:racemcp:hang",),
                    persist_hold=False,
                )
            )
            await asyncio.sleep(0)
            allow_exec_return.set()

            mcp_outcomes = await asyncio.wait_for(contain_task, timeout=5.0)
            assert len(mcp_outcomes) == 1
            with pytest.raises(Exception):
                await asyncio.wait_for(mcp_task, timeout=5.0)
            assert process_supervision.probe_process(mcp_pid) is None
            assert not rpc_log.exists(), "revoked MCP child must not receive initialize or tools/call"

            # Unrelated bash worker in the same session remains untouched.
            assert not bash_task.done()
            assert process_supervision.probe_process(unrelated_pid) is not None

            store.revoke_grant(exec_grant.grant_id, session_id=sid, block_scope=True)
            exec_outcomes = await supervisor.contain_by_capability_async(
                reason="cleanup_exec_grant",
                session_id=sid,
                grant_ids=(exec_grant.grant_id,),
                capabilities=exec_grant.capabilities,
                resource_scopes=exec_grant.resource_scopes,
                persist_hold=False,
            )
            assert len(exec_outcomes) == 1
            await asyncio.wait_for(bash_task, timeout=5.0)
            assert process_supervision.probe_process(unrelated_pid) is None

    asyncio.run(_scenario())


def test_scope_only_stdio_mcp_containment_preserves_unrelated_mcp_server(
    isolated_grant_runtime: tuple[Path, _FakeClock],
) -> None:
    """Containing narrow resource scope ``mcp:alphamcp:hang_a`` (without passing
    ``grant_ids``) must terminate only ``alphamcp::hang_a`` and preserve a
    concurrent ``betamcp::hang_b`` stdio MCP call in the same session.
    """
    ws, _clock = isolated_grant_runtime
    sess = studio.create_harness_session_db(
        "dual-mcp-scope", str(ws), False, "standard", "local", "critic"
    )
    sid = sess["id"]
    store = studio.get_scoped_grant_store()
    supervisor = studio.get_process_supervisor()

    dual_mcp = ws / "dual_mcp_server.py"
    pid_a_file = ws / "hang_a.pid"
    pid_b_file = ws / "hang_b.pid"
    dual_mcp.write_text(
        textwrap.dedent(
            f"""\
            import json, os, pathlib, sys, time
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method", "")
                if method == "initialize":
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"protocolVersion": "2024-11-05", "capabilities": {{}}, "serverInfo": {{"name": "dual", "version": "1"}}}}}}) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/list":
                    tools = [
                        {{"name": "hang_a", "description": "a", "inputSchema": {{"type": "object"}}}},
                        {{"name": "hang_b", "description": "b", "inputSchema": {{"type": "object"}}}},
                    ]
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"tools": tools}}}}) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/call":
                    tname = (msg.get("params") or {{}}).get("name", "")
                    if tname == "hang_a":
                        pathlib.Path({str(pid_a_file)!r}).write_text(str(os.getpid()))
                    elif tname == "hang_b":
                        pathlib.Path({str(pid_b_file)!r}).write_text(str(os.getpid()))
                    time.sleep(30.0)
            """
        ),
        encoding="utf-8",
    )

    async def _scenario() -> None:
        async with httpx.AsyncClient() as client:
            for alias in ("alphamcp", "betamcp"):
                studio.upsert_mcp_server(
                    {
                        "alias": alias,
                        "name": f"{alias} server",
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(dual_mcp)],
                        "approval_policy": "ask",
                        "enabled": True,
                    }
                )
                await studio.sync_mcp_server_tools(client, alias)

            grant_a = store.issue_grant(
                session_id=sid,
                capabilities=("external_mcp",),
                resource_scopes=("mcp:alphamcp:hang_a",),
                ttl_seconds=300.0,
                current_policy="standard",
            )
            grant_b = store.issue_grant(
                session_id=sid,
                capabilities=("external_mcp",),
                resource_scopes=("mcp:betamcp:hang_b",),
                ttl_seconds=300.0,
                current_policy="standard",
            )

            task_a = asyncio.create_task(
                studio.execute_mcp_tool_call(
                    client, "alphamcp", "hang_a", {}, policy="standard", approved=False, session_id=sid
                )
            )
            task_b = asyncio.create_task(
                studio.execute_mcp_tool_call(
                    client, "betamcp", "hang_b", {}, policy="standard", approved=False, session_id=sid
                )
            )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if pid_a_file.exists() and pid_b_file.exists():
                        break
                    await asyncio.sleep(0.03)
                assert pid_a_file.exists() and pid_b_file.exists()
                pid_a = int(pid_a_file.read_text().strip())
                pid_b = int(pid_b_file.read_text().strip())

                # Contain by narrow scope `mcp:alphamcp:hang_a` WITHOUT passing grant_ids!
                store.revoke_grant(grant_a.grant_id, session_id=sid, block_scope=True)
                out_a = await supervisor.contain_by_capability_async(
                    reason="revoke_hang_a_scope",
                    session_id=sid,
                    capabilities=("external_mcp",),
                    resource_scopes=("mcp:alphamcp:hang_a",),
                    persist_hold=False,
                )
                assert len(out_a) == 1
                with pytest.raises(Exception):
                    await asyncio.wait_for(task_a, timeout=5.0)
                assert process_supervision.probe_process(pid_a) is None

                # Unrelated `betamcp::hang_b` in the SAME session stays alive!
                assert not task_b.done()
                assert process_supervision.probe_process(pid_b) is not None

                store.revoke_grant(grant_b.grant_id, session_id=sid, block_scope=True)
                out_b = await supervisor.contain_by_capability_async(
                    reason="revoke_hang_b_scope",
                    session_id=sid,
                    grant_ids=(grant_b.grant_id,),
                    persist_hold=False,
                )
                assert len(out_b) == 1
                with pytest.raises(Exception):
                    await asyncio.wait_for(task_b, timeout=5.0)
                assert process_supervision.probe_process(pid_b) is None
            finally:
                for t in (task_a, task_b):
                    if not t.done():
                        t.cancel()
                        with pytest.raises(BaseException):
                            await t

    asyncio.run(_scenario())


def test_sync_containment_and_abort_spawn_cleanup_during_pending_spawn(
    tmp_path: Path,
) -> None:
    """Synchronous containment latched during ``begin_spawn`` terminates the
    process immediately on ``register_process``, and ``abort_spawn`` clears
    pending state cleanly when spawn fails before registration.
    """
    store = process_supervision.ContainmentStore(tmp_path / "containment")
    sup = process_supervision.ProcessSupervisor(store)

    # 1. abort_spawn clears pending entries without leaking.
    aborted_id = sup.new_supervision_id()
    sup.begin_spawn(
        supervision_id=aborted_id,
        tool_name="mcp__srv__hang",
        workspace_root=tmp_path,
        session_id="s-sync",
        grant_id="grant-abort",
        capabilities=("external_mcp",),
        resource_scopes=("mcp:srv:hang",),
    )
    sup.abort_spawn(aborted_id)
    assert sup._pending_spawns == {}

    # 2. Synchronous containment latched mid-spawn kills child upon register_process.
    async def _run_sync_latch() -> None:
        sup_id = sup.new_supervision_id()
        sup.begin_spawn(
            supervision_id=sup_id,
            tool_name="mcp__srv__hang",
            workspace_root=tmp_path,
            session_id="s-sync",
            grant_id="grant-sync",
            capabilities=("external_mcp",),
            resource_scopes=("mcp:srv:hang",),
        )
        # Sync containment fires BEFORE proc handle exists!
        sync_out = sup.contain_by_capability_sync(
            reason="sync_revoke_mid_spawn",
            session_id="s-sync",
            grant_ids=("grant-sync",),
            persist_hold=False,
        )
        assert sync_out == []
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            start_new_session=True,
        )
        assert proc.pid is not None
        child_pid = proc.pid
        sup.register_process(
            proc,
            supervision_id=sup_id,
            tool_name="mcp__srv__hang",
            workspace_root=tmp_path,
            session_id="s-sync",
            grant_id="grant-sync",
            capabilities=("external_mcp",),
            resource_scopes=("mcp:srv:hang",),
        )
        outcome = await sup.await_pending_containment_async(sup_id)
        assert outcome is not None
        assert outcome.reason == "sync_revoke_mid_spawn"
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        probe_after = process_supervision.probe_process(child_pid)
        assert probe_after is None or not probe_after.alive

    asyncio.run(_run_sync_latch())

