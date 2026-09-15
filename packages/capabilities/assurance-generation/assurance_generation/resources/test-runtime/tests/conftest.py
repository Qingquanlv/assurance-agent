"""HTTP client fixtures for the generic QA test runtime."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from tests.config import settings  # type: ignore[import-not-found]


def pytest_configure(config: pytest.Config) -> None:
    # Wheel-owned runner contract. Generated tests must not depend on SUT
    # pyproject.toml asyncio_mode=strict or planner-authored Markers.
    try:
        config.option.asyncio_mode = "auto"
    except AttributeError:
        pass


@pytest.fixture
def api_client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=settings.base_url) as client:
        yield client
