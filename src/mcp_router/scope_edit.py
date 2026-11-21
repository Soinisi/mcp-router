"""Raw scope editing shared by the terminal UI and its tests.

This module is deliberately terminal-free: the TUI is a thin view over these
operations. It edits **only** ``scopes`` and the durable ``mode`` /
``active_scope`` keys. Registration (``mcp_servers``) stays with the CLI, and
secrets are never resolved here, so a save can never write an expanded
``${VAR}`` back to disk.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import (
    CONFIG_VERSION,
    DEFAULT_SCOPE,
    MODE_GLOBAL,
    MODE_LOCAL_REPO,
    ConfigError,
    _read_yaml,
    global_config_path,
    project_config_path,
    resolve_mode,
    resolve_saved_scope,
    scope_uses_categories,
    write_raw_config,
)

RowState = Literal["absent", "inherited", "overridden", "present"]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def read_raw_files(
    paths: list[Path] | None = None,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any], bool]:
    """Read the raw global and repo documents without merging or validating.

    Returns ``(global_path, repo_path, global_doc, repo_doc, repo_exists)``.
    A missing document is an empty mapping; the editor creates it only when a
    save actually needs it.
    """
    if paths is None:
        paths = [global_config_path(), project_config_path()]
    global_path = paths[0]
    repo_path = paths[-1] if len(paths) > 1 else project_config_path()
    global_doc = _read_yaml(global_path) if global_path.is_file() else {}
    repo_exists = repo_path.is_file()
    repo_doc = _read_yaml(repo_path) if repo_exists else {}
    return global_path, repo_path, global_doc, repo_doc, repo_exists


def scope_entries(doc: dict[str, Any], scope_name: str) -> dict[str, Any]:
    """The raw server-entry mapping of one scope (never the shared document)."""
    scopes = _mapping(doc.get("scopes"))
    return _mapping(scopes.get(scope_name))


def _scope_names(doc: dict[str, Any]) -> set[str]:
    return set(_mapping(doc.get("scopes")))


def entry_tools(entry: Any) -> list[str] | None:
    """Read a raw entry's tool allow-list (``None`` means all tools)."""
    if entry is None:
        return None
    if isinstance(entry, list):
        return list(entry)
    if isinstance(entry, dict):
        tools = entry.get("tools")
        if tools is None:
            return None
        if isinstance(tools, list):
            return list(tools)
    return []


def _categorical_document(doc: dict[str, Any]) -> bool:
    return doc.get("version") in (2, CONFIG_VERSION) or scope_uses_categories(
        doc.get("scopes")
    )


def make_entry(
    tools: list[str] | None,
    base: Any,
    *,
    categorical: bool,
) -> Any:
    """Build a raw entry, preserving resource/prompt allow-lists from ``base``.

    A categorical document keeps the mapping shape; a v1 document keeps the
    legacy bare-list shape. ``None`` stays ``null`` (all tools), never a
    spelled-out list, so tools added later remain visible.
    """
    if not categorical:
        return None if tools is None else list(tools)
    base_entry = base if isinstance(base, dict) else {}
    resources = base_entry.get("resources", [])
    prompts = base_entry.get("prompts", [])
    if resources is None:
        resources = None
    elif isinstance(resources, list):
        resources = list(resources)
    else:
        resources = []
    if prompts is None:
        prompts = None
    elif isinstance(prompts, list):
        prompts = list(prompts)
    else:
        prompts = []
    return {
        "tools": None if tools is None else list(tools),
        "resources": resources,
        "prompts": prompts,
    }


@dataclass
class ServerRow:
    """One row of the tick list for the scope currently being edited."""

    name: str
    description: str
    ticked: bool
    state: RowState
    tools: list[str] | None
    #: Global mode only: the repo file overrides this server in this scope.
    shadowed: bool = False
    #: The raw entry this row edits, if any.
    entry: Any = None


