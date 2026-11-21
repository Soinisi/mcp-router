"""Mutating operations on the router config, used by the CLI.

Only the CLI writes config. The MCP tool surface is intentionally read/run
only, so registering a server never costs an agent any context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .config import (
    CONFIG_VERSION,
    ConfigError,
    RouterConfig,
    Server,
    _read_yaml,
    dump_config,
    global_config_path,
    project_config_path,
    scope_uses_categories,
    write_raw_config,
)

_SERVER_ADAPTER = TypeAdapter(Server)


def resolve_target(target: str) -> Path:
    if target == "user":
        return global_config_path()
    if target == "project":
        return project_config_path()
    raise ConfigError(f"Unknown target {target!r} (expected 'user' or 'project').")


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
    # A document that carries the durable selection keys is version 3; one with
    # (or already declared) per-category scopes is version 2; otherwise a CLI
    # mutation preserves the declared version.
    if (
        data.get("version") == CONFIG_VERSION
        or data.get("mode") is not None
        or data.get("active_scope") is not None
    ):
        data["version"] = CONFIG_VERSION
    elif data.get("version") == 2 or scope_uses_categories(data.get("scopes")):
        data["version"] = 2
    else:
        data.setdefault("version", 1)
    # Writers replace the file by rename so a running router never reads a
    # partial document. ``registry`` is the only CLI mutation path.
    write_raw_config(path, data)


def add_server(
    name: str,
    spec: dict[str, Any],
    target: str = "user",
    overwrite: bool = False,
) -> Server:
    if not name.strip():
        raise ConfigError("Server name must not be empty.")
    server = validate_server(spec)

    path = resolve_target(target)
    data = _load_raw(path)
    servers: dict[str, Any] = data["mcp_servers"]

    if name in servers and not overwrite:
        raise ConfigError(
            f"Server {name!r} already exists in {path}. Pass --overwrite to replace it."
        )

    servers[name] = server.model_dump(exclude_none=True)
    _write_raw(path, data)
    return server


def remove_server(name: str, target: str = "user") -> list[str]:
    path = resolve_target(target)
    data = _load_raw(path)
    servers: dict[str, Any] = data["mcp_servers"]
    if name not in servers:
        raise ConfigError(f"Server {name!r} is not registered in {path}.")

    sibling = resolve_target("project" if target == "user" else "user")
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
    "resolve_target",
    "server_summaries",
    "validate_server",
    "dump_config",
]
