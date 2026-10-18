"""Real end-to-end tests: a configurable LLM drives mcp-router.

Prompts are plain user goals; they never name the router, a server, a tool, or
an argument. The ticket tests are the strongest: a fabricated answer cannot
produce a ticket in the server's store, so success requires the router to have
worked.

These are excluded from the default suite (see ``addopts`` in pyproject.toml).
Run them with:

    uv run pytest -m e2e --e2e-model openai/gpt-6-luna
"""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.e2e

SERVERS = {"hello", "microsoftdocs", "tickets"}

_LEARN_URL = re.compile(r"https?://[^\s<>()\"']+")

_ACME_SEED = [
    {
        "id": "TCK-0001",
        "customer": "Acme Corp",
        "subject": "Invoice is wrong",
        "body": "The latest invoice has the wrong amount.",
        "priority": "normal",
        "status": "open",
    },
    {
        "id": "TCK-0002",
        "customer": "Acme Corp",
        "subject": "Refund request",
        "body": "Please refund the duplicate charge.",
        "priority": "normal",
        "status": "open",
    },
    {
        "id": "TCK-0003",
        "customer": "Acme Corp",
        "subject": "Old issue",
        "body": "Already resolved.",
        "priority": "normal",
        "status": "closed",
    },
    {
        "id": "TCK-0004",
        "customer": "Globex",
        "subject": "Unrelated ticket",
        "body": "Not Acme.",
        "priority": "normal",
        "status": "open",
    },
]


def _final_learn_urls(text: str) -> list[str]:
    urls = [match.rstrip(".,;:!?") for match in _LEARN_URL.findall(text)]
    return [url for url in urls if "learn.microsoft.com" in url.lower()]


def _assert_url_came_from_output(text: str, outputs: list[str]) -> None:
    urls = _final_learn_urls(text)
    assert urls, text
    assert any(url.lower() in output.lower() for url in urls for output in outputs), (
        urls,
        outputs,
    )


def _assert_jsonserializer_came_from_output(text: str, outputs: list[str]) -> None:
    assert "jsonserializer" in text.lower(), text
    assert any("jsonserializer" in output.lower() for output in outputs), outputs


_LEARN_CASES = {
    "docs-search": {
        "prompt": (
            "What does Microsoft's official documentation say about the Model "
            "Context Protocol? Give me one learn.microsoft.com link."
        ),
        "tool": "microsoft_docs_search",
        "check_arg": lambda call: "model context protocol"
        in (call.arg("query") or "").lower(),
        "check_text": lambda text: "learn.microsoft.com" in text,
        "check_output": _assert_url_came_from_output,
    },
    "code-sample": {
        "prompt": (
            "Find an official Microsoft code sample that shows an HTTP client in "
            "Python. Give me one learn.microsoft.com link."
        ),
        "tool": "microsoft_code_sample_search",
        "check_arg": lambda call: "http client" in (call.arg("query") or "").lower()
        and (call.arg("language") or "").lower() == "python",
        "check_text": lambda text: "learn.microsoft.com" in text,
        "check_output": _assert_url_came_from_output,
    },
    "docs-fetch": {
        "prompt": (
            "Find the Microsoft Learn page for the C# JsonSerializer class and "
            "tell me the exact title it displays."
        ),
        "tool": "microsoft_docs_fetch",
        "check_arg": lambda call: "jsonserializer"
        in (call.arg("url") or "").lower(),
        "check_text": lambda text: "jsonserializer" in text.lower(),
        "check_output": _assert_jsonserializer_came_from_output,
    },
}


