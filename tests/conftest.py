from collections.abc import Iterator

import pytest

from assurance_agent.product import reset_product, select_product
from assurance_agent.workflow.graph.capability_state import DEFAULT_PRODUCT_ID


@pytest.fixture(autouse=True)
def _reset_product_and_catalog() -> Iterator[None]:
    reset_product()
    select_product(DEFAULT_PRODUCT_ID)
    yield
    reset_product()
