"""Command-line interface for mcp-router.

The CLI owns every mutation (add / remove): registering a server must never
occupy an agent's context, so it is deliberately not exposed as an MCP tool.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml

from .config import (
    ConfigError,
    LoadedConfig,
    global_config_path,
    load_config,
    project_config_path,
    unresolved_secrets,
)
from .registry import add_server, remove_server, resolve_target, server_summaries
from .sessions import SessionManager
from .server import run_server

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="A single MCP server that dynamically discovers and proxies other MCP servers.",
)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _load_or_fail() -> LoadedConfig:
    try:
        return load_config()
    except ConfigError as exc:
        _fail(str(exc))


@app.command()
def serve(
    scope: Optional[str] = typer.Option(
        None, "--scope", help="Restrict this connection to a named scope."
    ),
) -> None:
    """Run the router as an MCP server over stdio (validates config on startup)."""
    try:
        asyncio.run(run_server(scope=scope))
    except ConfigError as exc:
        _fail(str(exc))


@app.command()
def scopes() -> None:
    """List the configured scopes and their server/tool allow-lists."""
    loaded = _load_or_fail()
    if not loaded.config.scopes:
        typer.echo("No scopes defined.")
        return
    for name in sorted(loaded.config.scopes):
        typer.echo(name)
        entries = loaded.config.scopes[name]
        if not entries:
            typer.echo("  (no servers)")
            continue
        for server in sorted(entries):
            tools = entries[server]
            if tools is None:
                shown = "all tools"
            elif tools:
                shown = ", ".join(tools)
            else:
                shown = "no tools"
            typer.echo(f"  {server}: {shown}")


@app.command("list")
def list_servers() -> None:
    """List the registered MCP servers."""
    loaded = _load_or_fail()
    if not loaded.sources:
        typer.echo("No router config found (looked in "
                   f"{global_config_path()} and {project_config_path()}).")
        return
    for summary in server_summaries(loaded.config):
        typer.echo(f"{summary['name']}\t[{summary['type']}]\t{summary['description']}")


@app.command()
def validate() -> None:
    """Validate the router configuration and the secrets it references."""
    loaded = _load_or_fail()
    if not loaded.sources:
        _fail("No router config found.")
    problems = unresolved_secrets(loaded.config, loaded.environ)
    if problems:
        _fail("Unresolved secrets:\n" + "\n".join(f"  - {p}" for p in problems))
    typer.secho(
        f"OK: {len(loaded.config.mcp_servers)} server(s) valid "
        f"({', '.join(str(s) for s in loaded.sources)}).",
        fg=typer.colors.GREEN,
    )


@app.command()
def tools(mcp_name: str = typer.Argument(..., help="Server name from `mcp-router list`.")) -> None:
    """List the tools exposed by one registered server."""
    loaded = _load_or_fail()

    async def _run() -> None:
        manager = SessionManager(loaded.config, environ=loaded.environ)
        try:
            session = await manager.session(mcp_name)
            result = await session.call(lambda s: s.list_tools())
            payload = [
                t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in result.tools
            ]
            typer.echo(json.dumps(payload, indent=2))
        finally:
            await manager.aclose()

    try:
        asyncio.run(_run())
    except (ConfigError, TimeoutError) as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"{type(exc).__name__}: {exc}")


@app.command()
def add(
    mcp_name: str = typer.Argument(..., help="Name to register the server under."),
    file: Optional[Path] = typer.Option(
        None, "--file", "-f", help="Path to a spec file; use '-' to read stdin."
    ),
    project: bool = typer.Option(False, "--project", help="Write to ./mcp-router.yaml."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing server."),
) -> None:
    """Register a server from our spec (single entry, not a foreign format)."""
    spec = _read_spec(file)
    target = "project" if project else "user"
    try:
        add_server(mcp_name, spec, target=target, overwrite=overwrite)
    except ConfigError as exc:
        _fail(str(exc))
    typer.secho(f"Added {mcp_name!r} to {resolve_target(target)}.", fg=typer.colors.GREEN)


@app.command()
def remove(
    mcp_name: str = typer.Argument(..., help="Name of the server to remove."),
    project: bool = typer.Option(False, "--project", help="Remove from ./mcp-router.yaml."),
) -> None:
    """Remove a registered server."""
    target = "project" if project else "user"
    try:
        affected = remove_server(mcp_name, target=target)
    except ConfigError as exc:
        _fail(str(exc))
    message = f"Removed {mcp_name!r} from {resolve_target(target)}."
    if affected:
        message += f" Updated scopes: {', '.join(affected)}."
    typer.secho(message, fg=typer.colors.GREEN)


def _read_spec(file: Optional[Path]) -> dict:
    if file is None:
        _fail("Provide a spec with --file (or '-' for stdin).")
    try:
        text = sys.stdin.read() if str(file) == "-" else Path(file).read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"Cannot read spec {file}: {exc}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _fail(f"Invalid YAML/JSON in spec {file}: {exc}")
    if not isinstance(data, dict):
        _fail("Spec must be a mapping describing a single server.")
    return data


def main() -> None:
    app()


if __name__ == "__main__":
    main()
