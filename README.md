# mcp-router

**One MCP server that fronts many.** `mcp-router` is a Model Context Protocol
router and proxy: connect your agent to this single MCP server and it discovers
the tools of every server behind it on demand — instead of loading all of their
tool definitions into the context window, and paying for them in tokens, on every
step.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

## Why mcp-router

- **One MCP server instead of many.** Register `mcp-router` once and list every
  other MCP server in its config; it starts local servers over stdio and reaches
  remote ones over HTTP or SSE.
- **Dynamic discovery.** The agent searches for tools and resources at runtime
  rather than having every server's catalog installed up front, so it only pays
  for what it looks up.
- **Lower context and token cost.** The agent sees a few router tool definitions,
  not every server's full catalog. In the [benchmark](#context-savings-benchmark)
  sample run, registration-only input context falls 72.5% at 10 servers and an
  end-to-end session saves 55.5% of its input tokens.
- **Per-connection scopes.** Each client or project sees only the servers and
  tools, resources and prompts it needs, chosen by the launching environment.
- **CLI-only registry changes.** Adding and removing servers is a command, never
  a tool call, so it never occupies an agent's context.
- **Works with any MCP client.** The router is a plain stdio MCP server, so
  Claude, opencode, and other Model Context Protocol clients point at it the same
  way.

## Table of Contents

- [Why mcp-router](#why-mcp-router)
- [How it works](#how-it-works)
- [Install](#install)
- [Agent skill](#agent-skill)
- [Configure](#configure)
  - [Secrets](#secrets)
  - [Scopes](#scopes)
- [CLI](#cli)
- [End-to-end tests](#end-to-end-tests)
- [Context-savings benchmark](#context-savings-benchmark)
  - [Analytical estimate](#analytical-estimate)
- [Connecting a client](#connecting-a-client)
- [Behaviour](#behaviour)

## How it works

Instead of connecting an agent to every MCP server (and paying for all their
tools up front), connect it to `mcp-router` alone. The router's surface follows
the capabilities of the servers behind it: `get_installed_mcps` is always
present, the tools pair appears when at least one scope-visible server offers
tools, and the resources pair appears when one offers resources. Reach
everything through those tools:

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
4. `search_resources(regex_filter?, mcp_name?)` — find resources and resource
   templates in one call, matching server and resource names, descriptions and
   URIs. Results are compact `{mcp_name, name, uri, description, mimeType}`
   summaries; a resource template also carries `template: true`. Cross-server
   results have the shape `{resources: [...], unreachable_servers: [...]}` and
   are capped like tool searches.
5. `read_resource(mcp_name, uri)` — read one resource, or an expanded resource
   template, and return its contents as embedded resources.

v2.0 collapses discovery into `search_tools` so the common path is
`search_tools` → `run_mcp_tool` instead of one round trip per step. A filter
that matches a server name or description narrows the scan to that server and
returns its catalog; use a tool keyword to search all servers. The surface is
dynamic, not a fixed count: a connection whose servers offer no tools omits the
tools pair, and one whose servers offer no resources omits the resource pair.
Prompt tools arrive in a later release.

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

A scope is a named, per-connection allow-list of servers and, optionally, their
tools, resources and prompts. Select one with `MCP_ROUTER_SCOPE=<name>` (or
`mcp-router serve --scope <name>`). With no scope selected, every registered
server is reachable.

```yaml
version: 2
scopes:
  support:
    tickets: null                  # all ticket tools (legacy shorthand)
  readonly:
    filesystem:
      tools: ["read_file", "list_directory"]
      resources: ["file:///home/simo/notes.md", "file:///home/simo/docs/"]
```

- `null` or `{tools: null}` — the server is visible with all its tools.
- a bare list — only those tools are listed and callable (legacy shorthand).
- a mapping — per-category allow-lists under `tools`, `resources` and
  `prompts` (prompt allow-lists are validated now; prompt tools ship later). A
  category omitted from the mapping allows none.
- a list within a category — only those names (tools, prompts) or URIs
  (resources) are visible and usable.
- an empty list — the server is visible with none of that category.
- a server absent from the scope — hidden and not callable.
- `mcp-router scopes` prints the matrix.

Resource entries match by URI. A literal entry matches only that exact URI; a
trailing `/` (such as `tickets://`) matches that prefix; and a template (such as
`docs://{path}`) matches concrete URIs in that shape, where a bare `{var}`
covers one path segment, `{+var}` spans segments and `{/var}` adds
slash-prefixed segments. Unsupported RFC 6570 operators are rejected when the
config is validated. See
[ADR 0003](docs/adr/0003-surface-tools-resources-prompts.md) for the rationale.

A document with resource or prompt allow-lists is written as `version: 2`. A
`version: 1` document keeps working unchanged, and its bare lists are read as
tools-only, so gaining the resource category never widens an existing scope.

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
provider). The `tools` column is the total catalog behind the router (N × 12
tools per server), added for scale:

```text
Registration-only (what the tool catalog costs before any work)

  N | tools | direct tokens | router tokens |   saved | savings %
-----------------------------------------------------------------
  1 |    12 |          2253 |          2225 |      28 |       1.2
  3 |    36 |          3544 |          2223 |    1321 |      37.3
  5 |    60 |          4825 |          2221 |    2604 |      54.0
 10 |   120 |          8090 |          2223 |    5867 |      72.5

End-to-end task (full session, discovery round-trips included)

  N | tools | direct tokens | router tokens |   saved | savings %
-----------------------------------------------------------------
  1 |    12 |          4626 |          7106 |   -2480 |     -53.6
  3 |    36 |          7232 |          7090 |     142 |       2.0
  5 |    60 |          9798 |          7090 |    2708 |      27.6
 10 |   120 |         16316 |          7260 |    9056 |      55.5
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
trips needed to find the target tool, and `Q` the cost of one round trip (an
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

- The router connects to every scope-visible server at startup to learn whether
  it offers tools, resources or prompts, then caches those sessions for its
  lifetime. A server that fails to connect is reported as unreachable and is
  still attempted lazily when used.
- Default timeout is 30s for connecting and for each tool call.
- Failures are returned as `isError` results, never raised.
- `search_tools` returns compact summaries by default and the child's
  `inputSchema` with `full=true`; `run_mcp_tool` returns the child's result
  verbatim, so the agent sees what it would have seen directly.
- `search_resources` returns compact resource and resource-template summaries and
  is capped at 50 results; `read_resource` returns the child's contents as
  embedded resources, preserving text or blob content and MIME type.

See `CONTEXT.md` for the glossary and `docs/adr/` for the decisions behind this
shape.
