"""Configuration schema, loading, validation and secret expansion.

The router owns this schema and validates it strictly. It never parses a
foreign client's format: an agent translating, say, a VS Code spec into this
shape is the agent's job, not ours.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic import BeforeValidator, TypeAdapter

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised when the router configuration is missing, malformed or invalid."""


class LocalServer(BaseModel):
    """A server the router starts itself as a local (stdio) child process."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["local"]
    description: str = ""
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class RemoteServer(BaseModel):
    """A server the router reaches over the network (http or sse)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["remote"]
    description: str = ""
    url: str
    transport: Literal["http", "sse"] = "http"
    headers: dict[str, str] = Field(default_factory=dict)


Server = Annotated[Union[LocalServer, RemoteServer], Field(discriminator="type")]

#: The MCP surface categories a scope can allow-list.
SCOPE_CATEGORIES = ("tools", "resources", "prompts")

_CATEGORY_LABELS = {
    "tools": "tool name",
    "resources": "resource pattern",
    "prompts": "prompt name",
}

# RFC 6570 operator characters. Only {var}, {+var} and {/var} can be matched
# without ambiguity, so the rest are rejected at validation rather than being
# compiled to a wildcard that could authorize sibling URIs.
_RESOURCE_TEMPLATE_PART = re.compile(r"\{([^}]*)\}")
_RFC6570_OPERATORS = frozenset("+#./;?&")
_SUPPORTED_RESOURCE_OPERATORS = frozenset("+/")
_RESOURCE_VARNAME_START = re.compile(r"[A-Za-z0-9_]")


def _reject_unsupported_resource_operator(
    scope_name: str, server_name: str, pattern: str
) -> None:
    """Fail closed on resource templates that would need a wildcard matcher."""
    for placeholder in _RESOURCE_TEMPLATE_PART.findall(pattern):
        operator = placeholder[:1]
        if operator in _SUPPORTED_RESOURCE_OPERATORS:
            continue
        if operator and _RESOURCE_VARNAME_START.fullmatch(operator):
            continue
        if operator in _RFC6570_OPERATORS:
            raise ValueError(
                f"scope {scope_name!r}.{server_name!r} resource pattern "
                f"{pattern!r} uses unsupported RFC 6570 operator "
                f"'{{{operator}}}'; only {{var}}, {{+var}} and {{/var}} are "
                f"supported"
            )
        raise ValueError(
            f"scope {scope_name!r}.{server_name!r} resource pattern "
            f"{pattern!r} has a malformed template expression "
            f"'{{{placeholder}}}'; only {{var}}, {{+var}} and {{/var}} are "
            f"supported"
        )


class ScopeEntry(BaseModel):
    """Per-server allow-lists for one scope, split by MCP surface category.

    ``None`` means every item in that category is allowed, a list allows only
    the named items, and an empty list allows none. A category omitted from a
    mapping entry allows none: a scope is an allow-list, so an absent category
    cannot silently widen access. The legacy bare-list form (and a bare
    ``null``) is read as ``tools: [...]`` / ``tools: null`` with resources and
    prompts empty.

    Tool and prompt entries are names. Resource entries are exact URIs by
    default; a trailing ``/`` makes the entry a prefix (``tickets://``), and a
    template such as ``docs://{path}`` matches concrete URIs in that shape.
    Templates support ``{var}``, ``{+var}`` and ``{/var}``; other RFC 6570
    operators are rejected at validation.
    """

    model_config = ConfigDict(extra="forbid")

    tools: list[str] | None = Field(default_factory=list)
    resources: list[str] | None = Field(default_factory=list)
    prompts: list[str] | None = Field(default_factory=list)


def _normalise_scope_entry(value: Any) -> Any:
    """Read a v1 scope entry as the v2 per-category shape."""
    if value is None:
        return {"tools": None}
    if isinstance(value, list):
        return {"tools": value}
    return value


ScopeEntryInput = Annotated[ScopeEntry, BeforeValidator(_normalise_scope_entry)]


