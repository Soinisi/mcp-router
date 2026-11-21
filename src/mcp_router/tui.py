"""Terminal editor for scopes and the durable mode/active-scope selection.

This is a thin view over :mod:`mcp_router.scope_edit`: the file is the only
channel, so a save says "saved" and nothing acknowledges it back. The editor
never writes ``mcp_servers`` and never resolves secrets into the document; it
connects to child servers directly only to enumerate tools for the ticks.

The whole module needs the optional ``tui`` extra (Textual). The CLI refuses to
import it unless stdin and stdout are a real terminal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Select,
    Static,
    Tree,
)

from .config import (
    DEFAULT_SCOPE,
    MODE_GLOBAL,
    MODE_LOCAL_REPO,
    ConfigError,
    load_config,
)
from .scope_edit import ScopeEditor, ServerRow
from .sessions import SessionManager

_UNRESTRICTED = "__unrestricted__"
_NO_SCOPE = "__no_scope__"


# ---------------------------------------------------------------------------
# Modal helpers
# ---------------------------------------------------------------------------


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question; used for discard, delete and hide-everything."""

    def __init__(self, message: str, *, yes: str = "Yes", no: str = "No") -> None:
        super().__init__()
        self.message = message
        self.yes_label = yes
        self.no_label = no

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self.message, id="confirm-message")
            with Horizontal():
                yield Button(self.yes_label, id="confirm-yes", variant="primary")
                yield Button(self.no_label, id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class PromptScreen(ModalScreen[str | None]):
    """Ask for a scope name (new or duplicate)."""

    def __init__(self, title: str, initial: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self.title_text)
            yield Input(value=self.initial, id="prompt-input")
            with Horizontal():
                yield Button("OK", id="prompt-ok", variant="primary")
                yield Button("Cancel", id="prompt-cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-ok":
            self.dismiss(self.query_one("#prompt-input", Input).value.strip())
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def _router_process_directories() -> set[Path] | None:
    """Best-effort set of directories where a router process appears to run.

    Returns ``None`` when the platform gives us no way to look, so the caller
    stays quiet rather than warning on a false negative.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    directories: set[Path] = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
            looks_like_router = (
                "mcp_router.server" in command
                or "mcp-router-server" in command
                or ("mcp-router" in command and "serve" in command)
            )
            if not looks_like_router:
                continue
            directories.add(Path(os.readlink(entry / "cwd")))
        except OSError:
            continue
    return directories


class ScopeTree(Tree):
    """The tick list. Space ticks the cursor row instead of expanding it."""

    BINDINGS = [Binding("space", "toggle_tick", "Tick/untick")]

    def action_toggle_tick(self) -> None:
        action = getattr(self.app, "action_toggle_selected", None)
        if action is not None:
            action()


class ScopeTui(App[None]):
    """Tick-list editor for one scope in the global or repo config file."""

    TITLE = "mcp-router scopes"
    CSS = """
    Screen { layout: vertical; }
    #toolbar { height: auto; padding: 0 1; }
    #selection { height: auto; padding: 0 1; }
    #selection Select { width: 32; }
    #mode { width: auto; height: auto; }
    #status { height: auto; padding: 0 1; color: $text-muted; }
    #servers { height: 1fr; }
    .modal { width: 64; height: auto; padding: 1 2; background: $panel; border: thick $accent; }
    .modal Horizontal { height: auto; align: right middle; }
    .modal Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding("r", "revert_inherited", "Revert"),
        Binding("s", "save", "Save"),
        Binding("n", "new_scope", "New scope"),
        Binding("d", "duplicate_scope", "Duplicate"),
        Binding("delete", "delete_scope", "Delete scope"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, editor: ScopeEditor | None = None) -> None:
        super().__init__()
        self.editor = editor or ScopeEditor.load()
        self.dirty = False
        self._suppress = False
        self._expanded: set[str] = set()
        self._available_tools: dict[str, list[str]] = {}
        self._enumeration_errors: dict[str, str] = {}
        self._manager: SessionManager | None = None
        self._status_message = ""
        self._warned_router = False

    # -- composition -----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="toolbar"):
            with Horizontal(id="selection"):
                yield Label("Mode: ")
                with RadioSet(id="mode"):
                    yield RadioButton("global", id="mode-global")
                    yield RadioButton("local-repo", id="mode-local")
                yield Label("  Edit scope: ")
                yield Select(
                    self._edit_scope_options(),
                    id="edit-scope",
                    allow_blank=False,
                )
                yield Label("  Active scope: ")
                yield Select(
                    [
                        ("(unrestricted)", _UNRESTRICTED),
                        *[(name, name) for name in self.editor.scope_names()],
                    ],
                    id="active-scope",
                    allow_blank=False,
                )
            with Horizontal(id="actions"):
                yield Button("Save", id="save", variant="primary")
                yield Button("New scope", id="new-scope")
                yield Button("Duplicate", id="duplicate-scope")
                yield Button("Delete scope", id="delete-scope")
                yield Button("Refresh tools", id="refresh-tools")
        yield Static("", id="status")
        with VerticalScroll():
            yield ScopeTree("servers", id="servers")
        yield Footer()

    def on_mount(self) -> None:
        self._sync_selection_widgets()
        self._rebuild_tree()
        self._update_status()
        self.run_worker(self._enumerate_tools(), exclusive=True, thread=False)
        if not self._warned_router:
            self._check_router_running()

    # -- selection widgets ----------------------------------------------

    def _edit_scope_options(self) -> list[tuple[str, str]]:
        names = self.editor.scope_names()
        if not names:
            return [("(no scope yet — press n)", _NO_SCOPE)]
        return [(name, name) for name in names]

    def _sync_selection_widgets(self) -> None:
        self._suppress = True
        try:
            self.query_one("#mode-global", RadioButton).value = (
                self.editor.mode == MODE_GLOBAL
            )
            self.query_one("#mode-local", RadioButton).value = (
                self.editor.mode == MODE_LOCAL_REPO
            )
            scopes = self.editor.scope_names()
            edit = self.query_one("#edit-scope", Select)
            edit.set_options(self._edit_scope_options())
            if self.editor.edited_scope in scopes:
                edit.value = self.editor.edited_scope
            elif scopes:
                edit.value = scopes[0]
            else:
                edit.value = _NO_SCOPE
            if (
                self.editor.active_scope is not None
                and self.editor.active_scope not in scopes
            ):
                self.editor.active_scope = None
            active = self.query_one("#active-scope", Select)
            active.set_options(
                [
                    ("(unrestricted)", _UNRESTRICTED),
                    *[(name, name) for name in scopes],
                ]
            )
            active.value = (
                self.editor.active_scope
                if self.editor.active_scope in scopes
                else _UNRESTRICTED
            )
        finally:
            self._suppress = False

    def _confirm_discard(self, then) -> None:
        if not self.dirty:
            then()
            return

        def decided(confirmed: bool | None) -> None:
            if confirmed:
                self.editor.restore_scope(self.editor.edited_scope)
                self.dirty = False
                then()

        self.push_screen(
            ConfirmScreen(
                "Discard unsaved tick changes?",
                yes="Discard",
                no="Keep editing",
            ),
            decided,
        )

    @on(RadioSet.Changed, "#mode")
    def _mode_changed(self, event: RadioSet.Changed) -> None:
        if self._suppress or event.pressed is None:
            return
        new_mode = (
            MODE_GLOBAL
            if event.pressed.id == "mode-global"
            else MODE_LOCAL_REPO
        )
        if new_mode == self.editor.mode:
            return

        def apply() -> None:
            self.editor.set_mode(new_mode)
            scopes = self.editor.scope_names()
            if (
                self.editor.active_scope is not None
                and self.editor.active_scope not in scopes
            ):
                self._status_message = (
                    f"active scope {self.editor.active_scope!r} is not defined "
                    f"in {new_mode} mode"
                )
                self.editor.active_scope = (
                    DEFAULT_SCOPE if DEFAULT_SCOPE in scopes else None
                )
            self.editor.edited_scope = (
                self.editor.active_scope
                if self.editor.active_scope in scopes
                else scopes[0]
                if scopes
                else self.editor.edited_scope
            )
            self._sync_selection_widgets()
            self._rebuild_tree()
            self._update_status()

        if self.dirty:
            self._sync_selection_widgets()
        self._confirm_discard(apply)

    @on(Select.Changed, "#edit-scope")
    def _edit_scope_changed(self, event: Select.Changed) -> None:
        if self._suppress or event.value is Select.BLANK:
            return
        name = str(event.value)
        if name == _NO_SCOPE or name == self.editor.edited_scope:
            return

        def apply() -> None:
            self.editor.edited_scope = name
            self._rebuild_tree()
            self._update_status()

        if self.dirty:
            self._sync_selection_widgets()
        self._confirm_discard(apply)

    @on(Select.Changed, "#active-scope")
    def _active_scope_changed(self, event: Select.Changed) -> None:
        if self._suppress or event.value is Select.BLANK:
            return
        value = str(event.value)
        self.editor.set_active_scope(
            None if value == _UNRESTRICTED else value
        )
        self._update_status()

    # -- buttons ---------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "save": self.action_save,
            "new-scope": self.action_new_scope,
            "duplicate-scope": self.action_duplicate_scope,
            "delete-scope": self.action_delete_scope,
            "refresh-tools": lambda: self.run_worker(
                self._enumerate_tools(), exclusive=True, thread=False
            ),
        }
        handler = handlers.get(event.button.id or "")
        if handler is not None:
            handler()

    # -- tree ------------------------------------------------------------

    def _row(self, name: str) -> ServerRow | None:
        for row in self.editor.rows():
            if row.name == name:
                return row
        return None

    def _server_label(self, row: ServerRow) -> str:
        tick = "☑" if row.ticked else "☐"
        suffix = ""
        if self.editor.mode == MODE_GLOBAL:
            if row.shadowed:
                suffix = "  — repo file shadows this in local mode"
        elif row.state == "inherited":
            suffix = "  (inherited from global)"
        elif row.state == "overridden":
            suffix = "  (overridden)"
        return f"{tick} {row.name}{suffix}"

    def _tool_label(self, row: ServerRow, tool: str) -> str:
        if not row.ticked:
            checked = False
        elif row.tools is None:
            checked = True
        else:
            checked = tool in row.tools
        tick = "☑" if checked else "☐"
        return f"    {tick} {tool}"

    def _rebuild_tree(self) -> None:
        tree = self.query_one("#servers", Tree)
        tree.clear()
        tree.root.expand()
        tree.show_root = False
        for row in self.editor.rows():
            node = tree.root.add(
                Text(self._server_label(row)),
                data=("server", row.name),
                expand=row.name in self._expanded,
            )
            if row.name in self._enumeration_errors:
                node.add_leaf(
                    Text(
                        "    cannot enumerate: "
                        f"{self._enumeration_errors[row.name]}"
                    )
                )
            else:
                tools = self._available_tools.get(row.name)
                if row.ticked and row.tools is not None and tools is None:
                    tools = list(row.tools)
                if tools is not None:
                    for tool in tools:
                        node.add_leaf(
                            Text(self._tool_label(row, tool)),
                            data=("tool", row.name, tool),
                        )
        tree.focus()

    def _selected_node_data(self) -> tuple[Any, ...] | None:
        node = self.query_one("#servers", Tree).cursor_node
        return node.data if node is not None else None

    def _remember_expansion(self) -> None:
        tree = self.query_one("#servers", Tree)
        for node in tree.root.children:
            data = node.data
            if isinstance(data, tuple) and data[:1] == ("server",):
                if node.is_expanded:
                    self._expanded.add(str(data[1]))
                else:
                    self._expanded.discard(str(data[1]))

    def action_toggle_selected(self) -> None:
        data = self._selected_node_data()
        if data is None:
            return
        changed = False
        if data[0] == "server":
            changed = self._toggle_server(str(data[1]))
        elif data[0] == "tool":
            changed = self._toggle_tool(str(data[1]), str(data[2]))
        if changed:
            self.dirty = True
        self._remember_expansion()
        self._rebuild_tree()
        self._update_status()

    def _toggle_server(self, name: str) -> bool:
        row = self._row(name)
        scope = self.editor.edited_scope
        if row is None:
            return False
        if self.editor.mode == MODE_GLOBAL:
            if row.ticked:
                self.editor.remove_server_entry(scope, name)
            else:
                entry = self.editor.make_target_entry(None, row.entry)
                self.editor.set_server_entry(scope, name, entry)
            return True

        if row.state == "inherited":
            self._status_message = (
                f"{name} is inherited from the global scope; switch to global "
                "mode to hide it, or press r on an override to revert."
            )
            return False
        if row.state == "overridden":
            # Unticking a repo override reverts it to the global definition.
            self.editor.remove_server_entry(scope, name)
            self._status_message = f"{name} reverted to inherited."
            return True
        entry = self.editor.make_target_entry(None, row.entry)
        self.editor.set_server_entry(scope, name, entry)
        return True

    def _toggle_tool(self, server: str, tool: str) -> bool:
        row = self._row(server)
        if row is None:
            return False
        available = self._available_tools.get(server)
        if available is None:
            self._status_message = (
                f"Cannot enumerate tools for {server}; toggle the server only."
            )
            return False
        if not row.ticked:
            selected: set[str] = set()
        elif row.tools is None:
            selected = set(available)
        else:
            selected = set(row.tools)
        if tool in selected:
            selected.remove(tool)
        else:
            selected.add(tool)
        # All ticked writes null so tools added later stay visible.
        tools = None if selected >= set(available) else sorted(selected)
        # An inherited row's base entry is the global one; creating an override
        # from it preserves that entry's resource/prompt allow-lists.
        entry = self.editor.make_target_entry(tools, row.entry)
        self.editor.set_server_entry(self.editor.edited_scope, server, entry)
        return True

    def action_revert_inherited(self) -> None:
        data = self._selected_node_data()
        name = None
        if data is not None and data[0] in ("server", "tool"):
            name = str(data[1])
        if name is None:
            return
        if self.editor.mode != MODE_LOCAL_REPO:
            self._status_message = (
                "Revert-to-inherited only works in local-repo mode."
            )
            self._update_status()
            return
        row = self._row(name)
        if row is None or row.state != "overridden":
            self._status_message = f"{name} has no repo override to revert."
            self._update_status()
            return
        self.editor.remove_server_entry(self.editor.edited_scope, name)
        self.dirty = True
        self._status_message = f"{name} reverted to inherited."
        self._rebuild_tree()
        self._update_status()

    # -- actions ---------------------------------------------------------

    def action_save(self) -> None:
        self._remember_expansion()
        rows = self.editor.rows()
        if not any(row.ticked for row in rows):
            self.push_screen(
                ConfirmScreen(
                    "This scope hides every server. Save anyway?",
                    yes="Save",
                    no="Keep editing",
                ),
                self._save_after_warning,
            )
            return
        self._save_now()

    def _save_after_warning(self, confirmed: bool | None) -> None:
        if confirmed:
            self._save_now()

    def _save_now(self) -> None:
        try:
            written = self.editor.save()
        except (ConfigError, OSError, ValueError) as exc:
            self._status_message = f"not saved: {exc}"
            self._update_status()
            return
        self.dirty = False
        if written:
            self._status_message = "saved to " + ", ".join(
                str(path) for path in written
            )
        else:
            self._status_message = "saved (nothing changed)"
        self._sync_selection_widgets()
        self._rebuild_tree()
        self._update_status()

    def action_new_scope(self) -> None:
        self._confirm_discard(self._prompt_new_scope)

    def _prompt_new_scope(self) -> None:
        def created(name: str | None) -> None:
            if not name:
                return
            try:
                self.editor.new_scope(name)
            except ConfigError as exc:
                self._status_message = str(exc)
                self._update_status()
                return
            self.dirty = False
            self._sync_selection_widgets()
            self._rebuild_tree()
            self._status_message = (
                f"new empty scope {name!r}; tick servers and Save"
            )
            self._update_status()

        self.push_screen(PromptScreen("New scope name"), created)

    def action_duplicate_scope(self) -> None:
        self._confirm_discard(self._prompt_duplicate_scope)

    def _prompt_duplicate_scope(self) -> None:
        source = self.editor.edited_scope

        def created(name: str | None) -> None:
            if not name:
                return
            try:
                self.editor.duplicate_scope(source, name)
            except ConfigError as exc:
                self._status_message = str(exc)
                self._update_status()
                return
            self.dirty = False
            self._sync_selection_widgets()
            self._rebuild_tree()
            self._status_message = f"duplicated {source!r} as {name!r}"
            self._update_status()

        self.push_screen(
            PromptScreen(f"Duplicate {source!r} as"), created
        )

    def action_delete_scope(self) -> None:
        self._confirm_discard(self._confirm_delete_scope)

    def _confirm_delete_scope(self) -> None:
        name = self.editor.edited_scope

        def decided(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                self.editor.delete_scope(name)
            except ConfigError as exc:
                self._status_message = str(exc)
                self._update_status()
                return
            self.dirty = False
            self._sync_selection_widgets()
            self._rebuild_tree()
            self._status_message = f"deleted scope {name!r}"
            self._update_status()

        self.push_screen(
            ConfirmScreen(
                f"Delete scope {name!r} from "
                f"{self.editor.target_path()}?",
                yes="Delete",
            ),
            decided,
        )

    def action_quit(self) -> None:
        if not self.dirty:
            self.exit()
            return

        def decided(confirmed: bool | None) -> None:
            if confirmed:
                self.exit()

        self.push_screen(
            ConfirmScreen("Discard unsaved tick changes and quit?"),
            decided,
        )

    # -- tools enumeration and status -----------------------------------

    async def _enumerate_tools(self) -> None:
        self._available_tools = {}
        self._enumeration_errors = {}
        try:
            loaded = load_config()
        except ConfigError as exc:
            for name in self.editor.registered_servers():
                self._enumeration_errors[name] = str(exc)
            self._rebuild_tree()
            return
        manager = SessionManager(loaded.config, environ=loaded.environ)
        self._manager = manager
        try:
            for name in sorted(self.editor.registered_servers()):
                try:
                    session = await manager.session(name)
                    result = await session.call(lambda s: s.list_tools())
                    self._available_tools[name] = sorted(
                        tool.name for tool in result.tools
                    )
                except Exception as exc:  # noqa: BLE001 - one bad server is fine
                    self._enumeration_errors[name] = (
                        f"{type(exc).__name__}: {exc}"
                    )
        finally:
            await manager.aclose()
            if self._manager is manager:
                self._manager = None
        self._rebuild_tree()
        self._update_status()

    def _check_router_running(self) -> None:
        directories = _router_process_directories()
        if directories is None:
            return
        repo = self.editor.repo_path.parent.resolve()
        if repo not in {directory.resolve() for directory in directories}:
            self._warned_router = True
            self._status_message += (
                f"  note: no running router appears to work from {repo}; "
                "saved changes reach a router started there."
            )
        self._update_status()

    def _update_status(self) -> None:
        mode = self.editor.mode
        target = self.editor.target_path()
        active = self.editor.active_scope or "(unrestricted)"
        parts = [
            f"mode={mode}",
            f"editing={self.editor.edited_scope}",
            f"active={active}",
            f"file={target}",
            f"repo={self.editor.repo_path} ({'found' if self.editor.repo_path.is_file() else 'not found'})",
        ]
        if self._status_message:
            parts.append(self._status_message)
        self.query_one("#status", Static).update("  |  ".join(parts))


def run_tui() -> None:
    ScopeTui().run()


__all__ = ["ScopeTui", "run_tui"]
