"""Safety invariants for the TUI's raw scope editor."""

from __future__ import annotations

import copy

import pytest
import yaml

from mcp_router.config import ConfigError, atomic_write_text, load_config
from mcp_router.scope_edit import ScopeEditor


def _write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture()
def configs(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {
                "tickets": {
                    "type": "remote",
                    "url": "https://tickets.test/mcp",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                },
                "docs": {"type": "local", "command": "docs"},
            },
            "scopes": {"default": {"tickets": None, "docs": None}},
        },
    )
    return global_cfg, repo_cfg


def test_all_ticked_tools_write_null_not_a_list(configs):
    global_cfg, repo_cfg = configs
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.edited_scope = "default"
    editor.set_server_entry(
        "default", "tickets", editor.make_target_entry(None, None)
    )
    editor.save(write_selection=False)

    raw = _read(global_cfg)
    assert raw["scopes"]["default"]["tickets"] is None


def test_unticked_every_tool_stays_an_explicit_empty_list(configs):
    global_cfg, repo_cfg = configs
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.set_server_entry(
        "default", "tickets", editor.make_target_entry([], None)
    )
    editor.save(write_selection=False)

    raw = _read(global_cfg)
    assert raw["scopes"]["default"]["tickets"] == []


def test_unticking_hides_but_never_removes_registration(configs):
    global_cfg, repo_cfg = configs
    before = _read(global_cfg)["mcp_servers"]
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.remove_server_entry("default", "tickets")
    editor.save(write_selection=False)

    raw = _read(global_cfg)
    assert "tickets" not in raw["scopes"]["default"]
    assert raw["mcp_servers"] == before


def test_editor_refuses_to_write_registration_changes(configs):
    global_cfg, repo_cfg = configs
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    # Simulate a code path that tried to mutate registrations.
    editor.global_doc["mcp_servers"]["surprise"] = {
        "type": "local",
        "command": "x",
    }
    with pytest.raises(ConfigError, match="Refusing to write registration"):
        editor.save(write_selection=False)
    assert "surprise" not in _read(global_cfg)["mcp_servers"]


