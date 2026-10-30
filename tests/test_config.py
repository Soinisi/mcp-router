from __future__ import annotations

import pytest

from mcp_router.config import (
    ConfigError,
    LocalServer,
    RemoteServer,
    build_environ,
    load_config,
    parse_config,
    resolve_config,
    resolve_server,
    unresolved_secrets,
)


def test_parses_local_and_remote():
    config = parse_config(
        {
            "version": 1,
            "mcp_servers": {
                "fs": {"type": "local", "command": "npx", "args": ["-y", "server"]},
                "docs": {"type": "remote", "url": "https://example.test/mcp"},
            },
        }
    )
    assert isinstance(config.mcp_servers["fs"], LocalServer)
    assert isinstance(config.mcp_servers["docs"], RemoteServer)
    assert config.mcp_servers["docs"].transport == "http"


def test_description_defaults_to_name():
    config = parse_config({"mcp_servers": {"fs": {"type": "local", "command": "true"}}})
    assert config.mcp_servers["fs"].description == "fs"


def test_unknown_field_is_rejected():
    with pytest.raises(ConfigError, match="extra_forbidden|Extra inputs"):
        parse_config({"mcp_servers": {"fs": {"type": "local", "command": "true", "nope": 1}}})


def test_local_requires_command():
    with pytest.raises(ConfigError, match="command"):
        parse_config({"mcp_servers": {"fs": {"type": "local"}}})


def test_remote_requires_url():
    with pytest.raises(ConfigError, match="url"):
        parse_config({"mcp_servers": {"r": {"type": "remote"}}})


def test_bad_version_rejected():
    with pytest.raises(ConfigError, match="version"):
        parse_config({"version": 2, "mcp_servers": {}})


def test_env_expansion_at_resolve_time():
    config = parse_config(
        {
            "mcp_servers": {
                "r": {
                    "type": "remote",
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                }
            }
        }
    )
    resolved = resolve_server(config.mcp_servers["r"], {"TOKEN": "s3cret"})
    assert resolved.headers["Authorization"] == "Bearer s3cret"


def test_placeholders_kept_until_resolved():
    config = parse_config(
        {"mcp_servers": {"r": {"type": "remote", "url": "${MISSING}"}}}
    )
    assert config.mcp_servers["r"].url == "${MISSING}"
    with pytest.raises(ConfigError, match="MISSING"):
        resolve_server(config.mcp_servers["r"], {})
    assert unresolved_secrets(config, {}) == [
        "r: Unresolved secret ${MISSING} at 'url'. Set it in the environment or a .env file."
    ]


def test_build_environ_real_env_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=from_file\nB=from_file\n")
    env = build_environ([env_file], base={"B": "from_process"})
    assert env["A"] == "from_file"
    assert env["B"] == "from_process"


def test_scopes_parse_and_resolve():
    config = parse_config(
        {
            "mcp_servers": {"tickets": {"type": "local", "command": "true"}},
            "scopes": {
                "support": {"tickets": None},
                "readonly": {"tickets": ["get_ticket"]},
            },
        }
    )
    assert config.resolve_scope("support") == {"tickets": None}
    assert config.resolve_scope("readonly") == {"tickets": ["get_ticket"]}
    assert config.resolve_scope(None) is None


def test_resolve_config_preserves_scopes():
    config = parse_config(
        {
            "mcp_servers": {"tickets": {"type": "local", "command": "true"}},
            "scopes": {"support": {"tickets": None}},
        }
    )
    resolved = resolve_config(config, {})
    assert resolved.scopes == {"support": {"tickets": None}}
    assert resolved.resolve_scope("support") == {"tickets": None}


def test_scope_unknown_server_rejected():
    with pytest.raises(ConfigError, match="unknown server"):
        parse_config({"mcp_servers": {}, "scopes": {"x": {"ghost": None}}})


def test_scope_empty_tool_name_rejected():
    with pytest.raises(ConfigError, match="empty tool name"):
        parse_config(
            {
                "mcp_servers": {"t": {"type": "local", "command": "true"}},
                "scopes": {"x": {"t": [" "]}},
            }
        )


def test_scope_empty_tool_list_means_no_tools():
    config = parse_config(
        {
            "mcp_servers": {"t": {"type": "local", "command": "true"}},
            "scopes": {"x": {"t": []}},
        }
    )
    assert config.resolve_scope("x") == {"t": []}


def test_unknown_scope_raises():
    config = parse_config({"mcp_servers": {}})
    with pytest.raises(ConfigError, match="Unknown scope"):
        config.resolve_scope("ghost")


def test_scopes_deep_merge_across_files(tmp_path):
    user = tmp_path / "user.yaml"
    user.write_text(
        """
mcp_servers:
  tickets: {type: local, command: "true"}
scopes:
  support:
    tickets: null
  override:
    tickets: null
""",
        encoding="utf-8",
    )
    project = tmp_path / "project.yaml"
    project.write_text(
        """
mcp_servers:
  docs: {type: local, command: "true"}
scopes:
  support:
    docs: null
  override:
    tickets: ["get_ticket"]
""",
        encoding="utf-8",
    )

    loaded = load_config([user, project], environ={})

    assert loaded.config.resolve_scope("support") == {
        "tickets": None,
        "docs": None,
    }
    assert loaded.config.resolve_scope("override") == {"tickets": ["get_ticket"]}
