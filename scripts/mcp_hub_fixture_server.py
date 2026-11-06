#!/usr/bin/env python3
"""A tiny stdio MCP server the MCP hub walk can really connect to (UIV2-21).

The hub's Connected and Activity tabs have to show live status, discovered tools and test-call
results. A real remote server is not reachable from CI, so this fixture speaks just enough of the
2024-11-05 stdio protocol for the app's own `mcp_client` to initialize it, list its tools and call
them. It writes nothing and touches no network.

    python3 scripts/mcp_hub_fixture_server.py

It answers `initialize`, `tools/list` and `tools/call` for three tools:

* `echo_note`  — succeeds and echoes the note back;
* `fail_always` — returns `isError: true` with an explanation, so the hub can show a failed call;
* `slow_echo`  — pauses 1.6s first, so the loading state is real;
* `big_output` — returns ~4,000 characters, so the playground and the activity row have to scroll.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "echo_note",
        "description": "Echo a note back unchanged. Used by the MCP hub walk.",
        "inputSchema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
    {
        "name": "fail_always",
        "description": "Always fails, so the hub has a real failed call to display.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow_echo",
        "description": "Echo a note after a pause, so the hub has a visible loading state.",
        "inputSchema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
    {
        "name": "big_output",
        "description": "Return a long block of text.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name: str, arguments: dict) -> dict:
    if name == "echo_note":
        note = str(arguments.get("note") or "")
        return {"content": [{"type": "text", "text": f"echo: {note}"}], "isError": False}
    if name == "fail_always":
        return {
            "content": [{"type": "text", "text": "the fixture tool was asked to fail, and did"}],
            "isError": True,
        }
    if name == "slow_echo":
        import time

        time.sleep(1.6)
        return {"content": [{"type": "text", "text": f"slow echo: {arguments.get('note') or ''}"}], "isError": False}
    if name == "big_output":
        body = "".join(f"line {index:03d} of the long fixture output\n" for index in range(1, 101))
        return {"content": [{"type": "text", "text": body}], "isError": False}
    return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_id = message.get("id")
        if request_id is None:
            continue
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-hub-fixture", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            arguments = params.get("arguments") if isinstance(params, dict) else {}
            result = call_tool(str(params.get("name") or ""), arguments if isinstance(arguments, dict) else {})
        elif method == "ping":
            result = {}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