#: A document that carries the durable selection keys is version 3.
CONFIG_VERSION = 3
MODE_GLOBAL = "global"
MODE_LOCAL_REPO = "local-repo"
ScopeMode = Literal["global", "local-repo"]

#: The fallback active scope when the effective config declares no
#: ``active_scope``; the terminal editor prefers it when it exists.
DEFAULT_SCOPE = "default"


class RouterConfig(BaseModel):
    """The whole router configuration document."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1, 2, 3] = 1
    #: Durable mode selection. ``None`` means "not declared in this document".
    mode: ScopeMode | None = None
    #: Durable active-scope selection. ``None`` has no declaration meaning here;
    #: presence is tracked on the raw document, where an explicit ``null``
    #: selects the unrestricted view.
    active_scope: str | None = None
    mcp_servers: dict[str, Server] = Field(default_factory=dict)
    # scope name -> {server name -> per-category allow-lists}
    scopes: dict[str, dict[str, ScopeEntryInput]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_versioned_keys(self) -> "RouterConfig":
        if self.version < CONFIG_VERSION and (
            self.mode is not None or self.active_scope is not None
        ):
            raise ValueError(
                "'mode' and 'active_scope' require version 3"
            )
        return self

    @model_validator(mode="after")
    def _validate_scopes(self) -> "RouterConfig":
        for scope_name, scope in self.scopes.items():
            unknown = set(scope) - set(self.mcp_servers)
            if unknown:
                raise ValueError(
                    f"scope {scope_name!r} references unknown server(s): "
                    f"{', '.join(sorted(unknown))}"
                )
            for server_name, entry in scope.items():
                for category in SCOPE_CATEGORIES:
                    values = getattr(entry, category)
                    if values is None:
                        continue
                    cleaned = [value.strip() for value in values]
                    if any(not value for value in cleaned):
                        raise ValueError(
                            f"scope {scope_name!r}.{server_name!r} has an empty "
                            f"{_CATEGORY_LABELS[category]}"
                        )
                    if category == "resources":
                        for pattern in cleaned:
                            _reject_unsupported_resource_operator(
                                scope_name, server_name, pattern
                            )
                    setattr(entry, category, list(dict.fromkeys(cleaned)))
        return self

    def names(self) -> list[str]:
        return sorted(self.mcp_servers)

    def resolve_scope(self, name: str | None) -> dict[str, ScopeEntry] | None:
        """Return the named scope, or ``None`` for the unrestricted view."""
        if not name:
            return None
        if name not in self.scopes:
            known = ", ".join(sorted(self.scopes)) or "<none>"
            raise ConfigError(f"Unknown scope {name!r}. Defined scopes: {known}.")
        return self.scopes[name]


_CONFIG_ADAPTER = TypeAdapter(RouterConfig)
_SERVER_ADAPTER = TypeAdapter(Server)


def global_config_path() -> Path:
    """The user-level config, honouring XDG and an MCP_ROUTER_CONFIG override."""
    override = os.environ.get("MCP_ROUTER_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mcp-router" / "config.yaml"


def project_config_path(cwd: Path | None = None) -> Path:
    """The per-project override, read from the current directory."""
    return (cwd or Path.cwd()) / "mcp-router.yaml"


def build_environ(env_files: list[Path], base: dict[str, str] | None = None) -> dict[str, str]:
    """Merge ``.env`` files under the real environment (real env wins)."""
    merged: dict[str, str] = {}
    for env_file in env_files:
        if env_file.is_file():
            for key, value in dotenv_values(env_file).items():
                if value is not None:
                    merged[key] = value
    merged.update(base if base is not None else os.environ)
    return merged


def _expand(node: Any, environ: dict[str, str], path: str = "") -> Any:
    """Recursively expand ``${VAR}`` in every string; unresolved is fatal."""
    if isinstance(node, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environ:
                where = path or "value"
                raise ConfigError(
                    f"Unresolved secret ${{{name}}} at {where!r}. "
                    f"Set it in the environment or a .env file."
                )
            return environ[name]

        return ENV_VAR_PATTERN.sub(replace, node)
    if isinstance(node, dict):
        return {k: _expand(v, environ, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v, environ, f"{path}[{i}]") for i, v in enumerate(node)]
    return node


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config {path} must be a mapping at the top level.")
    return data


def parse_config(data: dict[str, Any]) -> RouterConfig:
    """Validate the *structure* of a raw config mapping.

    Secrets are not resolved here: ``${VAR}`` placeholders are expanded only at
    connect time by :func:`resolve_server`, so listing servers never requires
    the secrets to be present.
    """
    try:
        config = _CONFIG_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    return _apply_defaults(config)


def resolve_server(server: Server, environ: dict[str, str]) -> Server:
    """Return a copy of ``server`` with every ``${VAR}`` expanded (or raise)."""
    data = server.model_dump(exclude_none=True)
    expanded = _expand(data, environ)
    return _SERVER_ADAPTER.validate_python(expanded)


def resolve_config(config: RouterConfig, environ: dict[str, str]) -> RouterConfig:
    """Return a copy of ``config`` with all secrets expanded (or raise)."""
    servers = config.model_dump(exclude_none=True)["mcp_servers"]
    return parse_config(
        {
            "version": config.version,
            "mode": config.mode,
            "active_scope": config.active_scope,
            "mcp_servers": _expand(servers, environ),
            "scopes": {
                scope_name: {
                    server_name: entry.model_dump()
                    for server_name, entry in scope.items()
                }
                for scope_name, scope in config.scopes.items()
            },
        }
    )


def unresolved_secrets(config: RouterConfig, environ: dict[str, str]) -> list[str]:
    """Return human-readable errors for every server whose secrets can't resolve."""
    problems: list[str] = []
    for name, server in config.mcp_servers.items():
        try:
            resolve_server(server, environ)
        except ConfigError as exc:
            problems.append(f"{name}: {exc}")
    return problems


