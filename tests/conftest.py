from collections.abc import Iterator

import pytest

from assurance_agent.workflow.driver.capability_catalog import reset_catalog


@pytest.fixture(autouse=True)
def _reset_capability_catalog() -> Iterator[None]:
    reset_catalog()
    yield
    reset_catalog()
