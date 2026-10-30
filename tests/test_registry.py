from __future__ import annotations

import pytest
import yaml

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


def test_remove_cascades_into_scopes(user_config):
    user_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": {
                    "tickets": {"type": "local", "command": "a"},
                    "docs": {"type": "local", "command": "b"},
                },
                "scopes": {
                    "support": {"tickets": None},
                    "mixed": {"tickets": ["get"], "docs": None},
                    "docs-only": {"docs": []},
                },
            }
        ),
        encoding="utf-8",
    )

    affected = remove_server("tickets")

    assert affected == ["mixed", "support"]
    loaded = load_config(environ={})
    assert set(loaded.config.mcp_servers) == {"docs"}
    assert loaded.config.scopes == {
        "mixed": {"docs": None},
        "docs-only": {"docs": []},
    }


def test_remove_refuses_when_sibling_scope_references_server(
    tmp_path, monkeypatch, user_config
):
    add_server("tickets", {"type": "local", "command": "a"})
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    project = project_dir / "mcp-router.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": {},
                "scopes": {"support": {"tickets": None}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)

    with pytest.raises(ConfigError) as excinfo:
        remove_server("tickets")

    message = str(excinfo.value)
    assert str(project) in message
    assert "support" in message
    assert "tickets" in load_config(environ={}).config.mcp_servers
    assert "support" in yaml.safe_load(project.read_text(encoding="utf-8"))["scopes"]


def test_remove_project_refuses_when_user_scope_references_server(
    tmp_path, monkeypatch, user_config
):
    user_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": {},
                "scopes": {"support": {"tickets": None}},
            }
        ),
        encoding="utf-8",
    )
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    add_server("tickets", {"type": "local", "command": "a"}, scope="project")

    with pytest.raises(ConfigError) as excinfo:
        remove_server("tickets", scope="project")

    message = str(excinfo.value)
    assert str(user_config) in message
    assert "support" in message


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