def _apply_defaults(config: RouterConfig) -> RouterConfig:
    for name, server in config.mcp_servers.items():
        if not server.description:
            server.description = name
    return config


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid router configuration:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


@dataclass
class LoadedConfig:
    """A structurally-valid config, its source files and the secret environment.

    ``mode`` and ``active_scope`` are the *resolved* durable selections. The
    raw per-file declarations stay on disk; ``repo_path`` and ``repo_exists``
    let the terminal editor show and edit the repo file even when the active
    mode ignores it.
    """

    config: RouterConfig
    sources: list[Path] = field(default_factory=list)
    environ: dict[str, str] = field(default_factory=dict)
    mode: str = MODE_GLOBAL
    active_scope: str | None = None
    global_path: Path | None = None
    repo_path: Path | None = None
    repo_exists: bool = False


def _read_versioned_document(path: Path) -> dict[str, Any]:
    """Read one raw document, checking its declared version and selection keys."""
    data = _read_yaml(path)
    version = data.get("version", 1)
    if version not in (1, 2, 3):
        raise ConfigError(
            f"Unsupported config version {version!r} in {path} "
            "(expected 1, 2 or 3)."
        )
    if version < CONFIG_VERSION and (
        data.get("mode") is not None or data.get("active_scope") is not None
    ):
        raise ConfigError(
            f"'mode' and 'active_scope' in {path} require version 3."
        )
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConfigError(f"'mcp_servers' must be a mapping in {path}.")
    scopes = data.get("scopes", {})
    if not isinstance(scopes, dict):
        raise ConfigError(f"'scopes' must be a mapping in {path}.")
    for scope_name, scope in scopes.items():
        if not isinstance(scope, dict):
            raise ConfigError(
                f"scope {scope_name!r} must be a mapping in {path}."
            )
    return data


def _has_declaration(data: dict[str, Any], key: str) -> bool:
    """True when a document declares ``key`` (an explicit null counts)."""
    return key in data


