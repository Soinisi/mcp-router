"""The discovery tools' regex filter matches names *and* descriptions."""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DESCRIPTION_SERVER = Path(__file__).with_name("description_server.py")

SERVERS = {
    "alpha": {
        "type": "local",
        "command": sys.executable,
        "args": [str(DESCRIPTION_SERVER)],
        "description": "Handles billing invoices and payment reconciliation.",
    },
    "beta": {
        "type": "local",
        "command": sys.executable,
        "args": [str(DESCRIPTION_SERVER)],
        "description": "Searches the product knowledge base.",
    },
}


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "router.yaml"
    config.write_text(
        yaml.safe_dump({"version": 1, "mcp_servers": SERVERS}), encoding="utf-8"
    )
    return config


@asynccontextmanager
async def _router(config: Path):
    env = {**os.environ, "MCP_ROUTER_CONFIG": str(config)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_router.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _installed(session: ClientSession, regex: str) -> set[str]:
    result = await session.call_tool("get_installed_mcps", {"regex_filter": regex})
    assert result.isError is False
    return {item["name"] for item in json.loads(result.content[0].text)}


async def _tools(session: ClientSession, regex: str) -> set[str]:
    result = await session.call_tool(
        "get_mcp_tools", {"mcp_name": "alpha", "regex_filter": regex}
    )
    assert result.isError is False
    return {item["name"] for item in json.loads(result.content[0].text)}


@pytest.mark.parametrize(
    ("regex", "expected"),
    [
        ("alpha", {"alpha"}),  # name only
        ("invoices", {"alpha"}),  # description only, absent from the name
        ("no-such-server", set()),  # non-match
        ("INVOICES", {"alpha"}),  # case-insensitive
    ],
)
async def test_installed_mcps_filter_matches_name_or_description(
    tmp_path: Path, regex: str, expected: set[str]
):
    async with _router(_write_config(tmp_path)) as session:
        assert await _installed(session, regex) == expected


@pytest.mark.parametrize(
    ("regex", "expected"),
    [
        ("list_users", {"list_users"}),  # name only
        ("directory", {"list_users"}),  # description only, absent from the name
        ("no-such-tool", set()),  # non-match
        ("DIRECTORY", {"list_users"}),  # case-insensitive
        # `ping` has no description (None); neither its name nor a literal
        # "None" description may match.
        ("none", set()),
    ],
)
async def test_mcp_tools_filter_matches_name_or_description(
    tmp_path: Path, regex: str, expected: set[str]
):
    async with _router(_write_config(tmp_path)) as session:
        assert await _tools(session, regex) == expected