def test_unticking_the_last_server_keeps_an_empty_scope(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 3,
            "active_scope": "default",
            "mcp_servers": {"a": {"type": "local", "command": "a"}},
            "scopes": {"default": {"a": None}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.remove_server_entry("default", "a")
    editor.save(write_selection=False)

    raw = _read(global_cfg)
    assert raw["scopes"]["default"] == {}
    assert raw["active_scope"] == "default"


def test_secrets_stay_as_placeholders(configs):
    global_cfg, repo_cfg = configs
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.set_server_entry(
        "default", "tickets", editor.make_target_entry(["list_tickets"], None)
    )
    editor.save(write_selection=False)
    text = global_cfg.read_text(encoding="utf-8")
    assert "${TOKEN}" in text
    assert "s3cret" not in text


def test_editing_one_file_never_writes_the_merged_view(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {"g": {"type": "local", "command": "g"}},
            "scopes": {"default": {"g": None}},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "mode": "local-repo",
            "mcp_servers": {"r": {"type": "local", "command": "r"}},
            "scopes": {"default": {"g": ["tool_g"]}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.set_server_entry(
        "default", "r", editor.make_target_entry(None, None)
    )
    editor.save(write_selection=False)

    repo = _read(repo_cfg)
    assert set(repo["scopes"]["default"]) == {"g", "r"}
    assert repo["scopes"]["default"]["g"] == ["tool_g"]
    assert "r" not in _read(global_cfg)["scopes"]["default"]


def test_keep_one_previous_generation(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    atomic_write_text(path, "version: 2\n")

    backup = tmp_path / "config.yaml.bak"
    assert backup.read_text(encoding="utf-8") == "version: 1\n"
    assert path.read_text(encoding="utf-8") == "version: 2\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_backup_inherits_the_config_file_permissions(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    path.chmod(0o640)
    atomic_write_text(path, "version: 2\n")

    backup = tmp_path / "config.yaml.bak"
    assert backup.stat().st_mode & 0o777 == 0o640


def test_atomic_write_creates_no_partial_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(path, "version: 2\n")
    assert path.read_text(encoding="utf-8") == "version: 1\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_local_mode_marks_inherited_and_overridden(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {
                "tickets": {"type": "local", "command": "t"},
                "docs": {"type": "local", "command": "d"},
            },
            "scopes": {"default": {"tickets": None, "docs": None}},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "mode": "local-repo",
            "scopes": {"default": {"tickets": ["get"]}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    rows = {row.name: row for row in editor.rows()}
    assert rows["tickets"].state == "overridden"
    assert rows["docs"].state == "inherited"
    assert rows["tickets"].ticked and rows["docs"].ticked


def test_revert_removes_only_the_repo_override(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {"tickets": {"type": "local", "command": "t"}},
            "scopes": {"default": {"tickets": None}},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "mode": "local-repo",
            "mcp_servers": {},
            "scopes": {"default": {"tickets": ["get"]}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.remove_server_entry("default", "tickets")
    editor.save(write_selection=False)

    assert "default" not in _read(repo_cfg).get("scopes", {})
    assert _read(global_cfg)["scopes"]["default"]["tickets"] is None


def test_switching_to_global_clears_repo_declaration_only(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {"version": 3, "active_scope": "default", "mcp_servers": {}},
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "mode": "local-repo",
            "active_scope": "default",
            "mcp_servers": {"r": {"type": "local", "command": "r"}},
            "scopes": {"default": {"r": None}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.set_mode("global")
    editor.set_active_scope(None)
    editor.save()

    repo = _read(repo_cfg)
    assert "mode" not in repo
    assert "active_scope" not in repo
    assert repo["mcp_servers"] == {"r": {"type": "local", "command": "r"}}
    assert repo["scopes"] == {"default": {"r": None}}
    global_raw = _read(global_cfg)
    assert global_raw["mode"] == "global"
    assert global_raw["active_scope"] is None


def test_switching_to_local_writes_repo_selection_and_creates_repo(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {"version": 1, "mcp_servers": {}, "scopes": {"default": {}}},
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.set_mode("local-repo")
    editor.set_active_scope("default")
    written = editor.save()

    assert repo_cfg in written
    repo = _read(repo_cfg)
    assert repo["mode"] == "local-repo"
    assert repo["active_scope"] == "default"
    assert repo["version"] == 3


def test_global_mode_does_not_offer_repo_only_scopes(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": {},
            "scopes": {"default": {}},
        },
    )
    _write(
        repo_cfg,
        {
            "version": 3,
            "scopes": {"repo-only": {}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    assert editor.scope_names() == ["default"]
    with pytest.raises(ConfigError, match="Unknown scope"):
        editor.set_active_scope("repo-only")


def test_new_scope_is_empty_and_cannot_delete_active(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {},
            "scopes": {"default": {}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.new_scope("work")
    assert editor.rows("work") == []
    editor.set_active_scope("work")
    with pytest.raises(ConfigError, match="active scope"):
        editor.delete_scope("work")


def test_discard_drops_pending_ticks(configs):
    global_cfg, repo_cfg = configs
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    original = copy.deepcopy(editor.global_doc["scopes"]["default"])
    editor.remove_server_entry("default", "tickets")
    assert "tickets" not in editor.global_doc["scopes"]["default"]
    editor.discard()
    assert editor.global_doc["scopes"]["default"] == original


def test_discard_restores_structural_scope_changes(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {},
            "scopes": {"default": {}, "work": {}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.new_scope("temporary")
    editor.duplicate_scope("default", "copy")
    editor.delete_scope("work")

    editor.discard()

    assert editor.scope_names() == ["default", "work"]


def test_duplicate_scope_copies_entries(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 1,
            "mcp_servers": {"a": {"type": "local", "command": "a"}},
            "scopes": {"default": {"a": ["echo"]}},
        },
    )
    editor = ScopeEditor.load([global_cfg, repo_cfg])
    editor.duplicate_scope("default", "copy")
    editor.save(write_selection=False)
    raw = _read(global_cfg)
    assert raw["scopes"]["copy"] == copy.deepcopy(raw["scopes"]["default"])


def test_inherited_allow_lists_survive_a_v1_repo_tool_override(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 2,
            "mcp_servers": {"tickets": {"type": "local", "command": "t"}},
            "scopes": {
                "default": {
                    "tickets": {
                        "tools": None,
                        "resources": ["tickets://"],
                        "prompts": ["summarise"],
                    }
                }
            },
        },
    )
    _write(
        repo_cfg,
        {
            "version": 1,
            "mcp_servers": {"tickets": {"type": "local", "command": "t"}},
        },
    )

    editor = ScopeEditor.load([global_cfg, repo_cfg])
    assert editor.mode == "local-repo"
    tickets = {row.name: row for row in editor.rows()}["tickets"]
    assert tickets.state == "inherited"
    editor.set_server_entry(
        "default",
        "tickets",
        editor.make_target_entry(["list_tickets"], tickets.entry),
    )
    editor.save(write_selection=False)

    repo = _read(repo_cfg)
    assert repo["version"] == 2
    entry = repo["scopes"]["default"]["tickets"]
    assert entry["tools"] == ["list_tickets"]
    assert entry["resources"] == ["tickets://"]
    assert entry["prompts"] == ["summarise"]

    loaded = load_config([global_cfg, repo_cfg], environ={})
    resolved = loaded.config.resolve_scope("default")["tickets"]
    assert resolved.resources == ["tickets://"]
    assert resolved.prompts == ["summarise"]


def test_global_mode_ignores_the_repo_active_scope_declaration(tmp_path):
    global_cfg = tmp_path / "config.yaml"
    repo_cfg = tmp_path / "mcp-router.yaml"
    _write(
        global_cfg,
        {
            "version": 3,
            "mode": "global",
            "mcp_servers": {"g": {"type": "local", "command": "g"}},
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

    editor = ScopeEditor.load([global_cfg, repo_cfg])
    assert editor.mode == "global"
    assert editor.active_scope == "default"


def test_scope_only_edit_keeps_v2_and_v1_versions(tmp_path):
    v2 = tmp_path / "v2.yaml"
    _write(
        v2,
        {
            "version": 2,
            "mcp_servers": {"a": {"type": "local", "command": "a"}},
            "scopes": {"default": {"a": {"tools": None, "resources": ["x://"]}}},
        },
    )
    editor = ScopeEditor.load([v2, tmp_path / "missing.yaml"])
    existing = editor.rows("default")[0].entry
    editor.set_server_entry(
        "default", "a", editor.make_target_entry(["echo"], existing)
    )
    editor.save(write_selection=False)
    raw = _read(v2)
    assert raw["version"] == 2
    assert raw["scopes"]["default"]["a"]["tools"] == ["echo"]
    # The editor preserves the existing resource allow-list when changing tools.
    assert raw["scopes"]["default"]["a"]["resources"] == ["x://"]
