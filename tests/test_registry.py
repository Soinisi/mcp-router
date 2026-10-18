from __future__ import annotations

import pytest

from mcp_router import config as config_mod
from mcp_router.config import ConfigError, load_config
from mcp_router.registry import add_server, remove_server, server_summaries


@pytest.fixture()
def user_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(path))
    return path


def test_add_then_load(user_config):
    add_server("fs", {"type": "local", "command": "npx", "args": ["-y", "server"]})
    assert user_config.is_file()
    loaded = load_config(environ={})
    assert user_config in loaded.sources
    assert loaded.config.mcp_servers["fs"].command == "npx"


def test_add_duplicate_requires_overwrite(user_config):
    add_server("fs", {"type": "local", "command": "a"})
    with pytest.raises(ConfigError, match="already exists"):
        add_server("fs", {"type": "local", "command": "b"})
    add_server("fs", {"type": "local", "command": "b"}, overwrite=True)
    assert load_config(environ={}).config.mcp_servers["fs"].command == "b"


def test_add_rejects_invalid_spec(user_config):
    with pytest.raises(ConfigError, match="Invalid server spec"):
        add_server("bad", {"type": "local"})


def test_secrets_preserved_as_placeholder(user_config):
    add_server(
        "r",
        {"type": "remote", "url": "https://x.test/mcp", "headers": {"A": "${TOKEN}"}},
    )
    assert "${TOKEN}" in user_config.read_text()


def test_remove(user_config):
    add_server("fs", {"type": "local", "command": "a"})
    remove_server("fs")
    assert "fs" not in load_config(environ={}).config.mcp_servers
    with pytest.raises(ConfigError, match="not registered"):
        remove_server("fs")


def test_project_override(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(user))
    add_server("fs", {"type": "local", "command": "global"})

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    add_server("fs", {"type": "local", "command": "local"}, scope="project")

    loaded = load_config(environ={})
    assert loaded.config.mcp_servers["fs"].command == "local"
    assert len(loaded.sources) == 2


def test_server_summaries_sorted(user_config):
    add_server("b", {"type": "local", "command": "x"})
    add_server("a", {"type": "local", "command": "y", "description": "A server"})
    summaries = server_summaries(load_config(environ={}).config)
    assert [s["name"] for s in summaries] == ["a", "b"]
    assert summaries[0]["description"] == "A server"
