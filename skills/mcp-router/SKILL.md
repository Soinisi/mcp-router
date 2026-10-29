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

## 3. Secrets

`${VAR}` stays literal until a connection is made, then resolves from the
process environment plus a `.env` file next to the config (or `.env` in the
current directory). Real environment variables win. Run `mcp-router validate`
to check for unresolved secrets.

## 4. Check it

```sh
mcp-router validate
mcp-router list
```

## 5. Connect the client

Register the router as the only MCP server:

```json
{ "command": "mcp-router", "args": ["serve"] }
```

For opencode, use an `mcp` entry with `type: "local"` and
`command: ["mcp-router", "serve"]`.

## Gotchas

- Config: `~/.config/mcp-router/config.yaml`, or `$MCP_ROUTER_CONFIG`; a
  `./mcp-router.yaml` overrides it per project.
- `add` and `remove` are CLI-only; the MCP surface is read/run only.
- The MCP tool surface is exactly `get_installed_mcps`, `get_mcp_tools`, and
  `run_mcp_tool`.
