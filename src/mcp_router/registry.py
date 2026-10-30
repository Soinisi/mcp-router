"""Mutating operations on the router config, used by the CLI.

Only the CLI writes config. The MCP tool surface is intentionally read/run
only, so registering a server never costs an agent any context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from .config import (
    ConfigError,
    RouterConfig,
    Server,
    _read_yaml,
    dump_config,
    global_config_path,
    project_config_path,
)

_SERVER_ADAPTER = TypeAdapter(Server)


def resolve_scope(scope: str) -> Path:
    if scope == "user":
        return global_config_path()
    if scope == "project":
        return project_config_path()
    raise ConfigError(f"Unknown scope {scope!r} (expected 'user' or 'project').")


def validate_server(spec: dict[str, Any]) -> Server:
    """Validate a single server entry against our schema (no secret expansion)."""
    try:
        return _SERVER_ADAPTER.validate_python(spec)
    except ValidationError as exc:
        lines = ["Invalid server spec:"]
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "mcp_servers": {}}
    data = _read_yaml(path)
    data.setdefault("version", 1)
    data.setdefault("mcp_servers", {})
    if not isinstance(data["mcp_servers"], dict):
        raise ConfigError(f"'mcp_servers' must be a mapping in {path}.")
    return data


def _write_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )


def add_server(
    name: str,
    spec: dict[str, Any],
    scope: str = "user",
    overwrite: bool = False,
) -> Server:
    if not name.strip():
        raise ConfigError("Server name must not be empty.")
    server = validate_server(spec)

    path = resolve_scope(scope)
    data = _load_raw(path)
    servers: dict[str, Any] = data["mcp_servers"]

    if name in servers and not overwrite:
        raise ConfigError(
            f"Server {name!r} already exists in {path}. Pass --overwrite to replace it."
        )

    servers[name] = server.model_dump(exclude_none=True)
    _write_raw(path, data)
    return server


def remove_server(name: str, scope: str = "user") -> list[str]:
    path = resolve_scope(scope)
    data = _load_raw(path)
    servers: dict[str, Any] = data["mcp_servers"]
    if name not in servers:
        raise ConfigError(f"Server {name!r} is not registered in {path}.")

    sibling = resolve_scope("project" if scope == "user" else "user")
    if sibling.resolve() != path.resolve() and sibling.is_file():
        sibling_data = _load_raw(sibling)
        if name not in sibling_data["mcp_servers"]:
            sibling_scopes = sibling_data.get("scopes")
            if isinstance(sibling_scopes, dict):
                conflicts = sorted(
                    scope_name
                    for scope_name, entries in sibling_scopes.items()
                    if isinstance(entries, dict) and name in entries
                )
                if conflicts:
                    raise ConfigError(
                        f"Cannot remove {name!r}: scopes in {sibling} still reference it "
                        f"({', '.join(conflicts)}). Remove {name!r} from those scopes first."
                    )

    del servers[name]

    affected: list[str] = []
    scopes = data.get("scopes")
    if isinstance(scopes, dict):
        for scope_name in list(scopes):
            entries = scopes[scope_name]
            if not isinstance(entries, dict) or name not in entries:
                continue
            del entries[name]
            affected.append(scope_name)
            if not entries:
                del scopes[scope_name]
        if not scopes:
            data.pop("scopes", None)

    _write_raw(path, data)
    return sorted(affected)


def server_summaries(config: RouterConfig) -> list[dict[str, Any]]:
    return [
        {"name": name, "description": server.description, "type": server.type}
        for name, server in sorted(config.mcp_servers.items())
    ]


__all__ = [
    "add_server",
    "remove_server",
    "resolve_scope",
    "server_summaries",
    "validate_server",
    "dump_config",
]
