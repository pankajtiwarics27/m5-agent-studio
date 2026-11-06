from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import moneyprinter_mcp
import moneyprinter_tool
from moneyprinter_mcp import MoneyPrinterMcpServer
from moneyprinter_tool import MoneyPrinterVideoResult

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_result(**overrides: Any) -> MoneyPrinterVideoResult:
    base: dict[str, Any] = {
        "status": "succeeded",
        "task_id": "task-1",
        "result": "/tmp/mpt/final-1.mp4",
        "videos": ("/tmp/mpt/final-1.mp4",),
        "error": None,
        "summary": {"task_id": "task-1", "result": {"state": "complete"}},
        "lineage_entry_ids": ("entry-1",),
        "lineage_error": None,
    }
    base.update(overrides)
    return MoneyPrinterVideoResult(**base)


def make_runner(seen: dict[str, Any], result: MoneyPrinterVideoResult | None = None, exc: Exception | None = None):
    async def fake_runner(payload: Any, **kwargs: Any) -> MoneyPrinterVideoResult:
        seen["payload"] = payload
        seen["kwargs"] = kwargs
        if exc is not None:
            raise exc
        assert result is not None
        return result

    return fake_runner


def server(environ: Mapping[str, str] | None = None, runner: Any = None, seen: dict[str, Any] | None = None) -> MoneyPrinterMcpServer:
    if runner is None:
        seen_ = seen if seen is not None else {}
        runner = make_runner(seen_, make_result())
    env = {"MONEYPRINTER_TURBO_DIR": "/opt/mpt"}
    if environ:
        env.update(environ)
    return MoneyPrinterMcpServer(environ=env, runner=runner)


def call_payload(result: Mapping[str, Any] | None) -> dict[str, Any]:
    assert result is not None
    content = result["content"]
    assert isinstance(content, list) and len(content) == 1
    block = content[0]
    assert block["type"] == "text"
    return json.loads(block["text"])


# ------------------------------------------------------------------ handshake


def test_initialize_reports_protocol_capabilities_and_server_info() -> None:
    srv = server()
    response = asyncio.run(srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
    assert response is not None
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == moneyprinter_mcp.PROTOCOL_VERSION
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == moneyprinter_mcp.SERVER_NAME
    assert result["serverInfo"]["version"] == moneyprinter_mcp.SERVER_VERSION
    assert "MONEYPRINTER_TURBO_DIR" in result["instructions"]


def test_ping_returns_empty_result() -> None:
    srv = server()
    response = asyncio.run(srv.handle_message({"jsonrpc": "2.0", "id": "p", "method": "ping"}))
    assert response == {"jsonrpc": "2.0", "id": "p", "result": {}}


def test_tools_list_exposes_the_single_video_generate_tool() -> None:
    srv = server()
    response = asyncio.run(srv.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    assert response is not None
    tools = response["result"]["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == moneyprinter_tool.TOOL_NAME
    assert tool["description"] == moneyprinter_tool.TOOL_DESCRIPTION
    schema = tool["inputSchema"]
    assert schema["required"] == ["video_subject"]
    assert schema["properties"]["video_aspect"]["enum"] == ["9:16", "16:9"]


def test_unknown_method_returns_method_not_found() -> None:
    srv = server()
    response = asyncio.run(srv.handle_message({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}))
    assert response is not None
    assert response["error"]["code"] == -32601


def test_unknown_method_notification_is_dropped() -> None:
    srv = server()
    response = asyncio.run(srv.handle_message({"jsonrpc": "2.0", "method": "resources/list"}))
    assert response is None


def test_notifications_initialized_are_accepted_without_response() -> None:
    srv = server()
    response = asyncio.run(
        srv.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    )
    assert response is None


def test_tools_call_notification_never_executes_runner() -> None:
    seen: dict[str, Any] = {}
    srv = server(runner=make_runner(seen, make_result()), seen=seen)
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "x"}},
            }
        )
    )
    assert response is None
    assert seen == {}


def test_non_object_message_returns_invalid_request() -> None:
    srv = server()
    response = asyncio.run(srv.handle_message(["not", "an", "object"]))
    assert response is not None
    assert response["error"]["code"] == -32600


def test_non_object_params_returns_invalid_params() -> None:
    srv = server()
    response = asyncio.run(
        srv.handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": "nope"})
    )
    assert response is not None
    assert response["error"]["code"] == -32602


# ------------------------------------------------------------------ tools/call


def test_unknown_tool_returns_invalid_params() -> None:
    srv = server()
    response = asyncio.run(
        srv.handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}})
    )
    assert response is not None
    assert response["error"]["code"] == -32602


