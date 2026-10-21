"""Real end-to-end tests: a configurable LLM drives mcp-router.

Prompts are plain user goals; they do not name the router or tell the model
which tool to call. The ticket tests assert the ticket server's JSON store, the
hello test proves local selection with a random nonce, and Microsoft Learn is
covered deterministically in ``test_learn_remote.py``.

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
    if call.own_output is None:
        return None
    return _tool_names(call.own_output)


def _assert_all_servers_were_discovered(run) -> None:
    installed = [call for call in run.calls_to("get_installed_mcps") if call.succeeded]
    assert installed, "get_installed_mcps did not succeed"
    names: set[str] = set()
    attributable = False
    for call in installed:
        if call.own_output is None or call.arg("regex_filter"):
            continue
        parsed = _parse_names(call)
        assert parsed is not None, call.own_output
        attributable = True
        names |= parsed
    if attributable:
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
        if call.own_output is None or call.arg("regex_filter"):
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
    assert ticket["id"].lower() in run.final_answer.lower()


def test_create_then_look_up_ticket(run_llm):
    run = run_llm(
        "Create a ticket for a refund request from Acme Corp, then look it up "
        "and tell me its status."
    )

    tickets = run.tickets
    assert len(tickets) == 1, tickets
    ticket = tickets[0]
    assert ticket["customer"].lower() == "acme corp"
    assert "refund" in f"{ticket['subject']} {ticket['body']}".lower()
    assert ticket["status"] == "open"
    assert ticket["id"].lower() in run.final_answer.lower()


def test_close_ticket(run_llm):
    run = run_llm("Close ticket TCK-0001 for Acme Corp.", seed=_ACME_SEED)

    expected = [dict(ticket) for ticket in _ACME_SEED]
    expected[0]["status"] = "closed"
    assert run.tickets == expected
    assert "tck-0001" in run.final_answer.lower()


def test_count_open_tickets(run_llm):
    run = run_llm(
        "How many open tickets does Acme Corp have?",
        seed=_ACME_SEED,
    )

    assert run.tickets == _ACME_SEED
    answer = run.final_answer.lower()
    assert re.search(r"\b(?:2|two)\b", answer), answer
    assert "acme" in answer, answer


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
    successful = [call for call in greeted if call.succeeded]
    assert successful, (
        f"the hello tool never succeeded: "
        f"{[(call.status, call.output) for call in greeted]}"
    )
    outputs = [call.own_output for call in successful if call.own_output is not None]
    if outputs:
        assert any(run.nonce in output for output in outputs)

    assert run.nonce in run.final_answer
    assert "GOODBYE_SENTINEL" not in run.final_answer

    routed = run.calls_to("run_mcp_tool")
    assert all(call.arg("mcp_tool") != "goodbye" for call in routed)
    assert all(
        "GOODBYE_SENTINEL" not in call.own_output
        for call in routed
        if call.own_output is not None
    )
