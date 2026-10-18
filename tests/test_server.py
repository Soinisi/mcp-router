from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_router.config import ConfigError, LocalServer, RouterConfig
from mcp_router.sessions import SessionManager

CHILD = Path(__file__).parent / "child_server.py"


def _child_config() -> RouterConfig:
    server = LocalServer(type="local", command=sys.executable, args=[str(CHILD)])
    return RouterConfig(version=1, mcp_servers={"child": server})


async def test_session_manager_lists_and_calls():
    manager = SessionManager(_child_config())
    try:
        session = await manager.session("child")
        listed = await session.call(lambda s: s.list_tools())
        assert {"echo", "add"} <= {t.name for t in listed.tools}

        echoed = await session.call(lambda s: s.call_tool("echo", arguments={"text": "hi"}))
        assert echoed.isError is False
        assert echoed.content[0].text == "hi"

        summed = await session.call(lambda s: s.call_tool("add", arguments={"a": 2, "b": 3}))
        assert summed.isError is False
    finally:
        await manager.aclose()


async def test_unknown_server_raises():
    manager = SessionManager(_child_config())
    with pytest.raises(ConfigError, match="Unknown server"):
        await manager.session("nope")


async def test_end_to_end_via_stdio(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": {
                    "child": {
                        "type": "local",
                        "command": sys.executable,
                        "args": [str(CHILD)],
                    }
                },
            }
        )
    )
    env = {**os.environ, "MCP_ROUTER_CONFIG": str(cfg)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_router.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "get_installed_mcps",
                "get_mcp_tools",
                "run_mcp_tool",
            }

            installed = await session.call_tool("get_installed_mcps", {})
            data = json.loads(installed.content[0].text)
            assert data[0]["name"] == "child"

            child_tools = await session.call_tool("get_mcp_tools", {"mcp_name": "child"})
            names = {t["name"] for t in json.loads(child_tools.content[0].text)}
            assert {"echo", "add"} <= names

            result = await session.call_tool(
                "run_mcp_tool",
                {"mcp_name": "child", "mcp_tool": "echo", "arguments": {"text": "hi"}},
            )
            assert result.isError is False
            assert result.content[0].text == "hi"


async def test_end_to_end_error_is_returned_not_raised(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"version": 1, "mcp_servers": {}}))
    env = {**os.environ, "MCP_ROUTER_CONFIG": str(cfg)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_router.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            bad = await session.call_tool("get_mcp_tools", {"mcp_name": "ghost"})
            assert bad.isError is True
            assert "Unknown server" in bad.content[0].text
