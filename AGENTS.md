# AGENTS.md

`mcp-router`: one MCP server an agent connects to, exposing 3 discovery/proxy
tools for all other ("registered") servers behind it. See `CONTEXT.md`
(glossary only) and `docs/adr/0001-*` (the core design decision).

## Commands

- Everything runs through uv: `uv sync`, then `uv run <cmd>`.
- Tests: `uv run pytest`. Single test: `uv run pytest tests/test_server.py::test_end_to_end_via_stdio`.
- CLI: `uv run mcp-router {serve,list,tools,add,remove,validate}`; stdio server is `uv run mcp-router-server` (or `python -m mcp_router.server`).
- No linter/formatter/typecheck is configured. Do not invent one.

## Git and shipping

- Git repo: `origin` → github.com/Soinisi/mcp-router, default branch `main`.
- Registered with **no-mistakes** (remote `no-mistakes` → `~/.no-mistakes/repos/1e9c4906ad41.git`).
  The gate validates committed history on a **non-default branch**, so create a
  feature branch before committing task work.
- To validate/ship, use the `no-mistakes` skill (`/no-mistakes`): review → tests →
  lint → push → PR → CI. A bare `git push` is not the gate.

## Architecture (non-obvious)

- `src/mcp_router/`: `config.py` (schema/load/resolve) → `registry.py` (add/remove) → `sessions.py` (connect+cache) → `server.py` (MCP surface) → `cli.py`.
- The MCP tool surface is **exactly three** read/run tools. `add`/`remove` are
  **CLI-only on purpose** — exposing them would cost every agent context. Do not
  add mutation tools (ADR 0001).
- The schema in `config.py` is **ours** (`extra="forbid"`). Never add parsing for
  VS Code/Claude/opencode formats; translating is the caller's job.
- Uses the **low-level** `mcp.server.lowlevel.Server`, not FastMCP, so child
  `CallToolResult`s (incl. `isError`) pass through verbatim. Failures are returned
  as `isError` results, never raised.

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
- Config: `~/.config/mcp-router/config.yaml` (or `$MCP_ROUTER_CONFIG`), overridden
  per project by `./mcp-router.yaml`.

## Tests

- `asyncio_mode=auto`: async tests need no decorator.
- Integration tests spawn a real child MCP server (`tests/child_server.py`) over
  stdio and drive `python -m mcp_router.server` end to end — they need the venv
  interpreter, so run tests via `uv run pytest`.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `CONTEXT.md` (project glossary) + `docs/adr/`. See `docs/agents/domain.md`.
