# mcp-router

One MCP server that an agent connects to instead of connecting to many, exposing
only a discovery façade and proxying every real server behind it.

## Language

**MCP router**:
The single MCP server this project provides; a façade that fronts all other servers.
_Avoid_: gateway, proxy server, mcp-of-mcps

**Registered server**:
A server listed in the router's config that the router can reach on the agent's behalf.
_Avoid_: installed MCP, connected MCP, downstream server

**mcp_name**:
The config key that identifies a registered server; the agent passes it back verbatim.
_Avoid_: server id, alias

**Local server**:
A registered server the router starts itself as a child process and speaks to over stdio.
_Avoid_: stdio MCP, spawn server

**Remote server**:
A registered server reached over the network (http or sse) by URL.
_Avoid_: url MCP, HTTP MCP

**Spec**:
The router's own mapping describing one server, validated against the router's schema.
_Avoid_: config entry, VS Code spec, claude spec

**Dynamic discovery**:
The agent learns which tools exist at runtime through the router, rather than having them all registered up front.
_Avoid_: lazy loading, tool listing

**Secrets expansion**:
Resolving `${VAR}` placeholders in a spec from the environment and `.env` at connect time.
_Avoid_: env substitution, interpolation

**Scope**:
A named, per-connection allow-list of registered servers and optionally their tools.
_Avoid_: profile, role, permission set

**Config target**:
Which config file a CLI mutation writes: the user config or the project's `./mcp-router.yaml`.
_Avoid_: scope, level
