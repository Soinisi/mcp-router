"""Deterministic Microsoft Learn coverage through the router (no LLM).

The LLM-driven Microsoft Learn cases can be answered from the model's own
knowledge, so this test exercises the remote transport and the router's
pass-through directly with an MCP client. It needs the network, hence ``e2e``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.e2e

LEARN_URL = "https://learn.microsoft.com/api/mcp"
LEARN_TOOLS = {
    "microsoft_docs_search",
    "microsoft_code_sample_search",
    "microsoft_docs_fetch",
}


async def test_microsoft_learn_through_router(tmp_path: Path):
    config = tmp_path / "router.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": {
                    "microsoftdocs": {
                        "type": "remote",
                        "url": LEARN_URL,
                        "transport": "http",
                        "description": "Microsoft Learn documentation",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    env = {**os.environ, "MCP_ROUTER_CONFIG": str(config)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_router.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            installed = await session.call_tool("get_installed_mcps", {})
            assert installed.isError is False
            assert "microsoftdocs" in installed.content[0].text

            tools = await session.call_tool(
                "get_mcp_tools", {"mcp_name": "microsoftdocs"}
            )
            assert tools.isError is False
            names = {item["name"] for item in json.loads(tools.content[0].text)}
            assert LEARN_TOOLS <= names

            result = await session.call_tool(
                "run_mcp_tool",
                {
                    "mcp_name": "microsoftdocs",
                    "mcp_tool": "microsoft_docs_search",
                    "arguments": {"query": "Model Context Protocol"},
                },
            )
            assert result.isError is False
            text = " ".join(
                block.text for block in result.content if hasattr(block, "text")
            )
            assert text.strip()
            assert "microsoft" in text.lower()
