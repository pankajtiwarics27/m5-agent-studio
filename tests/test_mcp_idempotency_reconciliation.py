"""Tests for M5-P3-05: MCP idempotency, action-ID binding, queryable-outcome reconciliation, safe pause, and central gate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

import app as studio
import execution_spine
import mcp_client
import scoped_grants
import side_effect_ledger

TEST_API_TOKEN = "test-mcp-studio-api-token-0123456789abcdef"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_TOKEN}"}


class _FakeClock:
    def __init__(self, initial: float = 1_750_000_000.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


@pytest.fixture()
def isolated_mcp_runtime(
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
    (ws / "README.md").write_text("# Demo\n", encoding="utf-8")

    clock = _FakeClock()
    monkeypatch.setattr(studio, "BASE_DIR", home)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_MCP_STDIO", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_PRIVATE_URLS", True)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", TEST_API_TOKEN)

    studio._MCP_HTTP_SESSIONS.clear()
    studio._PENDING_APPROVALS.clear()
    studio._RESOLVED_APPROVALS.clear()
    studio._RESOLVED_APPROVALS_TS.clear()
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._reset_execution_profile_controller(None)
    studio._reset_process_supervisor(None)
    studio._reset_scoped_grant_store(scoped_grants.ScopedGrantStore(store_dir, clock=clock))
    studio._reset_mcp_effect_store(mcp_client.McpExternalEffectStore(store_dir, clock=clock))
    studio.init_db()

    yield ws, clock

    studio._MCP_HTTP_SESSIONS.clear()
    studio._reset_mcp_effect_store(None)
    studio._reset_scoped_grant_store(None)
    studio._reset_process_supervisor(None)
    studio._reset_execution_profile_controller(None)


def test_connector_semantics_classification_and_explicit_unsupported_marking(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
) -> None:
    """Task 1 + AC 2: Unsupported idempotency is explicitly classified, persisted on sync, and surfaced in listings/prompts."""
    _ws, _clock = isolated_mcp_runtime

    raw_tools = [
        {
            "name": "search_issues",
            "description": "Search remote issues without mutating state.",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "purge_project",
            "description": "Destructive write even if mis-annotated readOnlyHint.",
            "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}},
            "annotations": {"readOnlyHint": True, "destructiveHint": True, "requiresApproval": True},
        },
        {
            "name": "create_comment",
            "description": "Append a comment with no remote idempotency key support.",
            "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}}},
        },
        {
            "name": "upsert_ticket",
            "description": "Create or update ticket with explicit client_mutation_id key.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "client_mutation_id": {"type": "string", "x-idempotency-key": True},
                },
            },
        },
        {
            "name": "create_issue",
            "description": "Non-idempotent issue creation with queryable receipt reconciliation.",
            "inputSchema": {"type": "object", "properties": {"external_ref": {"type": "string"}, "title": {"type": "string"}}},
            "x-reconciliation": {
                "query_tool": "search_issues",
                "mode": "resource_lookup",
                "query_arg_map": {"query": "external_ref"},
                "result_field": "items",
                "match_fields": ["external_ref", "title"],
                "authoritative_not_found": False,
            },
        },
    ]

    async def _sync_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["method"] == "tools/list"
        meta = body.get("params", {}).get("_meta", {})
        assert "idempotencyKey" not in meta
        assert "m5.studio/actionId" not in meta
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": raw_tools}})

    studio.upsert_mcp_server(
        {
            "alias": "tracker-prod",
            "name": "Tracker Prod",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "auto",
            "auto_sync": False,
        }
    )

    async def _run_sync() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_sync_handler)) as client:
            return await studio.sync_mcp_server_tools(client, "tracker-prod")

    synced = asyncio.run(_run_sync())
    by_name = {t["name"]: t for t in synced["tools"]}

    # 1. Read-only tool:
    assert by_name["search_issues"]["idempotency_supported"] is True
    assert by_name["search_issues"]["idempotency_status"] == "not_applicable_read_only"
    assert by_name["search_issues"]["idempotency_mechanism"] == "read_only"
    assert by_name["search_issues"]["replay_policy"] == "idempotent"

    # 2. Destructive override beats readOnlyHint fail-closed:
    assert by_name["purge_project"]["idempotency_supported"] is False
    assert by_name["purge_project"]["idempotency_status"] == "unsupported"
    assert by_name["purge_project"]["replay_policy"] == "non_idempotent"
    assert studio.mcp_tool_needs_approval("tracker-prod", "autonomous", "purge_project") is True

    # 3. Unsupported non-idempotent write explicitly marked (`AC 2`):
    assert by_name["create_comment"]["idempotency_supported"] is False
    assert by_name["create_comment"]["idempotency_status"] == "unsupported"
    assert by_name["create_comment"]["idempotency_mechanism"] == "unsupported"
    assert by_name["create_comment"]["replay_policy"] == "non_idempotent"

    # 4. Schema argument-level idempotency key (`Task 2`):
    assert by_name["upsert_ticket"]["idempotency_supported"] is True
    assert by_name["upsert_ticket"]["idempotency_status"] == "supported"
    assert by_name["upsert_ticket"]["idempotency_mechanism"] == "argument_field"
    # Even when idempotency_supported=True, blind replay stays disabled (`non_idempotent`) unless allow_keyed_replay=True.
    assert by_name["upsert_ticket"]["replay_policy"] == "non_idempotent"

    # 5. Reconcilable non-idempotent write:
    assert by_name["create_issue"]["idempotency_supported"] is False
    assert by_name["create_issue"]["idempotency_status"] == "unsupported"
    assert by_name["create_issue"]["reconciliation_supported"] is True
    assert by_name["create_issue"]["replay_policy"] == "reconcilable"

    assert set(synced["unsupported_idempotency_tools"]) == {"purge_project", "create_comment", "create_issue"}

    prompt = studio.build_active_mcp_tools_prompt()
    assert "`mcp__tracker-prod__search_issues` (Tracker Prod) [read-only]" in prompt
    assert "`mcp__tracker-prod__create_comment` (Tracker Prod) [idempotency=unsupported; non-idempotent write]" in prompt
    assert "`mcp__tracker-prod__upsert_ticket` (Tracker Prod) [idempotency=supported:argument_field]" in prompt
    assert "`mcp__tracker-prod__create_issue` (Tracker Prod) [idempotency=unsupported; non-idempotent write] [reconcilable]" in prompt


def test_action_id_and_idempotency_key_binding_across_http_stdio_and_inbound_bridge(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
    tmp_path: Path,
) -> None:
    """Task 2 + AC 2: Supported tools bind `_meta.idempotencyKey` + schema arg; unsupported writes omit fake `_meta.idempotencyKey`."""
    _ws, _clock = isolated_mcp_runtime

    captured_http: list[tuple[dict[str, str], dict[str, Any]]] = []

    async def _http_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_http.append(({k.lower(): v for k, v in request.headers.items()}, body))
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}},
        )

    studio.upsert_mcp_server(
        {
            "alias": "payments-api",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [
                {
                    "name": "charge_with_key",
                    "inputSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "amount_cents": {"type": "integer"},
                            "idempotency_key": {"type": "string", "x-idempotency-key": True},
                        },
                    },
                },
                {
                    "name": "legacy_wire_transfer",
                    "inputSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"amount_cents": {"type": "integer"}},
                    },
                },
            ],
        }
    )

    async def _exercise_http() -> tuple[dict[str, Any], dict[str, Any]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_http_handler)) as client:
            r_sup = await studio.execute_mcp_tool_call(
                client,
                "payments-api",
                "charge_with_key",
                {"amount_cents": 2500},
                policy="autonomous",
                session_id="sess-bind-1",
                action_id="act-pay-sup-1",
            )
            r_unsup = await studio.execute_mcp_tool_call(
                client,
                "payments-api",
                "legacy_wire_transfer",
                {"amount_cents": 2500},
                policy="autonomous",
                session_id="sess-bind-1",
                action_id="act-pay-unsup-1",
            )
            return r_sup, r_unsup

    res_sup, res_unsup = asyncio.run(_exercise_http())
    assert res_sup["ok"] is True
    assert res_sup["mcp_idempotency_supported"] is True
    assert res_sup["mcp_idempotency_status"] == "supported"
    assert res_sup["bound_idempotency_key"] == "act-pay-sup-1"

    assert res_unsup["ok"] is True
    assert res_unsup["mcp_idempotency_supported"] is False
    assert res_unsup["mcp_idempotency_status"] == "unsupported"
    assert res_unsup["bound_idempotency_key"] is None

    assert len(captured_http) == 2
    sup_headers, sup_body = captured_http[0]
    assert sup_headers["idempotency-key"] == "act-pay-sup-1"
    assert sup_headers["x-m5-idempotency-status"] == "supported"
    assert sup_body["params"]["arguments"]["idempotency_key"] == "act-pay-sup-1"
    assert sup_body["params"]["_meta"]["idempotencyKey"] == "act-pay-sup-1"
    assert sup_body["params"]["_meta"]["io.modelcontextprotocol/idempotencyKey"] == "act-pay-sup-1"
    assert sup_body["params"]["_meta"]["m5.studio/actionId"] == "act-pay-sup-1"
    assert sup_body["params"]["_meta"]["m5.studio/idempotencyStatus"] == "supported"

    unsup_headers, unsup_body = captured_http[1]
    assert unsup_headers["idempotency-key"] == "act-pay-unsup-1"
    assert unsup_headers["x-m5-idempotency-status"] == "unsupported"
    # Strict schema safety: no fabricated argument or fake `_meta.idempotencyKey` on unsupported tools!
    assert unsup_body["params"]["arguments"] == {"amount_cents": 2500}
    assert "idempotencyKey" not in unsup_body["params"]["_meta"]
    assert unsup_body["params"]["_meta"]["m5.studio/actionId"] == "act-pay-unsup-1"
    assert unsup_body["params"]["_meta"]["m5.studio/idempotencyStatus"] == "unsupported"

    # Stdio outbound MCP server receives `_meta` + bound argument on `tools/call`:
    stdio_log = tmp_path / "stdio_packets.jsonl"
    stdio_script = tmp_path / "mock_stdio_mcp.py"
    stdio_script.write_text(
        textwrap.dedent(
            f"""\
            import json, sys
            log_path = {str(stdio_log)!r}
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                msg = json.loads(line)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(msg) + "\\n")
                mid = msg.get("id")
                method = msg.get("method")
                if mid is None:
                    continue
                if method == "initialize":
                    res = {{"protocolVersion": "2024-11-05", "capabilities": {{"tools": {{}}}}}}
                elif method == "tools/call":
                    res = {{"content": [{{"type": "text", "text": "stdio-ok"}}], "isError": False}}
                else:
                    res = {{"tools": []}}
                sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": res}}) + "\\n")
                sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )
    studio.upsert_mcp_server(
        {
            "alias": "local-stdio",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(stdio_script)],
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [
                {
                    "name": "apply_patch",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "patch": {"type": "string"},
                            "request_id": {"type": "string", "x-idempotency-key": True},
                        },
                    },
                }
            ],
        }
    )
    stdio_res = asyncio.run(
        studio.execute_mcp_tool_call(
            None,  # type: ignore[arg-type]
            "local-stdio",
            "apply_patch",
            {"patch": "diff"},
            policy="autonomous",
            session_id="sess-stdio-1",
            action_id="act-stdio-patch-1",
        )
    )
    assert stdio_res["ok"] is True
    stdio_pkts = [json.loads(line) for line in stdio_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    call_pkt = next(p for p in stdio_pkts if p.get("method") == "tools/call")
    assert call_pkt["params"]["arguments"]["request_id"] == "act-stdio-patch-1"
    assert call_pkt["params"]["_meta"]["idempotencyKey"] == "act-stdio-patch-1"

    # Inbound `--mcp` bridge deduplicates duplicate `action_id` on `send_response`:
    hall = studio.create_hall_session("Idempotency council test", created_by="Tester")
    inbound_first = asyncio.run(
        studio._execute_mcp_stdio_tool(
            "send_response",
            {"session_id": hall["id"], "content": "One-time vote"},
            "InboundBot",
            action_id="act-inbound-vote-1",
        )
    )
    assert inbound_first["ok"] is True
    inbound_dup = asyncio.run(
        studio._execute_mcp_stdio_tool(
            "send_response",
            {"session_id": hall["id"], "content": "One-time vote"},
            "InboundBot",
            action_id="act-inbound-vote-1",
        )
    )
    assert inbound_dup["ok"] is False
    assert inbound_dup.get("replay_blocked") is True
    hall_state = studio.get_hall_session(hall["id"])
    ext_msgs = [m for m in hall_state["messages"] if m["content"] == "One-time vote"]
    assert len(ext_msgs) == 1


def test_queryable_outcome_reconciliation_prevents_duplicate_external_effect_on_recovery(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
) -> None:
    """Task 3 + AC 1: Remote write committed before HTTP 502 is reconciled via read-only query tool; recovery creates 0 duplicates."""
    _ws, _clock = isolated_mcp_runtime

    remote_issues: list[dict[str, Any]] = []
    rpc_log: list[tuple[str, dict[str, Any]]] = []
    fail_next_create_after_commit = True

    async def _tracker_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_next_create_after_commit
        body = json.loads(request.content.decode("utf-8"))
        params = body.get("params") or {}
        tname = str(params.get("name") or "")
        targs = dict(params.get("arguments") or {})
        rpc_log.append((tname, targs))

        if tname == "create_issue":
            issue = {
                "id": f"ISS-{len(remote_issues) + 1}",
                "external_ref": targs["external_ref"],
                "title": targs["title"],
            }
            remote_issues.append(issue)
            if fail_next_create_after_commit:
                fail_next_create_after_commit = False
                # Simulate reverse-proxy 502 AFTER upstream committed the issue!
                return httpx.Response(502, text="Bad Gateway after upstream commit")
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "structuredContent": {"issue": issue},
                        "content": [{"type": "text", "text": json.dumps(issue)}],
                        "isError": False,
                    },
                },
            )

        if tname == "find_issue":
            matches = [i for i in remote_issues if i["external_ref"] == targs.get("external_ref")]
            payload = {"found": bool(matches), "items": matches}
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "structuredContent": payload,
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                        "isError": False,
                    },
                },
            )

        return httpx.Response(400, text="unexpected tool")

    studio.upsert_mcp_server(
        {
            "alias": "linear-reconcile",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [
                {
                    "name": "create_issue",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"external_ref": {"type": "string"}, "title": {"type": "string"}},
                    },
                    "reconciliation": {
                        "query_tool": "find_issue",
                        "mode": "resource_lookup",
                        "query_arg_map": {"external_ref": "external_ref"},
                        "result_field": "items",
                        "match_fields": ["external_ref", "title"],
                        "authoritative_not_found": False,
                    },
                },
                {
                    "name": "find_issue",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {"external_ref": {"type": "string"}}},
                },
            ],
        }
    )

    sess = studio.create_harness_session_db(
        title="Recoverable MCP issue creation",
        workspace_dir=str(_ws),
        use_worktree=False,
        policy="autonomous",
        lead_model="local-test",
        critic_model="local-test",
    )
    sid = sess["id"]

    async def _run_crash_and_recovery() -> tuple[dict[str, Any], dict[str, Any]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_tracker_handler)) as client:
            # 1. Initial call hits HTTP 502 AFTER upstream created `ISS-1`, raising McpAmbiguousOutcomeError
            #    and pausing `sid` while recording `uncertain` in both DurableActionLedger and McpExternalEffectStore.
            with pytest.raises(mcp_client.McpAmbiguousOutcomeError):
                await studio.execute_mcp_tool_call(
                    client,
                    "linear-reconcile",
                    "create_issue",
                    {"external_ref": "outage-2026-10-31", "title": "DB failover alert"},
                    policy="autonomous",
                    session_id=sid,
                    action_id="act-linear-create-1",
                )
            assert studio.get_harness_session_db(sid)["status"] == "paused"

            # 2. Recovery replays `act-linear-create-1`: runtime auto-runs `find_issue`, reconciles `ISS-1`,
            #    and returns `already_committed=True, reconciled=True` WITHOUT calling `create_issue` a second time!
            recovery_replay = await studio.execute_mcp_tool_call(
                client,
                "linear-reconcile",
                "create_issue",
                {"external_ref": "outage-2026-10-31", "title": "DB failover alert"},
                policy="autonomous",
                session_id=sid,
                action_id="act-linear-create-1",
            )
            # 3. Because `act-linear-create-1` is now reconciled, a subsequent distinct write in `sid` is unblocked!
            second_issue = await studio.execute_mcp_tool_call(
                client,
                "linear-reconcile",
                "create_issue",
                {"external_ref": "followup-2026-10-31", "title": "Postmortem"},
                policy="autonomous",
                session_id=sid,
                action_id="act-linear-create-2",
            )
            return recovery_replay, second_issue

    r_rec, r2 = asyncio.run(_run_crash_and_recovery())

    # AC 1: Zero duplicate issue created for `outage-2026-10-31`!
    assert r_rec["ok"] is True
    assert r_rec["already_committed"] is True
    assert r_rec["reconciled"] is True
    assert "ISS-1" in r_rec["output"]
    assert "reconciled via `find_issue`" in r_rec["output"]

    assert r2["ok"] is True
    assert [i["external_ref"] for i in remote_issues] == ["outage-2026-10-31", "followup-2026-10-31"]
    assert [call[0] for call in rpc_log] == ["create_issue", "find_issue", "create_issue"]

    eff_rec = studio.get_mcp_effect_store().get_effect("act-linear-create-1")
    assert eff_rec is not None
    assert eff_rec.status == "reconciled_committed"
    assert eff_rec.remote_dispatches == 1


