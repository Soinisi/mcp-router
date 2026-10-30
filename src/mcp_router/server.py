"""The router's MCP surface: exactly three read/run tools.

Using the low-level server (rather than FastMCP) lets us return a child's
``CallToolResult`` verbatim, so the agent sees exactly what it would have seen
had it connected to that server directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .config import ConfigError, load_config
from .registry import server_summaries
from .sessions import DEFAULT_TIMEOUT, SessionManager

_INSTRUCTIONS = (
    "This is an MCP router. You are connected to one server that fronts many "
    "others. Discover tools at runtime:\n"
    "  1. get_installed_mcps -> the servers available to you.\n"
    "  2. get_mcp_tools(mcp_name) -> the tools one server exposes.\n"
    "  3. run_mcp_tool(mcp_name, mcp_tool, arguments) -> invoke one of them."
)

_TOOLS: list[types.Tool] = [
    types.Tool(
        name="get_installed_mcps",
        description=(
            "List the MCP servers registered with this router, optionally "
            "filtered by a regular expression over their names. Call this first "
            "to discover what is available. Returns each server's name, "
            "description and type. Set check=true to also connect and report "
            "reachability and tool counts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "regex_filter": {
                    "type": "string",
                    "description": "Case-insensitive regex matched against server names.",
                },
                "check": {
                    "type": "boolean",
                    "description": "If true, connect to each server and report reachability.",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="get_mcp_tools",
        description=(
            "List the tools exposed by a single registered server, as that "
            "server would return them from tools/list (name, description and "
            "inputSchema). Call this after get_installed_mcps and before "
            "run_mcp_tool so you know how to build the arguments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mcp_name": {
                    "type": "string",
                    "description": "A server name from get_installed_mcps.",
                },
                "regex_filter": {
                    "type": "string",
                    "description": "Case-insensitive regex matched against tool names.",
                },
            },
            "required": ["mcp_name"],
        },
    ),
    types.Tool(
        name="run_mcp_tool",
        description=(
            "Invoke a tool on a registered server. Pass mcp_name and mcp_tool "
            "from the previous steps, and arguments matching that tool's "
            "inputSchema. Returns the server's result verbatim."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mcp_name": {
                    "type": "string",
                    "description": "A server name from get_installed_mcps.",
                },
                "mcp_tool": {
                    "type": "string",
                    "description": "A tool name from get_mcp_tools.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments for the tool, matching its inputSchema.",
                },
            },
            "required": ["mcp_name", "mcp_tool"],
        },
    ),
]

_manager: SessionManager | None = None
_timeout: float = DEFAULT_TIMEOUT
_scope_name: str | None = None
_scope: dict[str, list[str] | None] | None = None


def _text(content: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=content)])


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], isError=True
    )


def _compile_filter(pattern: str | None) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid regex_filter {pattern!r}: {exc}") from exc


def _active_scope_label() -> str:
    return _scope_name or os.environ.get("MCP_ROUTER_SCOPE") or "<unknown>"


def _require_server_in_scope(mcp_name: str) -> None:
    if _scope is not None and mcp_name not in _scope:
        raise ConfigError(
            f"Server {mcp_name!r} is not available in scope "
            f"{_active_scope_label()!r}."
        )


def _scope_tools(mcp_name: str) -> list[str] | None:
    if _scope is None:
        return None
    return _scope.get(mcp_name)


def _require_tool_in_scope(mcp_name: str, mcp_tool: str) -> None:
    allowed = _scope_tools(mcp_name)
    if allowed is not None and mcp_tool not in allowed:
        raise ConfigError(
            f"Tool {mcp_tool!r} is not available on server {mcp_name!r} in scope "
            f"{_active_scope_label()!r}."
        )


def _filter_tools_by_scope(mcp_name: str, tools: list[types.Tool]) -> list[types.Tool]:
    allowed = _scope_tools(mcp_name)
    if allowed is None:
        return tools
    allowed_set = set(allowed)
    return [tool for tool in tools if tool.name in allowed_set]


@asynccontextmanager
async def _lifespan(server: Server) -> AsyncIterator[dict[str, Any]]:
    global _manager, _scope
    loaded = load_config()
    if not loaded.sources:
        raise ConfigError(
            "No router config found. Create one with `mcp-router add` or at "
            "~/.config/mcp-router/config.yaml."
        )
    active_scope = _scope_name if _scope_name is not None else os.environ.get("MCP_ROUTER_SCOPE")
    _scope = loaded.config.resolve_scope(active_scope)
    _manager = SessionManager(loaded.config, environ=loaded.environ, timeout=_timeout)
    try:
        yield {"config": loaded.config, "manager": _manager, "scope": _scope}
    finally:
        await _manager.aclose()
        _manager = None
        _scope = None


def build_server() -> Server:
    server = Server("mcp-router", lifespan=_lifespan, instructions=_INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        assert _manager is not None
        arguments = arguments or {}
        try:
            if name == "get_installed_mcps":
                return await _get_installed_mcps(_manager, arguments)
            if name == "get_mcp_tools":
                return await _get_mcp_tools(_manager, arguments)
            if name == "run_mcp_tool":
                return await _run_mcp_tool(_manager, arguments)
            return _error(f"Unknown tool {name!r}.")
        except (ConfigError, ValueError, TimeoutError) as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001 - surface any transport failure
            return _error(f"{type(exc).__name__}: {exc}")

    return server


async def _get_installed_mcps(manager: SessionManager, arguments: dict[str, Any]) -> types.CallToolResult:
    pattern = _compile_filter(arguments.get("regex_filter"))
    check = bool(arguments.get("check", False))
    summaries = server_summaries(manager.config)
    if _scope is not None:
        summaries = [s for s in summaries if s["name"] in _scope]
    if pattern is not None:
        summaries = [s for s in summaries if pattern.search(s["name"])]

    if check:
        for summary in summaries:
            try:
                session = await manager.session(summary["name"])
                result = await session.call(lambda s: s.list_tools())
                summary["reachable"] = True
                summary["tool_count"] = len(
                    _filter_tools_by_scope(summary["name"], result.tools)
                )
            except Exception as exc:  # noqa: BLE001
                summary["reachable"] = False
                summary["error"] = f"{type(exc).__name__}: {exc}"

    return _text(json.dumps(summaries, indent=2))


async def _get_mcp_tools(manager: SessionManager, arguments: dict[str, Any]) -> types.CallToolResult:
    mcp_name = arguments.get("mcp_name")
    if not mcp_name:
        return _error("mcp_name is required.")
    _require_server_in_scope(mcp_name)
    pattern = _compile_filter(arguments.get("regex_filter"))
    session = await manager.session(mcp_name)
    result = await session.call(lambda s: s.list_tools())
    tools = result.tools
    tools = _filter_tools_by_scope(mcp_name, tools)
    if pattern is not None:
        tools = [t for t in tools if pattern.search(t.name)]
    payload = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    return _text(json.dumps(payload, indent=2))


async def _run_mcp_tool(manager: SessionManager, arguments: dict[str, Any]) -> types.CallToolResult:
    mcp_name = arguments.get("mcp_name")
    mcp_tool = arguments.get("mcp_tool")
    if not mcp_name or not mcp_tool:
        return _error("mcp_name and mcp_tool are required.")
    _require_server_in_scope(mcp_name)
    _require_tool_in_scope(mcp_name, mcp_tool)
    tool_args = arguments.get("arguments") or {}
    if not isinstance(tool_args, dict):
        return _error("arguments must be an object.")
    session = await manager.session(mcp_name)
    return await session.call(lambda s: s.call_tool(mcp_tool, arguments=tool_args))


async def run_server(timeout: float = DEFAULT_TIMEOUT, scope: str | None = None) -> None:
    global _timeout, _scope_name
    _timeout = timeout
    _scope_name = scope
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
