from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from graph_engine.boot.graph_revision import GraphRevision
from graph_engine.canonical import JSONValue
from graph_engine.composition import FrozenComposition, SourceKey, SourceRole


@dataclass(frozen=True, slots=True)
class _LiveContractView:
    manifest: Any
    registries: Any
    lock: Any
    digest: str
    semantic_attempt_contracts: Mapping[str, Any]


def _live_contract_view(composition: FrozenComposition, contracts: Mapping[str, Any]) -> _LiveContractView:
    return _LiveContractView(
        manifest=composition.manifest,
        registries=composition.registries,
        lock=composition.lock,
        digest=composition.digest,
        semantic_attempt_contracts=MappingProxyType(dict(contracts)),
    )


def test_revision_query_preserves_full_manifest_revision(opencode_composition) -> None:
    from assurance_product import product

    lock = product.product_lock_from_composition(opencode_composition)
    full = product.product_graph_manifest(opencode_composition, lock)
    revision = product.product_graph_revision(opencode_composition, lock)

    assert revision == full.revision
    assert revision.product_lock_digest == lock.digest
    assert revision.factory_symbols[0] != ""
    assert revision.canonical_revision_id() == revision.revision_id


@dataclass
class _Contract:
    state_schema_version: str
    on_projection: Any = None

    def canonical_projection(self) -> dict[str, JSONValue]:
        if self.on_projection is not None:
            self.on_projection()
        return {"state_schema_version": self.state_schema_version}


def _small_composition() -> tuple[SimpleNamespace, SimpleNamespace, dict[SourceKey, Any]]:
    entries = {
        SourceKey(SourceRole.PRODUCT, "example.product"): SimpleNamespace(
            snapshot=SimpleNamespace(digest="1" * 64)
        ),
        SourceKey(SourceRole.PLUGIN, "assurance.intake"): SimpleNamespace(
            snapshot=SimpleNamespace(digest="2" * 64)
        ),
    }
    composition = SimpleNamespace(
        manifest=SimpleNamespace(product_id="example.product"),
        registries=SimpleNamespace(sources=SimpleNamespace(entries=entries)),
        semantic_attempt_contracts={},
    )
    return composition, SimpleNamespace(digest="a" * 64), entries


def test_small_composition_has_literal_revision_fields_and_fresh_package_version(monkeypatch) -> None:
    from importlib import metadata

    from assurance_product import product
    from assurance_product import graph_factories
    from assurance_product.graphs import revisions
    from graph_engine.boot.boot import CHECKPOINT_CONTRACT_VERSION

    composition, lock, _entries = _small_composition()
    monkeypatch.setattr(
        graph_factories, "FEATURE_GRAPH_FACTORIES", (SimpleNamespace(symbol="example:feature"),)
    )
    monkeypatch.setattr(revisions, "ENTRYPOINT_CONTRACTS", {"sample": _Contract("v7")})
    monkeypatch.setattr(metadata, "version", lambda _name: "revision-test-1")

    revision = product.product_graph_revision(composition, lock)  # type: ignore[arg-type]
    expected = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance.product": "1" * 64, "assurance.intake": "2" * 64},
        factory_symbols=(
            "assurance_product.graphs.factory:build_product_graphs",
            "example:feature",
        ),
        state_schema_versions={"sample": "v7"},
        langgraph_version="revision-test-1",
        checkpoint_contract_version=CHECKPOINT_CONTRACT_VERSION,
    )
    assert revision == expected
    assert revision.canonical_revision_id() == revision.revision_id

    monkeypatch.setattr(metadata, "version", lambda _name: "revision-test-2")
    fresh = product.product_graph_revision(composition, lock)  # type: ignore[arg-type]
    assert fresh.langgraph_version == "revision-test-2"
    assert fresh.revision_id != revision.revision_id


