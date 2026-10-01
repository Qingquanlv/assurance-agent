from __future__ import annotations

import importlib
import importlib.util
from importlib import metadata
import sys
from pathlib import Path

import pytest

from graph_engine.boot.boot import (
    BootRequest,
    BootValidationError,
    GraphEngineBoot,
    OrganizationOverrideError,
    RuntimePorts,
)
from graph_engine.boot.graph_revision import FeatureFactoryRef
from graph_engine.boot.source_authentication import (
    FactoryAuthenticationError,
    ProductFactoryRef,
    authenticate_factory_ref,
    authenticated_editable_source,
)

from assurance_product.feature_set import CAPABILITY_OWNERS
from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
from tests.product.unused_runtime_ports import (
    UNUSED_ATTEMPT_KERNEL,
    UNUSED_SECRET_RESOLVER,
    UNUSED_WORKSPACE_PROVIDER,
)


def test_feature_graph_factories_are_the_fixed_six_owner_symbol_pairs() -> None:
    assert tuple(item.owner_id for item in FEATURE_GRAPH_FACTORIES) == CAPABILITY_OWNERS
    assert len({item.symbol for item in FEATURE_GRAPH_FACTORIES}) == len(FEATURE_GRAPH_FACTORIES) == 6


def test_feature_graph_factories_are_not_derived_from_an_entrypoint_scan() -> None:
    scanned = tuple(
        FeatureFactoryRef(entry.name, entry.value)
        for group in ("graph_engine.plugins", "graph_engine.products", "graph_engine.graphs")
        for entry in metadata.entry_points().select(group=group)
    )
    assert FEATURE_GRAPH_FACTORIES != scanned
    assert not any(
        item.symbol == scanned_item.symbol for item in FEATURE_GRAPH_FACTORIES for scanned_item in scanned
    )


def test_feature_graph_factories_ignore_configuration_override(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    (project / ".aa").mkdir(parents=True)
    (project / ".aa" / "graph-factories.yaml").write_text(
        "factories:\n  - owner_id: assurance.rogue\n    symbol: rogue.graphs:build\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("AA_FEATURE_GRAPH_FACTORIES", "assurance.rogue:rogue.graphs:build")
    importlib.reload(sys.modules["assurance_product.graph_factories"])
    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES as reloaded

    assert reloaded == FEATURE_GRAPH_FACTORIES
    assert all(item.owner_id != "assurance.rogue" for item in reloaded)


def test_importing_the_allowlist_does_not_import_feature_factory_modules() -> None:
    for name in list(sys.modules):
        if ".graphs.factory" in name:
            del sys.modules[name]
    importlib.reload(sys.modules["assurance_product.graph_factories"])
    loaded = {name for name in sys.modules if name.endswith(".graphs.factory")}
    assert loaded == set()


def _boot_helpers():
    path = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "framework"
        / "graph-engine"
        / "tests"
        / "boot"
        / "test_boot.py"
    )
    spec = importlib.util.spec_from_file_location("feature_factory_allowlist_boot_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_allowlist_rejects_missing_extra_and_duplicate_owners(tmp_path: Path) -> None:
    helpers = _boot_helpers()
    missing = FEATURE_GRAPH_FACTORIES[1:]
    extra = FEATURE_GRAPH_FACTORIES + (
        FeatureFactoryRef("assurance.rogue", "rogue.graphs.factory:build_rogue_graphs"),
    )
    duplicate = FEATURE_GRAPH_FACTORIES + FEATURE_GRAPH_FACTORIES[:1]
    assert len(missing) == 5
    assert len(extra) == 7
    with pytest.raises(ValueError, match="unique"):
        BootRequest(
            product_factory=ProductFactoryRef(
                "assurance.product",
                "assurance_product.graphs.factory:build_product_graphs",
            ),
            feature_factories=duplicate,
            sources={},
            product_lock=helpers.product_lock(),
        )

    baseline = helpers.make_boot_request(tmp_path / "wheels")
    boot = GraphEngineBoot(
        authenticator=helpers.FactoryAuthenticator(),
        contract_resolver=helpers.FakeContractResolver(),
    )
    extra_request = BootRequest(
        product_factory=baseline.product_factory,
        feature_factories=extra,
        sources=baseline.sources,
        product_lock=baseline.product_lock,
    )
    with pytest.raises(BootValidationError, match="authenticated source"):
        boot.compile_manifest(extra_request)

    missing_request = BootRequest(
        product_factory=baseline.product_factory,
        feature_factories=missing,
        sources=baseline.sources,
        product_lock=baseline.product_lock,
        expected_manifest=boot.compile_manifest(baseline),
    )
    with pytest.raises(BootValidationError, match="revision"):
        boot.boot(
            missing_request,
            checkpointer=helpers.real_anchored_checkpointer(),
            runtime_ports=RuntimePorts(
                attempt_kernel=UNUSED_ATTEMPT_KERNEL,
                secret_resolver=UNUSED_SECRET_RESOLVER,
                workspace_provider=UNUSED_WORKSPACE_PROVIDER,
            ),
        )


@pytest.mark.parametrize(
    "symbol",
    [
        "assurance_generation.graphs.factory:build_generation_graphs",
        "assurance_intake.graphs.factory:_private",
        "sut_graphs:build",
        "tmp.sut:build",
    ],
)
def test_allowlist_rejects_wrong_origin_private_symbol_and_sut_path(symbol: str, tmp_path: Path) -> None:
    source = authenticated_editable_source(
        owner_id="assurance.intake",
        root=tmp_path / "wheel",
        import_roots=("assurance_intake",),
    )
    with pytest.raises(FactoryAuthenticationError):
        authenticate_factory_ref(
            FeatureFactoryRef(owner_id="assurance.intake", symbol=symbol),
            {"assurance.intake": source},
        )


def test_allowlist_rejects_config_supplied_factory_symbol(tmp_path: Path) -> None:
    helpers = _boot_helpers()
    organization_root = tmp_path / "project"
    (organization_root / ".aa").mkdir(parents=True)
    (organization_root / ".aa" / "factory.py").write_text(
        "def build_override():\n    return {}\n",
        encoding="utf-8",
    )
    request = helpers.make_boot_request(tmp_path / "wheels", organization_root=organization_root)
    assert request.feature_factories == FEATURE_GRAPH_FACTORIES
    boot = GraphEngineBoot(
        authenticator=helpers.FactoryAuthenticator(),
        contract_resolver=helpers.FakeContractResolver(),
    )
    with pytest.raises(OrganizationOverrideError, match=r"\.aa/"):
        boot.compile_manifest(request)