def test_tools_call_arguments_must_be_an_object() -> None:
    srv = server()
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": "nope"},
            }
        )
    )
    assert response is not None
    assert response["error"]["code"] == -32602


def test_tools_call_fails_closed_without_turbo_dir() -> None:
    seen: dict[str, Any] = {}
    srv = MoneyPrinterMcpServer(environ={}, runner=make_runner(seen, make_result()))
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "x"}},
            }
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    payload = call_payload(result)
    assert payload["status"] == "not_configured"
    assert seen == {}


def test_tools_call_rejects_invalid_video_params_without_running() -> None:
    seen: dict[str, Any] = {}
    srv = server(runner=make_runner(seen, make_result()), seen=seen)
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_aspect": "9:16"}},
            }
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert call_payload(result)["status"] == "invalid_input"
    assert seen == {}


def test_tools_call_success_passes_env_and_returns_mp4_payload() -> None:
    seen: dict[str, Any] = {}
    runner = make_runner(seen, make_result())
    srv = server(
        environ={
            "MONEYPRINTER_TURBO_DIR": "/opt/mpt",
            "MONEYPRINTER_FFMPEG": "/opt/ffmpeg/bin/ffmpeg",
            "MONEYPRINTER_LINEAGE_ROOT": "/var/lib/lineage",
            "MONEYPRINTER_TIMEOUT_SECONDS": "900",
        },
        runner=runner,
        seen=seen,
    )
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": moneyprinter_tool.TOOL_NAME,
                    "arguments": {"video_subject": "M5 chips", "video_aspect": "16:9"},
                },
            }
        )
    )
    assert seen["kwargs"]["project_dir"] == "/opt/mpt"
    assert seen["kwargs"]["ffmpeg_path"] == "/opt/ffmpeg/bin/ffmpeg"
    assert seen["kwargs"]["lineage_store_root"] == "/var/lib/lineage"
    assert seen["kwargs"]["timeout_seconds"] == 900.0
    request = seen["payload"]
    assert request.video_subject == "M5 chips"
    assert request.video_aspect == "16:9"
    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    payload = call_payload(result)
    assert payload["status"] == "succeeded"
    assert payload["result"] == "/tmp/mpt/final-1.mp4"
    assert payload["task_id"] == "task-1"
    assert payload["lineage_entry_ids"] == ["entry-1"]


def test_tools_call_failure_status_maps_to_is_error() -> None:
    seen: dict[str, Any] = {}
    runner = make_runner(seen, make_result(status="failed", result=None, error="boom"))
    srv = server(runner=runner, seen=seen)
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "x"}},
            }
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert call_payload(result)["status"] == "failed"


def test_tools_call_preflight_error_maps_to_preflight_failed() -> None:
    seen: dict[str, Any] = {}
    runner = make_runner(seen, exc=moneyprinter_tool.MoneyPrinterPreflightError("ffmpeg not found"))
    srv = server(runner=runner, seen=seen)
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "x"}},
            }
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    payload = call_payload(result)
    assert payload["status"] == "preflight_failed"
    assert "ffmpeg" in payload["error"]


def test_tools_call_unexpected_exception_maps_to_internal_error_and_server_survives() -> None:
    seen: dict[str, Any] = {}
    runner = make_runner(seen, exc=RuntimeError("kaboom"))
    srv = server(runner=runner, seen=seen)
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "x"}},
            }
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert call_payload(result)["status"] == "internal_error"
    # The server remains responsive after an unexpected runner failure.
    pong = asyncio.run(srv.handle_message({"jsonrpc": "2.0", "id": 13, "method": "ping"}))
    assert pong == {"jsonrpc": "2.0", "id": 13, "result": {}}


def test_tools_call_rejects_concurrent_generation() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        seen: dict[str, Any] = {}

        async def slow_runner(payload: Any, **kwargs: Any) -> MoneyPrinterVideoResult:
            seen["started"] = True
            started.set()
            await release.wait()
            return make_result()

        srv = server(runner=slow_runner, seen=seen)
        first = asyncio.create_task(
            srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": "a",
                    "method": "tools/call",
                    "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "one"}},
                }
            )
        )
        await started.wait()
        second = await srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "b",
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "two"}},
            }
        )
        release.set()
        first_result = await first
        assert second is not None
        assert second["result"]["isError"] is True
        assert call_payload(second["result"])["status"] == "busy"
        assert first_result is not None
        assert first_result["result"]["isError"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 1800.0),
        ("   ", 1800.0),
        ("not-a-number", 1800.0),
        ("0", 1800.0),
        ("-5", 1800.0),
        ("45", 45.0),
        ("3600", 3600.0),
        ("99999", 3600.0),
    ],
)
def test_timeout_env_parsing_and_clamping(raw: str, expected: float) -> None:
    environ = {"MONEYPRINTER_TIMEOUT_SECONDS": raw}
    srv = MoneyPrinterMcpServer(environ=environ, runner=make_runner({}, make_result()))
    assert srv._resolve_timeout() == expected


