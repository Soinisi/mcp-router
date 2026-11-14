"""A child server with more tools than the search result cap.

Used by ``test_search_tools.py`` to prove that a broad cross-server search is
refused instead of dumping an unbounded catalog into the agent's context.
"""

from __future__ import annotations

import asyncio

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOL_COUNT = 55

TOOLS = [
    types.Tool(
        name=f"sample_tool_{index}",
        description="A sample tool used by the router search cap test.",
        inputSchema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": [],
        },
    )
    for index in range(TOOL_COUNT)
]


def build_server() -> Server:
    server = Server("many-tools")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict | None
    ) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=name)]
        )

    return server


async def _serve() -> None:
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_serve())
