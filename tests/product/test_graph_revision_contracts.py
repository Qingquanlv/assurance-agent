from __future__ import annotations

from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assurance_product.graphs.factory import ThinEntrypointGraphs, build_thin_entrypoint_graphs
from assurance_product.graphs.revisions import (
    ENTRYPOINT_CONTRACTS,
    canonical_contract_projection,
    digest,
)
from assurance_product.models import PRODUCT_ENTRYPOINTS, THIN_ENTRYPOINTS
from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.canonical import canonical_digest

from tests.product.test_stategraph_entrypoints import _build_context, _real_features

EXPECTED_RECURSION_LIMITS = {
    "intake": 2048,
    "case": 1024,
    "full": 8192,
    "execute": 4096,
    "archive": 512,
    "retro": 2048,
    "issue-review": 512,
    "issue-analyze": 512,
    "issue-reconcile": 512,
    "improvement-review": 512,
    "improvement-evaluate": 512,
    "improvement-export": 512,
    "improvement-apply": 1024,
    "improvement-rollback": 512,
}


@pytest.fixture(scope="module")
def thin_graphs() -> ThinEntrypointGraphs:
    return build_thin_entrypoint_graphs(context=_build_context(), features=_real_features())


def test_thin_graphs_and_contracts_have_exact_keys(thin_graphs: ThinEntrypointGraphs) -> None:
    assert set(ENTRYPOINT_CONTRACTS) == set(PRODUCT_ENTRYPOINTS)
    assert set(thin_graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS) - {"full", "execute"}
    assert len(thin_graphs.entrypoints) == 12


def test_entrypoint_digest_changes_with_schema_or_limit_not_compiled_repr() -> None:
    contract = ENTRYPOINT_CONTRACTS["intake"]
    assert digest(contract) != digest(replace(contract, recursion_limit=contract.recursion_limit + 1))
    assert digest(contract) != digest(replace(contract, state_schema_digest="d" * 64))
    assert "CompiledStateGraph" not in canonical_contract_projection(contract)
    assert digest(contract) == canonical_digest(contract.canonical_projection())


def test_exact_limit_table_and_state_schema_version_one() -> None:
    assert set(EXPECTED_RECURSION_LIMITS) == set(PRODUCT_ENTRYPOINTS)
    for name, contract in ENTRYPOINT_CONTRACTS.items():
        assert isinstance(contract, EntrypointGraphContract)
        assert contract.name == name
        assert contract.recursion_limit == EXPECTED_RECURSION_LIMITS[name]
        assert contract.state_schema_version == "1"


def test_dry_and_runtime_contract_projections_match() -> None:
    features = _real_features()
    dry = build_thin_entrypoint_graphs(context=_build_context(None), features=features)
    runtime = build_thin_entrypoint_graphs(context=_build_context(InMemorySaver()), features=features)
    dry_projection = {
        name: canonical_contract_projection(ENTRYPOINT_CONTRACTS[name]) for name in dry.entrypoints
    }
    runtime_projection = {
        name: canonical_contract_projection(ENTRYPOINT_CONTRACTS[name]) for name in runtime.entrypoints
    }
    assert dry_projection == runtime_projection
    assert set(dry_projection) == set(THIN_ENTRYPOINTS)
    for name in THIN_ENTRYPOINTS:
        assert set(dry.entrypoints[name].nodes) == set(runtime.entrypoints[name].nodes)
        assert dry.entrypoints[name].checkpointer is None
        assert runtime.entrypoints[name].checkpointer is not None
        assert "CompiledStateGraph" not in repr(dry_projection[name])
