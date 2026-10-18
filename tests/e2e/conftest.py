"""Fixtures for the LLM-driven end-to-end tests.

The harness runs a real agent (opencode) whose only MCP server is `mcp-router`.
The router is configured with two children: Microsoft Learn (remote, keyless)
and a local hello-world server. Assertions read opencode's JSON event stream.

The child opencode process must be isolated from the session that launched the
tests: besides a fresh cwd, ``PWD`` is rewritten (opencode resolves its project
from it) and the inherited ``OPENCODE_SESSION_ID`` is dropped.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

HELLO_SERVER = Path(__file__).with_name("hello_server.py")
LEARN_URL = "https://learn.microsoft.com/api/mcp"


@dataclass
class RouterCall:
    """One call the agent made through the router's tool namespace."""

    tool: str
    input: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    output: str = ""
    block: int = 0
    block_size: int = 1

    def arg(self, name: str) -> str | None:
        if name in self.input:
            return str(self.input[name])
        arguments = self.input.get("arguments")
        if isinstance(arguments, dict) and name in arguments:
            return str(arguments[name])
        return None

    @property
    def succeeded(self) -> bool:
        if self.status != "completed":
            return False
        return self.block_size > 1 or bool(self.output.strip())

    @property
    def own_output(self) -> str | None:
        return self.output if self.block_size == 1 else None


@dataclass
class LLMRun:
    nonce: str
    final_text: str
    events: list[dict[str, Any]]

    @property
    def router_calls(self) -> list[RouterCall]:
        calls: list[RouterCall] = []
        for block, event in enumerate(self.events):
            if event.get("type") != "tool_use":
                continue
            state = event.get("part", {}).get("state", {})
            raw_output = state.get("output")
            if raw_output is None:
                output = ""
            elif isinstance(raw_output, str):
                output = raw_output
            else:
                output = json.dumps(raw_output, ensure_ascii=False, default=str)

            structured = (
                (state.get("metadata") or {}).get("metadata") or {}
            ).get("toolCalls") or []
            block_size = len(structured)
            for call in structured:
                tool = call.get("tool", "")
                if tool.startswith("mcp-router."):
                    calls.append(
                        RouterCall(
                            tool=tool.split(".", 1)[1],
                            input=call.get("input") or {},
                            status=str(call.get("status") or ""),
                            output=output,
                            block=block,
                            block_size=block_size,
                        )
                    )
        return calls

    def calls_to(self, tool: str) -> list[RouterCall]:
        return [call for call in self.router_calls if call.tool == tool]


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _final_text(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        event["part"].get("text", "")
        for event in events
        if event.get("type") == "text"
    )


@pytest.fixture(scope="session")
def opencode_exe() -> str:
    exe = shutil.which("opencode")
    if exe is None:
        pytest.skip("opencode is not installed")
    return exe


@pytest.fixture(scope="session")
def e2e_model(pytestconfig: pytest.Config, opencode_exe: str) -> str:
    model = pytestconfig.getoption("e2e_model")
    try:
        listed = subprocess.run(
            [opencode_exe, "models"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not list opencode models: {exc}")
    if model not in listed.stdout.split():
        pytest.skip(f"model {model!r} is not available to opencode")
    return model


@pytest.fixture()
def run_llm(opencode_exe: str, e2e_model: str) -> Callable[..., LLMRun]:
    def _run(prompt: str, attempts: int = 2) -> LLMRun:
        """Run the prompt, retrying only if the model ignored the router entirely.

        Tool selection itself is not retried: a model that engages the router
        but picks the wrong tool must fail, not be resampled into a pass.
        """
        last: LLMRun | None = None
        for _ in range(attempts):
            run_dir = Path(tempfile.mkdtemp(prefix="mcp-router-e2e-"))
            try:
                last = _run_in(run_dir, prompt, opencode_exe, e2e_model)
            finally:
                if not os.environ.get("E2E_KEEP_TMP"):
                    shutil.rmtree(run_dir, ignore_errors=True)
            if last.router_calls:
                return last
        assert last is not None
        return last

    return _run


def _run_in(
    run_dir: Path, prompt: str, opencode_exe: str, e2e_model: str
) -> LLMRun:
    nonce = secrets.token_hex(8)
    router_config = run_dir / "router.yaml"
    router_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mcp_servers": {
                    "microsoftdocs": {
                        "type": "remote",
                        "url": LEARN_URL,
                        "transport": "http",
                        "description": "Microsoft Learn documentation",
                    },
                    "hello": {
                        "type": "local",
                        "command": sys.executable,
                        "args": [str(HELLO_SERVER)],
                        "env": {"HELLO_NONCE": nonce},
                        "description": "Local hello-world test server",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    project = run_dir / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text(
        "Use the available tools to perform every task. Never answer from your "
        "own knowledge and never invent tool output. When a task names a URL or "
        "asks about a page, retrieve it with a tool instead of relying on memory.\n",
        encoding="utf-8",
    )
    (project / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                # Make mcp-router the only usable tool: the test is about
                # discovery, not whether the model prefers its built-ins.
                "agent": {
                    "build": {
                        "permission": {
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
                        }
                    }
                },
                "mcp": {
                    "timeout": {
                        "startup": 60000,
                        "catalog": 60000,
                        "execution": 180000,
                    },
                    "servers": {
                        "mcp-router": {
                            "type": "local",
                            "command": [sys.executable, "-m", "mcp_router.server"],
                            "environment": {"MCP_ROUTER_CONFIG": str(router_config)},
                            "disabled": False,
                            "timeout": {
                                "startup": 120000,
                                "catalog": 120000,
                                "execution": 120000,
                            },
                        }
                    },
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if k != "OPENCODE_SESSION_ID"}
    # opencode resolves its workspace from PWD, which subprocess cwd does not update.
    env["PWD"] = str(project)
    timeout = int(os.environ.get("E2E_TIMEOUT", "300"))
    try:
        proc = subprocess.run(
            [
                opencode_exe,
                "run",
                "--standalone",
                "--agent",
                "build",
                "--model",
                e2e_model,
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
        raise AssertionError(f"opencode did not finish within {timeout}s") from exc

    if proc.returncode != 0:
        raise AssertionError(
            f"opencode exited {proc.returncode}\nstderr:\n{proc.stderr[-2000:]}"
        )

    events = _parse_events(proc.stdout)
    return LLMRun(nonce=nonce, final_text=_final_text(events), events=events)
