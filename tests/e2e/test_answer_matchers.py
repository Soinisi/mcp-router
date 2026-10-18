"""Deterministic checks for the natural-answer matchers in the LLM e2e tests.

These pin the accept/reject behaviour of the prose assertions without calling a
model or the network, so the matchers can be checked in the default suite.
"""

from __future__ import annotations

import pytest

from test_llm_end_to_end import _assert_open_ticket_count, _assert_status_mentioned


@pytest.mark.parametrize(
    "text",
    [
        "Acme Corp has 2 open tickets.",
        "Acme Corp has two open tickets.",
        "There are 2 open tickets for Acme Corp.",
        "Acme Corp has 2 of the 3 open tickets.",
        "2 of the 3 open tickets belong to Acme Corp.",
        "Both Acme Corp and Globex have open tickets; Acme has 2 open ones.",
    ],
)
def test_open_ticket_count_accepts_acme_count(text):
    _assert_open_ticket_count(text, 2)


@pytest.mark.parametrize(
    "text",
    [
        "Acme Corp has 1 of the 3 open tickets.",
        "Acme Corp has 3 open tickets.",
        "Globex has 1 of the 3 open tickets.",
        "There are 2 open tickets.",
        "Acme Corp has 2 of the 3 closed tickets.",
    ],
)
def test_open_ticket_count_rejects_wrong_count_or_customer(text):
    with pytest.raises(AssertionError):
        _assert_open_ticket_count(text, 2)


@pytest.mark.parametrize(
    "text",
    [
        "TCK-0001 is open.",
        "The ticket TCK-0001 is currently open.",
        "TCK-0001's status is open.",
        "TCK-0001 has status open.",
        "I looked up TCK-0001. Its status is open.",
        "TCK-0001 is open, not closed.",
    ],
)
def test_status_answer_accepts_open_as_the_bound_predicate(text):
    _assert_status_mentioned(text, "TCK-0001", "open")


@pytest.mark.parametrize(
    "text",
    [
        "TCK-0001 is closed.",
        "TCK-0001 is closed; it was open initially.",
        "TCK-0001 is not open.",
        "TCK-0002 is open.",
    ],
)
def test_status_answer_rejects_competing_or_missing_status(text):
    with pytest.raises(AssertionError):
        _assert_status_mentioned(text, "TCK-0001", "open")


@pytest.mark.parametrize(
    "text",
    [
        "TCK-0001 is closed.",
        "I closed TCK-0001.",
        "I've closed ticket TCK-0001.",
        "TCK-0001 has been closed.",
    ],
)
def test_status_answer_accepts_closed_as_the_bound_predicate(text):
    _assert_status_mentioned(text, "TCK-0001", "closed")


@pytest.mark.parametrize(
    "text",
    [
        "TCK-0001 is open.",
        "TCK-0001 is not closed.",
        "I have not closed TCK-0001.",
    ],
)
def test_status_answer_rejects_wrong_closed_predicate(text):
    with pytest.raises(AssertionError):
        _assert_status_mentioned(text, "TCK-0001", "closed")
