"""A low-level MCP child whose advertised surface is chosen by argv.

FastMCP always registers resource handlers, so every FastMCP server advertises
``resources`` even when it exposes none. Capability-gating tests need a server
that genuinely advertises only a subset, which the low-level ``Server`` gives
us.

Usage: ``python surface_server.py {tools|resources|both|none}``.
"""

from __future__ import annotations

import sys

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

MODE = sys.argv[1] if len(sys.argv) > 1 else "tools"

server = Server("surface")


if MODE in {"tools", "both"}:

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="ping",
                description="Return pong.",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        return [types.TextContent(type="text", text="pong")]


if MODE in {"resources", "both"}:

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri="surface://doc",
                name="doc",
                description="A surface document.",
                mimeType="text/plain",
            ),
            types.Resource(
                uri="surface://doc_secret",
                name="doc_secret",
                description="A sibling document sharing the doc URI prefix.",
                mimeType="text/plain",
            ),
        ]

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                uriTemplate="surface://item/{id}",
                name="item",
                description="One surface item by id.",
                mimeType="text/plain",
            )
        ]

    @server.read_resource()
    async def read_resource(uri) -> list[ReadResourceContents]:
        return [ReadResourceContents(content=f"contents of {uri}", mime_type="text/plain")]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
