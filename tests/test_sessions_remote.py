from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from mcp_router import sessions
from mcp_router.config import RemoteServer


@pytest.fixture()
def captured(monkeypatch):
    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_streamable(url, *, http_client=None, terminate_on_close=True):
        seen["url"] = url
        seen["headers"] = dict(http_client.headers) if http_client is not None else None
        yield "read", "write", lambda: None

    # _connect imports this lazily, so patching the source module is enough.
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client", fake_streamable
    )
    return seen


async def test_remote_headers_are_sent_via_an_httpx_client(captured):
    server = RemoteServer(
        type="remote",
        url="https://example.test/mcp",
        headers={"Authorization": "Bearer token"},
    )
    async with sessions._connect(server) as streams:
        assert streams == ("read", "write")

    assert captured["url"] == "https://example.test/mcp"
    # httpx header names are case-insensitive and normalised to lower case.
    assert captured["headers"]["authorization"] == "Bearer token"


async def test_remote_without_headers_passes_no_http_client(captured):
    server = RemoteServer(type="remote", url="https://example.test/mcp")
    async with sessions._connect(server):
        pass

    assert captured["headers"] is None
