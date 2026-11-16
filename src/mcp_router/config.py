"""Configuration schema, loading, validation and secret expansion.

The router owns this schema and validates it strictly. It never parses a
foreign client's format: an agent translating, say, a VS Code spec into this
shape is the agent's job, not ours.
"""

from __future__ import annotations

import os
import re
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


class RouterConfig(BaseModel):
    """The whole router configuration document."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1, 2] = 1
    mcp_servers: dict[str, Server] = Field(default_factory=dict)
    # scope name -> {server name -> per-category allow-lists}
    scopes: dict[str, dict[str, ScopeEntryInput]] = Field(default_factory=dict)

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
    """A structurally-valid config, its source files and the secret environment."""

    config: RouterConfig
    sources: list[Path] = field(default_factory=list)
    environ: dict[str, str] = field(default_factory=dict)


def load_config(
    paths: list[Path] | None = None,
    environ: dict[str, str] | None = None,
) -> LoadedConfig:
    """Load and merge config files.

    Servers from later files override earlier ones by name. Scopes merge per
    scope name and per server, so a project scope adds to the user-level scope
    and overrides only the server entries it names.
    """
    if paths is None:
        paths = [global_config_path(), project_config_path()]

    existing = [p for p in paths if p.is_file()]

    merged: dict[str, Any] = {"version": 1, "mcp_servers": {}, "scopes": {}}
    for path in existing:
        data = _read_yaml(path)
        version = data.get("version", 1)
        if version not in (1, 2):
            raise ConfigError(
                f"Unsupported config version {version!r} in {path} "
                "(expected 1 or 2)."
            )
        merged["version"] = max(merged["version"], version)
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ConfigError(f"'mcp_servers' must be a mapping in {path}.")
        merged["mcp_servers"].update(servers)
        scopes = data.get("scopes", {})
        if not isinstance(scopes, dict):
            raise ConfigError(f"'scopes' must be a mapping in {path}.")
        for scope_name, scope in scopes.items():
            if not isinstance(scope, dict):
                raise ConfigError(
                    f"scope {scope_name!r} must be a mapping in {path}."
                )
            merged["scopes"].setdefault(scope_name, {}).update(scope)

    env_files = [p.parent / ".env" for p in existing]
    env_files.append(Path.cwd() / ".env")
    env = build_environ(env_files, base=environ)

    config = parse_config(merged)
    return LoadedConfig(config=config, sources=existing, environ=env)


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

    Rule: a document is written as version 2 as soon as it contains (or asks
    for) resource or prompt allow-lists; otherwise it keeps the declared
    version and the legacy bare-list shape. A parsed v2 config therefore stays
    v2 on write, and a parsed v1 config stays v1.
    """
    category_payload: dict[str, dict[str, Any]] = {
        scope_name: {
            server_name: entry.model_dump()
            for server_name, entry in entries.items()
        }
        for scope_name, entries in config.scopes.items()
    }
    needs_v2 = config.version == 2 or scope_uses_categories(category_payload)
    version = 2 if needs_v2 else 1

    if version == 2:
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
    if scopes:
        payload["scopes"] = scopes
    return yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)
