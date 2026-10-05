from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, TypedDict, get_type_hints

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.flow import Flow, root_schemas
from graph_engine.plugin_api import FrozenModel

from tests.product.test_stategraph_entrypoints import _build_context

if TYPE_CHECKING:
    from assurance_product.graphs.factory import ThinEntrypointGraphs

pytestmark = pytest.mark.usefixtures("installed_sources")

EXPECTED_RECURSION_LIMITS = {
    "intake": 2048,
    "full": 8192,
    "init": 512,
    "retro": 2048,
    "issue-review": 512,
    "issue-analyze": 512,
    "issue-reconcile": 512,
}


def _json_strings(value: JSONValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _json_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _json_strings(child)]
    return []


def _compiled_state(flow: Flow) -> type:
    state_type, _, _, _ = root_schemas(flow)
    return state_type


def _live_features() -> dict[str, object]:
    from assurance_execution.graphs.factory import build_execution_graphs
    from assurance_generation.graphs.factory import build_generation_graphs
    from assurance_healing.graphs.factory import build_healing_graphs
    from assurance_improvement.graphs.factory import build_improvement_graphs
    from assurance_intake.graphs.factory import build_intake_graphs
    from assurance_quality.graphs.factory import build_quality_graphs
    from graph_engine.testing import GraphHarness

    from tests.product.test_stategraph_entrypoints import _contracts_for

    builders = {
        "assurance.intake": build_intake_graphs,
        "assurance.generation": build_generation_graphs,
        "assurance.execution": build_execution_graphs,
        "assurance.quality": build_quality_graphs,
        "assurance.healing": build_healing_graphs,
        "assurance.improvement": build_improvement_graphs,
    }
    features: dict[str, object] = {}
    for owner_id, builder in builders.items():
        context = GraphHarness().recording_context(
            owner_id=owner_id,
            contracts=_contracts_for(owner_id),
        )
        features[owner_id] = builder(context)
    return features


@pytest.fixture(scope="module")
def thin_graphs() -> ThinEntrypointGraphs:
    from assurance_product.graphs.factory import build_thin_entrypoint_graphs, entrypoint_contracts

    entrypoint_contracts.cache_clear()
    return build_thin_entrypoint_graphs(context=_build_context(), features=_live_features())


def test_thin_graphs_and_contracts_have_exact_keys(thin_graphs: ThinEntrypointGraphs) -> None:
    from assurance_product.graphs.factory import entrypoint_contracts
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    contracts = entrypoint_contracts()
    assert set(contracts) == set(PRODUCT_ENTRYPOINTS)
    assert set(thin_graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS) - {"full"}
    assert len(thin_graphs.entrypoints) == 6


def test_entrypoint_digest_changes_with_schema_or_limit_not_compiled_repr() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts
    from assurance_product.graphs.revisions import canonical_contract_projection, digest

    contract = entrypoint_contracts()["intake"]
    assert digest(contract) != digest(replace(contract, recursion_limit=contract.recursion_limit + 1))
    assert digest(contract) != digest(replace(contract, state_schema_digest="d" * 64))
    assert "CompiledStateGraph" not in canonical_contract_projection(contract)
    assert digest(contract) == canonical_digest(contract.canonical_projection())


def test_exact_limit_table_uses_the_current_state_schema() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts
    from assurance_product.graphs.revisions import STATE_SCHEMA_VERSION
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    assert set(EXPECTED_RECURSION_LIMITS) == set(PRODUCT_ENTRYPOINTS)
    assert STATE_SCHEMA_VERSION == "6"
    for name, contract in entrypoint_contracts().items():
        assert isinstance(contract, EntrypointGraphContract)
        assert contract.name == name
        assert contract.recursion_limit == EXPECTED_RECURSION_LIMITS[name]
        assert contract.state_schema_version == "6"


def test_graph_revision_keeps_agent_routes_out_of_inspection_result() -> None:
    from assurance_quality.contracts.agent import InspectionResultV1

    assert "coverage_state" not in InspectionResultV1.model_fields
    assert "disposition" not in InspectionResultV1.model_fields


def test_dry_and_runtime_contract_projections_match() -> None:
    from assurance_product.graphs.factory import build_thin_entrypoint_graphs, entrypoint_contracts
    from assurance_product.graphs.revisions import canonical_contract_projection
    from assurance_product.models import THIN_ENTRYPOINTS

    features = _live_features()
    dry = build_thin_entrypoint_graphs(context=_build_context(None), features=features)
    runtime = build_thin_entrypoint_graphs(context=_build_context(InMemorySaver()), features=features)
    contracts = entrypoint_contracts()
    dry_projection = {name: canonical_contract_projection(contracts[name]) for name in dry.entrypoints}
    runtime_projection = {
        name: canonical_contract_projection(contracts[name]) for name in runtime.entrypoints
    }
    assert dry_projection == runtime_projection
    assert set(dry_projection) == set(THIN_ENTRYPOINTS)
    for name in THIN_ENTRYPOINTS:
        assert set(dry.entrypoints[name].nodes) == set(runtime.entrypoints[name].nodes)
        assert dry.entrypoints[name].checkpointer is None
        assert runtime.entrypoints[name].checkpointer is not None
        assert "CompiledStateGraph" not in repr(dry_projection[name])


