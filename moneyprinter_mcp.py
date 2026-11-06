"""moneyprinter_mcp.py — optional stdio MCP server for the MoneyPrinterTurbo tool.

This module exposes the Part 1 Studio tool contract
(:mod:`moneyprinter_tool`) as a single-purpose
[Model Context Protocol](https://modelcontextprotocol.io) server speaking
newline-delimited JSON-RPC 2.0 over stdin/stdout. It is the cross-harness
entry point that lets any MCP client which can launch a stdio server —
DeepSeek Harness (``@deepseek-ai/dsh-mcp-client``), M5 Agent Studio's own
MCP client, or another conforming host — call the MoneyPrinterTurbo CLI
under the same validated contract the Studio uses.

Trust boundaries, all deny-by-default:

* **No network.** The server binds stdin/stdout only. TTS, LLM, template,
  and search traffic is the MoneyPrinterTurbo CLI's own upstream concern;
  this module never opens a socket.
* **No implicit project.** The CLI runs inside ``MONEYPRINTER_TURBO_DIR``;
  when the variable is unset or unusable, tool calls fail closed instead of
  guessing a checkout.
* **No protocol on the content channel.** stdout carries JSON-RPC frames
  only; all diagnostics go to stderr and never echo credentials (none are
  read, but environment values are still kept out of log lines).
* **Bounded input.** Each input line is capped at ``MAX_MESSAGE_BYTES``;
  oversized frames are rejected instead of being buffered without limit.
* **Bounded concurrency.** At most one video generation runs at a time; a
  second request fails fast with ``isError`` instead of stacking renders
  on one machine.
* **Capped, timed output.** The child process reuses
  :func:`moneyprinter_tool.run_moneyprinter_video`, so stdout/stderr
  capture caps, the ffmpeg preflight, and the timeout policy are identical
  to the in-process contract.

Configuration (all optional except ``MONEYPRINTER_TURBO_DIR``):

``MONEYPRINTER_TURBO_DIR``
    Absolute path of the MoneyPrinterTurbo checkout the CLI runs in.
    Required; the tool call fails closed without it.
``MONEYPRINTER_FFMPEG``
    Optional path of the ``ffmpeg`` executable for the preflight probe.
``MONEYPRINTER_TIMEOUT_SECONDS``
    Optional wall-clock budget for one generation, in seconds.
    Default 1800; clamped to (0, 3600]. Malformed values fall back to the
    default and are logged on stderr.
``MONEYPRINTER_LINEAGE_ROOT``
    Optional directory for the artifact lineage store. Defaults to the
    Studio's standard lineage location.

Protocol notes: the server advertises revision ``2025-06-18`` and answers
``initialize``, ``ping``, ``tools/list``, and ``tools/call``.
``notifications/initialized`` and ``notifications/cancelled`` are accepted
and ignored (no cancellation of an in-flight render is promised — the
client's own tool timeout governs). Tool-level failures (missing config,
invalid params, ffmpeg preflight, CLI failure, busy server) are reported
as ``isError`` results so the calling model can read and react; JSON-RPC
errors are reserved for protocol faults (parse errors, unknown method,
unknown tool, non-object arguments). Message framing is one JSON object
per line with a trailing newline; stdout is flushed after every frame.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Awaitable, BinaryIO, Callable, Mapping

import moneyprinter_tool

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "m5-moneyprinterturbo-mcp"
SERVER_VERSION = "1.0.0"

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 1800.0
MAX_TIMEOUT_SECONDS = 3600.0

_STDERR_TAG = "[moneyprinter-mcp]"

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

_TOOL_SPEC = moneyprinter_tool.MONEYPRINTER_VIDEO_TOOL

Runner = Callable[..., Awaitable[moneyprinter_tool.MoneyPrinterVideoResult]]


class _UnknownMethod(Exception):
    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method


class _InvalidParams(Exception):
    pass


def _message_id(message: object) -> object:
    if isinstance(message, Mapping):
        return message.get("id")
    return None


def _error_response(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result_response(request_id: object, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _tool_error_result(tool: str, status: str, message: str) -> dict[str, Any]:
    payload = {"tool": tool, "status": status, "error": message}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": True,
    }


def _log_line(message: str) -> None:
    """Write one stderr diagnostic. Never echoes environment values."""
    line = f"{_STDERR_TAG} {message}\n"
    try:
        try:
            sys.stderr.buffer.write(line.encode("utf-8", errors="replace"))
            sys.stderr.buffer.flush()
        except AttributeError:
            sys.stderr.write(line)
    except (OSError, ValueError):
        pass


class MoneyPrinterMcpServer:
    """Single-tool MCP server core; transport-agnostic and fully testable."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        runner: Runner | None = None,

    ) -> None:
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._runner = runner or moneyprinter_tool.run_moneyprinter_video
        self._busy = False

    # ------------------------------------------------------------------
    # Framing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def encode_frame(message: Mapping[str, Any]) -> bytes:
        return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def handle_message(self, message: object) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message; return a response or ``None``."""
        request_id = _message_id(message)
        is_notification = isinstance(message, Mapping) and "id" not in message
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return _error_response(request_id, _INVALID_REQUEST, "expected a JSON-RPC 2.0 object.")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            if is_notification:
                return None
            return _error_response(request_id, _INVALID_REQUEST, "method must be a non-empty string.")
        if is_notification and not method.startswith("notifications/"):
            # A request without an id is a notification; never run side effects.
            return None
        params = message.get("params")
        if params is not None and not isinstance(params, Mapping):
            if is_notification:
                return None
            return _error_response(request_id, _INVALID_PARAMS, "params must be a JSON object.")
        try:
            result = await self._dispatch(method, params or {})
        except _UnknownMethod:
            if is_notification:
                return None
            return _error_response(request_id, _METHOD_NOT_FOUND, f"unknown method: {method}")
        except _InvalidParams as exc:
            if is_notification:
                return None
            return _error_response(request_id, _INVALID_PARAMS, str(exc))
        if is_notification:
            return None
        return _result_response(request_id, result)

    async def _dispatch(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Single-tool server for MoneyPrinterTurbo video generation. "
                    "The tool runs the local MoneyPrinterTurbo CLI; configure "
                    "MONEYPRINTER_TURBO_DIR before calling. Rendering can take "
                    "minutes and requires ffmpeg on PATH."
                ),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": _TOOL_SPEC["name"],
                        "description": _TOOL_SPEC["description"],
                        "inputSchema": _TOOL_SPEC["input_schema"],
                    }
                ]
            }
        if method == "tools/call":
            return await self._handle_tools_call(params)
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return {}
        raise _UnknownMethod(method)

    # ------------------------------------------------------------------
    # tools/call
    # ------------------------------------------------------------------
    async def _handle_tools_call(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or tool_name != _TOOL_SPEC["name"]:
            raise _InvalidParams(
                f"unknown tool: {tool_name!r}; this server exposes only {_TOOL_SPEC['name']!r}."
            )
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise _InvalidParams("arguments must be a JSON object.")

        project_dir = (self._environ.get("MONEYPRINTER_TURBO_DIR") or "").strip()
        if not project_dir:
            return _tool_error_result(
                _TOOL_SPEC["name"],
                "not_configured",
                "MONEYPRINTER_TURBO_DIR is not set; point it at a MoneyPrinterTurbo checkout.",
            )
        if self._busy:
            return _tool_error_result(
                _TOOL_SPEC["name"],
                "busy",
                "a MoneyPrinterTurbo generation is already running; wait for it to finish.",
            )

        ffmpeg_path = (self._environ.get("MONEYPRINTER_FFMPEG") or "").strip() or None
        lineage_root = (self._environ.get("MONEYPRINTER_LINEAGE_ROOT") or "").strip() or None
        timeout = self._resolve_timeout()

        try:
            request = moneyprinter_tool.validate_video_params(arguments)
        except moneyprinter_tool.MoneyPrinterInputError as exc:
            return _tool_error_result(_TOOL_SPEC["name"], "invalid_input", str(exc))

        self._busy = True
        try:
            result = await self._runner(
                request,
                project_dir=project_dir,
                ffmpeg_path=ffmpeg_path,
                timeout_seconds=timeout,
                lineage_store_root=lineage_root,
            )
        except moneyprinter_tool.MoneyPrinterInputError as exc:
            return _tool_error_result(_TOOL_SPEC["name"], "invalid_input", str(exc))
        except moneyprinter_tool.MoneyPrinterToolError as exc:
            # MoneyPrinterPreflightError (ffmpeg / project dir) and other tool errors.
            return _tool_error_result(_TOOL_SPEC["name"], "preflight_failed", str(exc))
        except Exception as exc:  # never let an unexpected error kill the server loop
            _log_line(f"runner raised {type(exc).__name__}")
            return _tool_error_result(
                _TOOL_SPEC["name"],
                "internal_error",
                f"moneyprinter runner failed with {type(exc).__name__}.",
            )
        finally:
            self._busy = False

        payload = result.to_mapping()
        is_error = payload.get("status") != "succeeded"
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        }

    def _resolve_timeout(self) -> float:
        raw = (self._environ.get("MONEYPRINTER_TIMEOUT_SECONDS") or "").strip()
        if not raw:
            return DEFAULT_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except ValueError:
            _log_line("MONEYPRINTER_TIMEOUT_SECONDS is malformed; using default.")
            return DEFAULT_TIMEOUT_SECONDS
        if value <= 0:
            _log_line("MONEYPRINTER_TIMEOUT_SECONDS must be positive; using default.")
            return DEFAULT_TIMEOUT_SECONDS
        return min(value, MAX_TIMEOUT_SECONDS)

    # ------------------------------------------------------------------
    # stdio pump
    # ------------------------------------------------------------------
    async def serve_stdio(
        self,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> int:
        in_buf: BinaryIO = stdin if stdin is not None else sys.stdin.buffer
        out_buf: BinaryIO = stdout if stdout is not None else sys.stdout.buffer
        while True:
            line = await asyncio.to_thread(_readline_capped, in_buf, MAX_MESSAGE_BYTES)
            if line is None:
                return 0
            stripped = line.rstrip(b"\n")
            if not stripped.strip():
                continue
            if len(stripped) > MAX_MESSAGE_BYTES:
                _safe_write(out_buf, self.encode_frame(_error_response(None, _PARSE_ERROR, "message exceeds the 4 MiB limit.")))
                continue
            try:
                message = json.loads(stripped)
            except (json.JSONDecodeError, UnicodeDecodeError):
                _safe_write(out_buf, self.encode_frame(_error_response(None, _PARSE_ERROR, "message is not valid JSON.")))
                continue
            response = await self.handle_message(message)
            if response is not None:
                if not _safe_write(out_buf, self.encode_frame(response)):
                    return 0  # client went away


def _safe_write(out: BinaryIO, frame: bytes) -> bool:
    try:
        out.write(frame)
        out.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def _readline_capped(buf: BinaryIO, limit: int) -> bytes | None:
    """Read one \\n-terminated line, at most ``limit`` bytes + 1.

    Returns ``None`` on EOF before any byte, the oversized line (limit + 1
    bytes) when the frame exceeds the cap, or the full line otherwise.
    Byte-at-a-time reads ride the buffered reader's block cache, which is
    fine for control messages that are a few KiB at most.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = buf.read(1)
        if not chunk:
            return b"".join(chunks) if chunks else None
        chunks.append(chunk)
        total += len(chunk)
        if chunk == b"\n":
            return b"".join(chunks)
        if total > limit:
            # Drain to end-of-line so the next frame starts clean.
            while chunk != b"\n":
                chunk = buf.read(1)
                if not chunk:
                    break
            return b"".join(chunks)


def main(argv: list[str] | None = None) -> int:
    _ = argv
    server = MoneyPrinterMcpServer()
    try:
        return asyncio.run(server.serve_stdio())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
