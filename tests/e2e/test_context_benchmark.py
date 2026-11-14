"""Opt-in pytest coverage for the real-agent context-savings benchmark.

The benchmark drives opencode twice per N (direct versus router) and reads the
first model step's input-token count, so it is slow and needs a configured
provider. It is excluded from the default suite by the ``benchmark`` marker:

    uv run pytest -m benchmark --e2e-model openai/gpt-6-luna
    MCP_ROUTER_BENCH_N=1,5,10 uv run pytest -m benchmark

The standalone report lives in ``scripts/context_benchmark.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.benchmark

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from context_benchmark import (  # noqa: E402
    DEFAULT_NS,
    DEFAULT_PROMPT,
    format_report,
    measure,
    parse_ns,
)


def _ns_from_env() -> list[int]:
    raw = os.environ.get("MCP_ROUTER_BENCH_N")
    if raw is None:
        return list(DEFAULT_NS)
    return parse_ns(raw)


def test_context_savings_grow_with_mcp_count(opencode_exe, e2e_model):
    ns = _ns_from_env()
    assert len(ns) >= 2, "need at least two N values to show growth"

    results = measure(
        opencode_exe,
        e2e_model,
        ns,
        timeout=int(os.environ.get("MCP_ROUTER_BENCH_TIMEOUT", "300")),
    )
    print(format_report(results, model=e2e_model, prompt=DEFAULT_PROMPT))

    for result in results:
        assert result.router_tokens < result.direct_tokens, result

    for earlier, later in zip(results, results[1:]):
        assert later.saved > earlier.saved, (
            f"savings did not grow from N={earlier.n} to N={later.n}: "
            f"{earlier.saved} -> {later.saved}"
        )