def test_state_schema_digest_tracks_compiled_root_schema() -> None:
    from assurance_product.graphs.factory import declared_root_flows, entrypoint_contracts
    from assurance_product.graphs.revisions import contract_for_root

    flow = declared_root_flows()["intake"]
    expected = contract_for_root("intake", flow)
    contract = entrypoint_contracts()["intake"]
    assert contract.state_model.endswith("Flow_intake_state")
    assert contract.state_schema_digest == expected.state_schema_digest
    assert contract.input_schema_digest == expected.input_schema_digest
    assert contract.output_schema_digest == expected.output_schema_digest
    assert contract.input_schema_digest != contract.state_schema_digest


def test_state_schema_digest_excludes_function_addresses() -> None:
    from assurance_product.graphs.factory import declared_root_flows, entrypoint_contracts
    from assurance_product.graphs.revisions import canonical_contract_projection

    flow = declared_root_flows()["intake"]
    hints = get_type_hints(_compiled_state(flow), include_extras=True)
    leaked_projection: dict[str, JSONValue] = {name: str(hints[name]) for name in sorted(hints)}
    assert any(isinstance(rendered, str) and "0x" in rendered for rendered in leaked_projection.values())
    digest_value = entrypoint_contracts()["intake"].state_schema_digest
    assert "0x" not in digest_value
    assert digest_value != canonical_digest(leaked_projection)
    assert all(
        "0x" not in item
        for item in _json_strings(canonical_contract_projection(entrypoint_contracts()["intake"]))
    )


def test_root_flow_field_changes_entrypoint_digest() -> None:
    from assurance_product.graphs.revisions import contract_for_root, digest

    class BaseIn(FrozenModel):
        change_id: str

    class ExtraIn(FrozenModel):
        change_id: str
        extra_field: str = ""

    public = {"completed": "completed", "failed": "failed"}
    left = contract_for_root(
        "init", Flow("init", input=BaseIn, outcomes=("completed", "failed"), public=public)
    )
    right = contract_for_root(
        "init", Flow("init", input=ExtraIn, outcomes=("completed", "failed"), public=public)
    )
    assert digest(left) != digest(right)
    assert left.state_schema_digest != right.state_schema_digest
    assert left.input_schema_digest != right.input_schema_digest


def test_control_output_field_constraint_changes_state_digest() -> None:
    from pydantic import Field

    from assurance_product.graphs.revisions import _typeddict_digest

    Loose = TypedDict("Loose", {"rounds_budget": Annotated[int, Field(ge=0)]})
    Tight = TypedDict("Tight", {"rounds_budget": Annotated[int, Field(ge=3)]})
    assert _typeddict_digest(Loose) != _typeddict_digest(Tight)


def test_control_output_field_type_changes_state_digest() -> None:
    from pydantic import Field

    from assurance_product.graphs.revisions import _typeddict_digest

    AsString = TypedDict("AsString", {"plan_digest": Annotated[str, Field(min_length=1)]})
    AsInt = TypedDict("AsInt", {"plan_digest": Annotated[int, Field(ge=0)]})
    assert _typeddict_digest(AsString) != _typeddict_digest(AsInt)


def test_root_input_field_constraint_changes_input_digest() -> None:
    from pydantic import Field

    from assurance_product.graphs.revisions import contract_for_root

    class Loose(FrozenModel):
        change_id: str = Field(min_length=1)

    class Tight(FrozenModel):
        change_id: str = Field(min_length=8)

    public = {"completed": "completed", "failed": "failed"}
    left = contract_for_root(
        "init", Flow("init", input=Loose, outcomes=("completed", "failed"), public=public)
    )
    right = contract_for_root(
        "init", Flow("init", input=Tight, outcomes=("completed", "failed"), public=public)
    )
    assert left.input_schema_digest != right.input_schema_digest


def test_declared_and_compiled_production_contracts_match() -> None:
    from assurance_product.graphs.factory import build_product_graphs, entrypoint_contracts

    graphs = build_product_graphs(context=_build_context(), features=_live_features())
    declared = entrypoint_contracts()
    assert set(declared) == set(graphs.contracts)
    for name in declared:
        assert declared[name] == graphs.contracts[name]
