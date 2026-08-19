from collections.abc import Iterator

import pytest

from assurance_agent.product import AssuranceProduct, reset_product
from assurance_agent.workflow.graph.product_hooks import install_product_hooks


@pytest.fixture(autouse=True)
def _reset_product_and_catalog() -> Iterator[None]:
    reset_product()
    install_product_hooks(AssuranceProduct().product_hooks())
    yield
    reset_product()
