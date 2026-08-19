from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent import resources
from assurance_agent.product import (
    AssuranceProduct,
    ProductError,
    install_product,
    load_product,
    reset_product,
    select_product,
    validate_product_id,
)
from assurance_agent.workflow.driver.capability_catalog import (
    current_operations,
    ensure_default_catalog,
)
from assurance_agent.workflow.graph.capability_state import (
    DEFAULT_PRODUCT_ID,
    CapabilityCatalog,
    CapabilityCatalogError,
    current_capability_view,
    current_product_id,
    install_current_product_id,
    reset_current_product_id,
)
from assurance_agent.workflow.graph.handlers.operation import stop_operation


def test_validate_product_id_accepts_assurance() -> None:
    assert validate_product_id(DEFAULT_PRODUCT_ID) == DEFAULT_PRODUCT_ID
    assert validate_product_id("sample") == "sample"


@pytest.mark.parametrize("bad", ["Assurance", "1abc", "ab_c", "", "a" * 33])
def test_validate_product_id_rejects_invalid(bad: str) -> None:
    with pytest.raises(ProductError, match="product"):
        validate_product_id(bad)


def test_load_unknown_product_fails() -> None:
    with pytest.raises(ProductError, match="product"):
        load_product("not-installed")


def test_assurance_entry_point_loads_matching_id() -> None:
    product = load_product(DEFAULT_PRODUCT_ID)
    assert product.id == DEFAULT_PRODUCT_ID
    root = product.resource_root()
    assert (root / "schemas" / "workflow-schema.yaml").is_file()
    view, operations, artifacts = product.register()
    assert "operation:stop" in operations
    assert view.operation_names == frozenset(operations)
    assert artifacts


def test_current_product_id_defaults_to_assurance() -> None:
    reset_current_product_id()
    try:
        assert current_product_id() == DEFAULT_PRODUCT_ID
        install_current_product_id("sample")
        assert current_product_id() == "sample"
        reset_current_product_id()
        assert current_product_id() == DEFAULT_PRODUCT_ID
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


def test_select_assurance_installs_default_catalog() -> None:
    reset_product()
    select_product(DEFAULT_PRODUCT_ID)
    assert current_capability_view() is not None
    assert "operation:stop" in current_operations()
    assert "full" in resources.read_text("schemas", "workflow-schema.yaml")


def test_ensure_default_does_not_clobber_installed_depleted_catalog() -> None:
    class Tiny:
        id = DEFAULT_PRODUCT_ID

        def resource_root(self):
            return AssuranceProduct().resource_root()

        def register(self):
            builder = CapabilityCatalog()
            builder.register_operation("operation:stop")
            view = builder.freeze()
            return view, {"operation:stop": stop_operation}, ()

    reset_product()
    install_product(Tiny())
    ensure_default_catalog()
    assert current_operations().keys() == {"operation:stop"}


def test_kernel_policy_readable_with_custom_product_root(tmp_path: Path) -> None:
    from assurance_agent.resources import set_product_resource_root

    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "workflow-schema.yaml").write_text("entrypoints:\n  full: {}\n")
    set_product_resource_root(tmp_path)
    try:
        assert "full" in resources.read_text("schemas", "workflow-schema.yaml")
        assert "policy" in resources.read_text("schemas", "policy-default.yaml")
    finally:
        set_product_resource_root(None)


def test_product_schema_missing_does_not_read_assurance_graph(tmp_path: Path) -> None:
    from assurance_agent.resources import set_product_resource_root

    (tmp_path / "schemas").mkdir()
    set_product_resource_root(tmp_path)
    try:
        with pytest.raises(FileNotFoundError):
            resources.read_text("schemas", "workflow-schema.yaml")
        assert "policy" in resources.read_text("schemas", "policy-default.yaml")
    finally:
        set_product_resource_root(None)


def test_install_product_does_not_mutate_when_register_fails() -> None:
    class Boom:
        id = "sample"

        def resource_root(self):
            return AssuranceProduct().resource_root()

        def register(self):
            raise RuntimeError("boom")

    reset_product()
    install_current_product_id("sample")
    with pytest.raises(RuntimeError, match="boom"):
        install_product(Boom())
    assert current_product_id() == "sample"
    assert current_capability_view() is None


def test_install_product_resets_on_catalog_install_failure() -> None:
    class Mismatch:
        id = "sample"

        def resource_root(self):
            return AssuranceProduct().resource_root()

        def register(self):
            builder = CapabilityCatalog()
            builder.register_operation("operation:stop")
            return builder.freeze(), {}, ()

    reset_product()
    with pytest.raises(CapabilityCatalogError, match="operations map"):
        install_product(Mismatch())
    assert current_capability_view() is None
    assert current_product_id() == DEFAULT_PRODUCT_ID
