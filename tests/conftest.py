"""Pytest config + shared fixtures for pipeline tests.

Markers:
- ``integration``: requires live network access (Reddit, Firecrawl). Skipped
  by default; enable with ``pytest -m integration`` (or
  ``pytest -m "integration or not integration"`` to run everything).

Phase 2+ adds VCR-recorded Reddit fixtures here as they become useful.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so ``pytest --strict-markers`` stays clean."""

    config.addinivalue_line(
        "markers",
        "integration: live-network test (Reddit, Firecrawl). Run with `-m integration`.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``@pytest.mark.integration`` tests unless ``-m integration`` was passed.

    Without this, ``pytest -v`` would attempt live network calls on every run.
    """

    # If the user explicitly asked for integration tests, don't skip them.
    markexpr = config.getoption("-m", default="")
    if "integration" in str(markexpr):
        return

    skip_integration = pytest.mark.skip(
        reason="integration tests skipped by default — run with `-m integration`"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
