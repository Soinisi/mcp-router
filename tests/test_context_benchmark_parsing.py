"""The benchmark's token parser must never turn a missing count into a saving."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from context_benchmark import (  # noqa: E402
    BenchmarkError,
    full_session_tokens,
    initial_context_tokens,
    parse_ns,
)


def _step(input_tokens: int, *, cache_read: int = 0, cache_write: int = 0) -> dict:
    return {
        "type": "step_finish",
        "part": {
            "tokens": {
                "input": input_tokens,
                "output": 3,
                "cache": {"read": cache_read, "write": cache_write},
            }
        },
    }


def test_reads_first_step_input_plus_cache():
    events = [
        _step(100, cache_read=20, cache_write=5),
        _step(900),  # a later step must not be used
    ]
    assert initial_context_tokens(events) == 125


def test_full_session_sums_every_step_input_plus_cache():
    events = [
        _step(100, cache_read=20, cache_write=5),
        _step(200, cache_read=50),
        _step(300),
    ]
    assert full_session_tokens(events) == 675


def test_missing_input_token_count_fails_loudly():
    events = [{"type": "step_finish", "part": {"tokens": {"output": 3}}}]
    for read in (initial_context_tokens, full_session_tokens):
        with pytest.raises(BenchmarkError, match="input token count"):
            read(events)


def test_zero_token_count_fails_loudly():
    for read in (initial_context_tokens, full_session_tokens):
        with pytest.raises(BenchmarkError, match="zero input tokens"):
            read([_step(0)])


def test_no_step_finish_fails_loudly():
    events = [{"type": "text", "part": {"text": "ok"}}]
    for read in (initial_context_tokens, full_session_tokens):
        with pytest.raises(BenchmarkError, match="no step_finish"):
            read(events)


def test_parse_ns_sorts_deduplicates_and_validates():
    assert parse_ns("10,1,3,1") == [1, 3, 10]
    with pytest.raises(BenchmarkError):
        parse_ns("")
    with pytest.raises(BenchmarkError):
        parse_ns("0")
    with pytest.raises(BenchmarkError):
        parse_ns("one")
