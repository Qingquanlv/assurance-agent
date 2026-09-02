from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from graph_engine.boot.graph_revision import FeatureFactoryRef
from graph_engine.boot.source_authentication import (
    FactoryAuthenticationError,
    ProductFactoryRef,
    authenticate_factory_ref,
    authenticated_editable_source,
)
from graph_engine.composition.models import ProductManifest
from graph_engine.composition.source_fs import SourceSnapshotError
from graph_engine.plugin_api import ProviderSource


def _product_source(*, import_roots: tuple[str, ...] = ("assurance_product",)) -> ProviderSource:
    return ProviderSource(
        distribution="assurance-product",
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name="assurance.product",
        entrypoint_value="assurance_product.graphs.factory:build_product_graphs",
        declaration_path="assurance_product/product-declaration.json",
        import_roots=import_roots,
    )


def _product_manifest_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": "1",
        "source": _product_source().model_dump(mode="json"),
        "product_id": "assurance.product",
        "product_version": "1.0.0",
        "engine_api": "2.0",
        "plugins": [{"plugin_id": "assurance.intake", "version_specifier": "==0.2.0"}],
        "entrypoints": {"full": "full"},
        "graph_factory_symbol": "assurance_product.graphs.factory:build_product_graphs",
    }
    values.update(overrides)
    return values


@pytest.fixture
def sources(tmp_path: Path) -> dict[str, object]:
    return {
        "assurance.intake": authenticated_editable_source(
            owner_id="assurance.intake",
            root=tmp_path / "wheel",
            import_roots=("assurance_intake",),
        )
    }


def test_factory_module_must_belong_to_authenticated_owner(tmp_path: Path) -> None:
    source = authenticated_editable_source(
        owner_id="assurance.intake",
        root=tmp_path / "wheel",
        import_roots=("assurance_intake",),
    )
    accepted = authenticate_factory_ref(
        FeatureFactoryRef(
            owner_id="assurance.intake",
            symbol="assurance_intake.graphs.factory:build_intake_graphs",
        ),
        {"assurance.intake": source},
    )
    assert accepted.owner_id == "assurance.intake"


@pytest.mark.parametrize(
    "symbol",
    [
        "sut_graphs:build",
        "assurance_generation.graphs.factory:build_generation_graphs",
        "assurance_intake.graphs.factory:_private",
    ],
)
def test_factory_origin_or_symbol_mismatch_is_rejected(symbol: str, sources: object) -> None:
    with pytest.raises(FactoryAuthenticationError):
        authenticate_factory_ref(FeatureFactoryRef(owner_id="assurance.intake", symbol=symbol), sources)


def test_sut_and_cross_owner_symbols_fail_before_import(tmp_path: Path) -> None:
    source = authenticated_editable_source(
        owner_id="assurance.intake",
        root=tmp_path / "wheel",
        import_roots=("assurance_intake",),
    )
    hostile = tmp_path / "hostile"
    (hostile / "assurance_generation" / "graphs").mkdir(parents=True)
    (hostile / "sut_graphs.py").write_text("raise RuntimeError('imported sut')\n", encoding="utf-8")
    (hostile / "assurance_generation" / "__init__.py").write_text("", encoding="utf-8")
    (hostile / "assurance_generation" / "graphs" / "__init__.py").write_text("", encoding="utf-8")
    (hostile / "assurance_generation" / "graphs" / "factory.py").write_text(
        "raise RuntimeError('imported generation')\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(hostile))
    sys.modules.pop("sut_graphs", None)
    sys.modules.pop("assurance_generation", None)
    sys.modules.pop("assurance_generation.graphs", None)
    sys.modules.pop("assurance_generation.graphs.factory", None)
    try:
        with pytest.raises(FactoryAuthenticationError):
            authenticate_factory_ref(
                FeatureFactoryRef(owner_id="assurance.intake", symbol="sut_graphs:build"),
                {"assurance.intake": source},
            )
        with pytest.raises(FactoryAuthenticationError):
            authenticate_factory_ref(
                FeatureFactoryRef(
                    owner_id="assurance.intake",
                    symbol="assurance_generation.graphs.factory:build_generation_graphs",
                ),
                {"assurance.intake": source},
            )
        assert "sut_graphs" not in sys.modules
        assert "assurance_generation.graphs.factory" not in sys.modules
    finally:
        sys.path.remove(str(hostile))


def test_planning_failure_after_preload_eviction_restores_sys_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = authenticated_editable_source(
        owner_id="assurance.intake",
        root=tmp_path / "wheel",
        import_roots=("rollback_intake",),
    )
    preload_name = "rollback_intake"
    preload = ModuleType(preload_name)
    sys.modules[preload_name] = preload
    evicted_before_planning_failure = False

    def fail_after_eviction(*_args: object, **_kwargs: object) -> object:
        nonlocal evicted_before_planning_failure
        evicted_before_planning_failure = preload_name not in sys.modules
        raise SourceSnapshotError("post-eviction planning failed")

    monkeypatch.setattr(
        "graph_engine.boot.source_authentication.extend_import_plan_with_quarantine",
        fail_after_eviction,
    )
    try:
        with pytest.raises(FactoryAuthenticationError, match="post-eviction planning failed"):
            authenticate_factory_ref(
                FeatureFactoryRef(
                    owner_id="assurance.intake",
                    symbol="rollback_intake.graphs.factory:build_intake_graphs",
                ),
                {"assurance.intake": source},
            )
        assert evicted_before_planning_failure
        assert sys.modules.get(preload_name) is preload
    finally:
        if sys.modules.get(preload_name) is preload:
            del sys.modules[preload_name]


def test_product_factory_ref_authenticates_against_product_source(tmp_path: Path) -> None:
    source = authenticated_editable_source(
        owner_id="assurance.product",
        root=tmp_path / "product-wheel",
        import_roots=("assurance_product",),
    )
    accepted = authenticate_factory_ref(
        ProductFactoryRef(
            product_id="assurance.product",
            symbol="assurance_product.graphs.factory:build_product_graphs",
        ),
        {"assurance.product": source},
    )
    assert accepted.owner_id == "assurance.product"
    assert callable(accepted.factory)


def test_product_manifest_accepts_graph_factory_symbol_form() -> None:
    manifest = ProductManifest.model_validate(_product_manifest_values())
    assert manifest.graph_factory_symbol == "assurance_product.graphs.factory:build_product_graphs"
    assert manifest.workflow is None
    assert manifest.workflow_resource_id is None
    assert manifest.workflow_module is None


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {
            "graph_factory_symbol": "assurance_product.graphs.factory:build_product_graphs",
            "workflow_resource_id": "assurance.product.flow",
        },
        {
            "graph_factory_symbol": "assurance_product.graphs.factory:build_product_graphs",
            "workflow_resource_id": "assurance.product.flow",
            "workflow": None,
        },
    ),
)
def test_product_manifest_rejects_mixed_or_empty_graph_factory_forms(payload: dict[str, object]) -> None:
    values = _product_manifest_values()
    values.pop("graph_factory_symbol")
    values.update(payload)
    with pytest.raises(ValidationError, match="exactly one workflow form"):
        ProductManifest.model_validate(values)


@pytest.mark.parametrize(
    "symbol",
    (
        "sut_graphs:build",
        "config_tree.graphs:build",
        "other_pkg.graphs.factory:build",
    ),
)
def test_product_manifest_rejects_sut_config_or_foreign_factory_symbols(symbol: str) -> None:
    with pytest.raises(ValidationError):
        ProductManifest.model_validate(_product_manifest_values(graph_factory_symbol=symbol))
