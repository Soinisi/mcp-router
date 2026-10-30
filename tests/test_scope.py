"""End-to-end coverage for scopes: per-connection server/tool allow-lists."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CHILD = Path(__file__).with_name("child_server.py")


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "router.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": {
                    "alpha": {
                        "type": "local",
                        "command": sys.executable,
                        "args": [str(CHILD)],
                    },
                    "beta": {
                        "type": "local",
                        "command": sys.executable,
                        "args": [str(CHILD)],
                    },
                },
                "scopes": {
                    "alpha-only": {"alpha": None},
                    "echo-only": {"alpha": ["echo"]},
                },
            }
        ),
        encoding="utf-8",
    )
    return config


@asynccontextmanager
async def _router(config: Path, scope: str | None):
    env = {**os.environ, "MCP_ROUTER_CONFIG": str(config)}
    if scope is not None:
        env["MCP_ROUTER_SCOPE"] = scope
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_router.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def test_scope_filters_servers(tmp_path: Path):
    config = _write_config(tmp_path)
    async with _router(config, "alpha-only") as session:
        installed = await session.call_tool("get_installed_mcps", {})
        assert installed.isError is False
        text = installed.content[0].text
        assert "alpha" in text
        assert "beta" not in text

        hidden = await session.call_tool("get_mcp_tools", {"mcp_name": "beta"})
        assert hidden.isError is True
        assert "not available in scope" in hidden.content[0].text

        denied = await session.call_tool(
            "run_mcp_tool",
            {"mcp_name": "beta", "mcp_tool": "echo", "arguments": {"text": "x"}},
        )
        assert denied.isError is True

        allowed = await session.call_tool(
            "run_mcp_tool",
            {"mcp_name": "alpha", "mcp_tool": "echo", "arguments": {"text": "hi"}},
        )
        assert allowed.isError is False
        assert allowed.content[0].text == "hi"


async def test_scope_filters_tools(tmp_path: Path):
    config = _write_config(tmp_path)
    async with _router(config, "echo-only") as session:
        tools = await session.call_tool("get_mcp_tools", {"mcp_name": "alpha"})
        assert tools.isError is False
        names = {item["name"] for item in json.loads(tools.content[0].text)}
        assert names == {"echo"}

        denied = await session.call_tool(
            "run_mcp_tool",
            {"mcp_name": "alpha", "mcp_tool": "add", "arguments": {"a": 1, "b": 2}},
        )
        assert denied.isError is True
        assert "not available" in denied.content[0].text

        allowed = await session.call_tool(
            "run_mcp_tool",
            {"mcp_name": "alpha", "mcp_tool": "echo", "arguments": {"text": "hi"}},
        )
        assert allowed.isError is False
        assert allowed.content[0].text == "hi"


async def test_no_scope_lists_everything(tmp_path: Path):
    config = _write_config(tmp_path)
    async with _router(config, None) as session:
        installed = await session.call_tool("get_installed_mcps", {})
        text = installed.content[0].text
        assert "alpha" in text and "beta" in text

        tools = await session.call_tool("get_mcp_tools", {"mcp_name": "beta"})
        names = {item["name"] for item in json.loads(tools.content[0].text)}
        assert {"echo", "add"} <= names


def test_unknown_scope_fails_startup(tmp_path: Path):
    config = _write_config(tmp_path)
    env = {
        **os.environ,
        "MCP_ROUTER_CONFIG": str(config),
        "MCP_ROUTER_SCOPE": "ghost",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_router.server"],
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode != 0
    assert "Unknown scope" in (proc.stdout + proc.stderr)
