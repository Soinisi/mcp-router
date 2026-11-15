"""End-to-end coverage for the dynamic surface and the resource proxy tools.

The router's tool list is gated by the capabilities its registered servers
advertise at startup, and resources are discovered/read through the proxy.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CHILD = Path(__file__).with_name("child_server.py")
SURFACE = Path(__file__).with_name("surface_server.py")


def _local(script: Path, *args: str, description: str = "") -> dict:
    return {
        "type": "local",
        "command": sys.executable,
        "args": [str(script), *args],
        "description": description,
    }


def _broken() -> dict:
    return {
        "type": "local",
        "command": sys.executable,
        "args": ["-c", "raise SystemExit(1)"],
        "description": "Broken test server.",
    }


def _write_config(
    tmp_path: Path,
    servers: dict,
    scopes: dict | None = None,
    version: int = 1,
) -> Path:
    payload: dict = {"version": version, "mcp_servers": servers}
    if scopes is not None:
        payload["scopes"] = scopes
    config = tmp_path / "router.yaml"
    config.write_text(yaml.safe_dump(payload), encoding="utf-8")
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


async def _tool_names(session: ClientSession) -> set[str]:
    result = await session.list_tools()
    return {tool.name for tool in result.tools}


async def _search(session: ClientSession, arguments: dict) -> list[dict]:
    result = await session.call_tool("search_resources", arguments)
    assert result.isError is False, result.content[0].text
    payload = json.loads(result.content[0].text)
    return payload["resources"] if isinstance(payload, dict) else payload


async def test_child_resources_are_searchable_and_readable(tmp_path: Path):
    config = _write_config(tmp_path, {"child": _local(CHILD)})
    async with _router(config) as session:
        assert await _tool_names(session) == {
            "get_installed_mcps",
            "search_tools",
            "run_mcp_tool",
            "search_resources",
            "read_resource",
        }

        entries = await _search(session, {"mcp_name": "child"})
        by_name = {entry["name"]: entry for entry in entries}
        assert by_name["readme"]["uri"] == "memory://readme"
        assert by_name["readme"]["mimeType"] == "text/plain"
        assert by_name["readme"].get("template") is not True
        assert by_name["greeting"]["template"] is True
        assert by_name["greeting"]["uri"] == "memory://greeting/{name}"

        filtered = await _search(
            session, {"mcp_name": "child", "regex_filter": "readme"}
        )
        assert [entry["name"] for entry in filtered] == ["readme"]

        read = await session.call_tool(
            "read_resource", {"mcp_name": "child", "uri": "memory://readme"}
        )
        assert read.isError is False
        assert read.content[0].type == "resource"
        assert read.content[0].resource.text == "hello from the child server"

        greeting = await session.call_tool(
            "read_resource",
            {"mcp_name": "child", "uri": "memory://greeting/ada"},
        )
        assert greeting.isError is False
        assert greeting.content[0].resource.text == "hello ada"


async def test_search_resources_across_servers(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"alpha": _local(CHILD), "beta": _local(CHILD)},
    )
    async with _router(config) as session:
        result = await session.call_tool(
            "search_resources", {"regex_filter": "readme"}
        )
        body = json.loads(result.content[0].text)
        assert body["unreachable_servers"] == []
        assert {(entry["mcp_name"], entry["name"]) for entry in body["resources"]} == {
            ("alpha", "readme"),
            ("beta", "readme"),
        }

        narrowed = await _search(session, {"mcp_name": "beta"})
        assert {entry["mcp_name"] for entry in narrowed} == {"beta"}


async def test_resource_scope_allow_list_filters_search_and_read(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"alpha": _local(CHILD)},
        scopes={
            "readme-only": {
                "alpha": {
                    "tools": [],
                    "resources": ["memory://readme"],
                    "prompts": [],
                }
            }
        },
        version=2,
    )
    async with _router(config, "readme-only") as session:
        entries = await _search(session, {"mcp_name": "alpha"})
        assert [entry["name"] for entry in entries] == ["readme"]

        allowed = await session.call_tool(
            "read_resource", {"mcp_name": "alpha", "uri": "memory://readme"}
        )
        assert allowed.isError is False

        denied = await session.call_tool(
            "read_resource", {"mcp_name": "alpha", "uri": "memory://greeting/ada"}
        )
        assert denied.isError is True
        assert "not available" in denied.content[0].text


async def test_resource_template_pattern_in_scope(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"alpha": _local(CHILD)},
        scopes={
            "greetings": {
                "alpha": {"resources": ["memory://greeting/{name}"]},
            }
        },
        version=2,
    )
    async with _router(config, "greetings") as session:
        entries = await _search(session, {"mcp_name": "alpha"})
        assert [entry["name"] for entry in entries] == ["greeting"]

        allowed = await session.call_tool(
            "read_resource", {"mcp_name": "alpha", "uri": "memory://greeting/ada"}
        )
        assert allowed.isError is False

        denied = await session.call_tool(
            "read_resource", {"mcp_name": "alpha", "uri": "memory://readme"}
        )
        assert denied.isError is True


async def test_v1_scope_keeps_resources_closed(tmp_path: Path):
    # A v1 bare list only allow-lists tools; resources stay unavailable rather
    # than silently widening when the router gains the category.
    config = _write_config(
        tmp_path,
        {"alpha": _local(CHILD)},
        scopes={"echo-only": {"alpha": ["echo"]}},
        version=1,
    )
    async with _router(config, "echo-only") as session:
        denied = await session.call_tool(
            "read_resource", {"mcp_name": "alpha", "uri": "memory://readme"}
        )
        assert denied.isError is True
        assert "not available" in denied.content[0].text

        hidden = await session.call_tool("search_resources", {"mcp_name": "alpha"})
        assert hidden.isError is True


async def test_unreachable_server_does_not_block_startup(tmp_path: Path):
    config = _write_config(
        tmp_path, {"alpha": _local(CHILD), "broken": _broken()}
    )
    async with _router(config) as session:
        assert "search_resources" in await _tool_names(session)

        installed = await session.call_tool("get_installed_mcps", {})
        summaries = {item["name"]: item for item in json.loads(installed.content[0].text)}
        assert summaries["broken"]["reachable"] is False
        assert "error" in summaries["broken"]

        result = await session.call_tool(
            "search_resources", {"regex_filter": "readme"}
        )
        assert result.isError is False
        body = json.loads(result.content[0].text)
        assert [item["mcp_name"] for item in body["unreachable_servers"]] == ["broken"]
        assert {entry["mcp_name"] for entry in body["resources"]} == {"alpha"}


async def test_tool_only_servers_omit_resource_tools(tmp_path: Path):
    config = _write_config(
        tmp_path, {"surface": _local(SURFACE, "tools")}
    )
    async with _router(config) as session:
        assert await _tool_names(session) == {
            "get_installed_mcps",
            "search_tools",
            "run_mcp_tool",
        }
        hidden = await session.call_tool("search_resources", {})
        assert hidden.isError is True
        assert "Unknown tool" in hidden.content[0].text


async def test_resource_only_servers_omit_tool_tools(tmp_path: Path):
    config = _write_config(
        tmp_path, {"surface": _local(SURFACE, "resources")}
    )
    async with _router(config) as session:
        assert await _tool_names(session) == {
            "get_installed_mcps",
            "search_resources",
            "read_resource",
        }
        hidden = await session.call_tool("search_tools", {})
        assert hidden.isError is True
        assert "Unknown tool" in hidden.content[0].text

        entries = await _search(session, {"mcp_name": "surface"})
        assert {(entry["name"], entry.get("template")) for entry in entries} == {
            ("doc", None),
            ("item", True),
        }
        read = await session.call_tool(
            "read_resource", {"mcp_name": "surface", "uri": "surface://doc"}
        )
        assert read.isError is False
        assert read.content[0].resource.text == "contents of surface://doc"


async def test_cross_search_skips_servers_without_the_category(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {
            "child": _local(CHILD),
            "tools_only": _local(SURFACE, "tools"),
            "resources_only": _local(SURFACE, "resources"),
        },
    )
    async with _router(config) as session:
        tools = await session.call_tool("search_tools", {"regex_filter": "echo"})
        assert tools.isError is False
        payload = json.loads(tools.content[0].text)
        assert payload["unreachable_servers"] == []
        assert {entry["mcp_name"] for entry in payload["tools"]} == {"child"}

        resources = await session.call_tool(
            "search_resources", {"regex_filter": "readme"}
        )
        assert resources.isError is False
        payload = json.loads(resources.content[0].text)
        assert payload["unreachable_servers"] == []
        assert {entry["mcp_name"] for entry in payload["resources"]} == {"child"}

        wrong_category = await session.call_tool(
            "search_tools", {"mcp_name": "resources_only"}
        )
        assert wrong_category.isError is True
        assert "does not offer tools" in wrong_category.content[0].text

        wrong_category = await session.call_tool(
            "search_resources", {"mcp_name": "tools_only"}
        )
        assert wrong_category.isError is True
        assert "does not offer resources" in wrong_category.content[0].text


async def test_no_capabilities_exposes_only_browse(tmp_path: Path):
    config = _write_config(
        tmp_path, {"surface": _local(SURFACE, "none")}
    )
    async with _router(config) as session:
        assert await _tool_names(session) == {"get_installed_mcps"}
