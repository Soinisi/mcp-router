"""A tiny local MCP server used by the end-to-end tests.

Each tool returns a distinct sentinel so a test can prove which tool an LLM
actually selected through the router. ``HELLO_NONCE`` lets a test make the
``hello`` output unique per run.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hello")


@mcp.tool()
def hello(name: str) -> str:
    """Greet a person by name. Use this for greetings."""
    nonce = os.environ.get("HELLO_NONCE", "static-nonce")
    return f"hello, {name}! nonce={nonce}"


@mcp.tool()
def goodbye(name: str) -> str:
    """Say farewell to a person by name. Use this for farewells."""
    return f"goodbye, {name}! sentinel=GOODBYE_SENTINEL"


@mcp.tool()
def echo(text: str) -> str:
    """Echo text back unchanged. Use this when asked to echo."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()