def resolve_mode(
    global_data: dict[str, Any], repo_data: dict[str, Any] | None
) -> str:
    """Resolve the durable mode repo-first, then the global declaration.

    With no declaration anywhere, a repo-level config means ``local-repo`` and
    no repo-level config means ``global``.
    """
    if repo_data is not None and repo_data.get("mode") is not None:
        return str(repo_data["mode"])
    if global_data.get("mode") is not None:
        return str(global_data["mode"])
    return MODE_LOCAL_REPO if repo_data is not None else MODE_GLOBAL


def resolve_saved_scope(
    global_data: dict[str, Any],
    repo_data: dict[str, Any] | None,
    scopes: dict[str, Any],
) -> str | None:
    """Resolve the durable active scope repo-first, then the ``default`` scope.

    An explicitly declared ``null`` means the unrestricted view; absence falls
    back to the ``default`` scope when one exists.
    """
    if repo_data is not None and _has_declaration(repo_data, "active_scope"):
        return repo_data.get("active_scope")
    if _has_declaration(global_data, "active_scope"):
        return global_data.get("active_scope")
    if DEFAULT_SCOPE in scopes:
        return DEFAULT_SCOPE
    return None


def load_config(
    paths: list[Path] | None = None,
    environ: dict[str, str] | None = None,
) -> LoadedConfig:
    """Load and merge config files under the durable mode/scope selection.

    Servers from later files override earlier ones by name. Scopes merge per
    scope name and per server, so a project scope adds to the user-level scope
    and overrides only the server entries it names. In ``global`` mode the repo
    file is ignored for the effective configuration; in ``local-repo`` mode it
    inherits the global definitions and overrides what it names.
    """
    if paths is None:
        paths = [global_config_path(), project_config_path()]

    global_path = paths[0] if paths else None
    repo_path = paths[-1] if len(paths) > 1 else None
    global_data = (
        _read_versioned_document(global_path)
        if global_path is not None and global_path.is_file()
        else {}
    )
    repo_exists = repo_path is not None and repo_path.is_file()
    repo_data = _read_versioned_document(repo_path) if repo_exists else None

    mode = resolve_mode(global_data, repo_data)
    if mode == MODE_GLOBAL:
        effective_paths = [
            p
            for p in paths
            if p == global_path and p is not None and p.is_file()
        ]
    else:
        effective_paths = [p for p in paths if p.is_file()]

    merged: dict[str, Any] = {"version": 1, "mcp_servers": {}, "scopes": {}}
    for path in effective_paths:
        data = (
            global_data
            if path == global_path
            else _read_versioned_document(path)
        )
        merged["version"] = max(merged["version"], data.get("version", 1))
        merged["mcp_servers"].update(data.get("mcp_servers", {}))
        for scope_name, scope in data.get("scopes", {}).items():
            merged["scopes"].setdefault(scope_name, {}).update(scope)

    selection_repo = None if mode == MODE_GLOBAL else repo_data
    active_scope = resolve_saved_scope(
        global_data, selection_repo, merged["scopes"]
    )
    # Only carry the selection keys in the in-memory document when they mean
    # something, so a plain v1/v2 document keeps its declared version.
    if mode != MODE_GLOBAL or active_scope is not None:
        merged["version"] = max(merged["version"], CONFIG_VERSION)
        merged["mode"] = mode
        merged["active_scope"] = active_scope

    existing = [p for p in paths if p.is_file()]
    env_files = [p.parent / ".env" for p in existing]
    env_files.append(Path.cwd() / ".env")
    env = build_environ(env_files, base=environ)

    config = parse_config(merged)
    return LoadedConfig(
        config=config,
        sources=effective_paths,
        environ=env,
        mode=mode,
        active_scope=active_scope,
        global_path=global_path,
        repo_path=repo_path,
        repo_exists=repo_exists,
    )


