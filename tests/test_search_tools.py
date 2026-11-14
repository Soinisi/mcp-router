"""Cross-server search, narrowing, guardrails, and full-schema output."""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DESCRIPTION_SERVER = Path(__file__).with_name("description_server.py")
MANY_TOOLS_SERVER = Path(__file__).with_name("many_tools_server.py")


def _alpha() -> dict:
    return {
        "type": "local",
        "command": sys.executable,
        "args": [str(DESCRIPTION_SERVER)],
        "description": "Handles billing invoices and payment reconciliation.",
    }


def _beta() -> dict:
    return {
        "type": "local",
        "command": sys.executable,
        "args": [str(DESCRIPTION_SERVER)],
        "description": "Searches the product knowledge base.",
    }


def _write_config(tmp_path: Path, servers: dict | None = None) -> Path:
    config = tmp_path / "router.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": servers
                if servers is not None
                else {"alpha": _alpha(), "beta": _beta()},
            }
        ),
        encoding="utf-8",
    )
    return config


@asynccontextmanager
async def _router(config: Path, scope: str | None = None):
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


def _tools(result) -> list[dict]:
    payload = json.loads(result.content[0].text)
    return payload["tools"] if isinstance(payload, dict) else payload


async def test_cross_server_search_returns_tagged_summaries(tmp_path: Path):
    async with _router(_write_config(tmp_path)) as session:
        result = await session.call_tool("search_tools", {"regex_filter": "directory"})
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert payload["unreachable_servers"] == []
        assert [(entry["mcp_name"], entry["name"]) for entry in payload["tools"]] == [
            ("alpha", "list_users"),
            ("beta", "list_users"),
        ]
        for entry in payload["tools"]:
            assert "signature" in entry
            assert "inputSchema" not in entry


async def test_mcp_name_narrows_the_search(tmp_path: Path):
    async with _router(_write_config(tmp_path)) as session:
        result = await session.call_tool(
            "search_tools", {"mcp_name": "alpha", "regex_filter": "users"}
        )
        assert result.isError is False
        entries = _tools(result)
        assert [(entry["mcp_name"], entry["name"]) for entry in entries] == [
            ("alpha", "list_users")
        ]


async def test_no_filter_lists_servers(tmp_path: Path):
    async with _router(_write_config(tmp_path)) as session:
        result = await session.call_tool("search_tools", {})
        assert result.isError is False
        assert {server["name"] for server in _tools(result)} == {"alpha", "beta"}


async def test_mcp_name_without_filter_lists_its_tools(tmp_path: Path):
    async with _router(_write_config(tmp_path)) as session:
        result = await session.call_tool("search_tools", {"mcp_name": "alpha"})
        assert result.isError is False
        names = {entry["name"] for entry in _tools(result)}
        assert {"list_users", "summarize", "ping"} <= names


async def test_full_true_returns_the_input_schema(tmp_path: Path):
    async with _router(_write_config(tmp_path)) as session:
        result = await session.call_tool(
            "search_tools",
            {"mcp_name": "alpha", "regex_filter": "list_users", "full": True},
        )
        assert result.isError is False
        entries = _tools(result)
        assert len(entries) == 1
        assert "inputSchema" in entries[0]
        assert "signature" not in entries[0]


async def test_no_match_returns_an_actionable_error(tmp_path: Path):
    async with _router(_write_config(tmp_path)) as session:
        result = await session.call_tool("search_tools", {"regex_filter": "zzz-nope"})
        assert result.isError is True
        text = result.content[0].text
        assert "No tools matched" in text
        assert "mcp_name" in text


async def test_whitespace_phrase_falls_back_to_or_of_words(tmp_path: Path):
    async with _router(_write_config(tmp_path)) as session:
        result = await session.call_tool(
            "search_tools", {"regex_filter": "alpha list_users"}
        )
        assert result.isError is False
        entries = _tools(result)
        assert any(
            entry["mcp_name"] == "alpha" and entry["name"] == "list_users"
            for entry in entries
        )


async def test_result_cap_refuses_a_broad_search(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {
            "many": {
                "type": "local",
                "command": sys.executable,
                "args": [str(MANY_TOOLS_SERVER)],
                "description": "Many sample tools for the search cap test.",
            }
        },
    )
    async with _router(config) as session:
        result = await session.call_tool("search_tools", {"regex_filter": "sample"})
        assert result.isError is True
        text = result.content[0].text
        assert "Too many matches" in text
        assert "Narrow the filter" in text
        assert "55" in text


async def test_unreachable_servers_are_reported(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"alpha": _alpha(), "broken": _broken()},
    )
    async with _router(config) as session:
        result = await session.call_tool("search_tools", {"regex_filter": "directory"})
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        names = {entry["name"] for entry in payload["tools"]}
        assert "list_users" in names
        assert [item["mcp_name"] for item in payload["unreachable_servers"]] == [
            "broken"
        ]


async def test_server_first_narrowing_skips_other_servers(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"alpha": _alpha(), "broken": _broken()},
    )
    async with _router(config) as session:
        result = await session.call_tool("search_tools", {"regex_filter": "alpha"})
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert payload["unreachable_servers"] == []
        assert {entry["mcp_name"] for entry in payload["tools"]} == {"alpha"}
        assert {"list_users", "summarize", "ping"} <= {
            entry["name"] for entry in payload["tools"]
        }


def _broken() -> dict:
    return {
        "type": "local",
        "command": sys.executable,
        "args": ["-c", "raise SystemExit(1)"],
        "description": "Broken test server.",
    }
