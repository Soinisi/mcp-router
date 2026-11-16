"""End-to-end coverage for the prompt proxy tools.

Prompts follow the same dynamic, capability-gated shape as resources: the pair
appears when a scope-visible server advertises prompts, discovery honors the
scope's prompt allow-list, and rendering proxies the child's result.
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
    result = await session.call_tool("search_prompts", arguments)
    assert result.isError is False, result.content[0].text
    payload = json.loads(result.content[0].text)
    return payload["prompts"] if isinstance(payload, dict) else payload


async def test_child_prompts_are_searchable_and_renderable(tmp_path: Path):
    config = _write_config(tmp_path, {"child": _local(CHILD)})
    async with _router(config) as session:
        assert {"search_prompts", "get_prompt"} <= await _tool_names(session)

        entries = await _search(session, {"mcp_name": "child"})
        by_name = {entry["name"]: entry for entry in entries}
        assert by_name["summarize"]["description"] == "Ask for a summary of a text."
        assert by_name["review"]["description"] == (
            "Ask for a review of the current change."
        )
        assert {entry["mcp_name"] for entry in entries} == {"child"}

        filtered = await _search(
            session, {"mcp_name": "child", "regex_filter": "summar"}
        )
        assert [entry["name"] for entry in filtered] == ["summarize"]

        rendered = await session.call_tool(
            "get_prompt",
            {
                "mcp_name": "child",
                "name": "summarize",
                "arguments": {"text": "hello", "style": "formal"},
            },
        )
        assert rendered.isError is False
        body = json.loads(rendered.content[0].text)
        assert body["description"] == "Ask for a summary of a text."
        assert body["messages"] == [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "Summarize the following text in a formal style:\n\nhello"
                    ),
                },
            }
        ]


async def test_search_prompts_across_servers(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"alpha": _local(CHILD), "beta": _local(CHILD)},
    )
    async with _router(config) as session:
        result = await session.call_tool(
            "search_prompts", {"regex_filter": "review"}
        )
        assert result.isError is False
        body = json.loads(result.content[0].text)
        assert body["unreachable_servers"] == []
        assert {(entry["mcp_name"], entry["name"]) for entry in body["prompts"]} == {
            ("alpha", "review"),
            ("beta", "review"),
        }

        narrowed = await _search(session, {"mcp_name": "beta"})
        assert {entry["mcp_name"] for entry in narrowed} == {"beta"}


async def test_prompt_scope_allow_list_filters_search_and_get(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"alpha": _local(CHILD)},
        scopes={"review-only": {"alpha": {"prompts": ["review"]}}},
        version=2,
    )
    async with _router(config, "review-only") as session:
        entries = await _search(session, {"mcp_name": "alpha"})
        assert [entry["name"] for entry in entries] == ["review"]

        allowed = await session.call_tool(
            "get_prompt", {"mcp_name": "alpha", "name": "review"}
        )
        assert allowed.isError is False
        assert json.loads(allowed.content[0].text)["messages"][0]["content"][
            "text"
        ] == "Review the current change and list any risks."

        denied = await session.call_tool(
            "get_prompt",
            {"mcp_name": "alpha", "name": "summarize", "arguments": {"text": "x"}},
        )
        assert denied.isError is True
        assert "not available" in denied.content[0].text


async def test_prompt_only_server_exposes_prompt_tools(tmp_path: Path):
    # Prompts are gated per category: this server has no resources and no
    # tools, yet the prompt pair must appear and work.
    config = _write_config(
        tmp_path, {"surface": _local(SURFACE, "prompts")}
    )
    async with _router(config) as session:
        assert await _tool_names(session) == {
            "get_installed_mcps",
            "search_prompts",
            "get_prompt",
        }

        entries = await _search(session, {"mcp_name": "surface"})
        assert {entry["name"] for entry in entries} == {"summarize", "review"}

        rendered = await session.call_tool(
            "get_prompt",
            {"mcp_name": "surface", "name": "summarize", "arguments": {"text": "hi"}},
        )
        assert rendered.isError is False
        assert json.loads(rendered.content[0].text)["messages"][0]["content"][
            "text"
        ] == "Summarize: hi"


async def test_servers_without_prompts_omit_prompt_tools(tmp_path: Path):
    config = _write_config(
        tmp_path, {"surface": _local(SURFACE, "tools,resources")}
    )
    async with _router(config) as session:
        names = await _tool_names(session)
        assert {"search_tools", "search_resources"} <= names
        assert "search_prompts" not in names
        assert "get_prompt" not in names

        hidden = await session.call_tool("search_prompts", {})
        assert hidden.isError is True
        assert "Unknown tool" in hidden.content[0].text


async def test_wrong_category_server_cannot_be_searched_for_prompts(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {
            "child": _local(CHILD),
            "tools_only": _local(SURFACE, "tools"),
        },
    )
    async with _router(config) as session:
        result = await session.call_tool(
            "search_prompts", {"regex_filter": "review"}
        )
        assert result.isError is False
        body = json.loads(result.content[0].text)
        assert body["unreachable_servers"] == []
        assert {entry["mcp_name"] for entry in body["prompts"]} == {"child"}

        wrong_category = await session.call_tool(
            "search_prompts", {"mcp_name": "tools_only"}
        )
        assert wrong_category.isError is True
        assert "does not offer prompts" in wrong_category.content[0].text
