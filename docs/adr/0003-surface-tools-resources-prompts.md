# Surface: tools, resources and prompts behind a dynamic façade

An MCP server can offer a client three kinds of capability: tools (invocable
actions), resources (data read by URI) and prompts (reusable message
templates). ADR 0001 established a single façade with runtime discovery instead
of registering every child tool up front. This ADR extends that shape across all
three categories: tools, resources and prompts.

This supersedes ADR 0001's "exactly three tools" count: the surface is now
dynamic, and three is only the legacy tool-only shape.

## Parallel tool families

Each category gets its own discover/act pair rather than one generic,
polymorphic pair:

- tools: `search_tools` / `run_mcp_tool`
- resources: `search_resources` / `read_resource`
- prompts: `search_prompts` / `get_prompt`

A single polymorphic pair was rejected. The arguments and results differ per
category — a tool call takes JSON arguments and returns the child's result, a
resource read takes a URI and returns contents, a prompt takes a name plus
arguments and returns messages — so the union would force every caller to learn
every shape. Separate pairs also let the router omit the categories a given
connection cannot use at all.

The families are parallel, not layered: resources are not modelled as tools
with a URI argument. A resource is data with no side effects and a stable URI;
wrapping it in a tool would hide that from the client and lose the URI as the
addressing mechanism.

## Dynamic tool surface

Before serving, the router eagerly connects to every scope-visible registered
server and reads the capabilities advertised in the initialize handshake
(presence of `tools`, `resources`, `prompts`). It caches the union for the
session and builds its own tool list from it:

- `get_installed_mcps` is always exposed, because browsing the registered
  servers is the entry point to everything else.
- The tool pair appears only when at least one scope-visible server offers
  tools; the resource pair only when one offers resources, and the prompt pair
  only when one offers prompts. Categories are gated independently: a server
  that offers prompts but no resources still makes the prompt pair visible.

Why eager instead of lazy? An MCP client asks for the tool list once at
initialize and does not re-ask. A static seven-tool surface would make every
agent carry resource and prompt tools even when no registered server exposes
them, which is exactly the context cost this design exists to avoid. The probe
costs one connection per server at startup, and the existing session cache reuses
those connections for real calls.

A server that fails to connect during the probe contributes no capabilities and
is recorded as unreachable; the router still starts and reports it through
`get_installed_mcps`. This keeps one broken child from taking the whole façade
down. A server that connected but advertised nothing is skipped by discovery
for that category instead of being reported as unreachable.

The capability set is cached for the session, so a child that starts offering a
new category later becomes visible after the router reconnects, not mid-session.

## Config schema v2

In schema v1 a scope entry is `{server: [tool names] | null}`. Schema v2
accepts two entry shapes per server:

- the legacy bare list or `null`, still read as the `tools` category;
- a mapping `{tools: [...], resources: [...], prompts: [...]}` where each value
  is a list or `null` (= all).

An omitted category means none. This is deliberate: a scope is an allow-list,
so a category that was not named must not be silently allowed. It also keeps v1
configs working unchanged — a bare tool list leaves resources and prompts
closed, so gaining a resource category never widens an existing scope.

Tools and prompts are matched by name. Resources are matched by URI: a literal
entry matches exactly that URI, an entry ending in `/` (such as `tickets://`)
matches that prefix, and a template such as `docs://{path}` matches concrete
URIs in that shape. Template expressions support the RFC 6570 forms that can
be matched without ambiguity: a bare `{var}` matches one path segment,
`{+var}` matches across segments, and `{/var}` matches slash-prefixed
segments. Unsupported operator-prefixed placeholders (`{?var}`, `{&var}`,
`{#var}`, `{.var}`, `{;var}` and any other RFC 6570 operator) and malformed
expressions such as `{}` or `{!var}` are rejected at config validation,
fail-closed: compiling them as a single-segment wildcard would authorize
siblings of the literal prefix. Resources have no stable name to match by, so
the URI is the only honest key.

Version rule, kept consistent across `add`, `remove` and `dump_config`: reads
accept version 1 and 2 and either entry shape; a write emits version 2 as soon
as the document contains (or declares) resource or prompt allow-lists, and
otherwise preserves the declared version and the legacy shape. A mapping that
carries only `tools` means the same as the legacy bare list, so it does not by
itself force version 2.

## Out of scope

Three MCP features are deliberately not proxied:

- **Subscriptions.** `resources/subscribe` is a per-URI push channel. Fronting
  it means fanning notifications out across the façade, tracking subscription
  lifetime per client, and deciding what a URI collision across two servers
  means. That is a reliability problem of its own; polling with `read_resource`
  is the honest fallback.
- **Roots.** Roots are a server-to-client query for the client's filesystem
  scope. The router is a server to its children, so it would have to answer for
  a client it cannot see; forwarding the request inverts the proxy direction
  and exposes one client's roots to every child.
- **Sampling.** Sampling is a server-to-client request to run an LLM completion.
  The router would have to relay arbitrary prompts into the client's model with
  no policy of its own, which is a security and cost decision outside a
  router's remit.

## Consequences

- The tool list is a function of the registered servers and the active scope,
  not a constant. Tests and documentation key on capability, not a fixed count.
- Read results are returned as embedded resources, preserving the child's
  content blocks (text or blob) and MIME type rather than flattening them.
- A resource scope entry grants exactly the URI it names unless it ends in `/`
  (prefix) or carries `{...}` placeholders (template, with RFC 6570 segment
  semantics). Listing one URI never authorizes a sibling that merely shares its
  prefix.
- The startup probe is bounded by the connect timeout and parallel across
  servers, but a slow server still delays the router's first response.
- The surface is complete across all three categories. Prompt discovery matches
  names and descriptions, `get_prompt` forwards the caller's arguments and
  returns the child's result verbatim, and the scope's `prompts` allow-list
  gates both discovery and rendering.
