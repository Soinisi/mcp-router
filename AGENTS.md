# AGENTS.md

`mcp-router`: one MCP server an agent connects to, exposing 3 discovery/proxy
tools for all other ("registered") servers behind it. See `CONTEXT.md`
(glossary only) and `docs/adr/0001-*` (the core design decision).

## Commands

- Everything runs through uv: `uv sync`, then `uv run <cmd>`.
- Tests: `uv run pytest`. Single test: `uv run pytest tests/test_server.py::test_end_to_end_via_stdio`.
- CLI: `uv run mcp-router {serve,list,tools,scopes,tui,add,remove,validate}`; stdio server is `uv run mcp-router-server` (or `python -m mcp_router.server`).
- No linter/formatter/typecheck is configured. Do not invent one.

## Git and shipping

- Git repo: `origin` → github.com/Soinisi/mcp-router, default branch `main`.
- Registered with **no-mistakes** (remote `no-mistakes` → `~/.no-mistakes/repos/1e9c4906ad41.git`).
  The gate validates committed history on a **non-default branch**, so create a
  feature branch before committing task work.
- To validate/ship, use the `no-mistakes` skill (`/no-mistakes`): review → tests →
  lint → push → PR → CI. A bare `git push` is not the gate.

## Architecture (non-obvious)

- `src/mcp_router/`: `config.py` (schema/load/resolve) → `registry.py`
  (add/remove) → `sessions.py` (connect+cache) → `server.py` (MCP surface) →
  `cli.py`. `scope_edit.py` + `tui.py` are the human-only terminal editor over
  the raw config files.
- The MCP tool surface is **dynamic** and read/run only: `get_installed_mcps`
  always, the tools pair (`search_tools`/`run_mcp_tool`) only when a
  scope-visible server offers tools, the resources pair
  (`search_resources`/`read_resource`) only when one offers resources, and the
  prompts pair (`search_prompts`/`get_prompt`) only when one offers prompts. A
  startup probe reads each server's advertised capabilities and caches the union
  for the session, and a config reload refreshes it (ADR 0003, ADR 0004).
  `add`/`remove` are **CLI-only on purpose** — exposing them would cost every
  agent context. Do not add mutation tools (ADR 0001).
- The schema in `config.py` is **ours** (`extra="forbid"`). Never add parsing for
  VS Code/Claude/opencode formats; translating is the caller's job.
- Uses the **low-level** `mcp.server.lowlevel.Server`, not FastMCP, so child
  `CallToolResult`s (incl. `isError`) pass through verbatim. Failures are returned
  as `isError` results, never raised.
- `skills/mcp-router/SKILL.md` is the user-facing install/register skill; it is
  symlinked into the user skills directory, not loaded by the router.
- The durable active scope and mode live in the config and are changed only by
  the terminal UI; `MCP_ROUTER_SCOPE` / `serve --scope` is a launch pin that
  cannot widen the saved selection. The tool, resource and prompt handlers in
  `server.py` filter discovery and reject out-of-scope calls (ADR 0002, ADR
  0003). Scope entries (v2+) allow-list tools, resources and prompts per server;
  v3 adds `mode` / `active_scope`; v1 bare lists are still read as tools-only.

## Gotchas

- MCP SDK is pinned `mcp>=1.2,<2` (currently 1.30.0). Use the 1.x API
  (`Server`, `stdio_client`, `streamable_http_client`, `ClientSession`); v2
  renames these. Don't bump to 2.x casually.
- Secret expansion is **lazy**: `${VAR}` stays literal until `resolve_server()`
  at connect time. Never expand at load — `list`/`save` must work without secrets.
  `mcp-router validate` is what checks them.
- `SessionManager` sessions have task affinity: each cached server is owned by one
  background task and all calls go through its queue. Don't call `session.*`
  from other tasks; use `session.call(lambda s: s.list_tools())`.
- Config: `~/.config/mcp-router/config.yaml` (or `$MCP_ROUTER_CONFIG`), merged
  with `./mcp-router.yaml` in `local-repo` mode (the default when the file
  exists); `global` mode ignores the repo file.

## Tests

- `asyncio_mode=auto`: async tests need no decorator.
- Integration tests spawn a real child MCP server (`tests/child_server.py`) over
  stdio and drive `python -m mcp_router.server` end to end — they need the venv
  interpreter, so run tests via `uv run pytest`.
- `e2e` tests are excluded from the default suite. Run them with
  `uv run pytest -m e2e --e2e-model openai/gpt-6-luna`; they drive `opencode`
  against a temp project with three servers: Microsoft Learn (network),
  `tests/e2e/hello_server.py`, and `tests/e2e/ticket_server.py`. Prompts are plain
  user goals; the ticket tests assert the server's JSON store. A deterministic
  `tests/e2e/test_learn_remote.py` covers the Microsoft Learn remote path without
  an LLM. `tests/test_sse_transport.py` covers the legacy SSE transport with a
  local FastMCP server and runs in the default suite.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `CONTEXT.md` (project glossary) + `docs/adr/`. See `docs/agents/domain.md`.