def test_authoritative_vs_non_authoritative_not_found_and_non_readonly_query_guard(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
) -> None:
    """Task 3 + AC 1: Negative lookup stays unresolved unless authoritative_not_found=True; mutating query_tool is rejected."""
    _ws, _clock = isolated_mcp_runtime

    dispatches = {"write_ev": 0, "write_auth": 0, "bad_mutating_probe": 0}
    fail_first = {"write_ev": True, "write_auth": True}

    async def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        tname = body["params"]["name"]
        if tname in ("write_ev", "write_auth"):
            if fail_first[tname]:
                fail_first[tname] = False
                # Upstream dropped BEFORE applying write!
                return httpx.Response(503, text="Service Unavailable before upstream write")
            dispatches[tname] += 1
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "created"}], "isError": False}},
            )
        if tname == "lookup_receipt":
            aid = body["params"]["arguments"]["action_id"]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"structuredContent": {"action_id": aid, "status": "not_found"}, "isError": False},
                },
            )
        if tname == "bad_mutating_probe":
            dispatches["bad_mutating_probe"] += 1
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": False}})
        return httpx.Response(400, text="unknown")

    studio.upsert_mcp_server(
        {
            "alias": "strict-reconcile",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [
                {
                    "name": "write_ev",
                    "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
                    "reconciliation": {
                        "query_tool": "lookup_receipt",
                        "mode": "action_receipt",
                        "authoritative_not_found": False,
                    },
                },
                {
                    "name": "write_auth",
                    "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
                    "reconciliation": {
                        "query_tool": "lookup_receipt",
                        "mode": "action_receipt",
                        "authoritative_not_found": True,
                    },
                },
                {
                    "name": "write_bad_probe",
                    "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
                    "reconciliation": {
                        "query_tool": "bad_mutating_probe",
                        "mode": "action_receipt",
                        "authoritative_not_found": True,
                    },
                },
                {
                    "name": "lookup_receipt",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {"action_id": {"type": "string"}}},
                },
                {
                    "name": "bad_mutating_probe",
                    # NOT read-only! Must never be invoked as a reconciliation probe!
                    "inputSchema": {"type": "object", "properties": {"action_id": {"type": "string"}}},
                },
            ],
        }
    )
    studio.upsert_mcp_server(
        {
            "alias": "strict-reconcile-auth",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [
                {
                    "name": "write_auth",
                    "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
                    "reconciliation": {
                        "query_tool": "lookup_receipt",
                        "mode": "action_receipt",
                        "authoritative_not_found": True,
                    },
                },
                {
                    "name": "lookup_receipt",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {"action_id": {"type": "string"}}},
                },
            ],
        }
    )

    async def _exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            # 1. Eventual-consistency (`authoritative_not_found=False`): `not_found` stays `unresolved` & blocks replay!
            with pytest.raises(mcp_client.McpAmbiguousOutcomeError):
                await studio.execute_mcp_tool_call(
                    client, "strict-reconcile", "write_ev", {"x": "1"},
                    policy="autonomous", session_id="sess-ev", action_id="act-ev-1",
                )
            with pytest.raises(side_effect_ledger.UncertainActionReplayError):
                await studio.execute_mcp_tool_call(
                    client, "strict-reconcile", "write_ev", {"x": "1"},
                    policy="autonomous", session_id="sess-ev", action_id="act-ev-1",
                )
            assert dispatches["write_ev"] == 0

            # 2. Authoritative receipt (`authoritative_not_found=True`): `not_found` resolves attempt 1 as not_applied and safely executes once!
            with pytest.raises(mcp_client.McpAmbiguousOutcomeError):
                await studio.execute_mcp_tool_call(
                    client, "strict-reconcile-auth", "write_auth", {"x": "1"},
                    policy="autonomous", session_id="sess-au", action_id="act-au-1",
                )
            au2 = await studio.execute_mcp_tool_call(
                client, "strict-reconcile-auth", "write_auth", {"x": "1"},
                policy="autonomous", session_id="sess-au", action_id="act-au-1",
            )
            assert au2["ok"] is True
            assert dispatches["write_auth"] == 1

            # 3. Misconfigured non-read-only `query_tool` is rejected before dispatch!
            studio.get_mcp_effect_store().stage_dispatch(
                action_id="act-bad-probe",
                session_id="sess-bad",
                task_id=None,
                epoch=0,
                semantics=studio._resolve_mcp_tool_semantics_for_tool("mcp__strict-reconcile__write_bad_probe"),  # type: ignore[arg-type]
                arguments={"x": "1"},
                bound_idempotency_key=None,
            )
            studio.get_mcp_effect_store().record_outcome("act-bad-probe", status="uncertain", error="502")
            rep = await studio.reconcile_mcp_effects_async(client, action_id="act-bad-probe", policy="autonomous")
            assert rep["unresolved"] == ["act-bad-probe"]
            assert rep["details"][0]["reason"] == "reconciliation_query_tool_not_read_only"
            assert dispatches["bad_mutating_probe"] == 0

    asyncio.run(_exercise())


