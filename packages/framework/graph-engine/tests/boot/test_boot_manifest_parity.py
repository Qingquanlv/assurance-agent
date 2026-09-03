from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.boot.boot import (
    BootRequest,
    BootValidationError,
    FactorySourcePolicyError,
    GraphEngineBoot,
    OrganizationOverrideError,
    RuntimePorts,
)
from graph_engine.boot.source_authentication import ProductFactoryRef
from graph_engine.canonical import canonical_digest
from langgraph.checkpoint.memory import InMemorySaver

from .test_boot import (
    EXPECTED_ENTRYPOINTS,
    FactoryAuthenticator,
    FakeContractResolver,
    PRODUCT_FACTORY,
    RecordingFactories,
    _HOSTILE_PRODUCT_FACTORY_SOURCE,
    make_boot_request,
    product_lock,
    product_lock_with_configuration,
    write_boot_sources,
)


def test_ambient_environment_and_sut_files_do_not_change_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = RecordingFactories()
    factories.install(monkeypatch)
    request = make_boot_request(tmp_path / "wheels")
    boot = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=FakeContractResolver(),
    )
    first = boot.compile_manifest(request)

    monkeypatch.setenv("ASSURANCE_BOOT_AMBIENT", "mutated")
    monkeypatch.setenv("LANGGRAPH_DEBUG", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sut_graph.py").write_text("def build():\n    raise RuntimeError('sut')\n", encoding="utf-8")
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "config.yaml").write_text("policy: ambient\n", encoding="utf-8")

    second = boot.compile_manifest(request)
    assert first == second
    assert first.revision.factory_symbols == second.revision.factory_symbols
    assert first.entrypoint_contract_digests == second.entrypoint_contract_digests
    assert set(first.entrypoint_contract_digests) == EXPECTED_ENTRYPOINTS


def test_aa_topology_factory_module_override_is_rejected(tmp_path: Path) -> None:
    organization_root = tmp_path / "project"
    (organization_root / ".aa" / "graphs").mkdir(parents=True)
    (organization_root / ".aa" / "graphs" / "factory.py").write_text(
        "def build_override():\n    return {}\n",
        encoding="utf-8",
    )
    request = make_boot_request(tmp_path / "wheels", organization_root=organization_root)
    boot = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=FakeContractResolver(),
    )
    with pytest.raises(OrganizationOverrideError, match=r"\.aa/"):
        boot.compile_manifest(request)


@pytest.mark.parametrize("override_name", ["topology.yaml", "module.yaml", "factory.py"])
def test_aa_named_overrides_are_rejected(tmp_path: Path, override_name: str) -> None:
    organization_root = tmp_path / "project"
    aa = organization_root / ".aa"
    aa.mkdir(parents=True)
    (aa / override_name).write_text("override: true\n", encoding="utf-8")
    request = make_boot_request(tmp_path / "wheels", organization_root=organization_root)
    boot = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=FakeContractResolver(),
    )
    with pytest.raises(OrganizationOverrideError):
        boot.compile_manifest(request)


def test_organization_policy_change_updates_revision_not_topology(tmp_path: Path) -> None:
    sources = write_boot_sources(tmp_path / "wheels")
    baseline = BootRequest(
        product_factory=PRODUCT_FACTORY,
        feature_factories=make_boot_request(tmp_path / "unused").feature_factories,
        sources=sources,
        product_lock=product_lock(),
    )
    changed = BootRequest(
        product_factory=PRODUCT_FACTORY,
        feature_factories=baseline.feature_factories,
        sources=sources,
        product_lock=product_lock_with_configuration({"policy": "strict", "budget": 3}),
    )
    boot = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=FakeContractResolver(),
    )
    first = boot.compile_manifest(baseline)
    second = boot.compile_manifest(changed)
    assert first.revision.product_lock_digest != second.revision.product_lock_digest
    assert first.revision.revision_id != second.revision.revision_id
    assert first.revision.factory_symbols == second.revision.factory_symbols
    assert first.entrypoint_contract_digests == second.entrypoint_contract_digests
    assert first.attempt_contract_digests == second.attempt_contract_digests
    assert first.revision.factory_symbols == tuple(
        sorted(
            {
                PRODUCT_FACTORY.symbol,
                *tuple(ref.symbol for ref in baseline.feature_factories),
            }
        )
    )


def test_authenticated_contract_change_updates_attempt_and_not_factory_symbols(
    tmp_path: Path,
) -> None:
    request = make_boot_request(tmp_path)
    baseline = FakeContractResolver()
    altered = FakeContractResolver()
    original = next(iter(altered.resolve_data_contracts().values()))
    from dataclasses import replace

    from pydantic import BaseModel

    class AlteredInput(BaseModel):
        change_id: str
        extra: str = ""

    drifted = replace(original, input_model=AlteredInput)
    altered._contracts[original.contract_id] = drifted
    first = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=baseline,
    ).compile_manifest(request)
    second = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=altered,
    ).compile_manifest(request)
    assert first.revision.factory_symbols == second.revision.factory_symbols
    assert first.entrypoint_contract_digests == second.entrypoint_contract_digests
    assert first.attempt_contract_digests != second.attempt_contract_digests
    assert first.attempt_contract_digests[original.contract_id] != canonical_digest(
        drifted.canonical_projection()
    )
    assert second.attempt_contract_digests[original.contract_id] == canonical_digest(
        drifted.canonical_projection()
    )


def test_factory_unapproved_source_read_fails_spy_context_policy(tmp_path: Path) -> None:
    request = make_boot_request(
        tmp_path,
        product_factory_source=_HOSTILE_PRODUCT_FACTORY_SOURCE,
    )
    boot = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=FakeContractResolver(),
    )
    with pytest.raises(FactorySourcePolicyError):
        boot.compile_manifest(request)


def test_runtime_boot_rejects_revision_mismatch(tmp_path: Path) -> None:
    request = make_boot_request(tmp_path)
    boot = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=FakeContractResolver(),
    )
    manifest = boot.compile_manifest(request)
    drifted = BootRequest(
        product_factory=request.product_factory,
        feature_factories=request.feature_factories,
        sources=request.sources,
        product_lock=product_lock_with_configuration({"policy": "other"}),
        expected_manifest=manifest,
    )
    with pytest.raises(BootValidationError, match="revision"):
        boot.boot(
            drifted,
            checkpointer=type("Saver", (InMemorySaver,), {"backend_id": "memory"})(),
            runtime_ports=RuntimePorts(
                attempt_kernel=object(),
                secret_resolver=object(),
                workspace_provider=object(),
            ),
        )


def test_boot_does_not_accept_product_factory_from_config_tree(tmp_path: Path) -> None:
    request = make_boot_request(tmp_path)
    hostile = BootRequest(
        product_factory=ProductFactoryRef(
            "assurance.product",
            "config_tree.graphs.factory:build_product_graphs",
        ),
        feature_factories=request.feature_factories,
        sources=request.sources,
        product_lock=request.product_lock,
    )
    boot = GraphEngineBoot(
        authenticator=FactoryAuthenticator(),
        contract_resolver=FakeContractResolver(),
    )
    with pytest.raises(Exception):
        boot.compile_manifest(hostile)
