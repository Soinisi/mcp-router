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

#: A broad search must not dump an unbounded catalog into the agent's context.
MAX_SEARCH_RESULTS = 50

#: Words ignored when a whitespace-separated search phrase is retried as an
#: OR of its terms. Only used after the literal regex matched nothing.
_SEARCH_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on",
    "with", "me", "my", "please", "search", "find", "tool", "tools",
}

_INSTRUCTIONS = (
    "This is an MCP router. You are connected to one server that fronts many "
    "others. Discover tools at runtime:\n"
    "  1. search_tools(regex_filter?, mcp_name?, full?) -> tools matching your "
    "filter. Pass a tool name or action keyword as regex_filter; pass mcp_name "
    "when you know the server. With neither it lists the servers (browse). "
    "This is normally your first call.\n"
    "  2. run_mcp_tool(mcp_name, mcp_tool, arguments) -> invoke one of them.\n"
    "get_installed_mcps lists server names, descriptions and reachability; use "
    "it only when you need that list or a reachability check."
)

_TOOLS: list[types.Tool] = [
    types.Tool(
        name="get_installed_mcps",
        description=(
            "List the MCP servers available to this connection (all registered "
            "servers, or the active scope's subset), optionally filtered by a "
            "regular expression over their names and descriptions. Use this to "
            "browse server names/descriptions or, with check=true, to report "
            "reachability and tool counts. For finding a tool, call "
            "search_tools instead. Returns each server's name, description and "
            "type."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "regex_filter": {
                    "type": "string",
                    "description": (
                        "Case-insensitive regex matched against server names and "
                        "descriptions."
                    ),
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
        name="search_tools",
        description=(
            "Search the tools exposed by the registered servers that the active "
            "scope allows. Pass regex_filter to match server and tool names and "
            "descriptions. With mcp_name, search that one server; without it, "
            "search every scope-visible server and tag each result with its "
            "mcp_name. A filter that matches a server name or description "
            "narrows the scan to that server. With neither mcp_name nor "
            "regex_filter, list the servers (browse). Results are compact "
            "{name, description, signature, mcp_name} summaries; set full=true "
            "to return the complete inputSchema instead. Cross-server results "
            "have the shape {tools: [...], unreachable_servers: [...]}. Pass "
            "mcp_name when you know the server, and always pass a regex_filter "
            "when you can: a narrow result is much cheaper than a full catalog."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "regex_filter": {
                    "type": "string",
                    "description": (
                        "Case-insensitive regex matched against server and tool "
                        "names and descriptions. Omit only to browse servers."
                    ),
                },
                "mcp_name": {
                    "type": "string",
                    "description": (
                        "A server name from get_installed_mcps. Omit to search "
                        "all scope-visible servers."
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "Return the complete inputSchema instead of the compact "
                        "signature. Use only when the signature is not enough."
                    ),
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="run_mcp_tool",
        description=(
            "Invoke a tool on a registered server, if the active scope allows "
            "it. Pass mcp_name and mcp_tool from search_tools, and arguments "
            "matching that tool's inputSchema. Returns the server's result "
            "verbatim."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mcp_name": {
                    "type": "string",
                    "description": "A server name from get_installed_mcps or search_tools.",
                },
                "mcp_tool": {
                    "type": "string",
                    "description": "A tool name from search_tools.",
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


def _matches(pattern: re.Pattern[str], *values: str | None) -> bool:
    """True when the regex matches any of the given (possibly missing) strings."""
    return any(value is not None and pattern.search(value) for value in values)


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


def _visible_server_summaries(manager: SessionManager) -> list[dict[str, Any]]:
    summaries = server_summaries(manager.config)
    if _scope is not None:
        summaries = [summary for summary in summaries if summary["name"] in _scope]
    return summaries


def _signature(tool: types.Tool) -> str:
    """A compact ``name(required:string, optional?:integer)`` signature."""
    schema = tool.inputSchema or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = []
    for name, prop in properties.items():
        kind = prop.get("type", "any") if isinstance(prop, dict) else "any"
        parts.append(f"{name}:{kind}" + ("" if name in required else "?"))
    return f"{tool.name}({', '.join(parts)})"


def _entry(tool: types.Tool, mcp_name: str, full: bool) -> dict[str, Any]:
    if full:
        entry = tool.model_dump(by_alias=True, exclude_none=True, mode="json")
    else:
        entry = {
            "name": tool.name,
            "description": tool.description,
            "signature": _signature(tool),
        }
    entry["mcp_name"] = mcp_name
    return entry


def _entries(tools: list[types.Tool], mcp_name: str, full: bool) -> list[dict[str, Any]]:
    return [_entry(tool, mcp_name, full) for tool in tools]


def _fallback_pattern(raw_filter: str | None) -> re.Pattern[str] | None:
    """A whitespace-separated phrase retried as an OR of its useful terms.

    Only used after the literal regex matched nothing, so it can never reduce
    the precision of a successful search.
    """
    if not raw_filter or not re.search(r"\s", raw_filter):
        return None
    terms = [
        term
        for term in re.findall(r"[A-Za-z0-9_]+", raw_filter)
        if term.lower() not in _SEARCH_STOPWORDS
    ]
    if len(terms) < 2:
        return None
    return re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)


def _no_match_message(raw_filter: str | None, mcp_name: str | None = None) -> str:
    where = f" on server {mcp_name!r}" if mcp_name else ""
    return (
        f"No tools matched regex_filter {raw_filter!r}{where}. Try one keyword "
        "(a tool name or an action), or pass mcp_name to search one server."
    )


def _too_many_message(count: int, raw_filter: str | None) -> str:
    return (
        f"Too many matches ({count} tools) for regex_filter {raw_filter!r}; the "
        f"limit is {MAX_SEARCH_RESULTS}. Narrow the filter (for example, add a "
        "tool name or action keyword) and try again."
    )


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
            if name == "search_tools":
                return await _search_tools(_manager, arguments)
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
    summaries = _visible_server_summaries(manager)
    if pattern is not None:
        summaries = [
            s
            for s in summaries
            if _matches(pattern, s["name"], s.get("description"))
        ]

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

    return _text(json.dumps(summaries, separators=(",", ":")))


async def _search_tools(manager: SessionManager, arguments: dict[str, Any]) -> types.CallToolResult:
    mcp_name = arguments.get("mcp_name")
    raw_filter = arguments.get("regex_filter")
    full = bool(arguments.get("full", False))
    pattern = _compile_filter(raw_filter)

    if mcp_name:
        _require_server_in_scope(mcp_name)
        session = await manager.session(mcp_name)
        result = await session.call(lambda s: s.list_tools())
        available = _filter_tools_by_scope(mcp_name, result.tools)
        if pattern is None:
            return _text(
                json.dumps(
                    _entries(available, mcp_name, full), separators=(",", ":")
                )
            )
        matched = [t for t in available if _matches(pattern, t.name, t.description)]
        if not matched:
            fallback = _fallback_pattern(raw_filter)
            if fallback is not None:
                matched = [
                    t for t in available if _matches(fallback, t.name, t.description)
                ]
        if not matched:
            return _error(_no_match_message(raw_filter, mcp_name))
        if len(matched) > MAX_SEARCH_RESULTS:
            return _error(_too_many_message(len(matched), raw_filter))
        return _text(json.dumps(_entries(matched, mcp_name, full), separators=(",", ":")))

    if pattern is None:
        return _text(json.dumps(_visible_server_summaries(manager), separators=(",", ":")))

    summaries = _visible_server_summaries(manager)
    fallback = _fallback_pattern(raw_filter)

    # Server-first narrowing: if the filter names or describes a server, only
    # that server's catalog is scanned. This keeps a known-server search cheap
    # and avoids spawning every child process.
    selected = [
        summary
        for summary in summaries
        if _matches(pattern, summary["name"], summary.get("description"))
    ]
    effective = pattern
    if not selected and fallback is not None:
        alternate = [
            summary
            for summary in summaries
            if _matches(fallback, summary["name"], summary.get("description"))
        ]
        if alternate:
            selected = alternate
            effective = fallback

    scan = selected or summaries
    include_all = bool(selected)
    catalogs: list[tuple[str, list[types.Tool]]] = []
    unreachable: list[dict[str, str]] = []
    for summary in scan:
        try:
            session = await manager.session(summary["name"])
            result = await session.call(lambda s: s.list_tools())
        except Exception as exc:  # noqa: BLE001 - one bad server must not fail a search
            unreachable.append(
                {
                    "mcp_name": summary["name"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        catalogs.append(
            (
                summary["name"],
                _filter_tools_by_scope(summary["name"], result.tools),
            )
        )

    def collect(match: re.Pattern[str] | None) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for name, tools in catalogs:
            for tool in tools:
                if match is not None and not _matches(
                    match, tool.name, tool.description
                ):
                    continue
                found.append(_entry(tool, name, full))
        return found

    entries = collect(None) if include_all else collect(effective)
    if not entries and not include_all and fallback is not None and effective is pattern:
        entries = collect(fallback)
    if not entries and not unreachable:
        return _error(_no_match_message(raw_filter))
    if len(entries) > MAX_SEARCH_RESULTS:
        return _error(_too_many_message(len(entries), raw_filter))
    return _text(
        json.dumps(
            {"tools": entries, "unreachable_servers": unreachable},
            separators=(",", ":"),
        )
    )


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
