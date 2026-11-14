# mcp-router

One MCP server that dynamically discovers and proxies other MCP servers.

Instead of connecting an agent to every MCP server (and paying for all their
tools up front), connect it to `mcp-router` alone. The agent sees exactly three
tools and reaches everything else through them:

1. `get_installed_mcps(regex_filter?)` — the registered servers. The filter
   matches server names and descriptions; `check=true` also reports reachability
   and tool counts. Use it to browse or diagnose, not as a required first step.
2. `search_tools(regex_filter?, mcp_name?, full?)` — find tools in one call.
   With `mcp_name`, search that server; without it, search every scope-visible
   server, matching server and tool names/descriptions and tagging each result
   with its `mcp_name`. With neither, list the servers. Results are compact
   `{name, description, signature, mcp_name}` summaries; `full=true` returns the
   complete `inputSchema`. Cross-server results also carry an
   `unreachable_servers` note, and a broad search over the 50-result cap is
   refused with a "narrow the filter" message.
3. `run_mcp_tool(mcp_name, mcp_tool, arguments?)` — invoke one of them.

v2.0 collapses discovery into `search_tools` so the common path is
`search_tools` → `run_mcp_tool` instead of one round trip per step. A filter
that matches a server name or description narrows the scan to that server; use a
tool keyword to search all servers. The three-tool surface is unchanged.

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

`scripts/context_benchmark.py` measures what the router actually saves by
driving a real `opencode` agent against N synthetic local MCP servers in two
configurations: **direct** (opencode registers all N servers) and **router**
(opencode registers only `mcp-router`, which fronts the same N). It reports two
measurements:

- **Registration-only** — a fixed `reply ok` prompt. The metric is the first
  model step's input context: the system prompt, every tool definition, and the
  prompt.
- **End-to-end** — a real task that requires finding and calling a tool on one
  specific server (`postgres.list_databases`). Only that server returns the
  expected payload, which the harness checks, so a run that answers without it
  is rejected. The metric sums every step's input context, so the router's
  `search_tools` → `run_mcp_tool` discovery round-trips are charged to it.

```sh
uv run python scripts/context_benchmark.py --model openai/gpt-6-luna
uv run python scripts/context_benchmark.py --mode end-to-end --n 1,3,5,10
uv run python scripts/context_benchmark.py --mode registration
```

- `--model` — `provider/model` (default `openai/gpt-6-luna`, the e2e default).
- `--n` — comma-separated server counts (default `1,3,5,10`).
- `--mode` — `registration`, `end-to-end`, or `both` (default `both`).
- `--timeout` — per-run timeout in seconds (default 300).
- `--keep-tmp` — keep the generated projects for debugging.

Registration-only and end-to-end measure different things, and the code reports
both. Example run with `openai/gpt-6-luna` (absolute counts vary by model and
provider):

```text
Registration-only (what the tool catalog costs before any work)

  N | direct tokens | router tokens |   saved | savings %
---------------------------------------------------------
  1 |          2253 |          2225 |      28 |       1.2
  3 |          3544 |          2223 |    1321 |      37.3
  5 |          4825 |          2221 |    2604 |      54.0
 10 |          8090 |          2223 |    5867 |      72.5

End-to-end task (full session, discovery round-trips included)

  N | direct tokens | router tokens |   saved | savings %
---------------------------------------------------------
  1 |          4626 |          7106 |   -2480 |     -53.6
  3 |          7232 |          7090 |     142 |       2.0
  5 |          9798 |          7090 |    2708 |      27.6
 10 |         16316 |          7260 |    9056 |      55.5
```

This is the crossover the benchmark is meant to show: at small N the router's
discovery round-trips cost more than the tools it avoids, so direct can win. In
this run the router costs 2,480 more at N=1, first saves at N=3 (+142), and
reaches +9,056 at N=10. Registration always saves, but at N=1 the saving is
small (28 tokens) because the router's own three tool definitions are close to
one server's catalog. As N grows, direct repeats N full tool catalogs on every
model step while the router keeps showing only three tools, so the end-to-end
saving grows. A broad cross-server search can grow the router's own output too;
the 50-result cap and the compact summaries bound it.

### Analytical estimate

The measured numbers include everything the agent actually did, so they move with
the model, provider and task. The end-to-end report also prints an analytical
estimate of the break-even point, and of when routing saves enough to be worth
it, next to the measured crossover. The model, with `M` total tools behind the
router and `S` model steps in a session, is:

```text
direct = S × M × P                         # every tool definition, re-sent each step
router = S × 3 × P + R × Q                 # three router tools + discovery round trips
```

`P` is the token cost of one tool definition, `R` the number of discovery round
trips needed to find the target server, and `Q` the cost of one round trip (an
extra model step re-sending the base prompt plus the discovery output). Setting
the two costs equal gives the break-even tool count:

```text
M* = 3 + R × Q / (S × P)
```

Requiring a saving of at least `m` of direct's tool-catalog cost (the base prompt is
excluded because both configurations pay it) gives the point where routing is
clearly worth it:

```text
M_worth = M* / (1 - m)
```

The defaults model the benchmark's current behaviour: 12 tools per server, ~55
tokens per tool definition, 2 model steps, one cross-server `search_tools` call
at ~3500 tokens (the extra model step plus its output), and a 25% margin:

```text
break-even  M* = 3 + 3500 / 110 = 34.8 -> 35 tools  (~2.9 servers at 12 tools each)
worth it    M  = 34.8 / 0.75    = 46.4 -> 47 tools  (~3.9 servers at 12 tools each)
```

So with today's single discovery call, routing only stops costing extra context
once roughly 35 tools are registered, and only saves enough to clearly justify
itself at around 47. Below that, a direct connection is the cheaper choice unless
the session is long. Longer sessions move both counts down, because direct
re-sends the whole catalog on every step while the router pays its discovery cost
once. This assumes a task finds its tool in the first cross-server search; a task
that searches server by server pays one round trip per server, which pushes the
break-even higher as N grows. Each round trip is charged once; re-sending its
output on later steps is not modeled. These are assumptions rather than
measurements; they track one `search_tools` call and should be revisited if
discovery changes again.

The same measurement is available as an opt-in pytest test (excluded from the
default suite by the `benchmark` marker):

```sh
uv run pytest -m benchmark --e2e-model openai/gpt-6-luna
MCP_ROUTER_BENCH_N=1,5,10 uv run pytest -m benchmark --e2e-model openai/gpt-6-luna
```

The test asserts that registration-only always saves and grows, that the
end-to-end router wins at the largest N, and that the end-to-end saving grows
from the smallest to the largest N. It deliberately does not assert a positive
end-to-end saving at N=1, where discovery overhead can exceed the avoided tools.
`MCP_ROUTER_BENCH_TIMEOUT` overrides its per-run timeout.

## Connecting a client

Point your MCP client at the stdio server:

```json
{ "command": "mcp-router", "args": ["serve"] }
```

## Behaviour

- Sessions are opened lazily on first use and cached for the router's lifetime.
- Default timeout is 30s for connecting and for each tool call.
- Failures are returned as `isError` results, never raised.
- `search_tools` returns compact summaries by default and the child's
  `inputSchema` with `full=true`; `run_mcp_tool` returns the child's result
  verbatim, so the agent sees what it would have seen directly.

See `CONTEXT.md` for the glossary and `docs/adr/` for the decisions behind this
shape.