@dataclass
class ScopeEditor:
    """A transaction over the two raw config files.

    Only :meth:`save` writes, and it refuses to write a document whose
    ``mcp_servers`` differs from what was read. That is the tick editor's
    never-write-registration guarantee.
    """

    global_path: Path
    repo_path: Path
    global_doc: dict[str, Any]
    repo_doc: dict[str, Any]
    repo_exists: bool = False
    mode: str = MODE_GLOBAL
    edited_scope: str = DEFAULT_SCOPE
    active_scope: str | None = None
    _originals: dict[Path, dict[str, Any]] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, paths: list[Path] | None = None) -> "ScopeEditor":
        global_path, repo_path, global_doc, repo_doc, repo_exists = (
            read_raw_files(paths)
        )
        editor = cls(
            global_path=global_path,
            repo_path=repo_path,
            global_doc=global_doc,
            repo_doc=repo_doc,
            repo_exists=repo_exists,
        )
        editor.mode = resolve_mode(global_doc, repo_doc if repo_exists else None)
        effective_scopes = _mapping(global_doc.get("scopes"))
        if repo_exists:
            effective_scopes = {**effective_scopes, **_mapping(repo_doc.get("scopes"))}
        editor.active_scope = resolve_saved_scope(
            global_doc,
            repo_doc if repo_exists else None,
            effective_scopes,
        )
        scopes = editor.scope_names()
        if editor.active_scope is not None and editor.active_scope not in scopes:
            # The mode may have changed on disk; do not carry an invalid
            # selection into a save that would fail validation.
            editor.active_scope = (
                DEFAULT_SCOPE if DEFAULT_SCOPE in scopes else None
            )
        if scopes:
            editor.edited_scope = (
                editor.active_scope
                if editor.active_scope in scopes
                else DEFAULT_SCOPE
                if DEFAULT_SCOPE in scopes
                else scopes[0]
            )
        else:
            editor.edited_scope = DEFAULT_SCOPE
        editor._originals = {
            global_path: copy.deepcopy(global_doc),
            repo_path: copy.deepcopy(repo_doc),
        }
        return editor

    # -- selection -------------------------------------------------------

    def scope_names(self) -> list[str]:
        """Scopes effective in the current mode, global definitions first.

        Global mode sees only the global document. Local mode also sees repo
        scopes, including a repo override for a scope that only exists globally.
        """
        if self.mode == MODE_GLOBAL:
            return sorted(_scope_names(self.global_doc))
        return sorted(
            _scope_names(self.global_doc) | _scope_names(self.repo_doc)
        )

    def target_doc(self) -> dict[str, Any]:
        """The document the scope being edited belongs to."""
        return (
            self.global_doc if self.mode == MODE_GLOBAL else self.repo_doc
        )

    def target_path(self) -> Path:
        return (
            self.global_path if self.mode == MODE_GLOBAL else self.repo_path
        )

    def target_is_repo(self) -> bool:
        return self.mode != MODE_GLOBAL

    def registered_servers(self) -> dict[str, Any]:
        """Every server the editor may tick, keyed by name.

        Global mode edits the global definition only, because the effective
        router ignores repo servers then. Local mode sees the union.
        """
        if self.mode == MODE_GLOBAL:
            return dict(_mapping(self.global_doc.get("mcp_servers")))
        servers = dict(_mapping(self.global_doc.get("mcp_servers")))
        servers.update(_mapping(self.repo_doc.get("mcp_servers")))
        return servers

    def rows(self, scope_name: str | None = None) -> list[ServerRow]:
        scope_name = scope_name or self.edited_scope
        target_entries = scope_entries(self.target_doc(), scope_name)
        global_entries = scope_entries(self.global_doc, scope_name)
        repo_entries = scope_entries(self.repo_doc, scope_name)
        rows: list[ServerRow] = []

        for name, server in sorted(self.registered_servers().items()):
            description = ""
            if isinstance(server, dict):
                description = str(server.get("description") or name)
            if self.mode == MODE_GLOBAL:
                entry = target_entries.get(name)
                rows.append(
                    ServerRow(
                        name=name,
                        description=description,
                        ticked=name in target_entries,
                        state="present" if name in target_entries else "absent",
                        tools=entry_tools(entry),
                        shadowed=name in repo_entries,
                        entry=entry,
                    )
                )
                continue

            if name in repo_entries:
                entry = repo_entries[name]
                state: RowState = "overridden"
                ticked = True
            elif name in global_entries:
                entry = global_entries[name]
                state = "inherited"
                ticked = True
            else:
                entry = None
                state = "absent"
                ticked = False
            rows.append(
                ServerRow(
                    name=name,
                    description=description,
                    ticked=ticked,
                    state=state,
                    tools=entry_tools(entry),
                    entry=entry,
                )
            )
        return rows

    # -- scope mutation --------------------------------------------------

    def effective_entries(self, scope_name: str) -> dict[str, Any]:
        """The entries a scope resolves to under the current mode."""
        entries = copy.deepcopy(scope_entries(self.global_doc, scope_name))
        if self.mode != MODE_GLOBAL:
            entries.update(
                copy.deepcopy(scope_entries(self.repo_doc, scope_name))
            )
        return entries

    def new_scope(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ConfigError("Scope name must not be empty.")
        if name in self.scope_names():
            raise ConfigError(f"Scope {name!r} already exists.")
        scopes = self.target_doc().setdefault("scopes", {})
        if not isinstance(scopes, dict):
            raise ConfigError("'scopes' must be a mapping.")
        scopes[name] = {}
        self.edited_scope = name

    def duplicate_scope(self, source: str, name: str) -> None:
        name = name.strip()
        if not name:
            raise ConfigError("Scope name must not be empty.")
        if name in self.scope_names():
            raise ConfigError(f"Scope {name!r} already exists.")
        entries = copy.deepcopy(self.effective_entries(source))
        scopes = self.target_doc().setdefault("scopes", {})
        if not isinstance(scopes, dict):
            raise ConfigError("'scopes' must be a mapping.")
        scopes[name] = entries
        self.edited_scope = name

    def delete_scope(self, name: str) -> None:
        if name == self.active_scope:
            raise ConfigError(
                f"Scope {name!r} is the active scope; make another scope "
                "active before deleting it."
            )
        scopes = self.target_doc().get("scopes")
        if not isinstance(scopes, dict) or name not in scopes:
            raise ConfigError(
                f"Scope {name!r} is not defined in {self.target_path()}."
            )
        del scopes[name]
        if not scopes:
            self.target_doc().pop("scopes", None)
        if self.edited_scope == name:
            self.edited_scope = (
                self.scope_names()[0] if self.scope_names() else DEFAULT_SCOPE
            )

    def set_server_entry(self, scope_name: str, server: str, entry: Any) -> None:
        """Put one raw entry in the target scope."""
        scopes = self.target_doc().setdefault("scopes", {})
        if not isinstance(scopes, dict):
            raise ConfigError("'scopes' must be a mapping.")
        entries = scopes.setdefault(scope_name, {})
        if not isinstance(entries, dict):
            raise ConfigError(f"scope {scope_name!r} must be a mapping.")
        entries[server] = entry

    def restore_scope(self, scope_name: str) -> None:
        """Throw away in-memory edits to one scope (navigation guard)."""
        original = self._originals.get(self.target_path(), {})
        original_scopes = _mapping(original.get("scopes"))
        scopes = self.target_doc().get("scopes")
        if not isinstance(scopes, dict):
            if scope_name not in original_scopes:
                return
            scopes = {}
            self.target_doc()["scopes"] = scopes
        if scope_name in original_scopes:
            scopes[scope_name] = copy.deepcopy(original_scopes[scope_name])
        else:
            scopes.pop(scope_name, None)
        if not scopes:
            self.target_doc().pop("scopes", None)

    def remove_server_entry(self, scope_name: str, server: str) -> None:
        """Remove one server entry (used by unticking / revert-to-inherited).

        An empty scope is kept: it is a valid "visible with nothing" scope and
        it keeps an active scope name resolvable. The one exception is a repo
        override of a global scope, where dropping the empty repo scope really
        does inherit the global definition.
        """
        scopes = self.target_doc().get("scopes")
        if not isinstance(scopes, dict):
            return
        entries = scopes.get(scope_name)
        if not isinstance(entries, dict):
            return
        entries.pop(server, None)
        if (
            not entries
            and self.target_is_repo()
            and scope_name in _scope_names(self.global_doc)
        ):
            del scopes[scope_name]

    def make_target_entry(self, tools: list[str] | None, base: Any) -> Any:
        """Build an entry for the target document's shape."""
        return make_entry(
            tools,
            base,
            categorical=_categorical_document(self.target_doc()),
        )

    # -- durable selection ----------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode not in (MODE_GLOBAL, MODE_LOCAL_REPO):
            raise ConfigError(f"Unknown mode {mode!r}.")
        self.mode = mode

    def set_active_scope(self, name: str | None) -> None:
        if name is not None and name not in self.scope_names():
            raise ConfigError(f"Unknown scope {name!r}.")
        self.active_scope = name

    def _apply_selection(self) -> None:
        if self.mode == MODE_GLOBAL:
            self.global_doc["mode"] = MODE_GLOBAL
            self.global_doc["active_scope"] = self.active_scope
            # Switching to global clears the repo declaration; it must not
            # rewrite the repo's server or scope entries.
            self.repo_doc.pop("mode", None)
            self.repo_doc.pop("active_scope", None)
        else:
            self.repo_doc["mode"] = MODE_LOCAL_REPO
            self.repo_doc["active_scope"] = self.active_scope
            self.repo_exists = True

    # -- saving ----------------------------------------------------------

    def _bump_version(self, doc: dict[str, Any]) -> None:
        version = doc.get("version", 1)
        has_selection = "mode" in doc or "active_scope" in doc
        if has_selection or version == CONFIG_VERSION:
            doc["version"] = CONFIG_VERSION
        elif _categorical_document(doc):
            doc["version"] = 2
        else:
            doc["version"] = 1

    @staticmethod
    def _assert_registration_untouched(
        original: dict[str, Any], current: dict[str, Any], path: Path
    ) -> None:
        if _mapping(original.get("mcp_servers")) != _mapping(
            current.get("mcp_servers")
        ):
            raise ConfigError(
                "Refusing to write registration changes from the scope editor "
                f"({path}); use `mcp-router add` / `mcp-router remove`."
            )

    def save(self, *, write_selection: bool = True) -> list[Path]:
        """Write the edited documents atomically, returning the paths written.

        The selection keys are written last as part of the same transaction:
        local mode writes them to the repo file, global mode writes them to the
        global file and clears the repo declaration.
        """
        if write_selection:
            self._apply_selection()

        written: list[Path] = []
        for path, doc in (
            (self.global_path, self.global_doc),
            (self.repo_path, self.repo_doc),
        ):
            original = self._originals.get(path, {})
            self._assert_registration_untouched(original, doc, path)
            if doc == original:
                continue
            if path == self.repo_path and not self.repo_exists and not doc:
                continue
            # A repo document that only holds a cleared declaration is noise;
            # keep it only when it has real content.
            self._bump_version(doc)
            if path == self.repo_path and not self.repo_exists:
                self.repo_exists = True
            write_raw_config(path, doc)
            self._originals[path] = copy.deepcopy(doc)
            written.append(path)
        return written

    # -- read-only helpers for the view ---------------------------------

    def shadowed_servers(self, scope_name: str | None = None) -> set[str]:
        """Servers the repo file overrides for this scope (global mode marks)."""
        scope_name = scope_name or self.edited_scope
        return set(scope_entries(self.repo_doc, scope_name))

    def inherited_servers(self, scope_name: str | None = None) -> set[str]:
        """Servers inherited from global by a local-repo scope."""
        scope_name = scope_name or self.edited_scope
        return set(scope_entries(self.global_doc, scope_name))


__all__ = [
    "ScopeEditor",
    "ServerRow",
    "entry_tools",
    "make_entry",
    "read_raw_files",
    "scope_entries",
]
