"""The terminal editor refuses non-interactive use and edits scopes on Save."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml
from textual.widgets import Static, Tree

from mcp_router.config import write_raw_config
from mcp_router.scope_edit import ScopeEditor
from mcp_router.tui import ScopeTui

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
