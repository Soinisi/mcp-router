# mcp-router

One MCP server that dynamically discovers and proxies other MCP servers.

Instead of connecting an agent to every MCP server (and paying for all their
tools up front), connect it to `mcp-router` alone. The agent sees exactly three
tools and reaches everything else through them:

1. `get_installed_mcps(regex_filter?)` — the registered servers. The filter
   matches server names and descriptions.
2. `get_mcp_tools(mcp_name, regex_filter?)` — the tools one server exposes. The
   filter matches tool names and descriptions.
3. `run_mcp_tool(mcp_name, mcp_tool, arguments?)` — invoke one of them.

Adding and removing servers is done from the CLI, never through the agent, so
those operations never occupy an agent's context.

## Install

```sh
uv tool install .        # provides `mcp-router` (and `mcp-router-server`)
```

## Agent skill

An agent skill that installs the router and registers servers lives at
`skills/mcp-router/SKILL.md`. Link it into each client's skills directory to
make it available globally (for example `~/.agents/skills` and
`~/.claude/skills`):

```sh
ln -s "$PWD/skills/mcp-router" ~/.agents/skills/mcp-router
ln -s "$PWD/skills/mcp-router" ~/.claude/skills/mcp-router
```

## Configure

`~/.config/mcp-router/config.yaml` (optionally overridden per project by
`./mcp-router.yaml`):

```yaml
version: 1
mcp_servers:
  microsoftdocs:
    description: "Microsoft Learn docs search"
    type: remote
    url: https://learn.microsoft.com/api/mcp
    transport: http
    headers:
      Authorization: "Bearer ${MSDOCS_TOKEN}"

  filesystem:
    description: "Local filesystem access"
    type: local
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/simo"]
    env:
      SOME_KEY: "${SOME_KEY}"
    cwd: /home/simo
```

- `type: local` requires `command`; optional `args`, `env`, `cwd`.
- `type: remote` requires `url`; optional `transport` (`http` default, or `sse`)
  and `headers`.
- `description` defaults to the server name.

### Secrets

`${VAR}` in any string resolves from the process environment plus a `.env` file
next to the config (or in the current directory). Real environment variables
win over `.env`. An unresolved variable is a hard error — the router never
sends a literal `${VAR}` to a child.

### Scopes

A scope is a named, per-connection allow-list of servers and, optionally, tools.
Select one with `MCP_ROUTER_SCOPE=<name>` (or `mcp-router serve --scope <name>`).
With no scope selected, every registered server is reachable.

```yaml
scopes:
  support:
    tickets: null                  # all ticket tools
  readonly:
    filesystem: ["read_file", "list_directory"]
```

- `null` — the server is visible with all its tools.
- a list — only those tools are listed and callable.
- an empty list — the server is visible with no tools.
- a server absent from the scope — hidden and not callable.
- `mcp-router scopes` prints the matrix.

A scope may only name registered servers, and serving with an undefined scope
name fails at startup.

Scopes merge across config files per server, like `mcp_servers`: a project scope
with the same name adds servers to the user-level scope and overrides the entry
for any server it names.

A scope is a guardrail selected by the launching client's environment, not an
authentication boundary.

## CLI

```sh
mcp-router list                 # registered servers, one per line
mcp-router tools filesystem     # the tools one server exposes
mcp-router validate             # validate the config
mcp-router scopes               # list scopes and their allow-lists
mcp-router serve [--scope NAME] # run as an MCP server over stdio
mcp-router add filesystem -f spec.yaml [--project] [--overwrite]
mcp-router remove filesystem [--project]
```

`add` takes **our** spec (a single server mapping); it does not parse VS Code or
any other client's format.

`remove` also drops the server from scopes in the config file it edits. If the
other config file's scopes still name the server and that file does not define
it, `remove` refuses and names the file and scope(s) rather than leaving a
dangling reference.

## End-to-end tests

The LLM-driven tests drive the router through a real agent (`opencode`) against
a local hello-world server (`tests/e2e/hello_server.py`) and a mock
customer-ticket server (`tests/e2e/ticket_server.py`). Prompts are plain user
goals ("Create a customer ticket...", "How many open tickets...") and do not
tell the model which tool to use. The hello test proves selection with a random
nonce; the ticket tests assert the server's JSON store, and the count test also
checks that the store is unchanged. They are excluded from the default suite.

```sh
uv run pytest -m e2e --e2e-model openai/gpt-6-luna
```

- `--e2e-model` — model in `provider/model` form (default `openai/gpt-6-luna`).
- `E2E_TIMEOUT` — per-run timeout in seconds (default 300).
- `E2E_KEEP_TMP=1` — keep the generated temp project for debugging.

The LLM tests require `opencode` on `PATH` with a configured provider and skip
when it or the model is unavailable. A separate deterministic test
(`tests/e2e/test_learn_remote.py`) exercises the Microsoft Learn remote path
through the router without an LLM and needs network access to
`learn.microsoft.com`. `tests/test_sse_transport.py` starts a local SSE server
and drives it through the router, so the legacy `sse` transport is covered by
the default suite.

## Context-savings benchmark

`scripts/context_benchmark.py` measures what the router actually saves: it drives
a real `opencode` session twice for several server counts N, once with opencode
connected directly to N synthetic local MCP servers and once through
`mcp-router`, then reads the input-token count of the first model step from
opencode's JSON event stream. The synthetic servers expose realistic tool
definitions with names, descriptions and inputSchemas; Code Mode is turned off
so both configurations place the tools on the provider's native tool list.

```sh
uv run python scripts/context_benchmark.py --model openai/gpt-6-luna
uv run python scripts/context_benchmark.py --n 1,3,5,10 --model openai/gpt-5-nano
```

- `--model` — `provider/model` (default `openai/gpt-6-luna`, the e2e default).
- `--n` — comma-separated server counts (default `1,3,5,10`).
- `--timeout` — per-run timeout in seconds (default 300).
- `--keep-tmp` — keep the generated projects for debugging.

The table reports `N | direct tokens | router tokens | saved | savings %` and
whether savings grow with N. Token figures are the first step's
`input + cache.read + cache.write`, so cached and uncached runs report the same
full context.

The same measurement is available as an opt-in pytest test (excluded from the
default suite by the `benchmark` marker):

```sh
uv run pytest -m benchmark --e2e-model openai/gpt-6-luna
MCP_ROUTER_BENCH_N=1,5,10 uv run pytest -m benchmark --e2e-model openai/gpt-6-luna
```

The test asserts that the router uses fewer input tokens for every N and that
the saving grows as N grows. `MCP_ROUTER_BENCH_TIMEOUT` overrides its per-run
timeout.

## Connecting a client

Point your MCP client at the stdio server:

```json
{ "command": "mcp-router", "args": ["serve"] }
```

## Behaviour

- Sessions are opened lazily on first use and cached for the router's lifetime.
- Default timeout is 30s for connecting and for each tool call.
- Failures are returned as `isError` results, never raised.
- `get_mcp_tools` and `run_mcp_tool` return the child's results with their
  standard MCP shapes, so the agent sees what it would have seen directly.

See `CONTEXT.md` for the glossary and `docs/adr/` for the decisions behind this
shape.
