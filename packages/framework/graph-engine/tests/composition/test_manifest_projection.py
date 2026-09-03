from __future__ import annotations

import pytest
from pydantic import ValidationError

from graph_engine.composition.lock import _manifest_projection
from graph_engine.composition.models import (
    PluginRequirement,
    ProductManifest,
)
from graph_engine.plugin_api import ProviderSource


def _factory_source() -> ProviderSource:
    return ProviderSource(
        distribution="toy-product",
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name="toy.product",
        entrypoint_value="toy.product:build",
        declaration_path="toy_product/product-declaration.json",
        import_roots=("", "toy"),
    )


def _factory_manifest(**overrides: object) -> ProductManifest:
    values: dict[str, object] = {
        "schema_version": "1",
        "source": _factory_source(),
        "product_id": "toy.product",
        "product_version": "1.0.0",
        "engine_api": "2.0",
        "plugins": (PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.0.0"),),
        "entrypoints": {"hello": "root"},
        "graph_factory_symbol": "toy.product:build",
    }
    values.update(overrides)
    return ProductManifest.model_validate(values)


def test_factory_manifest_projection_has_no_compiled_workflow() -> None:
    manifest = _factory_manifest()
    projection = _manifest_projection(manifest)
    assert projection["graph_factory_symbol"] == "toy.product:build"
    assert "workflow" not in projection
    assert "workflow_module" not in projection
    assert "workflow_module_resources" not in projection
    assert "workflow_slot_bindings" not in projection
    assert "workflow_resource_id" not in projection
    assert "compiled_workflow" not in projection
    ProductManifest.model_validate(projection)


def test_factory_manifest_projection_round_trips_entrypoints() -> None:
    manifest = _factory_manifest(entrypoints={"你好": "根"})
    projection = _manifest_projection(manifest)
    assert projection["entrypoints"] == {"你好": "根"}
    assert ProductManifest.model_validate(projection).graph_factory_symbol == "toy.product:build"


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"workflow": {"name": "toy"}},
        {"workflow_resource_id": "toy.product.flow"},
        {"workflow_module": {"owner_id": "toy.product"}},
        {"workflow_slot_bindings": []},
    ),
)
def test_product_manifest_rejects_missing_factory_or_leftover_workflow_forms(
    payload: dict[str, object],
) -> None:
    base = {
        "schema_version": "1",
        "source": None,
        "product_id": "toy.product",
        "product_version": "1.0.0",
        "engine_api": "2.0",
        "plugins": [{"plugin_id": "toy.runtime", "version_specifier": "==1.0.0"}],
        "entrypoints": {"main": "root"},
    }
    with pytest.raises(ValidationError):
        ProductManifest.model_validate({**base, **payload})


def test_product_manifest_requires_graph_factory_symbol() -> None:
    manifest = _factory_manifest()
    values = manifest.model_dump(mode="json")
    values.pop("graph_factory_symbol")
    with pytest.raises(ValidationError):
        ProductManifest.model_validate(values)
