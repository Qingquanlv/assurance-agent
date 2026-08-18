from collections.abc import Iterator

import pytest

from assurance_agent.workflow.driver.capability_catalog import reset_catalog
from assurance_agent.workflow.graph.capability_state import reset_current_product_id


@pytest.fixture(autouse=True)
def _reset_capability_catalog() -> Iterator[None]:
    reset_catalog()
    reset_current_product_id()
    yield
    reset_catalog()
    reset_current_product_id()
