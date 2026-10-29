"""Integration coverage for the legacy SSE transport.

Starts a local FastMCP server with ``transport="sse"`` and drives it through the
router, so the ``sse_client`` branch and its timeout are covered without any
external network.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SSE_SERVER = Path(__file__).with_name("e2e") / "sse_server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"SSE server did not listen on port {port}")


async def test_remote_sse_server_through_router(tmp_path: Path):
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, str(SSE_SERVER)],
        env={**os.environ, "SSE_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)

        config = tmp_path / "router.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "mcp_servers": {
                        "sse": {
                            "type": "remote",
                            "url": f"http://127.0.0.1:{port}/sse",
                            "transport": "sse",
                            "description": "Local SSE test server",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_router.server"],
            env={**os.environ, "MCP_ROUTER_CONFIG": str(config)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                installed = await session.call_tool("get_installed_mcps", {})
                assert installed.isError is False
                assert "sse" in installed.content[0].text

                tools = await session.call_tool("get_mcp_tools", {"mcp_name": "sse"})
                assert tools.isError is False
                names = {item["name"] for item in json.loads(tools.content[0].text)}
                assert "echo" in names

                result = await session.call_tool(
                    "run_mcp_tool",
                    {
                        "mcp_name": "sse",
                        "mcp_tool": "echo",
                        "arguments": {"text": "hello"},
                    },
                )
                assert result.isError is False
                assert result.content[0].text == "sse: hello"
    finally:
        server.terminate()
        server.wait(timeout=5)
