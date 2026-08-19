import pytest

from assurance_agent.workflow.graph.capability_state import (
    assert_product_compatible,
    install_current_product_id,
    reset_current_product_id,
)
from assurance_agent.workflow.graph.runtime import GraphRuntimeError, ProductDrift


def test_empty_pin_allows_assurance() -> None:
    reset_current_product_id()
    assert_product_compatible("")


def test_empty_pin_rejects_sample() -> None:
    install_current_product_id("sample")
    with pytest.raises(ProductDrift, match="product_id") as err:
        assert_product_compatible("")
    assert "graph_definition_changed" not in str(err.value)
    reset_current_product_id()


def test_mismatch_pin_rejects() -> None:
    reset_current_product_id()
    with pytest.raises(ProductDrift, match="product_id") as err:
        assert_product_compatible("sample")
    assert "graph_definition_changed" not in str(err.value)


def test_matching_pin_allows() -> None:
    install_current_product_id("sample")
    assert_product_compatible("sample")
    reset_current_product_id()


def test_product_drift_is_graph_runtime_error() -> None:
    assert issubclass(ProductDrift, GraphRuntimeError)
