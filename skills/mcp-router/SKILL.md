---
name: mcp-router
description: Install mcp-router and register MCP servers in it. Use when the user wants to install or set up mcp-router, or connect/add MCP servers to it.
---

# mcp-router setup

mcp-router is one MCP server that fronts many registered servers. The agent
connects to it alone and discovers the rest at runtime.

## 1. Install

Requires `uv` (Python 3.11+).

```sh
uv tool install git+ssh://git@github.com/Soinisi/mcp-router.git
```

If SSH is unavailable, clone the repository and run `uv tool install .` from its
root. Verify with `mcp-router --help`.

## 2. Register a server

`mcp-router add <name> -f <spec.yaml>` writes to the user config. Use
`--project` for `./mcp-router.yaml`, and `-f -` to read the spec from stdin.

The router has its own schema. Translate the user's server details into one of
these shapes:

```yaml
# local (stdio)
type: local
command: npx
args: ["-y", "some-server"]
env:
  SOME_KEY: "${SOME_KEY}"
cwd: /optional/workdir
description: "What this server is for"

# remote (http or sse)
type: remote
url: https://example.com/mcp
transport: http          # http | sse
headers:
  Authorization: "Bearer ${TOKEN}"
description: "What this server is for"
```

`description` defaults to the server name. Do not parse another client's config
format; translate it into this one.

## 3. Restrict access (optional)

A scope is a named allow-list of servers, and optionally of each server's tools.
Select it with `MCP_ROUTER_SCOPE=<name>` (or `mcp-router serve --scope <name>`).

```yaml
scopes:
  support:
    tickets: null                  # all ticket tools
  readonly:
    filesystem: ["read_file", "list_directory"]
```

`null` = all tools, a list = only those tools, `[]` = none, an absent server =
hidden. With no scope selected, everything is reachable. `mcp-router scopes`
prints the matrix. A scope is a guardrail, not authentication.

## 4. Secrets

`${VAR}` stays literal until a connection is made, then resolves from the
process environment plus a `.env` file next to the config (or `.env` in the
current directory). Real environment variables win. Run `mcp-router validate`
to check for unresolved secrets.

## 5. Check it

```sh
mcp-router validate
mcp-router list
```

## 6. Connect the client

Register the router as the only MCP server:

```json
{ "command": "mcp-router", "args": ["serve"] }
```

For opencode, use an `mcp` entry with `type: "local"` and
`command: ["mcp-router", "serve"]`. To restrict the view, add
`MCP_ROUTER_SCOPE` to the entry's environment.

## Gotchas

- Config: `~/.config/mcp-router/config.yaml`, or `$MCP_ROUTER_CONFIG`; a
  `./mcp-router.yaml` overrides it per project.
- `add` and `remove` are CLI-only; the MCP surface is read/run only.
- The MCP tool surface is exactly `get_installed_mcps`, `search_tools`, and
  `run_mcp_tool`.