def test_whitespace_only_turbo_dir_is_treated_as_unset() -> None:
    seen: dict[str, Any] = {}
    srv = MoneyPrinterMcpServer(environ={"MONEYPRINTER_TURBO_DIR": "   "}, runner=make_runner(seen, make_result()))
    response = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "x"}},
            }
        )
    )
    assert response is not None
    assert call_payload(response["result"])["status"] == "not_configured"
    assert seen == {}


# ------------------------------------------------------------------ framing


def test_encode_frame_is_single_line_json() -> None:
    srv = server()
    frame = srv.encode_frame({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert frame.endswith(b"\n")
    assert frame.count(b"\n") == 1
    assert json.loads(frame) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_readline_capped_roundtrip_and_eof() -> None:
    buf = BytesIO(b'{"a": 1}\n{"b": 2}\n')
    assert moneyprinter_mcp._readline_capped(buf, 1024) == b'{"a": 1}\n'
    assert moneyprinter_mcp._readline_capped(buf, 1024) == b'{"b": 2}\n'
    assert moneyprinter_mcp._readline_capped(buf, 1024) is None


def test_readline_capped_flags_oversized_frame() -> None:
    payload = b'{"big": "' + b"x" * 64 + b'"}\n'
    buf = BytesIO(payload)
    line = moneyprinter_mcp._readline_capped(buf, 16)
    assert line is not None
    assert len(line) > 16


def test_serve_stdio_full_exchange() -> None:
    seen: dict[str, Any] = {}
    runner = make_runner(seen, make_result())
    srv = server(runner=runner, seen=seen)
    frames = [
        {"jsonrpc": "2.0", "id": 100, "method": "initialize"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 101, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 102, "method": "ping"},
        {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {"name": moneyprinter_tool.TOOL_NAME, "arguments": {"video_subject": "stdio demo"}},
        },
    ]
    stdin = BytesIO(b"".join(json.dumps(frame).encode("utf-8") + b"\n" for frame in frames))
    stdout = BytesIO()
    rc = asyncio.run(srv.serve_stdio(stdin=stdin, stdout=stdout))
    assert rc == 0
    out_lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(out_lines) == 4  # notification produces no frame
    decoded = [json.loads(line) for line in out_lines]
    assert [frame["id"] for frame in decoded] == [100, 101, 102, 103]
    assert decoded[0]["result"]["protocolVersion"] == moneyprinter_mcp.PROTOCOL_VERSION
    assert decoded[1]["result"]["tools"][0]["name"] == moneyprinter_tool.TOOL_NAME
    assert decoded[2]["result"] == {}
    assert decoded[3]["result"]["isError"] is False
    assert seen["payload"].video_subject == "stdio demo"


def test_serve_stdio_reports_parse_errors_and_continues() -> None:
    srv = server()
    stdin = BytesIO(b"this is not json\n" + b'{"jsonrpc": "2.0", "id": 7, "method": "ping"}\n')
    stdout = BytesIO()
    rc = asyncio.run(srv.serve_stdio(stdin=stdin, stdout=stdout))
    assert rc == 0
    decoded = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert decoded[0]["error"]["code"] == -32700
    assert decoded[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_serve_stdio_exits_on_client_disconnect() -> None:
    srv = server()
    stdout = BytesIO()

    class ClosedStdout:
        def write(self, _data: bytes) -> int:
            raise BrokenPipeError("client gone")

        def flush(self) -> None:
            return None

    rc = asyncio.run(srv.serve_stdio(stdin=BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), stdout=ClosedStdout()))  # type: ignore[arg-type]
    assert rc == 0
    assert stdout.getvalue() == b""


# ------------------------------------------------------------------ e2e (real pipes, fake-free handshake only)


def test_stdio_end_to_end_handshake_and_tool_listing() -> None:
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    stdin_data = b"".join(json.dumps(frame).encode("utf-8") + b"\n" for frame in frames)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "moneyprinter_mcp.py")],
        input=stdin_data,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 3
    decoded = [json.loads(line) for line in lines]
    assert decoded[0]["result"]["serverInfo"]["name"] == moneyprinter_mcp.SERVER_NAME
    assert decoded[1]["result"]["tools"][0]["name"] == moneyprinter_tool.TOOL_NAME
    assert decoded[2]["result"] == {}
