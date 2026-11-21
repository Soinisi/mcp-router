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

A scope is a named allow-list of servers, and optionally of each server's
tools, resources and prompts. The active scope and the global/local-repo mode
are saved in the config and changed by a human in `mcp-router tui`; there is no
CLI verb for changing them. `MCP_ROUTER_SCOPE=<name>` (or
`mcp-router serve --scope <name>`) is only a launch pin: it may repeat the saved
scope or narrow an unrestricted saved view, but it cannot widen the saved
selection.

```yaml
version: 2
scopes:
  support:
    tickets: null                  # all ticket tools
  readonly:
    filesystem:
      tools: ["read_file", "list_directory"]
      resources: ["file:///home/simo/notes.md"]
```

`null` or `{tools: null}` = all tools, a bare list = only those tools, a
mapping = per-category `tools`/`resources`/`prompts` allow-lists (an omitted
category means none), `[]` = none, an absent server = hidden. A resource entry
matches an exact URI, a trailing-`/` prefix, or a `{var}`/`{+var}`/`{/var}`
URI template. With no scope selected, everything is reachable. `mcp-router
scopes` prints the matrix. A scope is a guardrail, not authentication.

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
`command: ["mcp-router", "serve"]`. To narrow an unrestricted saved view, add
`MCP_ROUTER_SCOPE` to the entry's environment; it cannot widen the saved
selection. Changing the saved scope is a human action in `mcp-router tui`.

## Gotchas

- Config: `~/.config/mcp-router/config.yaml`, or `$MCP_ROUTER_CONFIG`; a
  `./mcp-router.yaml` overrides it per project.
- `add` and `remove` are CLI-only; the MCP surface is read/run only.
- The MCP surface is dynamic: `get_installed_mcps` is always exposed, the
  `search_tools`/`run_mcp_tool` pair appears when a visible server offers tools,
  `search_resources`/`read_resource` when one offers resources, and
  `search_prompts`/`get_prompt` when one offers prompts.
