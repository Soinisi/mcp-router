from __future__ import annotations

import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HELLO = Path(__file__).with_name("hello_server.py")
NONCE = "test-nonce-123"


async def test_hello_server_exposes_three_tools_with_distinct_sentinels():
    params = StdioServerParameters(
        command=sys.executable, args=[str(HELLO)], env={"HELLO_NONCE": NONCE}
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.list_tools()
            assert {t.name for t in listed.tools} == {"hello", "goodbye", "echo"}

            greeted = await session.call_tool("hello", {"name": "Ada"})
            assert greeted.isError is False
            assert f"nonce={NONCE}" in greeted.content[0].text

            farewell = await session.call_tool("goodbye", {"name": "Ada"})
            assert farewell.isError is False
            assert "GOODBYE_SENTINEL" in farewell.content[0].text

            echoed = await session.call_tool("echo", {"text": "hi"})
            assert echoed.isError is False
            assert echoed.content[0].text == "echo: hi"


async def test_nonce_comes_from_the_environment_not_a_constant():
    params = StdioServerParameters(
        command=sys.executable, args=[str(HELLO)], env={"HELLO_NONCE": "other-nonce"}
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            greeted = await session.call_tool("hello", {"name": "Ada"})
            assert "other-nonce" in greeted.content[0].text
            assert NONCE not in greeted.content[0].text