def _tool_names(payload) -> set[str] | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if isinstance(payload, list):
        return {
            item["name"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    if isinstance(payload, dict):
        if isinstance(payload.get("tools"), list):
            return _tool_names(payload["tools"])
        content = payload.get("content")
        if isinstance(content, list):
            names: set[str] = set()
            found = False
            for item in content:
                if not isinstance(item, dict):
                    continue
                parsed = _tool_names(item.get("text"))
                if parsed is not None:
                    found = True
                    names |= parsed
            return names if found else None
    return None


def _parse_names(call) -> set[str] | None:
    output = call.own_output
    if output is None:
        return None
    return _tool_names(output)


def _assert_open_ticket_count(text: str, count: int) -> None:
    assert re.search(rf"\b{count}\b", text), text
    lowered = text.lower()
    assert re.search(
        r"\bopen\b[^.\n]{0,30}\btickets?\b|\btickets?\b[^.\n]{0,30}\bopen\b",
        lowered,
    ), text


def _assert_status_mentioned(text: str, status: str) -> None:
    lowered = text.lower()
    assert re.search(
        rf"\bstatus\b[^.\n]{{0,40}}\b{status}\b"
        rf"|\b(?:is|currently)\b[^.\n]{{0,20}}\b{status}\b",
        lowered,
    ), text


def _assert_all_servers_were_discovered(run) -> None:
    installed = [call for call in run.calls_to("get_installed_mcps") if call.succeeded]
    assert installed, "get_installed_mcps did not succeed"
    listings: list[set[str]] = []
    for call in installed:
        if call.own_output is None:
            continue
        parsed = _parse_names(call)
        assert parsed is not None, call.own_output
        listings.append(parsed)
    assert listings, (
        "no get_installed_mcps result could be attributed to a single call"
    )
    names = set().union(*listings)
    assert SERVERS <= names, names


def _assert_tool_listing(run, server: str, expected_tools: set[str]):
    listed = [
        call for call in run.calls_to("get_mcp_tools") if call.arg("mcp_name") == server
    ]
    assert listed, f"get_mcp_tools({server!r}) was not called"
    succeeded = [call for call in listed if call.succeeded]
    assert succeeded, (
        f"get_mcp_tools({server!r}) did not succeed: "
        f"{[(call.block, call.status) for call in listed]}"
    )
    names: set[str] = set()
    attributable = False
    for call in succeeded:
        if call.own_output is None:
            continue
        parsed = _parse_names(call)
        assert parsed is not None, call.own_output
        attributable = True
        names |= parsed
    if attributable:
        assert expected_tools <= names, (expected_tools, names)


def test_create_customer_ticket(run_llm):
    run = run_llm("Create a customer ticket for Acme Corp: their invoice is wrong.")

    tickets = run.tickets
    assert len(tickets) == 1, tickets
    ticket = tickets[0]
    assert ticket["customer"].lower() == "acme corp"
    assert "invoice" in f"{ticket['subject']} {ticket['body']}".lower()
    assert ticket["status"] == "open"
    assert ticket["id"] in run.final_text


def test_create_then_look_up_ticket(run_llm):
    run = run_llm(
        "Create a ticket for a refund request from Acme Corp, then look it up "
        "and tell me its status."
    )

    tickets = run.tickets
    assert len(tickets) == 1, tickets
    ticket = tickets[0]
    assert "refund" in f"{ticket['subject']} {ticket['body']}".lower()
    assert ticket["id"] in run.final_text
    _assert_status_mentioned(run.final_text, ticket["status"])


def test_close_ticket(run_llm):
    run = run_llm("Close ticket TCK-0001 for Acme Corp.", seed=_ACME_SEED)

    expected = [dict(ticket) for ticket in _ACME_SEED]
    expected[0]["status"] = "closed"
    assert run.tickets == expected
    assert "tck-0001" in run.final_text.lower()


def test_count_open_tickets(run_llm):
    run = run_llm(
        "How many open tickets does Acme Corp have?",
        seed=_ACME_SEED,
    )

    _assert_open_ticket_count(run.final_text, 2)
    assert "acme" in run.final_text.lower(), run.final_text


def test_local_tool_selection(run_llm):
    run = run_llm(
        "Say hello to Ada and give me the exact output you got back, including "
        "any unique code in it."
    )

    _assert_all_servers_were_discovered(run)
    _assert_tool_listing(run, "hello", {"hello", "goodbye", "echo"})

    greeted = [
        call for call in run.calls_to("run_mcp_tool") if call.arg("mcp_tool") == "hello"
    ]
    assert greeted, "the model did not call the hello tool"
    assert greeted[0].succeeded
    if greeted[0].own_output is not None:
        assert run.nonce in greeted[0].own_output

    assert run.nonce in run.final_text
    assert "GOODBYE_SENTINEL" not in run.final_text

    routed = run.calls_to("run_mcp_tool")
    assert all(call.arg("mcp_tool") != "goodbye" for call in routed)
    assert all(
        "GOODBYE_SENTINEL" not in call.own_output
        for call in routed
        if call.own_output is not None
    )


@pytest.mark.parametrize("case", _LEARN_CASES.values(), ids=list(_LEARN_CASES))
def test_microsoft_learn_tool_selection(run_llm, case):
    run = run_llm(case["prompt"])

    _assert_all_servers_were_discovered(run)
    _assert_tool_listing(run, "microsoftdocs", {case["tool"]})

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
    outputs = [
        call.own_output
        for call in calls
        if call.succeeded and call.own_output is not None
    ]
    if outputs:
        case["check_output"](run.final_text, outputs)
    assert case["check_text"](run.final_text), run.final_text
