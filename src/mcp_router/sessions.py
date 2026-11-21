"""Lazy connection management for registered MCP servers.

Each server is connected on first use and its session is cached for the
lifetime of the router process. Because the SDK's stdio / streamable-http
clients are async context managers with task affinity, every cached server
gets a dedicated background task that owns its ``AsyncExitStack`` and
``ClientSession``. Requests are funnelled through that task via a queue, so a
session is only ever touched from the task that created it.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import (
    ConfigError,
    LocalServer,
    RemoteServer,
    RouterConfig,
    Server,
    build_environ,
    resolve_server,
)

DEFAULT_TIMEOUT = 30.0


@asynccontextmanager
async def _connect(server: Server) -> AsyncIterator[tuple[Any, Any]]:
    """Open the transport for a server and yield its read/write streams."""
    if isinstance(server, LocalServer):
        params = StdioServerParameters(
            command=server.command,
            args=server.args,
            env=dict(server.env) or None,
            cwd=server.cwd,
        )
        async with stdio_client(params) as (read, write):
            yield read, write
        return

    assert isinstance(server, RemoteServer)
    headers = server.headers or None
    if server.transport == "sse":
        from mcp.client.sse import sse_client

        # sse_client defaults to a 5s connect timeout; match the session wait.
        async with sse_client(
            server.url, headers=headers, timeout=DEFAULT_TIMEOUT
        ) as (read, write):
            yield read, write
        return

    from mcp.client.streamable_http import streamable_http_client

    # mcp 1.30's streamable_http_client has no ``headers`` argument: headers
    # must be carried by an httpx client.
    if headers:
        # Match the MCP transports' defaults: 30s connect/write/pool, 300s read.
        # Redirects are left to the transport, which follows them within origin.
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(30.0, read=300.0)
        ) as client:
            async with streamable_http_client(server.url, http_client=client) as (
                read,
                write,
                _,
            ):
                yield read, write
    else:
        async with streamable_http_client(server.url) as (read, write, _):
            yield read, write


class ServerSession:
    """A live, cached session with one registered server."""

    def __init__(
        self,
        name: str,
        server: Server,
        environ: dict[str, str],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self.server = server
        self.environ = environ
        self.timeout = timeout
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._error: BaseException | None = None
        #: Advertised capabilities from the initialize handshake, set once the
        #: owning task has connected (and before ``start()`` returns).
        self.capabilities: Any | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mcp-router:{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            await self.aclose()
            raise TimeoutError(f"Timed out connecting to {self.name!r} after {self.timeout}s.")
        if self._error is not None:
            raise self._error

    async def _run(self) -> None:
        try:
            resolved = resolve_server(self.server, self.environ)
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(_connect(resolved))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.capabilities = session.get_server_capabilities()
                self._ready.set()
                while True:
                    item = await self._queue.get()
                    if item is None:
                        break
                    func, future = item
                    if future.cancelled():
                        continue
                    try:
                        result = await func(session)
                        if not future.done():
                            future.set_result(result)
                    except BaseException as exc:  # noqa: BLE001 - relayed to caller
                        if not future.done():
                            future.set_exception(exc)
        except BaseException as exc:  # noqa: BLE001 - surfaced via start() or call()
            self._error = exc
        finally:
            self._ready.set()

    async def call(self, func: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        """Run ``func(session)`` in the owning task and return its result."""
        if self._error is not None:
            raise self._error
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put((func, future))
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Tool call to {self.name!r} timed out after {self.timeout}s.")

    async def aclose(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=self.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        except Exception:
            pass
        self._task = None


class SessionManager:
    """Creates and caches :class:`ServerSession` objects, one per server."""

    def __init__(
        self,
        config: RouterConfig,
        environ: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.config = config
        self.environ = environ if environ is not None else build_environ([Path.cwd() / ".env"])
        self._timeout = timeout
        self._sessions: dict[str, ServerSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def server(self, name: str) -> Server:
        try:
            return self.config.mcp_servers[name]
        except KeyError:
            known = ", ".join(sorted(self.config.mcp_servers)) or "<none>"
            raise ConfigError(f"Unknown server {name!r}. Registered: {known}.") from None

    async def session(self, name: str) -> ServerSession:
        self.server(name)
        cached = self._sessions.get(name)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            cached = self._sessions.get(name)
            if cached is not None:
                return cached
            session = ServerSession(name, self.server(name), self.environ, self._timeout)
            await session.start()
            self._sessions[name] = session
            return session

    async def reconfigure(
        self, config: RouterConfig, environ: dict[str, str] | None = None
    ) -> set[str]:
        """Adopt a reloaded config, keeping sessions that are still valid.

        A cached session survives when its server name is still registered and
        its *raw* definition is unchanged (secrets stay unresolved until
        connect time). A changed definition, a removed server, or a changed
        secret environment closes the old session so the next call reconnects.
        Returns the names whose cached session was invalidated.
        """
        new_environ = environ if environ is not None else self.environ
        environ_changed = new_environ != self.environ
        self.config = config
        self.environ = new_environ
        invalidated: set[str] = set()
        for name in list(self._sessions):
            cached = self._sessions[name]
            definition_changed = (
                name not in config.mcp_servers
                or cached.server.model_dump()
                != config.mcp_servers[name].model_dump()
            )
            if environ_changed or definition_changed:
                self._sessions.pop(name, None)
                self._locks.pop(name, None)
                invalidated.add(name)
                await cached.aclose()
        return invalidated

    async def aclose(self) -> None:
        for session in list(self._sessions.values()):
            await session.aclose()
        self._sessions.clear()
        self._locks.clear()
