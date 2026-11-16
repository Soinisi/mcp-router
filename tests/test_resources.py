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


def _flaky(tmp_path: Path, mode: str) -> tuple[dict, Path]:
    """A server that exits at startup until the returned marker file exists."""
    marker = tmp_path / f"up-{mode}"
    script = tmp_path / f"flaky_{mode}.py"
    script.write_text(
        "import pathlib, runpy, sys\n"
        f"if not pathlib.Path({str(marker)!r}).exists():\n"
        "    sys.exit(1)\n"
        f"runpy.run_path({str(SURFACE)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return _local(script, mode), marker


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


async def test_resource_template_bare_placeholder_stays_in_one_segment(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"surface": _local(SURFACE, "resources")},
        scopes={"item": {"surface": {"resources": ["surface://item/{id}"]}}},
        version=2,
    )
    async with _router(config, "item") as session:
        entries = await _search(session, {"mcp_name": "surface"})
        assert [entry["name"] for entry in entries] == ["item"]

        allowed = await session.call_tool(
            "read_resource", {"mcp_name": "surface", "uri": "surface://item/7"}
        )
        assert allowed.isError is False

        nested = await session.call_tool(
            "read_resource",
            {"mcp_name": "surface", "uri": "surface://item/7/secret"},
        )
        assert nested.isError is True
        assert "not available" in nested.content[0].text


async def test_resource_template_plus_placeholder_spans_segments(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"surface": _local(SURFACE, "resources")},
        scopes={"item": {"surface": {"resources": ["surface://item/{+path}"]}}},
        version=2,
    )
    async with _router(config, "item") as session:
        allowed = await session.call_tool(
            "read_resource",
            {"mcp_name": "surface", "uri": "surface://item/7/secret"},
        )
        assert allowed.isError is False

        outside = await session.call_tool(
            "read_resource", {"mcp_name": "surface", "uri": "surface://doc"}
        )
        assert outside.isError is True
        assert "not available" in outside.content[0].text


async def test_resource_template_slash_placeholder_matches_slash_segments(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"surface": _local(SURFACE, "resources")},
        scopes={"item": {"surface": {"resources": ["surface://item{/path}"]}}},
        version=2,
    )
    async with _router(config, "item") as session:
        for uri in ("surface://item", "surface://item/7", "surface://item/7/8"):
            allowed = await session.call_tool(
                "read_resource", {"mcp_name": "surface", "uri": uri}
            )
            assert allowed.isError is False, uri

        outside = await session.call_tool(
            "read_resource", {"mcp_name": "surface", "uri": "surface://doc"}
        )
        assert outside.isError is True
        assert "not available" in outside.content[0].text


async def test_resource_scope_exact_entry_matches_only_itself(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"surface": _local(SURFACE, "resources")},
        scopes={
            "doc-only": {
                "surface": {"resources": ["surface://doc"]},
            }
        },
        version=2,
    )
    async with _router(config, "doc-only") as session:
        entries = await _search(session, {"mcp_name": "surface"})
        assert [entry["name"] for entry in entries] == ["doc"]

        allowed = await session.call_tool(
            "read_resource", {"mcp_name": "surface", "uri": "surface://doc"}
        )
        assert allowed.isError is False

        sibling = await session.call_tool(
            "read_resource",
            {"mcp_name": "surface", "uri": "surface://doc_secret"},
        )
        assert sibling.isError is True
        assert "not available" in sibling.content[0].text

        template = await session.call_tool(
            "read_resource", {"mcp_name": "surface", "uri": "surface://item/7"}
        )
        assert template.isError is True
        assert "not available" in template.content[0].text


async def test_resource_scope_trailing_slash_is_a_prefix(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {"surface": _local(SURFACE, "resources")},
        scopes={
            "surface-all": {
                "surface": {"resources": ["surface://"]},
            }
        },
        version=2,
    )
    async with _router(config, "surface-all") as session:
        entries = await _search(session, {"mcp_name": "surface"})
        assert {entry["name"] for entry in entries} == {
            "doc",
            "doc_secret",
            "item",
        }

        sibling = await session.call_tool(
            "read_resource",
            {"mcp_name": "surface", "uri": "surface://doc_secret"},
        )
        assert sibling.isError is False

        other_scheme = await session.call_tool(
            "read_resource", {"mcp_name": "surface", "uri": "other://doc"}
        )
        assert other_scheme.isError is True
        assert "not available" in other_scheme.content[0].text


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


