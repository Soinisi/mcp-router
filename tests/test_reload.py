"""Live config reload: watch paths, debounce, last-good and notifications."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_router.config import write_raw_config

CHILD = Path(__file__).with_name("child_server.py")


def _config(*, servers=("alpha", "beta"), scope_servers=("alpha",)):
    return {
        "version": 3,
        "mcp_servers": {
            name: {
                "type": "local",
                "command": sys.executable,
                "args": [str(CHILD)],
            }
            for name in servers
        },
        "scopes": {
            "default": {name: None for name in scope_servers},
        },
    }


def _params(
    config: Path, cwd: Path | None = None, scope: str | None = None
) -> StdioServerParameters:
    env = {**os.environ, "MCP_ROUTER_CONFIG": str(config)}
    if scope is not None:
        env["MCP_ROUTER_SCOPE"] = scope
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_router.server"],
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def _installed_names(result) -> set[str]:
    return {item["name"] for item in json.loads(result.content[0].text)}


async def _wait_for_names(session: ClientSession, expected: set[str], timeout=12.0):
    deadline = time.monotonic() + timeout
    last: set[str] = set()
    while time.monotonic() < deadline:
        result = await session.call_tool("get_installed_mcps", {})
        last = _installed_names(result)
        if last == expected:
            return last
        await asyncio.sleep(0.3)
    raise AssertionError(f"expected {expected}, last saw {last}")


async def test_live_reload_swaps_scope_and_notifies(tmp_path):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())

    notifications: list[types.ServerNotification] = []

    async def handler(message):
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            notifications.append(message)

    async with stdio_client(_params(config)) as (read, write):
        async with ClientSession(
            read, write, message_handler=handler
        ) as session:
            await session.initialize()
            # Capture the live session from a request (the lifespan has none).
            await session.list_tools()
            assert await _wait_for_names(session, {"alpha"}) == {"alpha"}

            write_raw_config(config, _config(scope_servers=("beta",)))
            assert await _wait_for_names(session, {"beta"}) == {"beta"}

            deadline = time.monotonic() + 5.0
            while not notifications and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert notifications, "expected notifications/tools/list_changed"


async def test_creating_a_repo_file_switches_mode_and_scope_live(tmp_path):
    config = tmp_path / "config.yaml"
    write_raw_config(
        config,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": _config()["mcp_servers"],
        },
    )
    repo = tmp_path / "mcp-router.yaml"

    async with stdio_client(_params(config, cwd=tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert await _wait_for_names(session, {"alpha", "beta"}) == {
                "alpha",
                "beta",
            }

            # A new repo file is watched by path even though it did not exist
            # at startup. Local-repo mode plus a default scope hides beta.
            write_raw_config(
                repo,
                {
                    "version": 3,
                    "mode": "local-repo",
                    "active_scope": "default",
                    "scopes": {"default": {"alpha": None}},
                },
            )
            assert await _wait_for_names(session, {"alpha"}) == {"alpha"}


async def test_invalid_config_keeps_last_good_and_recovers(tmp_path):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())

    async with stdio_client(_params(config)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert await _wait_for_names(session, {"alpha"}) == {"alpha"}

            # Invalid YAML must not take the running surface down.
            config.write_text("mcp_servers: [unterminated\n", encoding="utf-8")
            await asyncio.sleep(2.5)
            result = await session.call_tool("get_installed_mcps", {})
            assert _installed_names(result) == {"alpha"}

            # The watcher survives and adopts the next valid document.
            write_raw_config(config, _config(scope_servers=("beta",)))
            assert await _wait_for_names(session, {"beta"}) == {"beta"}


async def test_reload_applies_the_saved_scope_over_a_stale_launch_pin(tmp_path):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["active_scope"] = "default"
    raw["scopes"]["beta-only"] = {"beta": None}
    write_raw_config(config, raw)

    async with stdio_client(_params(config, scope="default")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert await _wait_for_names(session, {"alpha"}) == {"alpha"}

            raw["active_scope"] = "beta-only"
            write_raw_config(config, raw)
            assert await _wait_for_names(session, {"beta"}) == {"beta"}
