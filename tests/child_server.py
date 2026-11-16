"""A minimal MCP server used as a child in integration tests.

It advertises tools, resources (a fixed resource plus a template) and prompts,
so the router's discovery, read and render paths can be exercised end to end.
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


@mcp.prompt(
    name="summarize",
    description="Ask for a summary of a text.",
)
def summarize(text: str, style: str = "concise") -> str:
    return f"Summarize the following text in a {style} style:\n\n{text}"


@mcp.prompt(
    name="review",
    description="Ask for a review of the current change.",
)
def review() -> str:
    return "Review the current change and list any risks."


if __name__ == "__main__":
    mcp.run()
