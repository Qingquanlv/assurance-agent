from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest

from graph_engine.boot.graph_revision import (
    BootArtifact,
    EntrypointGraphContract,
    FeatureFactoryRef,
    GraphBuildManifest,
    GraphRevision,
)
from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import InvocationLock, ProductLock


def _invocation_lock() -> InvocationLock:
    path = Path(__file__).resolve().parents[1] / "composition" / "test_lock_model.py"
    spec = importlib.util.spec_from_file_location("graph_engine_lock_model_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._lock()


def test_revision_digest_is_order_independent_and_source_sensitive() -> None:
    left = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance.intake": "b" * 64, "assurance.product": "c" * 64},
        factory_symbols=("assurance_intake.graphs.factory:build_intake_graphs",),
        state_schema_versions={"intake": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )
    reordered = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance.product": "c" * 64, "assurance.intake": "b" * 64},
        factory_symbols=("assurance_intake.graphs.factory:build_intake_graphs",),
        state_schema_versions={"intake": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )
    changed = replace(left, langgraph_version="1.2.12", revision_id="0" * 64)
    assert left == reordered
    assert changed.canonical_revision_id() != left.revision_id


def test_manifest_json_excludes_compiled_graphs_and_runtime_ports(
    manifest: GraphBuildManifest,
) -> None:
    encoded = manifest.model_dump(mode="json")
    assert set(encoded) == {"revision", "entrypoint_contract_digests", "attempt_contract_digests"}
    assert "checkpointer" not in repr(encoded)
    assert "kernel" not in repr(encoded)


def test_product_lock_v3_has_no_compiled_workflow_or_execution_host(
    product_lock: ProductLock,
) -> None:
    document = product_lock.model_dump(mode="json")
    assert document["schema_version"] == "3"
    assert "compiled_workflow" not in document
    assert "execution_host" not in document


def test_legacy_invocation_lock_v2_golden_is_unchanged(
    invocation_lock_v2: InvocationLock,
    legacy_golden_path: Path,
) -> None:
    assert invocation_lock_v2.model_dump_json() == legacy_golden_path.read_text(encoding="utf-8")


def test_revision_validates_sha256_sorted_symbols_and_schema_versions() -> None:
    common = {
        "product_lock_digest": "a" * 64,
        "wheel_source_digests": {"assurance.intake": "b" * 64},
        "factory_symbols": ("assurance_intake.graphs.factory:build_intake_graphs",),
        "state_schema_versions": {"intake": "1"},
        "langgraph_version": "1.2.11",
        "checkpoint_contract_version": "1",
    }
    with pytest.raises(ValueError, match="SHA-256"):
        GraphRevision.build(**{**common, "product_lock_digest": "NOT-A-DIGEST"})
    with pytest.raises(ValueError, match="SHA-256"):
        GraphRevision.build(**{**common, "wheel_source_digests": {"assurance.intake": "B" * 64}})
    with pytest.raises(ValueError, match="factory symbols"):
        GraphRevision.build(
            **{
                **common,
                "factory_symbols": (
                    "assurance_intake.graphs.factory:build_intake_graphs",
                    "assurance_intake.graphs.factory:build_intake_graphs",
                ),
            }
        )
    with pytest.raises(ValueError, match="schema version"):
        GraphRevision.build(**{**common, "state_schema_versions": {"intake": ""}})


def test_revision_id_is_the_canonical_projection_digest() -> None:
    revision = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance.product": "c" * 64, "assurance.intake": "b" * 64},
        factory_symbols=(
            "assurance_product.graphs.factory:build_product_graphs",
            "assurance_intake.graphs.factory:build_intake_graphs",
        ),
        state_schema_versions={"intake": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )
    assert revision.factory_symbols == (
        "assurance_intake.graphs.factory:build_intake_graphs",
        "assurance_product.graphs.factory:build_product_graphs",
    )
    assert revision.revision_id == revision.canonical_revision_id()
    assert revision.revision_id == canonical_digest(
        {
            "checkpoint_contract_version": "1",
            "factory_symbols": list(revision.factory_symbols),
            "langgraph_version": "1.2.11",
            "product_lock_digest": "a" * 64,
            "state_schema_versions": {"intake": "1"},
            "wheel_source_digests": {
                "assurance.intake": "b" * 64,
                "assurance.product": "c" * 64,
            },
        }
    )


def test_feature_factory_ref_is_owner_and_symbol() -> None:
    ref = FeatureFactoryRef(
        "assurance.intake",
        "assurance_intake.graphs.factory:build_intake_graphs",
    )
    assert ref.owner_id == "assurance.intake"
    assert ref.symbol == "assurance_intake.graphs.factory:build_intake_graphs"


def test_entrypoint_contract_projection_is_data_only() -> None:
    contract = EntrypointGraphContract(
        name="intake",
        input_model="assurance_product.models.IntakeInput",
        output_model="assurance_product.models.IntakeOutput",
        state_model="assurance_product.models.IntakeState",
        input_schema_digest="a" * 64,
        output_schema_digest="b" * 64,
        state_schema_digest="c" * 64,
        state_schema_version="1",
        recursion_limit=2048,
    )
    projection = contract.canonical_projection()
    assert projection["name"] == "intake"
    assert "CompiledStateGraph" not in repr(projection)
    assert callable not in projection.values()


def test_boot_artifact_is_runtime_only(manifest: GraphBuildManifest) -> None:
    artifact = BootArtifact(
        manifest=manifest,
        entrypoints={"intake": object()},
        attempt_contracts={},
        checkpointer_backend_id="memory",
    )
    assert not hasattr(artifact, "model_dump")
    assert not hasattr(artifact, "model_dump_json")


@pytest.fixture
def revision() -> GraphRevision:
    return GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance.intake": "b" * 64, "assurance.product": "c" * 64},
        factory_symbols=("assurance_intake.graphs.factory:build_intake_graphs",),
        state_schema_versions={"intake": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )


@pytest.fixture
def manifest(revision: GraphRevision) -> GraphBuildManifest:
    return GraphBuildManifest(
        revision=revision,
        entrypoint_contract_digests={"intake": "d" * 64},
        attempt_contract_digests={"assurance.intake.agent.prepare.v1": "e" * 64},
    )


@pytest.fixture
def invocation_lock_v2() -> InvocationLock:
    return _invocation_lock()


@pytest.fixture
def product_lock() -> ProductLock:
    lock = _invocation_lock()
    return ProductLock.create(
        engine_api=lock.engine_api,
        engine=lock.engine,
        engine_digest=lock.engine_digest,
        product=lock.product,
        plugins=lock.plugins,
        dependency_order=lock.dependency_order,
        registry_projections=lock.registry_projections,
        registry_digests=lock.registry_digests,
        configuration=lock.configuration,
        configuration_digest=lock.configuration_digest,
        capability_bindings=lock.capability_bindings,
        capability_bindings_digest=lock.capability_bindings_digest,
    )


@pytest.fixture
def legacy_golden_path() -> Path:
    return Path(__file__).resolve().parents[1] / "composition" / "invocation-lock-v2.golden.json"
