"""The terminal editor refuses non-interactive use and edits scopes on Save."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml
import pytest
from textual.widgets import (
    Input,
    OptionList,
    RadioButton,
    Select,
    Static,
    Tree,
)

from mcp_router.config import write_raw_config
from mcp_router.scope_edit import ScopeEditor
from mcp_router.tui import ConfirmScreen, PromptScreen, ScopeTui

CHILD = Path(__file__).with_name("child_server.py")


def _config():
    return {
        "version": 3,
        "mcp_servers": {
            "alpha": {
                "type": "local",
                "command": sys.executable,
                "args": [str(CHILD)],
            },
            "beta": {
                "type": "local",
                "command": sys.executable,
                "args": [str(CHILD)],
            },
        },
        "scopes": {"default": {"alpha": None}},
    }


def test_tui_refuses_without_a_real_terminal(tmp_path):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_router.cli", "tui"],
        input="",
        capture_output=True,
        text=True,
        env={**os.environ, "MCP_ROUTER_CONFIG": str(config)},
        timeout=20,
    )
    assert proc.returncode != 0
    assert "real terminal" in (proc.stdout + proc.stderr)


async def test_tui_mounts_without_any_declared_scope(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw.pop("scopes")
    write_raw_config(config, raw)
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.editor.scope_names() == []
        # The editor offers a placeholder rather than an empty Select.
        assert app.query_one("#edit-scope") is not None


async def test_tui_space_ticks_the_selected_server(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#servers", Tree)
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert app.dirty is True
        rows = {row.name: row for row in app.editor.rows()}
        assert rows["alpha"].ticked is False


async def test_tui_active_scope_change_prompts_before_quitting(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["work"] = {"alpha": None}
    write_raw_config(config, raw)
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.dirty is False

        app.query_one("#active-scope", Select).value = "work"
        await pilot.pause()
        assert app.editor.active_scope == "work"
        assert app.dirty is True

        app.action_quit()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")

    assert app.editor.active_scope == "default"
    assert app.dirty is False


async def test_tui_mode_change_counts_as_unsaved(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.dirty is False

        app.query_one("#mode-local", RadioButton).value = True
        await pilot.pause()
        assert app.editor.mode == "local-repo"
        assert app.dirty is True


async def test_tui_mode_switch_with_non_default_active_scope_is_one_change(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["active_scope"] = "readonly"
    raw["scopes"]["readonly"] = {"beta": None}
    write_raw_config(config, raw)
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.editor.active_scope == "readonly"
        assert app.dirty is False
        assert len(app.screen_stack) == 1

        app.query_one("#mode-local", RadioButton).value = True
        for _ in range(5):
            await pilot.pause()
        # The click alone is one unsaved change, not a self-reproducing
        # discard prompt; the active scope stays on the saved selection.
        assert app.editor.mode == "local-repo"
        assert app.editor.active_scope == "readonly"
        assert app.dirty is True
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)

        # Quitting still prompts exactly once and the discard is dismissable.
        app.action_quit()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert len(app.screen_stack) == 1

    assert app.editor.mode == "global"
    assert app.editor.active_scope == "readonly"
    assert app.dirty is False


async def test_tui_save_writes_the_tick_list(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "mcp-router.yaml"

    app = ScopeTui(ScopeEditor.load([config, repo]))
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#servers", Tree)
        labels = " ".join(str(node.label) for node in tree.root.children)
        assert "alpha" in labels and "beta" in labels

        editor = app.editor
        editor.set_server_entry(
            "default", "beta", editor.make_target_entry(None, None)
        )
        app.action_save()
        await pilot.pause()
        status = str(app.query_one("#status", Static).render())
        assert "saved" in status
        assert "applied" not in status

    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert "beta" in raw["scopes"]["default"]
    assert raw["mcp_servers"] == _config()["mcp_servers"]


def test_ticking_every_visible_tool_keeps_an_explicit_allowlist(tmp_path):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["default"] = {"alpha": ["echo"]}
    write_raw_config(config, raw)

    editor = ScopeEditor.load([config, tmp_path / "mcp-router.yaml"])
    app = ScopeTui(editor)
    app._available_tools = {"alpha": ["add", "echo"]}

    assert app._toggle_tool("alpha", "add") is True
    entry = editor.target_doc()["scopes"]["default"]["alpha"]
    assert entry["tools"] == ["add", "echo"]


def test_reticking_a_tool_on_a_null_entry_restores_all_tools(tmp_path):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["default"] = {"alpha": None}
    write_raw_config(config, raw)

    editor = ScopeEditor.load([config, tmp_path / "mcp-router.yaml"])
    app = ScopeTui(editor)
    app._available_tools = {"alpha": ["add", "echo"]}

    assert app._toggle_tool("alpha", "add") is True
    entry = editor.target_doc()["scopes"]["default"]["alpha"]
    assert entry["tools"] == ["echo"]

    assert app._toggle_tool("alpha", "add") is True
    entry = editor.target_doc()["scopes"]["default"]["alpha"]
    assert entry["tools"] is None


def test_enabling_an_absent_server_writes_an_explicit_allowlist(tmp_path):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["default"] = {}
    write_raw_config(config, raw)

    editor = ScopeEditor.load([config, tmp_path / "mcp-router.yaml"])
    app = ScopeTui(editor)
    app._available_tools = {"alpha": ["add"]}

    assert app._toggle_tool("alpha", "add") is True
    entry = editor.target_doc()["scopes"]["default"]["alpha"]
    assert entry["tools"] == ["add"]


def test_unticking_and_reticking_a_server_restores_its_committed_entry(
    tmp_path,
):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["default"] = {
        "alpha": {
            "tools": ["add"],
            "resources": ["alpha://docs"],
            "prompts": ["summarise"],
        }
    }
    write_raw_config(config, raw)

    editor = ScopeEditor.load([config, tmp_path / "mcp-router.yaml"])
    app = ScopeTui(editor)
    app._available_tools = {"alpha": ["add", "echo"]}

    assert app._toggle_server("alpha") is True
    assert "alpha" not in editor.target_doc()["scopes"]["default"]

    assert app._toggle_server("alpha") is True
    assert editor.target_doc()["scopes"]["default"]["alpha"] == {
        "tools": ["add"],
        "resources": ["alpha://docs"],
        "prompts": ["summarise"],
    }


def test_ticking_a_tool_on_an_unticked_server_keeps_its_allowlists(tmp_path):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["default"] = {
        "alpha": {
            "tools": ["add"],
            "resources": ["alpha://docs"],
            "prompts": ["summarise"],
        }
    }
    write_raw_config(config, raw)

    editor = ScopeEditor.load([config, tmp_path / "mcp-router.yaml"])
    app = ScopeTui(editor)
    app._available_tools = {"alpha": ["add", "echo"]}

    assert app._toggle_server("alpha") is True
    assert app._toggle_tool("alpha", "echo") is True
    assert editor.target_doc()["scopes"]["default"]["alpha"] == {
        "tools": ["echo"],
        "resources": ["alpha://docs"],
        "prompts": ["summarise"],
    }


def test_reverting_an_override_preserves_inherited_allowlists_on_a_tool_tick(
    tmp_path,
):
    config = tmp_path / "config.yaml"
    repo = tmp_path / "mcp-router.yaml"
    write_raw_config(
        config,
        {
            "version": 2,
            "mcp_servers": {
                "alpha": {
                    "type": "local",
                    "command": sys.executable,
                    "args": [str(CHILD)],
                }
            },
            "scopes": {
                "default": {
                    "alpha": {
                        "tools": None,
                        "resources": ["global://docs"],
                        "prompts": ["global-prompt"],
                    }
                }
            },
        },
    )
    write_raw_config(
        repo,
        {
            "version": 2,
            "mode": "local-repo",
            "scopes": {
                "default": {
                    "alpha": {
                        "tools": ["add"],
                        "resources": ["repo://docs"],
                        "prompts": ["repo-prompt"],
                    }
                }
            },
        },
    )

    editor = ScopeEditor.load([config, repo])
    app = ScopeTui(editor)
    app._available_tools = {"alpha": ["add", "echo"]}

    assert app._toggle_server("alpha") is True
    rows = {row.name: row for row in editor.rows()}
    assert rows["alpha"].state == "inherited"

    assert app._toggle_tool("alpha", "echo") is True
    assert editor.target_doc()["scopes"]["default"]["alpha"] == {
        "tools": ["add"],
        "resources": ["global://docs"],
        "prompts": ["global-prompt"],
    }


def test_reverting_an_override_restores_the_inherited_null_tool_baseline(
    tmp_path,
):
    config = tmp_path / "config.yaml"
    repo = tmp_path / "mcp-router.yaml"
    write_raw_config(
        config,
        {
            "version": 2,
            "mcp_servers": {
                "alpha": {
                    "type": "local",
                    "command": sys.executable,
                    "args": [str(CHILD)],
                }
            },
            "scopes": {
                "default": {
                    "alpha": {
                        "tools": None,
                        "resources": ["global://docs"],
                        "prompts": ["global-prompt"],
                    }
                }
            },
        },
    )
    write_raw_config(
        repo,
        {
            "version": 2,
            "mode": "local-repo",
            "scopes": {"default": {"alpha": {"tools": ["add"]}}},
        },
    )

    editor = ScopeEditor.load([config, repo])
    app = ScopeTui(editor)
    app._available_tools = {"alpha": ["add", "echo"]}
    assert editor.baseline_is_null("default", "alpha") is False

    assert app._toggle_server("alpha") is True
    assert editor.baseline_is_null("default", "alpha") is True

    assert app._toggle_tool("alpha", "echo") is True
    assert editor.target_doc()["scopes"]["default"]["alpha"]["tools"] == ["add"]

    assert app._toggle_tool("alpha", "echo") is True
    entry = editor.target_doc()["scopes"]["default"]["alpha"]
    assert entry["tools"] is None
    assert entry["resources"] == ["global://docs"]
    assert entry["prompts"] == ["global-prompt"]


@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
async def test_tui_selection_controls_fit_and_active_scope_dropdown_works(
    tmp_path, monkeypatch, size
):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["work"] = {"alpha": None}
    write_raw_config(config, raw)
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        width, height = app.size
        for selector in ("#mode", "#edit-scope", "#active-scope"):
            region = app.query_one(selector).region
            assert region.x >= 0 and region.right <= width, selector
            assert region.y >= 0 and region.bottom <= height, selector
        for button in app.query_one("#mode").query(RadioButton):
            region = button.region
            assert region.right <= width and region.bottom <= height

        # More than one registered server is visible without scrolling.
        assert app.query_one("#servers", Tree).size.height > 1

        # A human can open the Active scope list and pick a scope from it.
        active = app.query_one("#active-scope", Select)
        active.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert active.expanded is True
        overlay = active.query_one(OptionList)
        assert overlay.region.width > 0 and overlay.region.height > 0
        assert overlay.region.right <= width and overlay.region.bottom <= height
        labels = [
            str(overlay.get_option_at_index(index).prompt)
            for index in range(overlay.option_count)
        ]
        target = labels.index("work")
        while overlay.highlighted < target:
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert active.value == "work"
        assert app.editor.active_scope == "work"
        assert app.dirty is True


async def test_tui_new_scope_counts_as_unsaved(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_new_scope()
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        app.screen.query_one("#prompt-input", Input).value = "work"
        await pilot.press("enter")
        await pilot.pause()
        assert "work" in app.editor.scope_names()
        assert app.dirty is True

        app.action_quit()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")

    assert "work" not in app.editor.scope_names()


async def test_tui_duplicate_scope_counts_as_unsaved(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    write_raw_config(config, _config())
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_duplicate_scope()
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        app.screen.query_one("#prompt-input", Input).value = "copy"
        await pilot.press("enter")
        await pilot.pause()
        assert "copy" in app.editor.scope_names()
        assert app.dirty is True

        app.action_quit()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")

    assert "copy" not in app.editor.scope_names()


async def test_tui_delete_scope_counts_as_unsaved(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["work"] = {"alpha": None}
    write_raw_config(config, raw)
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.editor.edited_scope = "work"
        app.action_delete_scope()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert "work" not in app.editor.scope_names()
        assert app.dirty is True

        app.action_quit()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")

    assert "work" in app.editor.scope_names()


async def test_edit_scope_select_resyncs_after_discard_and_navigate(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.yaml"
    raw = _config()
    raw["scopes"]["work"] = {"alpha": None}
    write_raw_config(config, raw)
    monkeypatch.setenv("MCP_ROUTER_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)

    app = ScopeTui(ScopeEditor.load([config, tmp_path / "mcp-router.yaml"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#servers", Tree)
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert app.dirty is True

        selection = app.query_one("#edit-scope", Select)
        selection.value = "work"
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")
        await pilot.pause()

        assert app.editor.edited_scope == "work"
        assert selection.value == "work"
