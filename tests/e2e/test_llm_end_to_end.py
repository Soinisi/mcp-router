"""Real end-to-end tests: a configurable LLM drives mcp-router.

Prompts are plain user goals; they do not name the router or tell the model
which tool to call. The create and close tests assert the ticket server's JSON
store, and the lookup and count tests assert the final answer.

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
_URL_TRIM = ".,;:!?*_`~[]\"'"

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
    urls = [match.strip(_URL_TRIM) for match in _LEARN_URL.findall(text)]
    return [url for url in urls if "learn.microsoft.com" in url.lower()]


def _assert_url_came_from_output(text: str, outputs: list[str]) -> None:
    urls = _final_learn_urls(text)
    assert urls, text
    assert any(url.lower() in output.lower() for url in urls for output in outputs), (
        urls,
        outputs,
    )


_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TITLE_FIELD = re.compile(
    r"^\s*(?:title|name)\s*[:=]\s*[\"'`]?(.+?)[\"'`]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _collect_titles(payload, titles: list[str]) -> None:
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith(("{", "[")):
            try:
                _collect_titles(json.loads(stripped), titles)
                return
            except json.JSONDecodeError:
                pass
        titles.extend(_HEADING.findall(payload))
        titles.extend(_HTML_TITLE.findall(payload))
        titles.extend(_TITLE_FIELD.findall(payload))
    elif isinstance(payload, list):
        for item in payload:
            _collect_titles(item, titles)
    elif isinstance(payload, dict):
        for key in ("title", "name"):
            value = payload.get(key)
            if isinstance(value, str):
                titles.append(value)
        for key in ("content", "text", "result", "output"):
            if key in payload:
                _collect_titles(payload[key], titles)


def _normalised_title(text: str) -> str:
    text = re.sub(r"[`*_#>\[\]]+", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _assert_title_came_from_output(text: str, outputs: list[str]) -> None:
    titles: list[str] = []
    for output in outputs:
        _collect_titles(output, titles)
    candidates = {
        _normalised_title(title)
        for title in titles
        if len(_normalised_title(title)) >= 15
        and " " in _normalised_title(title)
    }
    assert candidates, (titles, outputs)
    answer = _normalised_title(text)
    assert any(title in answer for title in candidates), (candidates, text)


def _code_sample_arg(call) -> bool:
    query = (call.arg("query") or "").lower()
    language = (call.arg("language") or "").lower()
    if "http client" not in query:
        return False
    return language == "python" or (not language and "python" in query)


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
        "check_arg": _code_sample_arg,
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
        "check_output": _assert_title_came_from_output,
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


_NUMBER_WORDS = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}
_CLAUSE_SPLIT = re.compile(r"[.;!?\n]|,?\s+(?:and|but|while|whereas)\s+")
_NEGATION = re.compile(r"\b(?:not|no|never|cannot)\b|\w+n't\b")
_OPEN_TICKETS = r"\bopen\s+(?:[a-z-]+\s+){0,2}(?:tickets?|ones?)\b"
_OPEN_TICKET_PATTERNS = (
    r"\bacme\b[^.;!?\n\d]{{0,60}}?{number}\s+(?:[a-z-]+\s+){{0,2}}{open_tickets}",
    r"\bacme\b[^.;!?\n\d]{{0,60}}?{number}\s+(?:[a-z-]+\s+){{0,2}}tickets?\s+"
    r"(?:are\s+|is\s+)?(?:currently\s+|still\s+|now\s+)?open\b",
    r"{open_tickets}[^.;!?\n\d]{{0,40}}?\bacme\b[^.;!?\n\d]{{0,20}}?{number}",
    r"\bacme\b[^.;!?\n\d]{{0,40}}?{open_tickets}[^.;!?\n\d]{{0,20}}?{number}",
    r"{number}\s+(?:[a-z-]+\s+){{0,2}}{open_tickets}[^.;!?\n\d]{{0,30}}?\bacme\b",
    r"\bacme\b[^.;!?\n\d]{{0,60}}?{number}\s+of\s+(?:the\s+)?"
    r"(?:\d+\s+|[a-z-]+\s+)?{open_tickets}",
    r"{number}\s+of\s+(?:the\s+)?(?:\d+\s+|[a-z-]+\s+)?{open_tickets}"
    r"[^.;!?\n\d]{{0,40}}?\bacme\b",
)


def _clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in _CLAUSE_SPLIT.split(text.lower())
        if clause.strip()
    ]


def _plain(text: str) -> str:
    """Drop Markdown emphasis/code markers before natural-language matching."""
    return text.replace("**", "").replace("__", "").replace("`", "").replace("*", "")


def _assert_open_ticket_count(text: str, count: int) -> None:
    text = _plain(text)
    word = _NUMBER_WORDS.get(count)
    number = rf"\b(?:{count}{'|' + word if word else ''})\b"
    patterns = [
        pattern.format(number=number, open_tickets=_OPEN_TICKETS)
        for pattern in _OPEN_TICKET_PATTERNS
    ]
    for clause in _clauses(text):
        for pattern in patterns:
            for match in re.finditer(pattern, clause):
                number_start = match.start() + re.search(number, match.group(0)).start()
                if not _NEGATION.search(clause[:number_start]):
                    return
    raise AssertionError(text)


_STATUS_REFERENCE = (
    r"(?:{ticket_id}|its|it|this\s+ticket|the\s+ticket|that\s+ticket)\b"
)
_STATUS_COPULA = r"(?:is|are|remains?|stays?|has\s+been|have\s+been|['’]s)"
_STATUS_ADVERB = r"(?:\s+(?:now|currently|still|already))?"


def _status_predicate(sentence: str, ticket_id: str, status: str) -> bool:
    reference = _STATUS_REFERENCE.format(ticket_id=re.escape(ticket_id.lower()))
    patterns = [
        re.compile(
            rf"{reference}[^.;!?\n]{{0,40}}?\b{_STATUS_COPULA}\b{_STATUS_ADVERB}"
            rf"\s+(?:(?:a|an)\s+)?{status}\b"
        ),
        re.compile(
            rf"{reference}[^.;!?\n]{{0,40}}?\bstatus\s*[:=]\s*{status}\b"
        ),
        re.compile(
            rf"{reference}[^.;!?\n]{{0,40}}?\b(?:has|with)\s+"
            rf"(?:(?:a|an|the)\s+)?status\s+(?:of\s+)?{status}\b"
        ),
    ]
    if status == "closed":
        patterns.append(
            re.compile(
                rf"\b(?:i\s+)?(?:have\s+|'ve\s+|had\s+)?closed\s+"
                rf"(?:the\s+|ticket\s+)?{reference}"
            )
        )
    for pattern in patterns:
        for match in pattern.finditer(sentence):
            prefix = sentence[max(0, match.start() - 20) : match.start()]
            if not _NEGATION.search(prefix):
                return True
    return False


def _assert_status_mentioned(text: str, ticket_id: str, status: str) -> None:
    lowered = _plain(text).lower()
    assert ticket_id.lower() in lowered, text
    expected = status.lower()
    competitor = "closed" if expected == "open" else "open"
    sentences = [
        sentence
        for sentence in re.split(r"[.;!?\n]", lowered)
        if sentence.strip()
    ]
    assert any(
        _status_predicate(sentence, ticket_id, expected) for sentence in sentences
    ), text
    assert not any(
        _status_predicate(sentence, ticket_id, competitor) for sentence in sentences
    ), text


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
    _assert_status_mentioned(run.final_answer, ticket["id"], "open")


def test_close_ticket(run_llm):
    run = run_llm("Close ticket TCK-0001 for Acme Corp.", seed=_ACME_SEED)

    expected = [dict(ticket) for ticket in _ACME_SEED]
    expected[0]["status"] = "closed"
    assert run.tickets == expected
    _assert_status_mentioned(run.final_answer, "TCK-0001", "closed")


def test_count_open_tickets(run_llm):
    run = run_llm(
        "How many open tickets does Acme Corp have?",
        seed=_ACME_SEED,
    )

    assert run.tickets == _ACME_SEED
    open_tickets = [
        ticket
        for ticket in _ACME_SEED
        if ticket["customer"].lower() == "acme corp" and ticket["status"] == "open"
    ]
    _assert_open_ticket_count(run.final_answer, len(open_tickets))


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
    matching = [
        call for call in calls if call.succeeded and case["check_arg"](call)
    ]
    assert matching, (
        f"no successful {case['tool']!r} call matched its arguments: "
        f"{[(call.status, call.input) for call in calls]}"
    )
    outputs = [call.own_output for call in matching if call.own_output is not None]
    if outputs:
        case["check_output"](run.final_answer, outputs)
    assert case["check_text"](run.final_answer), run.final_text