def test_uncertain_non_idempotent_write_pauses_session_blocks_subsequent_writes_and_resolves_via_api(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 4: Uncertain non-idempotent write pauses live harness turn, blocks subsequent writes, allows read-only inspection, and resolves via API."""
    ws, _clock = isolated_mcp_runtime

    remote_calls: list[str] = []

    async def _ambiguous_webhook_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        tname = body["params"]["name"]
        remote_calls.append(tname)
        if tname == "send_slack_alert":
            return httpx.Response(504, text="Gateway Timeout after upstream webhook POST")
        if tname == "list_channels":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "#ops"}], "isError": False}},
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "sent"}], "isError": False}},
        )

    studio.upsert_mcp_server(
        {
            "alias": "slack-ops",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [
                {
                    "name": "send_slack_alert",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "api_token": {"type": "string"}}},
                },
                {
                    "name": "pin_message",
                    "inputSchema": {"type": "object", "properties": {"msg_id": {"type": "string"}}},
                },
                {
                    "name": "list_channels",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ],
        }
    )

    # Mock LLM emitting TWO tool calls in step 1: `send_slack_alert` (ambiguous 504) followed by `pin_message`.
    # The loop MUST pause immediately after `send_slack_alert` without dispatching `pin_message`!
    async def _fake_stream_any_model(
        _client: Any,
        _model: str,
        _messages: list[dict[str, Any]],
        **_kwargs: Any,
    ):
        tool_text = (
            '<tool_call>{"name": "mcp__slack-ops__send_slack_alert", '
            '"arguments": {"text": "Deploy started", "api_token": "xoxb-super-secret-token-999"}}</tool_call>\n'
            '<tool_call>{"name": "mcp__slack-ops__pin_message", "arguments": {"msg_id": "123"}}</tool_call>'
        )
        yield {"type": "content", "text": tool_text}
        yield {"type": "usage", "decode_tps": 50.0, "prefill_tps": 200.0, "ttft": 0.05, "completion_tokens": 30, "cached_tokens": 0}

    monkeypatch.setattr(studio, "stream_any_model", _fake_stream_any_model)

    with TestClient(studio.app, base_url="http://127.0.0.1:8787", headers=AUTH_HEADERS) as tc:
        studio.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(_ambiguous_webhook_handler))
        created = tc.post(
            "/api/harness/sessions",
            json={
                "title": "Slack alert turn",
                "workspace_dir": str(ws),
                "use_worktree": False,
                "policy": "autonomous",
                "lead_model": "local-test",
                "critic_model": "local-test",
            },
        )
        assert created.status_code == 200
        sid = created.json()["session"]["id"]

        run_resp = tc.post(
            "/api/harness/run",
            json={"session_id": sid, "prompt": "Send deploy alert and pin it", "max_steps": 3},
        )
        assert run_resp.status_code == 200
        sse_body = run_resp.text
        assert '"event": "paused"' in sse_body or "event: paused" in sse_body
        assert "uncertain_external_effect" in sse_body

        # `pin_message` was NEVER called because the turn paused immediately after `send_slack_alert` became uncertain!
        assert remote_calls == ["send_slack_alert"]
        assert studio.get_harness_session_db(sid)["status"] == "paused"

        # Inspect `/mcp-effects` endpoint + secret redaction on disk:
        eff_resp = tc.get(f"/api/harness/sessions/{sid}/mcp-effects")
        assert eff_resp.status_code == 200
        eff_data = eff_resp.json()
        assert eff_data["summary"]["uncertain"] == 1
        assert eff_data["summary"]["unsupported_idempotency_count"] == 1
        uncertain_aid = eff_data["effects"][0]["action_id"]
        assert eff_data["effects"][0]["redacted_args"]["api_token"] == "[REDACTED]"

        state_file = studio.HARNESS_STORE_DIR / "mcp_effects" / "state.json"
        raw_disk = state_file.read_text(encoding="utf-8")
        assert "xoxb-super-secret-token-999" not in raw_disk
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

        # A brand-new non-idempotent write in `sid` (`pin_message`) is blocked while `uncertain_aid` is unresolved:
        blocked_write = asyncio.run(
            studio.execute_mcp_tool_call(
                studio.app.state.http,
                "slack-ops",
                "pin_message",
                {"msg_id": "999"},
                policy="autonomous",
                session_id=sid,
                action_id="act-pin-after-uncertain",
            )
        )
        assert blocked_write["ok"] is False
        assert blocked_write["error_code"] == "uncertain_mcp_write_paused"
        assert blocked_write["safe_pause_required"] is True
        assert remote_calls == ["send_slack_alert"]

        # Read-only inspection tool (`list_channels`) on the SAME server IS permitted during pause:
        ro_ok = asyncio.run(
            studio.execute_mcp_tool_call(
                studio.app.state.http,
                "slack-ops",
                "list_channels",
                {},
                policy="autonomous",
                session_id=sid,
                action_id="act-ro-inspect",
            )
        )
        assert ro_ok["ok"] is True
        assert remote_calls == ["send_slack_alert", "list_channels"]

        # `/resume` fails closed while `uncertain_aid` is unresolved:
        res_fail = tc.post(f"/api/harness/sessions/{sid}/resume")
        assert res_fail.status_code == 409

        # Explicit operator resolution (`committed`) resolves the uncertain write, unblocks `/resume` and new writes,
        # while STILL preventing duplicate replay of `uncertain_aid` (`AC 1`)!
        resolve_resp = tc.post(
            f"/api/harness/sessions/{sid}/mcp-effects/{uncertain_aid}/resolve",
            json={"resolution": "committed", "reason": "Confirmed message visible in #ops"},
        )
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["effect"]["status"] == "reconciled_committed"

        with pytest.raises(side_effect_ledger.CommittedActionReplayError):
            asyncio.run(
                studio.execute_mcp_tool_call(
                    studio.app.state.http,
                    "slack-ops",
                    "send_slack_alert",
                    {"text": "Deploy started", "api_token": "xoxb-super-secret-token-999"},
                    policy="autonomous",
                    session_id=sid,
                    action_id=uncertain_aid,
                )
            )
        assert remote_calls == ["send_slack_alert", "list_channels"]

        # Subsequent distinct write (`pin_message`) is now unblocked:
        pin_ok = asyncio.run(
            studio.execute_mcp_tool_call(
                studio.app.state.http,
                "slack-ops",
                "pin_message",
                {"msg_id": "999"},
                policy="autonomous",
                session_id=sid,
                action_id="act-pin-after-resolved",
            )
        )
        assert pin_ok["ok"] is True
        assert remote_calls == ["send_slack_alert", "list_channels", "pin_message"]

        resume_ok = tc.post(f"/api/harness/sessions/{sid}/resume")
        assert resume_ok.status_code == 200


def test_central_gate_enforced_across_direct_rpc_stdio_opt_in_and_reconciliation_probes(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3: Direct RPC `tools/call` bypasses fail closed; stdio opt-in, allowlists, and grant revocation gate reconciliation probes."""
    ws, _clock = isolated_mcp_runtime

    wire_hits: list[str] = []

    async def _gate_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        wire_hits.append(body["params"]["name"])
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"structuredContent": {"status": "committed"}, "isError": False},
            },
        )

    srv = studio.upsert_mcp_server(
        {
            "alias": "gated-cloud",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "always_ask",
            "auto_sync": False,
            "tools": [
                {
                    "name": "deploy_release",
                    "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}}},
                    "reconciliation": {"query_tool": "get_release_status", "mode": "action_receipt"},
                },
                {
                    "name": "get_release_status",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {"action_id": {"type": "string"}}},
                },
            ],
        }
    )

    # 1. Direct wire-level `_mcp_http_rpc` and `_mcp_stdio_rpc` on `tools/call` raise DirectExecutorBypassError!
    async def _attempt_direct_http() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_gate_handler)) as client:
            await studio._mcp_http_rpc(client, srv, "tools/call", {"name": "deploy_release", "arguments": {"tag": "v1"}})

    with pytest.raises(execution_spine.DirectExecutorBypassError):
        asyncio.run(_attempt_direct_http())

    with pytest.raises(execution_spine.DirectExecutorBypassError):
        asyncio.run(
            studio._mcp_stdio_rpc(
                {"alias": "stdio-bypass", "transport": "stdio", "command": sys.executable, "args": ["-c", "pass"]},
                "tools/call",
                {"name": "ping", "arguments": {}},
            )
        )
    assert wire_hits == []

    # 2. `STUDIO_ALLOW_MCP_STDIO=False` is rejected at `authorize_tool_invocation` central policy gate:
    studio.upsert_mcp_server(
        {
            "alias": "stdio-gated",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [{"name": "ping", "annotations": {"readOnlyHint": True}}],
        }
    )
    monkeypatch.setattr(studio, "STUDIO_ALLOW_MCP_STDIO", False)
    auth_stdio = studio.authorize_tool_invocation("mcp__stdio-gated__ping", {}, "autonomous", approved=True)
    assert auth_stdio.allowed is False
    assert auth_stdio.error_code == "mcp_stdio_disabled"

    # 3. Reconciliation probes pass through `execute_mcp_tool_call` central gate (scoped grant + allowlist):
    g_store = studio.get_scoped_grant_store()
    grant = g_store.issue_grant(
        session_id="sess-gate-rec",
        capabilities=("external_mcp",),
        resource_scopes=("mcp:gated-cloud",),
        tool_names=("mcp__gated-cloud__deploy_release", "mcp__gated-cloud__get_release_status"),
        ttl_seconds=300.0,
        workspace_root=ws,
    )
    sem_deploy = studio._resolve_mcp_tool_semantics_for_tool("mcp__gated-cloud__deploy_release")
    assert sem_deploy is not None
    studio.get_mcp_effect_store().stage_dispatch(
        action_id="act-deploy-1",
        session_id="sess-gate-rec",
        task_id=None,
        epoch=0,
        semantics=sem_deploy,
        arguments={"tag": "v1.2.3"},
        bound_idempotency_key=None,
    )
    studio.get_mcp_effect_store().record_outcome("act-deploy-1", status="uncertain", error="502")

    async def _probe_denied_then_granted_then_revoked() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_gate_handler)) as client:
            # 3a. Revoke `mcp:gated-cloud` scope -> reconciliation probe is denied at central gate (`0` wire hits)!
            g_store.revoke_scope(
                session_id="sess-gate-rec",
                capabilities=["external_mcp"],
                resource_scopes=["mcp:gated-cloud"],
                reason="security_freeze",
                workspace_root=ws,
            )
            rep_revoked = await studio.reconcile_mcp_effects_async(
                client,
                action_id="act-deploy-1",
                policy="standard",
                approved=True,
                grant_source="scoped_grant",
                grant_id=grant.grant_id,
            )
            assert rep_revoked["unresolved"] == ["act-deploy-1"]
            assert wire_hits == []

            # 3b. Even with operator approval, removing `get_release_status` from `enabled_tools` denies the probe!
            studio.upsert_mcp_server(
                {
                    "alias": "gated-cloud",
                    "transport": "http",
                    "url": "https://93.184.216.34/mcp",
                    "approval_policy": "always_ask",
                    "enabled_tools": ["deploy_release"],  # `get_release_status` excluded from allowlist!
                    "auto_sync": False,
                }
            )
            rep_unlisted = await studio.reconcile_mcp_effects_async(
                client,
                action_id="act-deploy-1",
                policy="standard",
                approved=True,
                grant_source="operator_one_call",
            )
            assert rep_unlisted["unresolved"] == ["act-deploy-1"]
            assert wire_hits == []

    asyncio.run(_probe_denied_then_granted_then_revoked())


