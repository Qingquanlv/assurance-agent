from __future__ import annotations

import pytest

from assurance_agent.product import (
    ProductError,
    load_product,
    validate_product_id,
)
from assurance_agent.workflow.graph.capability_state import (
    current_product_id,
    install_current_product_id,
    reset_current_product_id,
)


def test_validate_product_id_accepts_assurance() -> None:
    assert validate_product_id("assurance") == "assurance"
    assert validate_product_id("sample") == "sample"


@pytest.mark.parametrize("bad", ["Assurance", "1abc", "ab_c", "", "a" * 33])
def test_validate_product_id_rejects_invalid(bad: str) -> None:
    with pytest.raises(ProductError, match="product"):
        validate_product_id(bad)


def test_load_unknown_product_fails() -> None:
    with pytest.raises(ProductError, match="product"):
        load_product("not-installed")


def test_assurance_entry_point_loads_matching_id() -> None:
    product = load_product("assurance")
    assert product.id == "assurance"
    root = product.resource_root()
    assert (root / "schemas" / "workflow-schema.yaml").is_file()
    view, operations, artifacts = product.register()
    assert "operation:stop" in operations
    assert view.operation_names == frozenset(operations)
    assert artifacts


def test_current_product_id_defaults_to_assurance() -> None:
    reset_current_product_id()
    try:
        assert current_product_id() == "assurance"
        install_current_product_id("sample")
        assert current_product_id() == "sample"
        reset_current_product_id()
        assert current_product_id() == "assurance"
    finally:
        reset_current_product_id()


def test_duplicate_product_entry_points_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_agent import product as product_mod

    class _Ep:
        def __init__(self, name: str) -> None:
            self.name = name

    def fake_by_name() -> dict[str, list[tuple[object, str]]]:
        ep = _Ep("sample")
        return {"sample": [(ep, "dist-a"), (ep, "dist-b")]}

    monkeypatch.setattr(product_mod, "_entry_points_by_name", fake_by_name)
    with pytest.raises(ProductError, match="product"):
        load_product("sample")