async def test_check_clears_stale_probe_error_after_reconnect(tmp_path: Path):
    marker = tmp_path / "up"
    flaky = tmp_path / "flaky_server.py"
    flaky.write_text(
        "import pathlib, runpy, sys\n"
        f"if not pathlib.Path({str(marker)!r}).exists():\n"
        "    sys.exit(1)\n"
        f"runpy.run_path({str(SURFACE)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    config = _write_config(tmp_path, {"flaky": _local(flaky, "both")})

    async with _router(config) as session:
        before = await session.call_tool("get_installed_mcps", {})
        summary = {
            item["name"]: item for item in json.loads(before.content[0].text)
        }["flaky"]
        assert summary["reachable"] is False
        assert "error" in summary

        marker.write_text("up", encoding="utf-8")

        after = await session.call_tool("get_installed_mcps", {"check": True})
        summary = {
            item["name"]: item for item in json.loads(after.content[0].text)
        }["flaky"]
        assert summary["reachable"] is True
        assert "error" not in summary
        assert summary["tool_count"] == 1

        later = await session.call_tool("get_installed_mcps", {})
        summary = {
            item["name"]: item for item in json.loads(later.content[0].text)
        }["flaky"]
        assert "error" not in summary


async def test_check_reports_recovered_server_with_live_capabilities(tmp_path: Path):
    flaky, marker = _flaky(tmp_path, "resources")
    config = _write_config(tmp_path, {"alpha": _local(CHILD), "flaky": flaky})

    async with _router(config) as session:
        marker.write_text("up", encoding="utf-8")

        after = await session.call_tool("get_installed_mcps", {"check": True})
        summary = {
            item["name"]: item for item in json.loads(after.content[0].text)
        }["flaky"]
        assert summary["reachable"] is True
        assert "error" not in summary
        assert summary["tool_count"] == 0


async def test_recovered_server_uses_live_capabilities_in_searches(tmp_path: Path):
    flaky, marker = _flaky(tmp_path, "resources")
    config = _write_config(tmp_path, {"alpha": _local(CHILD), "flaky": flaky})

    async with _router(config) as session:
        marker.write_text("up", encoding="utf-8")

        tools = await session.call_tool("search_tools", {"mcp_name": "flaky"})
        assert tools.isError is True
        assert "does not offer tools" in tools.content[0].text

        resources = await session.call_tool(
            "search_resources", {"mcp_name": "flaky"}
        )
        assert resources.isError is False
        assert {entry["name"] for entry in json.loads(resources.content[0].text)} == {
            "doc",
            "doc_secret",
            "item",
        }

        installed = await session.call_tool("get_installed_mcps", {})
        summary = {
            item["name"]: item for item in json.loads(installed.content[0].text)
        }["flaky"]
        assert "error" not in summary
        assert summary.get("reachable") is not False


async def test_cross_search_skips_recovered_wrong_category_server(tmp_path: Path):
    flaky, marker = _flaky(tmp_path, "tools")
    config = _write_config(tmp_path, {"alpha": _local(CHILD), "flaky": flaky})

    async with _router(config) as session:
        marker.write_text("up", encoding="utf-8")

        resources = await session.call_tool(
            "search_resources", {"regex_filter": "readme"}
        )
        assert resources.isError is False
        body = json.loads(resources.content[0].text)
        assert body["unreachable_servers"] == []
        assert {entry["mcp_name"] for entry in body["resources"]} == {"alpha"}

        tools = await session.call_tool("search_tools", {"regex_filter": "echo"})
        assert tools.isError is False
        body = json.loads(tools.content[0].text)
        assert body["unreachable_servers"] == []
        assert {entry["mcp_name"] for entry in body["tools"]} == {"alpha"}


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
            ("doc_secret", None),
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