def test_corrupted_mcp_effect_store_fails_closed_and_single_file_studio_bundle_matches(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
    tmp_path: Path,
) -> None:
    """Fail-closed store integrity (HTTP 503) and single-file `studio.py` parity check."""
    ws, _clock = isolated_mcp_runtime

    # Corrupt `mcp_effects/state.json` on disk -> fails closed with McpEffectStoreIntegrityError / HTTP 503:
    eff_dir = studio.HARNESS_STORE_DIR / "mcp_effects"
    eff_dir.mkdir(parents=True, exist_ok=True)
    (eff_dir / "state.json").write_text("{corrupted-json", encoding="utf-8")
    studio._reset_mcp_effect_store(None)

    sess = studio.create_harness_session_db(
        title="Corrupt store check",
        workspace_dir=str(ws),
        use_worktree=False,
        policy="standard",
        lead_model="local-test",
        critic_model="local-test",
    )
    with pytest.raises(mcp_client.McpEffectStoreIntegrityError):
        studio.get_mcp_effect_store().list_effects()

    with TestClient(studio.app, base_url="http://127.0.0.1:8787", headers=AUTH_HEADERS) as tc:
        resp_503 = tc.get(f"/api/harness/sessions/{sess['id']}/mcp-effects")
        assert resp_503.status_code == 503

    # Verify single-file `studio.py` exposes the M5-P3-05 symbols cleanly in isolated mode (`python -I`):
    import subprocess

    studio_bundle = Path(__file__).resolve().parent.parent / "studio.py"
    check_code = (
        "import importlib.util, pathlib, sys; "
        f"spec = importlib.util.spec_from_file_location('studio_bundle', {str(studio_bundle)!r}); "
        "mod = importlib.util.module_from_spec(spec); sys.modules['studio_bundle'] = mod; spec.loader.exec_module(mod); "
        "sem = mod.classify_mcp_tool_semantics({'alias': 'x', 'transport': 'http'}, {'name': 'w'}, remote_tool='w'); "
        "assert sem.idempotency_supported is False and sem.idempotency_status == 'unsupported'; "
        "assert hasattr(mod, 'McpExternalEffectStore') and hasattr(mod, 'reconcile_mcp_effects_async')"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", check_code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_api_reconcile_endpoint_and_worker_lease_takeover_without_duplicate_effects(
    isolated_mcp_runtime: tuple[Path, _FakeClock],
) -> None:
    """Task 3 + AC 1: `/mcp-effects/reconcile` and `WorkerLeaseCoordinator` takeover reconcile remote state without re-sending writes."""
    ws, _clock = isolated_mcp_runtime

    remote_deployments: list[str] = []
    first_drop = True

    async def _deploy_handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_drop
        body = json.loads(request.content.decode("utf-8"))
        tname = body["params"]["name"]
        targs = body["params"]["arguments"]
        if tname == "trigger_deploy":
            remote_deployments.append(targs["release_id"])
            if first_drop:
                first_drop = False
                return httpx.Response(502, text="Gateway timeout after upstream deployment started")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": False}})
        if tname == "get_deploy_receipt":
            found = targs.get("release_id") in remote_deployments
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "structuredContent": {
                            "found": found,
                            "release_id": targs.get("release_id"),
                            "status": "committed" if found else "not_found",
                        },
                        "isError": False,
                    },
                },
            )
        return httpx.Response(400, text="unknown")

    studio.upsert_mcp_server(
        {
            "alias": "deploy-cloud",
            "transport": "http",
            "url": "https://93.184.216.34/mcp",
            "approval_policy": "auto",
            "auto_sync": False,
            "tools": [
                {
                    "name": "trigger_deploy",
                    "inputSchema": {"type": "object", "properties": {"release_id": {"type": "string"}}},
                    "reconciliation": {
                        "query_tool": "get_deploy_receipt",
                        "mode": "resource_lookup",
                        "query_arg_map": {"release_id": "release_id"},
                        "match_fields": ["release_id"],
                    },
                },
                {
                    "name": "get_deploy_receipt",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {"release_id": {"type": "string"}}},
                },
            ],
        }
    )

    with TestClient(studio.app, base_url="http://127.0.0.1:8787", headers=AUTH_HEADERS) as tc:
        studio.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(_deploy_handler))
        sess = studio.create_harness_session_db(
            title="Deploy takeover test",
            workspace_dir=str(ws),
            use_worktree=False,
            policy="autonomous",
            lead_model="local-test",
            critic_model="local-test",
        )
        sid = sess["id"]
        sm = studio.get_task_state_machine()
        sm.ensure_root_task(sid, session_id=sid, title="Deploy", workspace_dir=str(ws), policy="autonomous")
        coord = studio.get_worker_lease_coordinator(ws)
        w1 = f"wrk-{studio._PROCESS_BOOT_ID}-alpha"
        w2 = f"wrk-{studio._PROCESS_BOOT_ID}-beta"
        claim1 = coord.acquire_or_takeover(sid, w1, lease_ttl_s=30.0)

        with pytest.raises(mcp_client.McpAmbiguousOutcomeError):
            asyncio.run(
                studio.execute_mcp_tool_call(
                    studio.app.state.http,
                    "deploy-cloud",
                    "trigger_deploy",
                    {"release_id": "rel-2026-10-31"},
                    policy="autonomous",
                    session_id=sid,
                    task_id=sid,
                    epoch=claim1.epoch_record.epoch,
                    worker_id=w1,
                    action_id="act-deploy-rel-1",
                )
            )

        assert remote_deployments == ["rel-2026-10-31"]

        # Call explicit `/mcp-effects/reconcile` endpoint:
        rec_resp = tc.post(f"/api/harness/sessions/{sid}/mcp-effects/reconcile", json={})
        assert rec_resp.status_code == 200
        rec_json = rec_resp.json()
        assert rec_json["report"]["verified_committed"] == ["act-deploy-rel-1"]
        assert rec_json["summary"]["reconciled_committed"] == 1
        assert remote_deployments == ["rel-2026-10-31"]

        # Worker 2 takes over after lease expiry without needing `known_action_args`, and replay creates 0 duplicates:
        coord.store.expire_stale_leases(now=claim1.epoch_record.lease_expires_at + 10.0)
        claim2 = coord.acquire_or_takeover(
            sid,
            w2,
            lease_ttl_s=30.0,
            now=claim1.epoch_record.lease_expires_at + 10.0,
        )
        replay = asyncio.run(
            studio.execute_mcp_tool_call(
                studio.app.state.http,
                "deploy-cloud",
                "trigger_deploy",
                {"release_id": "rel-2026-10-31"},
                policy="autonomous",
                session_id=sid,
                task_id=sid,
                epoch=claim2.epoch_record.epoch,
                worker_id=w2,
                action_id="act-deploy-rel-1",
            )
        )
        assert replay["ok"] is True
        assert replay["reconciled"] is True
        assert replay["already_committed"] is True
        assert remote_deployments == ["rel-2026-10-31"]



