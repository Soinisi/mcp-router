"""A local MCP server using the legacy SSE transport, for transport tests."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sse-test", host="127.0.0.1", port=int(os.environ["SSE_PORT"]))


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"sse: {text}"


if __name__ == "__main__":
    mcp.run(transport="sse")
