"""HTTP client fixtures for the generic QA test runtime."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from tests.config import settings  # type: ignore[import-not-found]


@pytest.fixture
def api_client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=settings.base_url) as client:
        yield client
