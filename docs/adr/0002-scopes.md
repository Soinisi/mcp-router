# Scopes: soft per-connection allow-lists

The router exposes three generic tools, so a client's per-tool permission system
cannot express "this agent may use `tickets` but not `filesystem`": every
underlying tool is an argument to `run_mcp_tool`. To give each agent a narrower
view without adding MCP tools (which would cost every agent context), a scope is
a named map from `mcp_name` to an optional tool allow-list, selected by
`MCP_ROUTER_SCOPE` (or `serve --scope`). `get_installed_mcps` lists only the
scope's servers, `get_mcp_tools` filters to the allow-list, and `run_mcp_tool`
rejects out-of-scope servers and tools.

Semantics: `null` = all tools, a list = only those tools, `[]` = none, an absent
server = hidden. No scope selected = unrestricted.

## Consequences

- It is a guardrail, not an authentication boundary: the scope comes from the
  client's environment, and an agent with shell access could launch the router
  with a wider scope. Real isolation would need an authenticated remote router
  and identity-based policy.
- Tool names cannot be validated at load (they are discovered on connect), so a
  scope may name a tool the server no longer has; it simply never appears.
- Scope filtering lives in the tool handlers; the MCP surface stays exactly
  three tools.