# --- durable-record redaction (review follow-up) ------------------------------

_CREDENTIAL_ARG_KEYS = [
    "Authorization",
    "authorization",
    "authentication",
    "oauth",
    "authToken",
    "accessToken",
    "bearerToken",
    "refreshToken",
    "sessionKey",
    "X-Api-Key",
    "apiKey",
    "API_KEY",
    "clientSecret",
    "secret",
    "password",
    "privateKey",
    "cookie",
    "set-cookie",
    "webhook_url",
    "X-Auth-Token",
]

_BENIGN_ARG_KEYS = [
    "order_id",
    "user_name",
    "quantity",
    "note",
    "max_tokens_value",  # a compound that is not a credential name
    "idempotency_key",
]


@pytest.mark.parametrize("key", _CREDENTIAL_ARG_KEYS)
def test_credential_argument_names_are_redacted_whatever_their_spelling(key: str) -> None:
    """`Authorization`, `X-Api-Key` and `accessToken` used to be stored verbatim.

    The old match required the token to be delimited by `_` or `-` on both sides, so
    `auth` lost to `orization` and camelCase compounds matched nothing at all.
    """
    root = Path(tempfile.mkdtemp())
    store = mcp_client.McpExternalEffectStore(root)
    semantics = mcp_client.classify_mcp_tool_semantics(
        {"alias": "srv", "transport": "http"}, {"name": "create"}
    )
    store.stage_dispatch(
        action_id="act-redact",
        session_id="sess",
        task_id=None,
        epoch=0,
        semantics=semantics,
        arguments={key: "LEAKME-VALUE"},
        bound_idempotency_key="act-redact",
        known_secret_values=("LEAKME-VALUE",),
    )
    state = json.loads((root / "mcp_effects" / "state.json").read_text(encoding="utf-8"))
    stored = state["effects"]["act-redact"]["redacted_args"]
    assert stored[key] == "[REDACTED]", stored
    assert "LEAKME-VALUE" not in json.dumps(state)


