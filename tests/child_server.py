"""A minimal MCP server used as a child in integration tests."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("child")


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    mcp.run()
