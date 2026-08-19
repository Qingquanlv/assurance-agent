from collections.abc import Iterator

import pytest

from assurance_agent.product import reset_product, select_product
from assurance_agent.workflow.graph.capability_state import DEFAULT_PRODUCT_ID


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    # Collection imports some tests that load workflow-schema.yaml at module
    # level. Autouse fixtures run too late; select the product first.
    select_product(DEFAULT_PRODUCT_ID)


@pytest.fixture(autouse=True)
def _reset_product_and_catalog() -> Iterator[None]:
    reset_product()
    select_product(DEFAULT_PRODUCT_ID)
    yield
    reset_product()
