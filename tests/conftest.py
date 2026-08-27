from collections.abc import Iterator

import pytest

from assurance_agent.product import ProductError, reset_product, select_product
from assurance_agent.workflow.graph.capability_state import DEFAULT_PRODUCT_ID

_UNKNOWN_ASSURANCE = f"unknown product: {DEFAULT_PRODUCT_ID}"


def _select_default_product() -> None:
    try:
        select_product(DEFAULT_PRODUCT_ID)
    except ProductError as exc:
        message = str(exc)
        if message == _UNKNOWN_ASSURANCE or message.startswith(_UNKNOWN_ASSURANCE):
            return
        raise


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    # Collection imports some tests that load workflow-schema.yaml at module
    # level. Autouse fixtures run too late; select the product first.
    # After Task 7 the root no longer publishes this entry point.
    _select_default_product()


@pytest.fixture(autouse=True)
def _reset_product_and_catalog() -> Iterator[None]:
    reset_product()
    _select_default_product()
    yield
    reset_product()
