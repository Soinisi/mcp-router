# Config as the channel: live reload without a control socket

## Context

A router process is long-lived and is started by a client (often an agent) with
a fixed set of environment variables. We want a human to be able to change the
active scope — and the global/local-repo mode — and have an already-running
router adopt it, without asking the agent to restart the server and without
exposing a mutation tool that would cost every agent context (ADR 0001).

There is no natural back-channel to a stdio MCP server. Adding a control socket
or a status file would create a second source of truth, a protocol to version,
and an authentication problem, while the config file already exists and is
already the registration surface.

## Decision

The config file is the only channel.

- `mode` and `active_scope` are durable config keys (version 3), resolved
  repo-first. There is no runtime state file and no separate control channel.
- A running router polls the config **paths** about once a second using mtime +
  size. It watches paths rather than an open descriptor or an inode event
  because editors and the CLI save by rename; an inode watch would silently go
  stale.
- A short burst of saves is debounced into one reload. A reload re-reads,
  re-validates, recomputes mode/scope, probes only newly visible servers, reuses
  cached sessions, rebuilds the tool surface and notifies.
- An invalid document keeps the last good configuration and is reported on
  stderr. It never crashes the server and never falls back to unrestricted.
- On reload the saved selection is authoritative. A stale launch pin that names
  a different scope is reported on stderr and does not defeat the selection the
  human just saved.
- The server advertises `listChanged` through `NotificationOptions` and sends
  `notifications/tools/list_changed` after a successful reload. The lifespan
  runs before a client session exists, so the session is captured from the first
  request. Only the tools notification is needed because the router's whole
  agent-facing surface is tools.
- Every config write is atomic (temporary file in the same directory, then
  rename) and keeps one previous generation as `<name>.bak`. This includes the
  CLI's existing `add`/`remove` write path, because a running router reads the
  file while the CLI rewrites it.
- The terminal editor writes raw target files only, never the merged view, and
  never `mcp_servers`. It says "saved", not "applied", because nothing
  acknowledges the write back to it.

## Consequences

- Live changes are eventual: the poll interval plus debounce bounds how long a
  save takes to reach a running router. The editor cannot know whether a router
  is running from that directory; it reports only where it saved.
- An invalid save leaves the running router on the previous generation. The
  human sees the editor's validation, but a hand-edited file is reported only on
  the router's stderr.
- **Honest limit:** anything able to write the config can still change the
  active scope. This is a guardrail against an agent casually widening its own
  view, not an authentication or isolation boundary. The terminal editor is the
  offered switcher; a shell can bypass it.
- Follow-ups deliberately not built here: a resources/prompts drill-down in the
  editor (tool-level toggling only for now), a repo-root search that walks up
  from the current directory, server-to-editor acknowledgement or a status file,
  per-client targeting of a save, and any authentication or isolation boundary.
