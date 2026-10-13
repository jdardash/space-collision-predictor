"""Model Context Protocol server over stdio for the SDA orbital engine.

A dependency-free JSON-RPC 2.0 server speaking the MCP stdio transport
(newline-delimited JSON on stdin and stdout). It exposes the same typed tools
as :mod:`sda.agent`, so a desktop assistant gets the identical catalog, the
identical validation and the identical disclaimer as the HTTP front door.

Implemented methods
-------------------
``initialize``              handshake, advertises the ``tools`` capability
``notifications/initialized`` acknowledged silently (no response, per spec)
``ping``                    liveness
``tools/list``              the tool catalogue with JSON schemas
``tools/call``              run one tool and return its result as text content

Running it
----------
::

    python -m sda.mcp_server

Nothing is printed to stdout except JSON-RPC frames; diagnostics go to stderr,
because anything else on stdout corrupts the transport.

Attaching it to Claude Desktop
------------------------------
Add this to ``claude_desktop_config.json`` (macOS:
``~/Library/Application Support/Claude/claude_desktop_config.json``; Windows:
``%APPDATA%\\Claude\\claude_desktop_config.json``), then restart the app::

    {
      "mcpServers": {
        "sda": {
          "command": "python",
          "args": ["-m", "sda.mcp_server"],
          "env": {"PYTHONPATH": "C:/path/to/space-collision-predictor/src"}
        }
      }
    }

Attaching it to Claude Code
---------------------------
From the repository root::

    claude mcp add sda -- .venv/Scripts/python -m sda.mcp_server

or, on a POSIX machine, ``claude mcp add sda -- .venv/bin/python -m sda.mcp_server``.
Verify with ``/mcp`` inside a session; the tools appear as ``sda:list_satellites``
and so on.

The catalog
-----------
The server seeds its TLE store from the bundled offline demo catalog so it works
with no network. Feed it real elements with the ``SDA_MCP_TLE_FILE`` environment
variable, which is read once at startup and ingested through
``TLEStore.load_from_text()`` like every other TLE path in this package.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from sda.agent import DISCLAIMER, ToolContext, execute_tool, tool_catalogue
from sda.tle_store import TLEStore

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "space-collision-predictor"
SERVER_VERSION = "0.3.0"

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_context: ToolContext | None = None


def _build_store() -> TLEStore:
    """Seed a catalog: the file named by SDA_MCP_TLE_FILE, else the demo set."""
    store = TLEStore()
    path = os.environ.get("SDA_MCP_TLE_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                store.load_from_text(handle.read())
            return store
        except OSError as exc:
            print(f"sda.mcp_server: could not read {path}: {exc}", file=sys.stderr)

    from sda.demo_tles import DEMO_TLE_TEXT

    store.load_from_text(DEMO_TLE_TEXT)
    return store


def get_context() -> ToolContext:
    """Process-wide tool context, built on first use."""
    global _context
    if _context is None:
        _context = ToolContext(store=_build_store())
    return _context


def set_context(context: ToolContext | None) -> None:
    """Replace the tool context (tests inject their own catalog)."""
    global _context
    _context = context


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _mcp_tools() -> list[dict[str, Any]]:
    """The agent catalogue in MCP's tool shape (inputSchema, not parameters)."""
    tools: list[dict[str, Any]] = []
    for tool in tool_catalogue():
        description = tool["description"]
        if tool["client_action"]:
            description += (
                " This tool returns an action for the caller's viewer to carry "
                "out; the server does not execute it."
            )
        tools.append(
            {
                "name": tool["name"],
                "description": description,
                "inputSchema": tool["parameters"],
            }
        )
    return tools


def _handle_initialize(request_id: Any, _params: dict[str, Any]) -> dict[str, Any]:
    return _result(
        request_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Tools over an SGP4 conjunction-analysis engine. Every distance "
                "is km, every speed km/s, every angle degrees, every time UTC "
                "ISO 8601, and every state vector Earth-Centered Inertial "
                "(TEME). " + DISCLAIMER
            ),
        },
    )


def _handle_tools_list(request_id: Any, _params: dict[str, Any]) -> dict[str, Any]:
    return _result(request_id, {"tools": _mcp_tools()})


def _handle_tools_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error(request_id, INVALID_PARAMS, "tools/call requires a tool name")

    raw_arguments = params.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

    invocation = execute_tool(name, arguments, get_context())
    if invocation.ok:
        text = json.dumps(invocation.to_dict()["result"], indent=2, sort_keys=True)
        return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})

    return _result(
        request_id,
        {
            "content": [{"type": "text", "text": f"Tool error: {invocation.error}"}],
            "isError": True,
        },
    )


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications.

    Exposed so tests (and any embedding host) can drive the server without
    spawning a process or touching stdio.
    """
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "message must be a JSON object")

    request_id = message.get("id")
    method = message.get("method")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    if not isinstance(method, str):
        return _error(request_id, INVALID_REQUEST, "missing method")

    if method.startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            return _handle_initialize(request_id, params)
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _handle_tools_list(request_id, params)
        if method == "tools/call":
            return _handle_tools_call(request_id, params)
    except Exception as exc:
        return _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    return _error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout

    for line in source:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError as exc:
            response: dict[str, Any] | None = _error(
                None, PARSE_ERROR, f"invalid JSON: {exc}"
            )
        else:
            response = handle_message(message)
        if response is None:
            continue
        sink.write(json.dumps(response) + "\n")
        sink.flush()
    return 0


def main() -> int:
    """Entry point for ``python -m sda.mcp_server``."""
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
