"""Measure the agent context saved by mcp-router versus direct MCP connections.

For each number of MCP servers N, this drives real ``opencode`` sessions in two
configurations:

* **direct** -- opencode registers all N synthetic servers itself.
* **router** -- opencode registers only mcp-router, which fronts the same N.

Two measurements are reported:

* **registration** -- the fixed prompt "reply ok". The metric is the first model
  step's input tokens: the context an agent pays before doing any work (system
  prompt + every tool definition + prompt).
* **end-to-end** -- a real task that must find and call one unique tool
  (``postgres.list_databases``). The metric is the whole session: every
  ``step_finish`` input token count is summed, so the router's discovery
  round-trips are charged to it. At small N the router can cost more; as N grows,
  direct repeats N full tool catalogs on every step while the router still shows
  only three tools.

``opencode run --format json`` reports tokens on each ``step_finish`` event as
``part.tokens``. A first request reports its whole context in ``input``; once a
provider caches the prompt, uncached tokens appear in ``input`` and the rest in
``cache.read`` / ``cache.write``. We sum all three so repeated runs report the
same full context instead of a cache-dependent slice.

Run it with::

    uv run python scripts/context_benchmark.py --model openai/gpt-6-luna
    uv run python scripts/context_benchmark.py --n 1,3,5,10 --model openai/gpt-5-nano
    uv run python scripts/context_benchmark.py --mode end-to-end

The same core backs ``tests/e2e/test_context_benchmark.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

BENCH_SERVER = Path(__file__).resolve().with_name("bench_server.py")
DEFAULT_MODEL = "openai/gpt-6-luna"
DEFAULT_NS = (1, 3, 5, 10)
DEFAULT_TIMEOUT = 300

#: Registration-only mode: no tool call, so only the tool catalog is measured.
REGISTRATION_PROMPT = "Reply with exactly the word ok and nothing else."
#: End-to-end mode: a real task that requires finding and calling one tool.
END_TO_END_PROMPT = (
    "Use the Postgres MCP server to list its databases with the list_databases "
    "tool once, and report the exact output you get back."
)
MODES = ("registration", "end-to-end")

#: opencode resolves its project from PWD and would otherwise inherit the
#: session that launched the benchmark (see tests/e2e/conftest.py).
_INHERITED_OPENCODE_VARS = {
    "OPENCODE_SESSION_ID",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_CLI_CONFIG_CONTENT",
}

#: Keep the benchmark about MCP discovery: no built-in tool should win the turn.
_AGENT_PERMISSIONS = {
    "websearch": "deny",
    "webfetch": "deny",
    "bash": "deny",
    "shell": "deny",
    "read": "deny",
    "glob": "deny",
    "grep": "deny",
    "list": "deny",
    "edit": "deny",
    "task": "deny",
    "external_directory": "deny",
    "todowrite": "deny",
    "question": "deny",
    "lsp": "deny",
    "skill": "deny",
    # `execute` is opencode's code-mode bridge (and a generic code runner). The
    # benchmark uses native tool definitions on both sides, so it must not be a
    # way for the agent to reach MCP servers without paying for their schemas.
    "execute": "deny",
    # Resource helpers are unrelated to the tool-discovery task and would add
    # extra round-trips to the router side only.
    "opencode_list_mcp_resources": "deny",
    "opencode_read_mcp_resource": "deny",
}

_MCP_TIMEOUT = {"startup": 60000, "catalog": 60000, "execution": 180000}
_SERVER_TIMEOUT = {"startup": 60000, "catalog": 60000, "execution": 60000}


class BenchmarkError(RuntimeError):
    """A benchmark run could not produce a trustworthy measurement."""


@dataclass(frozen=True)
class BenchmarkResult:
    n: int
    direct_tokens: int
    router_tokens: int

    @property
    def saved(self) -> int:
        return self.direct_tokens - self.router_tokens

    @property
    def savings_pct(self) -> float:
        return 100.0 * self.saved / self.direct_tokens


def parse_ns(value: str) -> list[int]:
    try:
        ns = [int(part) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise BenchmarkError(f"invalid N list {value!r}: {exc}") from exc
    if not ns:
        raise BenchmarkError("N list must contain at least one integer.")
    if any(n < 1 for n in ns):
        raise BenchmarkError("N must be a positive integer.")
    return sorted(dict.fromkeys(ns))


@lru_cache(maxsize=1)
def _bench_server_module():
    spec = importlib.util.spec_from_file_location("_bench_server", BENCH_SERVER)
    if spec is None or spec.loader is None:  # pragma: no cover - file exists
        raise BenchmarkError(f"cannot load {BENCH_SERVER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _domain_names(n: int) -> list[str]:
    domains = list(_bench_server_module().DOMAINS)
    if n > len(domains):
        raise BenchmarkError(
            f"N={n} needs {n} synthetic servers but only {len(domains)} domains exist."
        )
    return domains[:n]


def _write_router_config(path: Path, n: int) -> None:
    servers = {
        domain: {
            "type": "local",
            "description": f"Synthetic {domain} MCP server for the context benchmark.",
            "command": sys.executable,
            "args": [str(BENCH_SERVER)],
            "env": {"BENCH_DOMAIN": domain},
        }
        for domain in _domain_names(n)
    }
    path.write_text(
        yaml.safe_dump({"version": 1, "mcp_servers": servers}), encoding="utf-8"
    )


def _direct_mcp_server(domain: str) -> dict[str, Any]:
    return {
        "type": "local",
        "command": [sys.executable, str(BENCH_SERVER)],
        "environment": {"BENCH_DOMAIN": domain},
        "disabled": False,
        # Code Mode (opencode's default) hides MCP schemas behind an `execute`
        # tool. The benchmark compares real tool definitions in context, so both
        # configurations turn Code Mode off.
        "codemode": False,
        "timeout": dict(_SERVER_TIMEOUT),
    }


def _router_mcp_server(router_config: Path) -> dict[str, Any]:
    return {
        "type": "local",
        "command": [sys.executable, "-m", "mcp_router.server"],
        "environment": {"MCP_ROUTER_CONFIG": str(router_config)},
        "disabled": False,
        "codemode": False,
        "timeout": dict(_MCP_TIMEOUT),
    }


def _write_opencode_config(
    project: Path, mode: str, n: int, router_config: Path | None
) -> None:
    if mode == "direct":
        servers = {
            f"bench-{domain}": _direct_mcp_server(domain)
            for domain in _domain_names(n)
        }
    elif mode == "router":
        assert router_config is not None
        servers = {"mcp-router": _router_mcp_server(router_config)}
    else:  # pragma: no cover - guarded by run_once
        raise BenchmarkError(f"unknown mode {mode!r}")

    config = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {"build": {"permission": dict(_AGENT_PERMISSIONS)}},
        "mcp": {"timeout": dict(_MCP_TIMEOUT), "servers": servers},
    }
    (project / "opencode.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BenchmarkError(
                f"opencode emitted a non-JSON line: {line[:200]!r}"
            ) from exc
    return events


def _step_tokens(event: dict[str, Any]) -> int:
    """The full input context of one step: input + cache read + cache write."""
    tokens = (event.get("part") or {}).get("tokens")
    if not isinstance(tokens, dict):
        raise BenchmarkError(f"step_finish event has no token counts: {event!r}")
    raw_input = tokens.get("input")
    if not isinstance(raw_input, int) or isinstance(raw_input, bool):
        raise BenchmarkError(
            f"step_finish event has no integer input token count: {event!r}"
        )
    cache = tokens.get("cache") or {}
    total = (
        raw_input
        + int(cache.get("read") or 0)
        + int(cache.get("write") or 0)
    )
    if total <= 0:
        raise BenchmarkError(
            f"step_finish reported zero input tokens, refusing to report a "
            f"bogus saving: {event!r}"
        )
    return total


def initial_context_tokens(events: list[dict[str, Any]]) -> int:
    """Input tokens of the first model step (the session's starting context)."""
    for event in events:
        if event.get("type") != "step_finish":
            continue
        return _step_tokens(event)
    raise BenchmarkError(
        "opencode produced no step_finish event; cannot read an input token count."
    )


def full_session_tokens(events: list[dict[str, Any]]) -> int:
    """Sum of every model step's input context over the whole session."""
    steps = [event for event in events if event.get("type") == "step_finish"]
    if not steps:
        raise BenchmarkError(
            "opencode produced no step_finish event; cannot read an input token count."
        )
    return sum(_step_tokens(event) for event in steps)


def _tool_outputs(events: list[dict[str, Any]]) -> list[str]:
    outputs: list[str] = []
    for event in events:
        if event.get("type") != "tool_use":
            continue
        state = (event.get("part") or {}).get("state") or {}
        output = state.get("output")
        if isinstance(output, str):
            outputs.append(output)
        elif output is not None:
            outputs.append(json.dumps(output))
    return outputs


def require_target_tool_called(
    events: list[dict[str, Any]], *, marker: str, mode: str, n: int
) -> None:
    """Fail when an end-to-end run never executed the unique target tool.

    A token count from a run that answered without calling the tool would
    measure something other than the task, so it is not a valid data point.
    """
    if not any(marker in output for output in _tool_outputs(events)):
        raise BenchmarkError(
            f"end-to-end opencode run ({mode}, N={n}) never returned the target "
            f"tool output {marker!r}; refusing to report its token count."
        )


def _run_session(
    opencode_exe: str,
    model: str,
    configuration: str,
    n: int,
    prompt: str,
    timeout: int,
    keep_tmp: bool,
) -> list[dict[str, Any]]:
    run_dir = Path(tempfile.mkdtemp(prefix="mcp-router-bench-"))
    try:
        router_config = run_dir / "router.yaml"
        if configuration == "router":
            _write_router_config(router_config, n)

        project = run_dir / "project"
        project.mkdir()
        _write_opencode_config(
            project,
            configuration,
            n,
            router_config if configuration == "router" else None,
        )

        config_home = run_dir / "config"
        config_home.mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in _INHERITED_OPENCODE_VARS
        }
        env["PWD"] = str(project)
        env["XDG_CONFIG_HOME"] = str(config_home)

        try:
            proc = subprocess.run(
                [
                    opencode_exe,
                    "run",
                    "--standalone",
                    "--agent",
                    "build",
                    "--model",
                    model,
                    "--format",
                    "json",
                    "--auto",
                    prompt,
                ],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BenchmarkError(
                f"opencode ({configuration}, N={n}) did not finish within {timeout}s"
            ) from exc
        except OSError as exc:
            raise BenchmarkError(f"could not run opencode: {exc}") from exc

        if proc.returncode != 0:
            raise BenchmarkError(
                f"opencode ({configuration}, N={n}) exited {proc.returncode}\n"
                f"stderr:\n{proc.stderr[-2000:]}"
            )
        return _parse_events(proc.stdout)
    finally:
        if keep_tmp:
            print(f"kept benchmark project: {run_dir}", file=sys.stderr)
        else:
            shutil.rmtree(run_dir, ignore_errors=True)


def measure(
    opencode_exe: str,
    model: str,
    ns: list[int],
    *,
    mode: str = "registration",
    timeout: int = DEFAULT_TIMEOUT,
    keep_tmp: bool = False,
    log: Callable[[str], None] | None = None,
) -> list[BenchmarkResult]:
    """Run both configurations for every N and return one result per N."""
    if mode not in MODES:
        raise BenchmarkError(f"unknown mode {mode!r} (expected one of {MODES})")
    prompt = REGISTRATION_PROMPT if mode == "registration" else END_TO_END_PROMPT
    tokens_for = (
        initial_context_tokens if mode == "registration" else full_session_tokens
    )
    marker = _bench_server_module().TARGET_OUTPUT_MARKER
    emit = log or (lambda _message: None)
    results: list[BenchmarkResult] = []
    for n in ns:
        tokens: dict[str, int] = {}
        for configuration in ("direct", "router"):
            emit(f"measuring {mode} N={n} {configuration} ...")
            events = _run_session(
                opencode_exe, model, configuration, n, prompt, timeout, keep_tmp
            )
            if mode == "end-to-end":
                require_target_tool_called(
                    events, marker=marker, mode=configuration, n=n
                )
            tokens[configuration] = tokens_for(events)
        results.append(
            BenchmarkResult(
                n=n, direct_tokens=tokens["direct"], router_tokens=tokens["router"]
            )
        )
    return results


def _table(results: list[BenchmarkResult]) -> list[str]:
    header = (
        f"{'N':>3} | {'direct tokens':>13} | {'router tokens':>13} | "
        f"{'saved':>7} | {'savings %':>9}"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        lines.append(
            f"{result.n:>3} | {result.direct_tokens:>13} | "
            f"{result.router_tokens:>13} | {result.saved:>7} | "
            f"{result.savings_pct:>9.1f}"
        )
    return lines


def _growth_line(results: list[BenchmarkResult]) -> str:
    if len(results) < 2:
        return ""
    grows = all(
        later.saved > earlier.saved for earlier, later in zip(results, results[1:])
    )
    return f"savings grow with N: {'yes' if grows else 'NO'}"


def _trend_line(results: list[BenchmarkResult]) -> str:
    """End-to-end saving is a trend, not necessarily monotonic at every N.

    The router's own discovery output grows with N too, and real agent runs can
    vary; what matters is that the saving grows from the smallest to the largest
    N and that the largest N wins.
    """
    if len(results) < 2:
        return ""
    first, last = results[0], results[-1]
    grew = last.saved > first.saved
    best = max(results, key=lambda result: result.saved)
    return (
        f"saving grew from N={first.n} ({first.saved}) to N={last.n} "
        f"({last.saved}): {'yes' if grew else 'NO'}; "
        f"largest saving at N={best.n} ({best.saved})"
    )


def format_report(
    results: list[BenchmarkResult],
    *,
    model: str,
    prompt: str,
    heading: str = "registration-only",
    note: str = "first model step's input context (input + cache read/write tokens)",
    trend: bool = False,
) -> str:
    lines = [
        f"mcp-router context-savings benchmark - {heading}",
        f"model: {model}",
        f"prompt: {prompt}",
        "",
        *_table(results),
    ]
    growth = _trend_line(results) if trend else _growth_line(results)
    if growth:
        lines += ["", growth]
    lines += ["", f"Measured as {note}."]
    return "\n".join(lines)


def _crossover_note(results: list[BenchmarkResult]) -> str:
    first, last = results[0], results[-1]
    winners = [result for result in results if result.router_tokens < result.direct_tokens]
    if not winners:
        return (
            f"Crossover: the router does not beat direct for any N in this sweep "
            f"(N={first.n} costs {-first.saved} more; N={last.n} costs "
            f"{-last.saved} more)."
        )
    winner = winners[0]
    if winner is first:
        return (
            f"Crossover: the router already saves context at N={first.n} "
            f"({first.saved}); the saving grows to {last.saved} at N={last.n}."
        )
    return (
        f"Crossover: at N={first.n} the router costs {-first.saved} more because "
        f"its discovery round-trips outweigh the tools it avoids; it first saves "
        f"at N={winner.n} ({winner.saved}) and reaches {last.saved} at "
        f"N={last.n}. The router's discovery output also grows with N, so the "
        f"end-to-end gain can dip before direct's repeated full catalogs make it "
        f"win."
    )


def format_comparison(
    registration: list[BenchmarkResult],
    end_to_end: list[BenchmarkResult],
    *,
    model: str,
) -> str:
    lines = [
        "mcp-router context-savings benchmark",
        f"model: {model}",
        "",
        "Registration-only (what the tool catalog costs before any work)",
        "",
        *_table(registration),
        "",
        _growth_line(registration),
        "",
        "End-to-end task (full session, discovery round-trips included)",
        "",
        *_table(end_to_end),
        "",
        _trend_line(end_to_end),
        "",
        _crossover_note(end_to_end),
        "",
        (
            "Registration measures the first step's input context; end-to-end "
            "sums every step's input + cache read/write tokens."
        ),
    ]
    return "\n".join(lines)


def _resolve_opencode(explicit: str | None) -> str:
    exe = explicit or shutil.which("opencode")
    if exe is None:
        raise BenchmarkError("opencode is not on PATH; pass --opencode /path/to/opencode")
    return exe


def _require_model(opencode_exe: str, model: str) -> None:
    try:
        listed = subprocess.run(
            [opencode_exe, "models"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError(f"could not list opencode models: {exc}") from exc
    if model not in listed.stdout.split():
        raise BenchmarkError(
            f"model {model!r} is not available to opencode. "
            f"Pass --model provider/model from `opencode models`."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"opencode provider/model to drive (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--n",
        default=",".join(str(n) for n in DEFAULT_NS),
        help=f"comma-separated server counts (default: {DEFAULT_NS})",
    )
    parser.add_argument("--opencode", default=None, help="path to the opencode binary")
    parser.add_argument(
        "--mode",
        choices=(*MODES, "both"),
        default="both",
        help="registration-only, end-to-end task, or both (default: both)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"per-run timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--keep-tmp", action="store_true", help="keep each generated project directory"
    )
    args = parser.parse_args(argv)

    try:
        ns = parse_ns(args.n)
        opencode_exe = _resolve_opencode(args.opencode)
        _require_model(opencode_exe, args.model)
        log = lambda message: print(message, file=sys.stderr, flush=True)
        modes = MODES if args.mode == "both" else (args.mode,)
        measured = {
            mode: measure(
                opencode_exe,
                args.model,
                ns,
                mode=mode,
                timeout=args.timeout,
                keep_tmp=args.keep_tmp,
                log=log,
            )
            for mode in modes
        }
    except BenchmarkError as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1

    if args.mode == "both":
        print(
            format_comparison(
                measured["registration"],
                measured["end-to-end"],
                model=args.model,
            )
        )
    else:
        prompt = (
            REGISTRATION_PROMPT
            if args.mode == "registration"
            else END_TO_END_PROMPT
        )
        note = (
            "the first model step's input context (input + cache read/write tokens)"
            if args.mode == "registration"
            else "the full session: every step's input + cache read/write tokens"
        )
        print(
            format_report(
                measured[args.mode],
                model=args.model,
                prompt=prompt,
                heading=args.mode,
                note=note,
                trend=args.mode == "end-to-end",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
