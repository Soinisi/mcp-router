# Scopes: soft per-connection allow-lists

_Extended by ADR 0003: scope entries may now carry per-category allow-lists for
tools, resources and prompts, and the MCP surface is dynamic rather than a fixed
three tools. The scope semantics below apply within each category._

The router exposes a generic discovery surface rather than one MCP tool per
underlying tool, so a client's per-tool permission system cannot express "this
agent may use `tickets` but not `filesystem`": every underlying tool is an
argument to `run_mcp_tool`. To give each agent a narrower view without adding MCP
tools (which would cost every agent context), a scope is a named map from
`mcp_name` to an optional tool allow-list, selected by `MCP_ROUTER_SCOPE` (or
`serve --scope`). `get_installed_mcps` lists only the scope's servers,
`search_tools` filters to the allow-list, and `run_mcp_tool` rejects out-of-scope
servers and tools.

Semantics: `null` = all tools, a list = only those tools, `[]` = none, an absent
server = hidden. No scope selected = unrestricted.

Scopes merge across the user and project config files per server, the same way
`mcp_servers` do: a project scope with the same name adds servers and overrides
individual server entries, rather than replacing the whole scope.

## Consequences

- It is a guardrail, not an authentication boundary: the scope comes from the
  client's environment, and an agent with shell access could launch the router
  with a wider scope. Real isolation would need an authenticated remote router
  and identity-based policy.
- Tool names cannot be validated at load (they are discovered on connect), so a
  scope may name a tool the server no longer has; it simply never appears.
- Scope filtering lives in the tool and resource handlers; the surface count is
  dynamic (ADR 0003).
