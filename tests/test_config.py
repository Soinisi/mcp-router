from __future__ import annotations

import pytest
import yaml

from mcp_router.config import (
    ConfigError,
    LocalServer,
    RemoteServer,
    ScopeEntry,
    build_environ,
    dump_config,
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
        parse_config({"version": 3, "mcp_servers": {}})


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
    assert config.resolve_scope("support") == {
        "tickets": ScopeEntry(tools=None, resources=[], prompts=[])
    }
    assert config.resolve_scope("readonly") == {
        "tickets": ScopeEntry(tools=["get_ticket"], resources=[], prompts=[])
    }
    assert config.resolve_scope(None) is None


def test_scope_v2_mapping_parses_each_category():
    config = parse_config(
        {
            "version": 2,
            "mcp_servers": {"tickets": {"type": "local", "command": "true"}},
            "scopes": {
                "support": {
                    "tickets": {
                        "tools": ["get_ticket"],
                        "resources": ["tickets://"],
                        "prompts": None,
                    }
                }
            },
        }
    )
    entry = config.resolve_scope("support")["tickets"]
    assert entry.tools == ["get_ticket"]
    assert entry.resources == ["tickets://"]
    assert entry.prompts is None


def test_scope_v2_omitted_category_allows_none():
    config = parse_config(
        {
            "version": 2,
            "mcp_servers": {"tickets": {"type": "local", "command": "true"}},
            "scopes": {"support": {"tickets": {"tools": None}}},
        }
    )
    entry = config.resolve_scope("support")["tickets"]
    assert entry.tools is None
    assert entry.resources == []
    assert entry.prompts == []


def test_scope_v2_rejects_unknown_category():
    with pytest.raises(ConfigError, match="extra_forbidden|Extra inputs"):
        parse_config(
            {
                "version": 2,
                "mcp_servers": {"t": {"type": "local", "command": "true"}},
                "scopes": {"x": {"t": {"tools": [], "subscriptions": []}}},
            }
        )


def test_scope_v2_rejects_empty_resource_pattern():
    with pytest.raises(ConfigError, match="empty resource pattern"):
        parse_config(
            {
                "version": 2,
                "mcp_servers": {"t": {"type": "local", "command": "true"}},
                "scopes": {"x": {"t": {"resources": [" "]}}},
            }
        )


def test_scope_v1_still_accepts_bare_lists_and_null():
    config = parse_config(
        {
            "version": 1,
            "mcp_servers": {"t": {"type": "local", "command": "true"}},
            "scopes": {
                "all": {"t": None},
                "some": {"t": ["echo"]},
                "none": {"t": []},
            },
        }
    )
    assert config.resolve_scope("all")["t"].tools is None
    assert config.resolve_scope("some")["t"].tools == ["echo"]
    assert config.resolve_scope("none")["t"].tools == []
    # v1 has no resource or prompt allow-list, so those categories are closed.
    assert config.resolve_scope("some")["t"].resources == []
    assert config.resolve_scope("some")["t"].prompts == []


def test_v2_config_accepted():
    config = parse_config({"version": 2, "mcp_servers": {}})
    assert config.version == 2


def test_resolve_config_preserves_scopes():
    config = parse_config(
        {
            "mcp_servers": {"tickets": {"type": "local", "command": "true"}},
            "scopes": {"support": {"tickets": None}},
        }
    )
    resolved = resolve_config(config, {})
    assert resolved.scopes == {
        "support": {"tickets": ScopeEntry(tools=None, resources=[], prompts=[])}
    }
    assert resolved.resolve_scope("support") == {
        "tickets": ScopeEntry(tools=None, resources=[], prompts=[])
    }


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
    assert config.resolve_scope("x")["t"].tools == []


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
        "tickets": ScopeEntry(tools=None, resources=[], prompts=[]),
        "docs": ScopeEntry(tools=None, resources=[], prompts=[]),
    }
    assert loaded.config.resolve_scope("override") == {
        "tickets": ScopeEntry(tools=["get_ticket"], resources=[], prompts=[])
    }


def test_dump_config_keeps_v1_shape():
    config = parse_config(
        {
            "version": 1,
            "mcp_servers": {"t": {"type": "local", "command": "true"}},
            "scopes": {"x": {"t": ["echo"]}},
        }
    )
    dump = yaml.safe_load(dump_config(config))
    assert dump["version"] == 1
    assert dump["scopes"] == {"x": {"t": ["echo"]}}


def test_dump_config_writes_v2_for_per_category_scopes():
    config = parse_config(
        {
            "version": 2,
            "mcp_servers": {"t": {"type": "local", "command": "true"}},
            "scopes": {"x": {"t": {"tools": ["echo"], "resources": ["docs://"]}}},
        }
    )
    dump = yaml.safe_load(dump_config(config))
    assert dump["version"] == 2
    assert dump["scopes"]["x"]["t"] == {
        "tools": ["echo"],
        "resources": ["docs://"],
        "prompts": [],
    }


def test_dump_config_bumps_version_for_category_values_in_v1_document():
    config = parse_config(
        {
            "version": 1,
            "mcp_servers": {"t": {"type": "local", "command": "true"}},
            "scopes": {"x": {"t": {"resources": ["docs://"]}}},
        }
    )
    dump = yaml.safe_load(dump_config(config))
    assert dump["version"] == 2
    assert dump["scopes"]["x"]["t"]["resources"] == ["docs://"]
