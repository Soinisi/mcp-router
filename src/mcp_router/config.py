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
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic import TypeAdapter

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


class RouterConfig(BaseModel):
    """The whole router configuration document."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    mcp_servers: dict[str, Server] = Field(default_factory=dict)

    def names(self) -> list[str]:
        return sorted(self.mcp_servers)


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
    return parse_config(
        {"version": config.version, "mcp_servers": _expand(config.model_dump(exclude_none=True)["mcp_servers"], environ)}
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
    """Load and merge config files; later files override earlier ones per server."""
    if paths is None:
        paths = [global_config_path(), project_config_path()]

    existing = [p for p in paths if p.is_file()]

    merged: dict[str, Any] = {"version": 1, "mcp_servers": {}}
    for path in existing:
        data = _read_yaml(path)
        version = data.get("version", 1)
        if version != 1:
            raise ConfigError(f"Unsupported config version {version!r} in {path} (expected 1).")
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ConfigError(f"'mcp_servers' must be a mapping in {path}.")
        merged["mcp_servers"].update(servers)

    env_files = [p.parent / ".env" for p in existing]
    env_files.append(Path.cwd() / ".env")
    env = build_environ(env_files, base=environ)

    config = parse_config(merged)
    return LoadedConfig(config=config, sources=existing, environ=env)


def dump_config(config: RouterConfig) -> str:
    """Serialise a config back to YAML (secrets are stored as ``${VAR}``)."""
    data = config.model_dump(exclude_defaults=False, exclude_none=True)
    data.pop("version", None)
    payload = {"version": config.version, "mcp_servers": data.get("mcp_servers", {})}
    return yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)
