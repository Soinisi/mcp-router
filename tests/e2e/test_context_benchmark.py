"""Opt-in pytest coverage for the real-agent context-savings benchmark.

The benchmark drives opencode four times per N: registration-only and a real
end-to-end task, each direct versus through the router. That is slow and needs a
configured provider, so it is excluded from the default suite by the
``benchmark`` marker:

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
    format_comparison,
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
    timeout = int(os.environ.get("MCP_ROUTER_BENCH_TIMEOUT", "300"))

    registration = measure(
        opencode_exe, e2e_model, ns, mode="registration", timeout=timeout
    )
    end_to_end = measure(
        opencode_exe, e2e_model, ns, mode="end-to-end", timeout=timeout
    )
    print(format_comparison(registration, end_to_end, model=e2e_model))

    for result in registration:
        assert result.router_tokens < result.direct_tokens, result
    for earlier, later in zip(registration, registration[1:]):
        assert later.saved > earlier.saved, (
            f"registration saving did not grow from N={earlier.n} to N={later.n}: "
            f"{earlier.saved} -> {later.saved}"
        )

    # End-to-end honesty: discovery round-trips can make the router cost more at
    # small N, so only the largest N is asserted to win. The saving must still
    # grow across the sweep, which is the property the router exists to provide;
    # real agent runs vary, so this is an endpoint trend rather than a claim that
    # every adjacent pair increases.
    first, last = end_to_end[0], end_to_end[-1]
    assert last.router_tokens < last.direct_tokens, last
    assert last.saved > first.saved, (first, last)
    assert last.savings_pct > first.savings_pct, (first, last)
