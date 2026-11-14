# Single façade with dynamic discovery and a self-owned schema

An agent connects to exactly one MCP server — the router — rather than to every
registered server. The router exposes only three read/run tools
(`get_installed_mcps`, `search_tools`, `run_mcp_tool`) and pulls the rest of its
knowledge from the registered servers at runtime. This trades a little latency
for a large context win: the agent never carries the tool definitions of servers
it isn't using.

Two consequences of that shape are deliberate and easy to "fix" wrongly:

- **Mutation is CLI-only.** `add`/`remove` are not MCP tools. Exposing them would
  put two always-on tools in every agent's context forever, which is exactly the
  cost this design exists to avoid.
- **The schema is ours.** The router validates its own spec and never parses a
  foreign client's format (VS Code, Claude, opencode). Translating a foreign spec
  into ours is the caller's job. The alternative — detecting and normalising every
  client's config — was rejected as unbounded surface area for no gain, since we
  don't write to anyone else's config.

## Considered options

- **Aggregate every tool up front** (register all child tools directly): rejected —
  it defeats progressive disclosure and re-introduces the context pressure.
- **Ingest foreign config formats**: rejected in favour of the agent translating.
  The router owning one strict schema keeps validation honest.

## Consequences

- `search_tools` returns compact tool summaries by default, and the child's full
  `inputSchema` when `full=true`; `run_mcp_tool` reproduces the child's result
  shape rather than re-wrapping it. A proxy hop cannot be byte-identical to a
  direct connection, so results are faithful, not literally identical.
- `search_tools` may search across every scope-visible server when `mcp_name` is
  omitted, so discovery can be one round trip. A filter that matches a server
  name narrows the scan to that server, a broad result is capped, and a partial
  scan reports its `unreachable_servers`.
- Secrets resolve at connect time from the environment and `.env`, so listing
  servers never requires the secrets — only using a server does.
