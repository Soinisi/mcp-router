"""The analytical routing estimate is pure arithmetic: no network, no model."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from context_benchmark import (  # noqa: E402
    BenchmarkError,
    BenchmarkResult,
    RoutingEstimate,
    format_comparison,
    format_estimate,
)


def _estimate(**overrides) -> RoutingEstimate:
    values = {
        "tools_per_server": 10,
        "tokens_per_tool": 100,
        "steps": 2,
        "router_tools": 3,
        "discovery_round_trips": 2,
        "discovery_tokens_per_round_trip": 1000,
        "worth_routing_margin": 0.25,
    }
    values.update(overrides)
    return RoutingEstimate(**values)


def test_break_even_is_where_costs_are_equal():
    estimate = _estimate()
    # direct(13) = 2 * 13 * 100 = 2600
    # router      = 2 * 3 * 100 + 2 * 1000 = 2600
    assert estimate.break_even_tools() == pytest.approx(13.0)
    assert estimate.direct_tokens(13) == estimate.router_tokens()


def test_worth_routing_requires_the_configured_margin():
    estimate = _estimate()
    worth = estimate.worth_routing_tools()
    assert worth == pytest.approx(13.0 / 0.75)

    above = 18  # first integer tool count whose saving clears 25%
    saving = 1.0 - estimate.router_tokens() / estimate.direct_tokens(above)
    assert saving >= 0.25

    below = 17
    saving = 1.0 - estimate.router_tokens() / estimate.direct_tokens(below)
    assert saving < 0.25


def test_longer_sessions_lower_the_break_even():
    short = _estimate(steps=2)
    long = _estimate(steps=4)
    assert long.break_even_tools() < short.break_even_tools()
    assert long.worth_routing_tools() < short.worth_routing_tools()


def test_more_discovery_round_trips_raise_the_break_even():
    fewer = _estimate(discovery_round_trips=1)
    more = _estimate(discovery_round_trips=3)
    assert more.break_even_tools() > fewer.break_even_tools()


def test_invalid_assumptions_fail_loudly():
    with pytest.raises(BenchmarkError, match="tools_per_server"):
        _estimate(tools_per_server=0)
    with pytest.raises(BenchmarkError, match="tokens_per_tool"):
        _estimate(tokens_per_tool=0)
    with pytest.raises(BenchmarkError, match="steps"):
        _estimate(steps=0)
    with pytest.raises(BenchmarkError, match="discovery_round_trips"):
        _estimate(discovery_round_trips=-1)
    with pytest.raises(BenchmarkError, match="worth_routing_margin"):
        _estimate(worth_routing_margin=1.0)
    with pytest.raises(BenchmarkError, match="worth_routing_margin"):
        _estimate().worth_routing_tools(margin=-0.1)
    with pytest.raises(BenchmarkError, match="must not be negative"):
        _estimate().direct_tokens(-1)


def test_estimate_block_prints_formula_break_even_and_threshold():
    lines = format_estimate(_estimate())
    text = "\n".join(lines)
    assert "direct(M) = 2 x M x 100" in text
    assert "router    = 2 x 3 x 100 + 2000" in text
    assert "break-even: M* = 3 + 2000 / (2 x 100) = 13 tools" in text
    assert "worth routing (>= 25% of direct's tool catalog): M = M* / (1 - 25%) = 18 tools" in text
    assert "assumptions: 10 tools per server" in text


def test_default_estimate_matches_the_measured_single_search_crossover():
    estimate = RoutingEstimate()
    # Measured end-to-end: the router costs more at N=1 and first saves at N=3,
    # i.e. around 35 tools with 12 tools per server (README crossover table).
    assert estimate.router_tokens() > estimate.direct_tokens(
        estimate.tools_per_server
    )
    assert estimate.router_tokens() < estimate.direct_tokens(
        3 * estimate.tools_per_server
    )


def test_comparison_prints_estimate_next_to_measured_crossover():
    registration = [
        BenchmarkResult(n=1, direct_tokens=2000, router_tokens=1800),
        BenchmarkResult(n=10, direct_tokens=8000, router_tokens=1800),
    ]
    end_to_end = [
        BenchmarkResult(n=1, direct_tokens=5000, router_tokens=9000),
        BenchmarkResult(n=10, direct_tokens=16000, router_tokens=14000),
    ]
    report = format_comparison(registration, end_to_end, model="test/model")
    crossover_at = report.index("Crossover:")
    estimate_at = report.index("Analytical estimate")
    assert crossover_at < estimate_at
    assert "break-even" in report
    assert "worth routing" in report
