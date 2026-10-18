"""Real end-to-end tests: a configurable LLM drives mcp-router.

These are excluded from the default suite (see ``addopts`` in pyproject.toml).
Run them with:

    uv run pytest -m e2e --e2e-model openai/gpt-6-luna
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.e2e

LEARN_FETCH_URL = "https://learn.microsoft.com/en-us/dotnet/csharp/"

LOCAL_PROMPT = """You are testing an MCP router through its tools. Follow these steps exactly.
1. Call get_installed_mcps and note the registered server names.
2. Call get_mcp_tools for the "hello" server.
3. Call run_mcp_tool on the "hello" server using its "hello" tool with arguments {"name": "Ada"}. Do NOT call the "goodbye" or "echo" tools.
4. Using the "microsoftdocs" server, call run_mcp_tool with its "microsoft_docs_search" tool and arguments {"query": "Model Context Protocol"}.
Finish with exactly two lines:
NONCE=<the exact nonce printed by the hello tool>
URL=<one learn.microsoft.com URL from the documentation search result>"""

_LEARN_CASES = {
    "docs-search": {
        "prompt": (
            "You must use the mcp-router tools. Call run_mcp_tool with mcp_name "
            "'microsoftdocs', mcp_tool 'microsoft_docs_search', and arguments "
            '{"query": "Model Context Protocol"}. Then reply with one '
            "learn.microsoft.com URL from the result. Do not use the code-sample "
            "or fetch tools."
        ),
        "tool": "microsoft_docs_search",
        "check_arg": lambda call: "model context protocol"
        in (call.arg("query") or "").lower(),
    },
    "code-sample": {
        "prompt": (
            "You must use the mcp-router tools. Call run_mcp_tool with mcp_name "
            "'microsoftdocs', mcp_tool 'microsoft_code_sample_search', and arguments "
            '{"query": "HTTP client in Python", "language": "python"}. Then reply '
            "with one learn.microsoft.com URL from the result. Do not use the "
            "docs-search or fetch tools."
        ),
        "tool": "microsoft_code_sample_search",
        "check_arg": lambda call: "http client" in (call.arg("query") or "").lower()
        and (call.arg("language") or "").lower() == "python",
    },
    "docs-fetch": {
        "prompt": (
            "You must use the mcp-router tools. Call run_mcp_tool with mcp_name "
            "'microsoftdocs', mcp_tool 'microsoft_docs_fetch', and arguments "
            f'{{"url": "{LEARN_FETCH_URL}"}} to fetch that page. Then reply with '
            "the page title. Do not use the search tools."
        ),
        "tool": "microsoft_docs_fetch",
        "check_arg": lambda call: LEARN_FETCH_URL in (call.arg("url") or ""),
    },
}


def test_local_and_remote_through_one_router(run_llm):
    run = run_llm(LOCAL_PROMPT)

    installed = run.calls_to("get_installed_mcps")
    assert installed, "get_installed_mcps was not called"
    assert "hello" in installed[0].output and "microsoftdocs" in installed[0].output

    listed = [call for call in run.calls_to("get_mcp_tools") if "hello" in call.code]
    assert listed, "get_mcp_tools('hello') was not called"
    names = set(re.findall(r'"name":\s*"([^"]+)"', listed[0].output))
    assert {"hello", "goodbye", "echo"} <= names

    greeted = [
        call for call in run.calls_to("run_mcp_tool") if call.arg("mcp_tool") == "hello"
    ]
    assert greeted, "run_mcp_tool was not called with mcp_tool='hello'"
    assert greeted[0].succeeded
    assert run.nonce in greeted[0].output

    learned = [
        call
        for call in run.calls_to("run_mcp_tool")
        if call.arg("mcp_name") == "microsoftdocs"
        and call.arg("mcp_tool") == "microsoft_docs_search"
    ]
    assert learned, "run_mcp_tool was not called on microsoftdocs/microsoft_docs_search"
    assert learned[0].succeeded

    assert run.nonce in run.final_text
    assert "GOODBYE_SENTINEL" not in run.final_text
    assert "learn.microsoft.com" in run.final_text

    routed = run.calls_to("run_mcp_tool")
    assert all("GOODBYE_SENTINEL" not in call.output for call in routed)
    assert all(call.arg("mcp_tool") != "goodbye" for call in routed)


@pytest.mark.parametrize("case", _LEARN_CASES.values(), ids=list(_LEARN_CASES))
def test_microsoft_learn_tool_selection(run_llm, case):
    run = run_llm(case["prompt"])

    calls = [
        call
        for call in run.calls_to("run_mcp_tool")
        if call.arg("mcp_name") == "microsoftdocs"
        and call.arg("mcp_tool") == case["tool"]
    ]
    seen = [call.arg("mcp_tool") for call in run.calls_to("run_mcp_tool")]
    assert calls, f"expected {case['tool']!r}; saw run_mcp_tool calls {seen}"
    assert calls[0].succeeded, calls[0].output
    assert case["check_arg"](calls[0]), calls[0].code