def resolve_launch_scope(
    loaded: LoadedConfig, launch_scope: str | None = None
) -> str | None:
    """The active scope for this connection, honouring the saved selection.

    The saved selection is authoritative: a launch-time ``--scope`` /
    ``MCP_ROUTER_SCOPE`` may only repeat it (narrowing is not computed here).
    A pin against an unrestricted saved view is allowed, because it narrows.
    """
    pin = launch_scope or os.environ.get("MCP_ROUTER_SCOPE")
    if not pin:
        return loaded.active_scope
    if loaded.active_scope is not None and pin != loaded.active_scope:
        raise ConfigError(
            f"Launch scope {pin!r} conflicts with the saved active scope "
            f"{loaded.active_scope!r}. The saved selection is authoritative; "
            "switch scopes in the terminal UI."
        )
    return pin


def resolve_reload_scope(
    loaded: LoadedConfig, launch_scope: str | None = None
) -> tuple[str | None, str | None]:
    """The active scope for a reload, where a saved change outranks the pin.

    A stale launch pin must not defeat a selection the human just saved. The
    saved selection wins and the conflict is returned for the caller to
    report; a pin still narrows an unrestricted saved view.
    """
    pin = launch_scope or os.environ.get("MCP_ROUTER_SCOPE")
    if loaded.active_scope is not None:
        conflict = None
        if pin and pin != loaded.active_scope:
            conflict = (
                f"Launch scope {pin!r} conflicts with the saved active scope "
                f"{loaded.active_scope!r}; applying the saved selection."
            )
        return loaded.active_scope, conflict
    return pin, None


def scope_uses_categories(scopes: Any) -> bool:
    """True when a scope entry needs the v2 per-category mapping shape.

    Only a resource or prompt allow-list forces version 2: a mapping carrying
    just ``tools`` means the same as the legacy bare list, so it does not.
    """
    if not isinstance(scopes, dict):
        return False
    for entries in scopes.values():
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if isinstance(entry, dict) and any(
                category in entry and entry[category] != []
                for category in ("resources", "prompts")
            ):
                return True
    return False


def _serialise_scopes(config: RouterConfig) -> tuple[int, dict[str, dict[str, Any]]]:
    """Scope payload plus the version its shape requires.

    Rule: a document that carries the durable selection keys is version 3; a
    document with resource or prompt allow-lists is at least version 2;
    otherwise it keeps the declared version and the legacy bare-list shape. A
    parsed v3 config therefore stays v3 on write, a parsed v2 config stays v2,
    and a parsed v1 config stays v1.
    """
    category_payload: dict[str, dict[str, Any]] = {
        scope_name: {
            server_name: entry.model_dump()
            for server_name, entry in entries.items()
        }
        for scope_name, entries in config.scopes.items()
    }
    needs_v3 = config.version == 3 or config.mode is not None or config.active_scope is not None
    needs_v2 = config.version == 2 or scope_uses_categories(category_payload)
    version = 3 if needs_v3 else 2 if needs_v2 else 1

    if version >= 2:
        return version, category_payload

    payload: dict[str, dict[str, Any]] = {
        scope_name: {
            server_name: None if entry.tools is None else list(entry.tools)
            for server_name, entry in entries.items()
        }
        for scope_name, entries in config.scopes.items()
    }
    return version, payload


def dump_config(config: RouterConfig) -> str:
    """Serialise a config back to YAML (secrets are stored as ``${VAR}``)."""
    version, scopes = _serialise_scopes(config)
    payload: dict[str, Any] = {
        "version": version,
        "mcp_servers": config.model_dump(exclude_none=True)["mcp_servers"],
    }
    if config.mode is not None:
        payload["mode"] = config.mode
    if config.active_scope is not None:
        payload["active_scope"] = config.active_scope
    if scopes:
        payload["scopes"] = scopes
    return yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` atomically, keeping one previous generation as ``.bak``.

    A running router reads the config while the CLI or terminal editor rewrites
    it, and editors save by rename; writing a temp file in the same directory
    and renaming it means a reader never sees a half-written document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    if path.is_file():
        shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def dump_raw_config(data: dict[str, Any]) -> str:
    """Serialise a raw (unmerged) document, preserving its ``${VAR}`` strings."""
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False)


def write_raw_config(path: Path, data: dict[str, Any]) -> None:
    """Atomically write one raw config document."""
    atomic_write_text(path, dump_raw_config(data))