def test_revision_query_skips_live_attempt_projection_but_full_manifest_runs_it(
    opencode_composition,
    tmp_path: Path,
) -> None:
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.invocation_identity import InvocationIdentityRecord, write_initializing
    from assurance_product import product
    from tests.product.cli_support import lifecycle_authorization, write_project_dir

    contract_id, resolved = next(iter(opencode_composition.semantic_attempt_contracts.items()))
    projections: list[str] = []

    class RecordingContract:
        def canonical_projection(self) -> dict[str, Any]:
            projections.append(contract_id)
            return resolved.canonical_projection()

    contracts = dict(opencode_composition.semantic_attempt_contracts)
    contracts[contract_id] = RecordingContract()
    composition = _live_contract_view(opencode_composition, contracts)
    lock = product.product_lock_from_composition(composition)  # type: ignore[arg-type]

    revision = product.product_graph_revision(composition, lock)  # type: ignore[arg-type]
    assert projections == []

    workspace = product.prepare_change_workspace(write_project_dir(tmp_path / "project"), "CH-REVISION-QUERY")
    write_initializing(
        workspace,
        InvocationIdentityRecord(
            schema_version="1",
            phase="initializing",
            invocation_id="inv-revision-query",
            entrypoint="improvement-evaluate",
            root_input_digest="f" * 64,
            product_lock_digest=lock.digest,
            revision_id=revision.revision_id,
        ),
    )
    shown = AssuranceProductApplication().lock_show(
        workspace=workspace,
        composition=composition,
        authorization=lifecycle_authorization(),
        invocation_id="inv-revision-query",
    )
    assert shown["revision"] == revision.model_dump(mode="json")
    assert projections == []

    full = product.product_graph_manifest(composition, lock)  # type: ignore[arg-type]
    assert projections == [contract_id]
    assert full.attempt_contract_digests[contract_id]


def test_full_manifest_observes_replaced_live_contract_with_same_composition_identity(
    opencode_composition,
) -> None:
    from assurance_product import product

    class ChangedInput(BaseModel):
        changed: int

    contract_id, resolved = next(iter(opencode_composition.semantic_attempt_contracts.items()))
    changed_contract = replace(resolved.contract, input_model=ChangedInput)
    changed_resolved = replace(resolved, contract=changed_contract)
    lock = product.product_lock_from_composition(opencode_composition)
    original = product.product_graph_manifest(opencode_composition, lock)
    contracts = dict(opencode_composition.semantic_attempt_contracts)
    contracts[contract_id] = changed_resolved
    composition = _live_contract_view(opencode_composition, contracts)

    updated = product.product_graph_manifest(composition, lock)  # type: ignore[arg-type]
    assert composition.digest == opencode_composition.digest
    assert updated.revision == original.revision
    assert updated.attempt_contract_digests[contract_id] != original.attempt_contract_digests[contract_id]


