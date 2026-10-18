"""Real end-to-end tests: a configurable LLM drives mcp-router.

The prompts are goal-only: they never name the router, a server, a tool, or an
argument. The model must discover what is available and choose the right tool
from the descriptions it reads.

These are excluded from the default suite (see ``addopts`` in pyproject.toml).
Run them with:

    uv run pytest -m e2e --e2e-model openai/gpt-6-luna
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

_LEARN_CASES = {
    "docs-search": {
        "prompt": (
            "Use a tool to check what Microsoft's official documentation says "
            "about the Model Context Protocol, and give me one "
            "learn.microsoft.com link."
        ),
        "tool": "microsoft_docs_search",
        "check_arg": lambda call: "model context protocol"
        in (call.arg("query") or "").lower(),
        "check_text": lambda text: "learn.microsoft.com" in text,
    },
    "code-sample": {
        "prompt": (
            "Use a tool to find an official Microsoft code sample that shows an "
            "HTTP client in Python, and give me one learn.microsoft.com link."
        ),
        "tool": "microsoft_code_sample_search",
        "check_arg": lambda call: "http client" in (call.arg("query") or "").lower()
        and (call.arg("language") or "").lower() == "python",
        "check_text": lambda text: "learn.microsoft.com" in text,
    },
    "docs-fetch": {
        "prompt": (
            "Use a tool to find the Microsoft Learn page for the C# "
            "JsonSerializer class, then read that page and tell me the exact "
            "title it displays."
        ),
        "tool": "microsoft_docs_fetch",
        "check_arg": lambda call: "jsonserializer"
        in (call.arg("url") or "").lower(),
        "check_text": lambda text: "jsonserializer" in text.lower(),
    },
}


def _assert_both_servers_were_discovered(run) -> None:
    installed = [call for call in run.calls_to("get_installed_mcps") if call.succeeded]
    assert installed, "get_installed_mcps did not succeed"
    listing = "\n".join(call.output for call in installed)
    assert "hello" in listing and "microsoftdocs" in listing


def _assert_tool_listing(run, server: str, expected_tool: str):
    listed = [
        call for call in run.calls_to("get_mcp_tools") if call.arg("mcp_name") == server
    ]
    assert listed, f"get_mcp_tools({server!r}) was not called"
    listing = listed[0]
    assert listing.succeeded, listing.output
    assert expected_tool in listing.output, listing.output
    return listing


def test_local_tool_selection(run_llm):
    run = run_llm(
        "Say hello to Ada using a tool, then give me the exact output the tool "
        "returned, including any unique code in it."
    )

    _assert_both_servers_were_discovered(run)
    _assert_tool_listing(run, "hello", "hello")

    greeted = [
        call for call in run.calls_to("run_mcp_tool") if call.arg("mcp_tool") == "hello"
    ]
    assert greeted, "the model did not call the hello tool"
    assert greeted[0].succeeded
    assert run.nonce in greeted[0].output

    assert run.nonce in run.final_text
    assert "GOODBYE_SENTINEL" not in run.final_text

    routed = run.calls_to("run_mcp_tool")
    assert all(call.arg("mcp_tool") != "goodbye" for call in routed)
    assert all("GOODBYE_SENTINEL" not in call.output for call in routed)


@pytest.mark.parametrize("case", _LEARN_CASES.values(), ids=list(_LEARN_CASES))
def test_microsoft_learn_tool_selection(run_llm, case):
    run = run_llm(case["prompt"])

    _assert_both_servers_were_discovered(run)
    _assert_tool_listing(run, "microsoftdocs", case["tool"])

    calls = [
        call
        for call in run.calls_to("run_mcp_tool")
        if call.arg("mcp_name") == "microsoftdocs"
        and call.arg("mcp_tool") == case["tool"]
    ]
    seen = [call.arg("mcp_tool") for call in run.calls_to("run_mcp_tool")]
    assert calls, f"expected {case['tool']!r}; saw run_mcp_tool calls {seen}"
    assert calls[0].succeeded, calls[0].output
    assert case["check_arg"](calls[0]), calls[0].input
    assert case["check_text"](run.final_text), run.final_text
