from collections.abc import Iterator

import pytest

from assurance_agent.product import reset_product


@pytest.fixture(autouse=True)
def _reset_product_and_catalog() -> Iterator[None]:
    reset_product()
    yield
    reset_product()