@pytest.mark.parametrize("key", _BENIGN_ARG_KEYS)
def test_ordinary_argument_names_survive_redaction(key: str) -> None:
    store = mcp_client.McpExternalEffectStore(Path(tempfile.mkdtemp()))
    semantics = mcp_client.classify_mcp_tool_semantics(
        {"alias": "srv", "transport": "http"}, {"name": "create"}
    )
    record = store.stage_dispatch(
        action_id="act-benign",
        session_id="sess",
        task_id=None,
        epoch=0,
        semantics=semantics,
        arguments={key: "A-1"},
        bound_idempotency_key="act-benign",
    )
    assert record.redacted_args[key] == "A-1"


def test_remote_text_is_scrubbed_before_it_is_persisted() -> None:
    """`last_error` and `reconciled_output` carried remote-controlled text unscrubbed."""
    root = Path(tempfile.mkdtemp())
    store = mcp_client.McpExternalEffectStore(root)
    semantics = mcp_client.classify_mcp_tool_semantics(
        {"alias": "srv", "transport": "http"}, {"name": "create"}
    )
    store.stage_dispatch(
        action_id="act-text",
        session_id="sess",
        task_id=None,
        epoch=0,
        semantics=semantics,
        arguments={"order": "A-1"},
        bound_idempotency_key="act-text",
    )
    store.record_outcome(
        "act-text",
        status="uncertain",
        output="ok, caller used Bearer tok-abcdef123",
        error="upstream rejected Bearer tok-abcdef123 for key sk-live-REGISTERED",
        known_secret_values=("sk-live-REGISTERED",),
    )
    raw = (root / "mcp_effects" / "state.json").read_text(encoding="utf-8")
    assert "tok-abcdef123" not in raw
    assert "sk-live-REGISTERED" not in raw
    assert "[REDACTED]" in raw


