from collections.abc import Iterator
from contextlib import suppress

import pytest

from assurance_agent.product import ProductError, reset_product, select_product
from assurance_agent.workflow.graph.capability_state import DEFAULT_PRODUCT_ID


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    # Collection imports some tests that load workflow-schema.yaml at module
    # level. Autouse fixtures run too late; select the product first.
    # After Task 7 the root no longer publishes this entry point.
    with suppress(ProductError):
        select_product(DEFAULT_PRODUCT_ID)


@pytest.fixture(autouse=True)
def _reset_product_and_catalog() -> Iterator[None]:
    reset_product()
    with suppress(ProductError):
        select_product(DEFAULT_PRODUCT_ID)
    yield
    reset_product()
