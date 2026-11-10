"""AP-004 actual-consumer tests for immutable mission authority and root budgets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient
import pytest

import app as studio
import execution_budget
import runtime_persistence


@pytest.fixture()
def mission_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store_dir = tmp_path / ".harnesses"
    db_path = tmp_path / "mission.sqlite3"
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "BASE_DIR", tmp_path)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "m" * 40)
    studio._reset_scoped_grant_store()
    studio._reset_execution_profile_controller()
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._PENDING_APPROVALS.clear()
    studio.init_db()
    studio._set_default_projects_dir(str(workspace), create_if_missing=True)
    return workspace


def _budget(max_tools: int) -> str:
    return execution_budget.TaskBudgetState(
        spec=execution_budget.TaskBudgetSpec(
            max_steps=50,
            max_tool_calls=max_tools,
            max_tokens=50_000,
            max_elapsed_s=600,
            max_recoveries=2,
        )
    ).to_json()


def _create_running_task(
    task_id: str,
    *,
    session_id: str,
    max_tools: int,
    parent_task_id: str | None = None,
) -> None:
    runtime_persistence.RuntimePersistenceStore(studio.STORAGE, studio.DB_PATH).create_task(
        task_id,
        parent_task_id=parent_task_id,
        session_id=session_id,
        state="running",
        policy="autonomous",
        budget_json=_budget(max_tools),
        now=time.time(),
    )


def _session(workspace: Path, session_id: str, task_id: str) -> dict[str, Any]:
    return {
        "id": session_id,
        "task_id": task_id,
        "worktree_dir": str(workspace),
        "policy": "autonomous",
        "status": "running",
    }


def _audit_statuses(tool_name: str) -> list[str]:
    return runtime_persistence.run_read(
        studio.STORAGE,
        studio.DB_PATH,
        lambda conn: [
            str(row[0])
            for row in conn.execute(
                "SELECT status FROM tool_audit_events WHERE tool_name=? ORDER BY id",
                (tool_name,),
            ).fetchall()
        ],
    )


def test_leaf_and_root_tool_budgets_charge_atomically_without_join_double_debit(
    mission_env: Path,
) -> None:
    """A child charges itself and its authoritative root; root exhaustion rolls both back."""
    sid = "mission-root"
    child_id = "mission-child"
    _create_running_task(sid, session_id=sid, max_tools=2)
    _create_running_task(child_id, session_id=sid, max_tools=5, parent_task_id=sid)
    sample = mission_env / "sample.txt"
    sample.write_text("authority\n", encoding="utf-8")

    root_envelope = studio.capture_mission_authority_envelope(sid, sid)
    child_envelope = studio.capture_mission_authority_envelope(sid, child_id)
    assert root_envelope.root_task_id == sid
    assert child_envelope.root_task_id == sid

    root_result = asyncio.run(
        studio._execute_harness_tool(
            None,
            _session(mission_env, sid, sid),
            "read_file",
            {"path": "sample.txt"},
            step_id=1,
            call_index=0,
            mission_envelope=root_envelope,
        )
    )
    child_result = asyncio.run(
        studio._execute_harness_tool(
            None,
            _session(mission_env, sid, child_id),
            "read_file",
            {"path": "sample.txt"},
            step_id=1,
            call_index=0,
            mission_envelope=child_envelope,
        )
    )
    assert root_result["ok"] is True
    assert child_result["ok"] is True

    denied = asyncio.run(
        studio._execute_harness_tool(
            None,
            _session(mission_env, sid, child_id),
            "read_file",
            {"path": "sample.txt"},
            step_id=2,
            call_index=0,
            mission_envelope=child_envelope,
        )
    )
    assert denied["ok"] is False
    assert denied["error_code"] == "task_budget_exhausted"
    budgets = studio.get_task_state_machine().budgets
    assert budgets.get_budget(sid).tool_calls_used == 2
    assert budgets.get_budget(child_id).tool_calls_used == 1
    assert _audit_statuses("read_file") == ["completed", "completed", "denied"]


def test_policy_generation_invalidates_queued_operation_before_actual_consumer(
    mission_env: Path,
) -> None:
    sid = "stale-generation"
    _create_running_task(sid, session_id=sid, max_tools=4)
    (mission_env / "queued.txt").write_text("old queue\n", encoding="utf-8")
    queued = studio.capture_mission_authority_envelope(sid, sid)

    new_revision, _revoked = studio.get_scoped_grant_store().bump_session_policy_revision(
        sid,
        new_policy="autonomous",
        reason="operator_revoked_queued_generation",
    )
    assert new_revision > queued.session_policy_revision

    stale_result = asyncio.run(
        studio._execute_harness_tool(
            None,
            _session(mission_env, sid, sid),
            "read_file",
            {"path": "queued.txt"},
            step_id="queued-before-revision",
            call_index=0,
            mission_envelope=queued,
        )
    )
    assert stale_result["ok"] is False
    assert stale_result["error_code"] == "stale_policy_generation"
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 0

    fresh = studio.capture_mission_authority_envelope(sid, sid)
    fresh_result = asyncio.run(
        studio._execute_harness_tool(
            None,
            _session(mission_env, sid, sid),
            "read_file",
            {"path": "queued.txt"},
            step_id="queued-after-revision",
            call_index=0,
            mission_envelope=fresh,
        )
    )
    assert fresh_result["ok"] is True
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 1

    queued_before_global_change = studio.capture_mission_authority_envelope(sid, sid)
    studio.get_scoped_grant_store().bump_global_policy_revision(reason="global_revocation")
    global_stale = asyncio.run(
        studio._execute_harness_tool(
            None,
            _session(mission_env, sid, sid),
            "read_file",
            {"path": "queued.txt"},
            step_id="queued-before-global-revision",
            call_index=0,
            mission_envelope=queued_before_global_change,
        )
    )
    assert global_stale["error_code"] == "stale_policy_generation"
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 1


class _JsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _InspirationClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str, **_kwargs: Any) -> _JsonResponse:
        self.urls.append(url)
        if "github.com" in url:
            return _JsonResponse(
                {
                    "items": [
                        {
                            "full_name": "example/reference",
                            "stargazers_count": 42,
                            "language": "Python",
                            "description": "Reference architecture",
                            "topics": ["agents"],
                        }
                    ]
                }
            )
        return _JsonResponse({"AbstractText": "Bounded web signal", "RelatedTopics": []})


def test_fixed_http_api_consumers_share_stale_generation_and_budget_boundary(
    mission_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "inspiration-consumer"
    _create_running_task(sid, session_id=sid, max_tools=3)
    monkeypatch.setattr(studio, "list_mcp_servers", lambda **_kwargs: [])
    queued = studio.capture_mission_authority_envelope(sid, sid)
    studio.get_scoped_grant_store().bump_session_policy_revision(
        sid,
        new_policy="autonomous",
        reason="invalidate_http_queue",
    )
    client = _InspirationClient()

    stale_text = asyncio.run(
        studio._fetch_online_inspiration(
            client,
            "agent architecture",
            mission_env,
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=queued,
        )
    )
    assert "Offline / local mode" in stale_text
    assert client.urls == []
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 0

    fresh = studio.capture_mission_authority_envelope(sid, sid)
    live_text = asyncio.run(
        studio._fetch_online_inspiration(
            client,
            "agent architecture",
            mission_env,
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=fresh,
        )
    )
    assert "example/reference" in live_text
    assert "Bounded web signal" in live_text
    assert len(client.urls) == 2
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 2
    assert _audit_statuses("inspiration_http_github") == ["denied", "completed"]
    assert _audit_statuses("inspiration_http_duckduckgo") == ["denied", "completed"]

    # Revocation is checked live even when the captured generation itself is
    # current; it closes both fixed HTTP consumers through shared authority.
    current = studio.capture_mission_authority_envelope(sid, sid)
    studio.get_scoped_grant_store().revoke_scope(
        session_id=sid,
        task_id=sid,
        tool_names=["inspiration_http_github"],
        reason="revoke_external_research",
    )
    before_urls = list(client.urls)
    revoked_text = asyncio.run(
        studio._fetch_online_inspiration(
            client,
            "agent architecture",
            mission_env,
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=current,
        )
    )
    assert "Offline / local mode" in revoked_text
    assert client.urls == before_urls
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 2


def test_task_bound_inbound_mcp_bridge_reuses_captured_mission_authority(
    mission_env: Path,
) -> None:
    sid = "inbound-mcp"
    _create_running_task(sid, session_id=sid, max_tools=2)
    fresh = studio.capture_mission_authority_envelope(sid, sid)

    admitted = asyncio.run(
        studio._execute_mcp_stdio_tool(
            "oc_lint",
            {},
            "ExternalAgent",
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=fresh,
        )
    )
    assert admitted["ok"] is True
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 1

    queued = studio.capture_mission_authority_envelope(sid, sid)
    studio.get_scoped_grant_store().bump_session_policy_revision(
        sid,
        new_policy="autonomous",
        reason="invalidate_inbound_mcp_queue",
    )
    denied = asyncio.run(
        studio._execute_mcp_stdio_tool(
            "oc_lint",
            {},
            "ExternalAgent",
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=queued,
        )
    )
    assert denied["ok"] is False
    assert denied["error_code"] == "stale_policy_generation"
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 1
    assert _audit_statuses("mcp_bridge__oc_lint") == ["completed", "denied"]


def _register_mcp(alias: str, remote_tool: str) -> None:
    studio.upsert_mcp_server(
        {
            "alias": alias,
            "name": alias,
            "transport": "http",
            "url": f"https://{alias}.example/mcp",
            "enabled": True,
            "approval_policy": "auto",
        }
    )
    runtime_persistence.run_write_transaction(
        studio.STORAGE,
        studio.DB_PATH,
        lambda conn: conn.execute(
            "UPDATE mcp_servers SET tools_json=?, enabled_tools_json=?, updated_at=? WHERE alias=?",
            (
                json.dumps([{"name": remote_tool}]),
                json.dumps([remote_tool]),
                time.time(),
                alias,
            ),
        ),
    )


def test_mcp_browser_media_and_skill_consumers_cannot_refresh_stale_authority(
    mission_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "multi-surface"
    _create_running_task(sid, session_id=sid, max_tools=6)
    _register_mcp("playwright", "browser_navigate")
    _register_mcp("moneyprinter", "moneyprinter_video_generate")
    rpc_calls: list[str] = []

    async def _rpc(_client: Any, _server: dict[str, Any], method: str, params: dict[str, Any], **_kw: Any):
        assert method == "tools/call"
        rpc_calls.append(str(params["name"]))
        return {"content": [{"type": "text", "text": f"ran:{params['name']}"}], "isError": False}

    monkeypatch.setattr(studio, "_mcp_http_rpc", _rpc)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    script_calls: list[str] = []

    async def _script_inner(script: Path, _args: list[str], *, cwd: Path, timeout: float) -> dict[str, Any]:
        del cwd, timeout
        script_calls.append(script.name)
        return {"ok": True, "output": "script-authorized", "diff": "", "elapsed_ms": 1}

    monkeypatch.setattr(studio, "_execute_skill_script_inner", _script_inner)
    script = mission_env / "consumer.py"
    script.write_text("print('not directly executed')\n", encoding="utf-8")

    fresh = studio.capture_mission_authority_envelope(sid, sid)
    browser = asyncio.run(
        studio.execute_mcp_tool_call(
            None,
            "playwright",
            "browser_navigate",
            {"url": "https://example.test"},
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=fresh,
        )
    )
    media = asyncio.run(
        studio.execute_mcp_tool_call(
            None,
            "moneyprinter",
            "moneyprinter_video_generate",
            {"video_subject": "M5 authority"},
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=fresh,
        )
    )
    script_out = asyncio.run(
        studio._run_skill_script(
            script,
            [],
            cwd=mission_env,
            timeout=2,
            policy="autonomous",
            session_id=sid,
            task_id=sid,
            mission_envelope=fresh,
        )
    )
    assert browser["ok"] is True
    assert media["ok"] is True
    assert script_out == "script-authorized"
    assert rpc_calls == ["browser_navigate", "moneyprinter_video_generate"]
    assert script_calls == ["consumer.py"]

    stale = studio.capture_mission_authority_envelope(sid, sid)
    studio.get_scoped_grant_store().bump_session_policy_revision(
        sid,
        new_policy="autonomous",
        reason="invalidate_all_surface_queues",
    )
    for alias, remote_tool, arguments in (
        ("playwright", "browser_navigate", {"url": "https://stale.test"}),
        ("moneyprinter", "moneyprinter_video_generate", {"video_subject": "stale"}),
    ):
        with pytest.raises(PermissionError, match="invalidated"):
            asyncio.run(
                studio.execute_mcp_tool_call(
                    None,
                    alias,
                    remote_tool,
                    arguments,
                    policy="autonomous",
                    session_id=sid,
                    task_id=sid,
                    mission_envelope=stale,
                )
            )
    with pytest.raises(PermissionError, match="invalidated"):
        asyncio.run(
            studio._run_skill_script(
                script,
                [],
                cwd=mission_env,
                timeout=2,
                policy="autonomous",
                session_id=sid,
                task_id=sid,
                mission_envelope=stale,
            )
        )
    assert rpc_calls == ["browser_navigate", "moneyprinter_video_generate"]
    assert script_calls == ["consumer.py"]
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 3


def test_harness_api_zero_tool_budget_denies_before_write_dispatch(
    mission_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = studio.create_harness_session_db(
        title="API budget boundary",
        workspace_dir=str(mission_env),
        use_worktree=False,
        policy="autonomous",
        lead_model="fixture-model",
        critic_model="fixture-critic",
    )
    sid = str(sess["id"])

    async def _stream(
        _client: Any,
        _model: str,
        _messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        call = json.dumps(
            {"name": "write_file", "arguments": {"path": "must-not-exist.txt", "content": "denied"}}
        )
        yield {"type": "content", "text": f"<tool_call>{call}</tool_call>"}
        yield {
            "type": "usage",
            "decode_tps": 1.0,
            "prefill_tps": 1.0,
            "ttft": 0.01,
            "completion_tokens": 5,
            "cached_tokens": 0,
        }

    monkeypatch.setattr(studio, "stream_any_model", _stream)
    headers = {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}
    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/api/harness/run",
            headers=headers,
            json={
                "session_id": sid,
                "prompt": "write one file",
                "policy": "autonomous",
                "max_steps": 2,
                "budget": {
                    "max_steps": 4,
                    "max_tool_calls": 0,
                    "max_tokens": 1_000,
                    "max_elapsed_s": 60,
                    "max_recoveries": 1,
                },
            },
        )
    assert response.status_code == 200
    assert "event: budget_exceeded" in response.text
    assert not (mission_env / "must-not-exist.txt").exists()
    assert studio.get_task_state_machine().budgets.get_budget(sid).tool_calls_used == 0
    assert _audit_statuses("write_file")[-1] == "denied"
