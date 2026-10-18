from __future__ import annotations

import pytest

DEFAULT_E2E_MODEL = "openai/gpt-6-luna"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-model",
        action="store",
        default=DEFAULT_E2E_MODEL,
        help=f"provider/model used by the e2e tests (default: {DEFAULT_E2E_MODEL})",
    )
