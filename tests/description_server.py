"""An MCP child with deliberately controlled tool descriptions.

Used by ``test_description_filters.py`` to prove that the router's regex filter
matches tool descriptions as well as names. ``ping`` deliberately has no
docstring, so its description is ``None`` and the filter must not crash on it.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("descriptions")


@mcp.tool()
def list_users(limit: int = 10) -> list[str]:
    """Fetch every registered account from the directory."""
    return ["ada", "grace"][:limit]


@mcp.tool()
def summarize(text: str) -> str:
    """Condense a long document into its key points."""
    return text[:20]


@mcp.tool()
def ping() -> str:
    return "pong"


if __name__ == "__main__":
    mcp.run()