def test_full_manifest_keeps_source_factory_observation_before_schema_callbacks(
    opencode_composition, monkeypatch
) -> None:
    from assurance_product import product
    from assurance_product import graph_factories
    from assurance_product.graphs import revisions

    composition, lock, entries = _small_composition()
    before_factory = SimpleNamespace(symbol="example:before")
    after_factory = SimpleNamespace(symbol="example:after")
    monkeypatch.setattr(graph_factories, "FEATURE_GRAPH_FACTORIES", (before_factory,))
    version_contracts = {"sample": _Contract("before")}
    monkeypatch.setattr(revisions, "ENTRYPOINT_CONTRACTS", version_contracts)
    contract_id, resolved = next(iter(opencode_composition.semantic_attempt_contracts.items()))
    input_model = resolved.contract.input_model
    original_schema = input_model.model_json_schema
    schema_calls: list[str] = []

    def schema_with_later_observations(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema_calls.append(contract_id)
        entries[SourceKey(SourceRole.PRODUCT, "example.product")] = SimpleNamespace(
            snapshot=SimpleNamespace(digest="3" * 64)
        )
        monkeypatch.setattr(graph_factories, "FEATURE_GRAPH_FACTORIES", (after_factory,))
        version_contracts["sample"] = _Contract("after")
        return original_schema(*args, **kwargs)

    monkeypatch.setattr(input_model, "model_json_schema", classmethod(schema_with_later_observations))
    composition.semantic_attempt_contracts = {contract_id: resolved}
    full = product.product_graph_manifest(composition, lock)  # type: ignore[arg-type]

    assert schema_calls == [contract_id]
    assert full.attempt_contract_digests[contract_id]
    assert full.revision.wheel_source_digests["assurance.product"] == "1" * 64
    assert "example:before" in full.revision.factory_symbols
    assert "example:after" not in full.revision.factory_symbols
    assert full.revision.state_schema_versions["sample"] == "after"
    assert full.entrypoint_contract_digests["sample"]

    fresh = product.product_graph_revision(composition, lock)  # type: ignore[arg-type]
    assert fresh.wheel_source_digests["assurance.product"] == "3" * 64
    assert "example:after" in fresh.factory_symbols
    assert fresh.revision_id != full.revision.revision_id


def test_full_manifest_uses_entrypoint_and_checkpoint_bindings_captured_before_callback(
    monkeypatch,
) -> None:
    from assurance_product import product
    from assurance_product.graphs import revisions
    from graph_engine.boot import boot as boot_module
    from graph_engine.canonical import canonical_digest

    composition, lock, _entries = _small_composition()
    captured_contract = _Contract("captured")
    rebound_contract = _Contract("rebound")
    monkeypatch.setattr(revisions, "ENTRYPOINT_CONTRACTS", {"sample": captured_contract})
    captured_checkpoint = boot_module.CHECKPOINT_CONTRACT_VERSION

    def rebind_modules() -> None:
        monkeypatch.setattr(revisions, "ENTRYPOINT_CONTRACTS", {"sample": rebound_contract})
        monkeypatch.setattr(boot_module, "CHECKPOINT_CONTRACT_VERSION", "rebound-checkpoint")

    composition.semantic_attempt_contracts = {"trigger": _Contract("unused", rebind_modules)}
    full = product.product_graph_manifest(composition, lock)  # type: ignore[arg-type]

    assert full.revision.state_schema_versions == {"sample": "captured"}
    assert full.revision.checkpoint_contract_version == captured_checkpoint
    assert full.entrypoint_contract_digests["sample"] == canonical_digest(
        captured_contract.canonical_projection()
    )


def test_bad_live_model_fails_full_gates_before_start_identity_or_checkpoint(
    opencode_composition, installed_sources, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.product import (
        prepare_change_workspace,
        product_graph_revision,
        product_lock_from_composition,
    )
    from assurance_product.runtime_ports import ProductRuntimePorts
    from tests.product.cli_support import (
        common_lifecycle_args,
        lifecycle_authorization,
    )

    resolved = next(iter(opencode_composition.semantic_attempt_contracts.values()))

    def broken_live_schema(cls: type[BaseModel], *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("broken live schema")

    monkeypatch.setattr(resolved.contract.input_model, "model_json_schema", classmethod(broken_live_schema))
    composition = opencode_composition
    lock = product_lock_from_composition(composition)
    assert product_graph_revision(composition, lock).product_lock_digest == lock.digest

    app = AssuranceProductApplication()
    with pytest.raises(RuntimeError, match="broken live schema"):
        app.compile(
            composition,
            product="assurance-opencode",
            config_tree=str(installed_sources.configuration_tree.path),
        )

    args, project, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-broken-schema",
        entrypoint="improvement-evaluate",
    )
    input_path = Path(args[args.index("--input") + 1])
    workspace = prepare_change_workspace(project, change_id)
    with pytest.raises(RuntimeError, match="broken live schema"):
        app.start(
            project_dir=project,
            change_id=change_id,
            invocation_id="inv-broken-schema",
            composition=composition,
            authorization=lifecycle_authorization(),
            entrypoint="improvement-evaluate",
            input_path=input_path,
            workspace=workspace,
        )
    assert not (workspace.paths.langgraph_identities / "inv-broken-schema.json").exists()
    assert not (
        workspace.paths.langgraph_leases / "revisions" / "bindings" / "inv-broken-schema.json"
    ).exists()

    async def open_ports() -> None:
        async with ProductRuntimePorts.open(workspace, composition, invocation="inv-broken-schema"):
            pass

    with pytest.raises(RuntimeError, match="broken live schema"):
        asyncio.run(open_ports())
    assert not (workspace.paths.langgraph_checkpoints / "checkpoints.sqlite3").exists()
