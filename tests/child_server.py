"""A minimal MCP server used as a child in integration tests.

It advertises both tools and resources (a fixed resource plus a template), so
the router's resource discovery and read paths can be exercised end to end.
"""

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


@mcp.resource(
    "memory://readme",
    name="readme",
    description="The child server's readme document.",
    mime_type="text/plain",
)
def readme() -> str:
    return "hello from the child server"


@mcp.resource(
    "memory://greeting/{name}",
    name="greeting",
    description="A greeting for one person.",
    mime_type="text/plain",
)
def greeting(name: str) -> str:
    return f"hello {name}"


if __name__ == "__main__":
    mcp.run()
