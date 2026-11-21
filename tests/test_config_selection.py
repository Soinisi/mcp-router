"""Durable mode/active-scope selection and the global/local-repo inheritance."""

from __future__ import annotations

import yaml

import pytest

from mcp_router.config import (
    ConfigError,
    RouterConfig,
    ScopeEntry,
    dump_config,
    load_config,
    parse_config,
    resolve_launch_scope,
    resolve_mode,
    resolve_saved_scope,
)


def _write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _server(name: str = "tickets"):
    return {"type": "local", "command": f"{name}-cmd"}


def test_v3_parses_selection_keys():
    config = parse_config(
        {
            "version": 3,
            "mode": "local-repo",
            "active_scope": "default",
            "mcp_servers": {"tickets": _server()},
            "scopes": {"default": {"tickets": None}},
        }
    )
    assert config.mode == "local-repo"
    assert config.active_scope == "default"


def test_v2_cannot_carry_selection_keys():
    with pytest.raises(ConfigError, match="version 3"):
        parse_config(
            {
                "version": 2,
                "mode": "global",
                "mcp_servers": {},
            }
        )


def test_v1_and_v2_documents_stay_unchanged(tmp_path):
    v1 = tmp_path / "v1.yaml"
    _write(v1, {"version": 1, "mcp_servers": {"a": _server("a")}})
    v2 = tmp_path / "v2.yaml"
    _write(
        v2,
        {
            "version": 2,
            "mcp_servers": {"a": _server("a")},
            "scopes": {"s": {"a": {"tools": None, "resources": ["x://"]}}},
        },
    )

    loaded_v1 = load_config([v1, tmp_path / "missing.yaml"], environ={})
    loaded_v2 = load_config([v2, tmp_path / "missing.yaml"], environ={})
    assert loaded_v1.config.version == 1
    assert loaded_v2.config.version == 2
    assert loaded_v2.config.resolve_scope("s")["a"].resources == ["x://"]


def test_default_scope_is_the_fallback_active_scope(tmp_path):
    path = tmp_path / "config.yaml"
    _write(
        path,
        {
            "version": 3,
            "mcp_servers": {"a": _server("a")},
            "scopes": {"default": {"a": None}},
        },
    )
    loaded = load_config([path, tmp_path / "missing.yaml"], environ={})
    assert loaded.active_scope == "default"
    assert loaded.config.resolve_scope(loaded.active_scope) == {
        "a": ScopeEntry(tools=None, resources=[], prompts=[])
    }


def test_no_default_scope_and_no_selection_is_unrestricted(tmp_path):
    path = tmp_path / "config.yaml"
    _write(path, {"version": 1, "mcp_servers": {"a": _server("a")}})
    loaded = load_config([path, tmp_path / "missing.yaml"], environ={})
    assert loaded.active_scope is None
    assert loaded.config.resolve_scope(None) is None


def test_explicit_null_selection_means_unrestricted(tmp_path):
    path = tmp_path / "config.yaml"
    _write(
        path,
        {
            "version": 3,
            "active_scope": None,
            "mcp_servers": {"a": _server("a")},
            "scopes": {"default": {"a": None}},
        },
    )
    loaded = load_config([path, tmp_path / "missing.yaml"], environ={})
    assert loaded.active_scope is None


def test_mode_resolution_is_repo_first_then_global(tmp_path):
    assert resolve_mode({}, None) == "global"
    assert resolve_mode({}, {}) == "local-repo"
    assert resolve_mode({"mode": "local-repo"}, {}) == "local-repo"
    assert resolve_mode({"mode": "local-repo"}, {"mode": "global"}) == "global"


def test_repo_mode_wins_over_global(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": {"g": _server("g")},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "mode": "local-repo",
            "mcp_servers": {"r": _server("r")},
            "scopes": {"default": {"g": None, "r": None}},
        },
    )
    loaded = load_config([global_cfg, repo_cfg], environ={})
    assert loaded.mode == "local-repo"
    assert set(loaded.config.mcp_servers) == {"g", "r"}


def test_global_mode_ignores_repo_definitions(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": {"g": _server("g")},
            "scopes": {"default": {"g": None}},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": {"r": _server("r")},
            "scopes": {"default": {"r": None}},
        },
    )
    loaded = load_config([global_cfg, repo_cfg], environ={})
    assert loaded.mode == "global"
    assert set(loaded.config.mcp_servers) == {"g"}
    assert set(loaded.config.resolve_scope("default")) == {"g"}


def test_global_mode_ignores_the_repo_active_scope(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": {"g": _server("g")},
            "scopes": {"default": {"g": None}},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "active_scope": "repo-only",
            "scopes": {"repo-only": {"g": None}},
        },
    )

    loaded = load_config([global_cfg, repo_cfg], environ={})
    assert loaded.mode == "global"
    assert loaded.active_scope == "default"
    assert set(loaded.config.resolve_scope(loaded.active_scope)) == {"g"}


def test_global_mode_ignores_a_repo_active_scope_name_global_also_defines(
    tmp_path,
):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": {"g": _server("g")},
            "scopes": {
                "default": {"g": None},
                "repo-only": {"g": None},
            },
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "active_scope": "repo-only",
            "scopes": {"repo-only": {"g": None}},
        },
    )

    loaded = load_config([global_cfg, repo_cfg], environ={})
    assert loaded.active_scope == "default"


def test_local_repo_inherits_and_overrides_per_server(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {"tickets": _server("t"), "docs": _server("d")},
            "scopes": {"default": {"tickets": None, "docs": None}},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "mode": "local-repo",
            "scopes": {"default": {"tickets": ["get_ticket"]}},
        },
    )
    loaded = load_config([global_cfg, repo_cfg], environ={})
    scope = loaded.config.resolve_scope("default")
    assert scope["tickets"].tools == ["get_ticket"]
    assert scope["docs"].tools is None  # inherited


def test_launch_pin_must_not_differ_from_saved_selection(tmp_path):
    saved = type(
        "Loaded",
        (),
        {"active_scope": "default"},
    )()
    assert resolve_launch_scope(saved, "default") == "default"
    with pytest.raises(ConfigError, match="conflicts with the saved active scope"):
        resolve_launch_scope(saved, "wider")


def test_launch_pin_narrows_an_unrestricted_saved_view():
    saved = type("Loaded", (), {"active_scope": None})()
    assert resolve_launch_scope(saved, "narrow") == "narrow"


def test_dump_config_carries_v3_selection():
    config = RouterConfig(
        version=3,
        mode="local-repo",
        active_scope="default",
        mcp_servers={"a": _server("a")},
        scopes={"default": {"a": None}},
    )
    raw = yaml.safe_load(dump_config(config))
    assert raw["version"] == 3
    assert raw["mode"] == "local-repo"
    assert raw["active_scope"] == "default"
    assert raw["scopes"]["default"]["a"]["tools"] is None


def test_dump_config_keeps_v1_without_selection():
    config = parse_config(
        {"version": 1, "mcp_servers": {"a": _server("a")}}
    )
    raw = yaml.safe_load(dump_config(config))
    assert raw["version"] == 1
    assert "mode" not in raw
    assert "active_scope" not in raw


def test_saved_scope_resolution_prefers_repo(tmp_path):
    global_data = {"active_scope": "global-choice"}
    repo_data = {"active_scope": "repo-choice"}
    assert (
        resolve_saved_scope(global_data, repo_data, {}) == "repo-choice"
    )
    assert resolve_saved_scope(global_data, None, {}) == "global-choice"
    assert (
        resolve_saved_scope(global_data, {}, {"default": {}}) == "global-choice"
    )