def test_reconciliation_text_is_scrubbed_before_it_is_persisted() -> None:
    root = Path(tempfile.mkdtemp())
    store = mcp_client.McpExternalEffectStore(root)
    semantics = mcp_client.classify_mcp_tool_semantics(
        {"alias": "srv", "transport": "http"}, {"name": "create"}
    )
    store.stage_dispatch(
        action_id="act-rec",
        session_id="sess",
        task_id=None,
        epoch=0,
        semantics=semantics,
        arguments={"order": "A-1"},
        bound_idempotency_key="act-rec",
    )
    store.record_reconciliation(
        "act-rec",
        verdict="verified_committed",
        reconciled_by="operator used Bearer tok-abcdef123",
        reconciled_output='{"Authorization": "Bearer tok-abcdef123"}',
        known_secret_values=(),
    )
    raw = (root / "mcp_effects" / "state.json").read_text(encoding="utf-8")
    assert "tok-abcdef123" not in raw


def test_correlation_metadata_is_only_bound_to_tools_call() -> None:
    """A future `tools/list` call site cannot pick up an `actionId` by accident."""
    semantics = mcp_client.classify_mcp_tool_semantics(
        {"alias": "srv", "transport": "http"}, {"name": "send", "annotations": {"idempotentHint": True}}
    )
    params = {"name": "send", "arguments": {"x": 1}}
    bound, key = mcp_client.bind_tool_call_params(
        params, action_id="act-1", semantics=semantics, method="tools/list"
    )
    assert bound == params
    assert key is None

    bound_call, key_call = mcp_client.bind_tool_call_params(
        params, action_id="act-1", semantics=semantics
    )
    assert bound_call["_meta"]["m5.studio/actionId"] == "act-1"
    assert key_call == "act-1"
