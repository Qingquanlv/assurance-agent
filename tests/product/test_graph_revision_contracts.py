from __future__ import annotations

from dataclasses import replace
from typing import Annotated, get_args, get_origin, get_type_hints

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assurance_product.graphs.factory import ThinEntrypointGraphs, build_thin_entrypoint_graphs
from assurance_product.graphs.revisions import (
    ENTRYPOINT_CONTRACTS,
    STATE_SCHEMA_VERSION,
    canonical_contract_projection,
    digest,
)
from assurance_product.graphs.state import ProductState, ProductStateDocument
from assurance_product.models import PRODUCT_ENTRYPOINTS, THIN_ENTRYPOINTS
from assurance_quality.contracts.agent import InspectionResultV1
from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.canonical import JSONValue, canonical_digest

from tests.product.test_stategraph_entrypoints import _build_context, _real_features


def _stable_type_name(hint: object) -> str:
    if isinstance(hint, type):
        if hint.__module__ == "builtins":
            return hint.__qualname__
        return f"{hint.__module__}.{hint.__qualname__}"
    name = getattr(hint, "__name__", None)
    if isinstance(name, str):
        return name
    raise TypeError(f"unsupported type hint: {hint!r}")


def _stable_origin_type(hint: object) -> str:
    origin = get_origin(hint)
    if origin is None:
        return _stable_type_name(hint)
    rendered_args = [_stable_origin_type(arg) for arg in get_args(hint)]
    rendered_origin = _stable_type_name(origin)
    if not rendered_args:
        return rendered_origin
    return f"{rendered_origin}[{', '.join(rendered_args)}]"


def _stable_hint_projection(hint: object) -> JSONValue:
    origin = get_origin(hint)
    if origin is Annotated:
        annotated_origin, *metadata = get_args(hint)
        return {
            "origin": _stable_origin_type(annotated_origin),
            "reducers": [{"module": extra.__module__, "qualname": extra.__qualname__} for extra in metadata],
        }
    return {"origin": _stable_origin_type(hint), "reducers": []}


def _stable_product_state_projection() -> dict[str, JSONValue]:
    hints = get_type_hints(ProductState, include_extras=True)
    return {name: _stable_hint_projection(hints[name]) for name in sorted(hints)}


EXPECTED_RECURSION_LIMITS = {
    "intake": 2048,
    "case": 1024,
    "full": 8192,
    "execute": 4096,
    "init": 512,
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
    assert len(thin_graphs.entrypoints) == 13


def test_entrypoint_digest_changes_with_schema_or_limit_not_compiled_repr() -> None:
    contract = ENTRYPOINT_CONTRACTS["intake"]
    assert digest(contract) != digest(replace(contract, recursion_limit=contract.recursion_limit + 1))
    assert digest(contract) != digest(replace(contract, state_schema_digest="d" * 64))
    assert "CompiledStateGraph" not in canonical_contract_projection(contract)
    assert digest(contract) == canonical_digest(contract.canonical_projection())


def test_exact_limit_table_uses_the_current_state_schema() -> None:
    assert set(EXPECTED_RECURSION_LIMITS) == set(PRODUCT_ENTRYPOINTS)
    assert STATE_SCHEMA_VERSION == "2"
    for name, contract in ENTRYPOINT_CONTRACTS.items():
        assert isinstance(contract, EntrypointGraphContract)
        assert contract.name == name
        assert contract.recursion_limit == EXPECTED_RECURSION_LIMITS[name]
        assert contract.state_schema_version == "2"


def test_graph_revision_keeps_agent_routes_out_of_inspection_result() -> None:
    assert "coverage_state" not in InspectionResultV1.model_fields
    assert "disposition" not in InspectionResultV1.model_fields


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


def test_state_schema_digest_tracks_product_state_runtime_schema() -> None:
    hints = get_type_hints(ProductState, include_extras=True)
    feature_native = {
        "feature_input",
        "feature_output",
        "rounds_budget",
        "rounds_used",
        "artifact_paths",
        "evidence_refs",
        "receipt_refs",
    }
    assert feature_native <= set(hints)
    runtime_digest = canonical_digest(_stable_product_state_projection())
    document_digest = canonical_digest(ProductStateDocument.model_json_schema())
    contract = ENTRYPOINT_CONTRACTS["intake"]
    assert contract.state_model == "assurance_product.graphs.state.ProductState"
    assert contract.state_schema_digest == runtime_digest
    assert contract.state_schema_digest != document_digest


def test_state_schema_digest_excludes_function_addresses() -> None:
    hints = get_type_hints(ProductState, include_extras=True)
    leaked_projection: dict[str, JSONValue] = {name: str(hints[name]) for name in sorted(hints)}
    assert any(isinstance(rendered, str) and "0x" in rendered for rendered in leaked_projection.values())

    stable_projection = _stable_product_state_projection()
    assert all("0x" not in rendered for rendered in _json_strings(stable_projection))

    digest_value = ENTRYPOINT_CONTRACTS["intake"].state_schema_digest
    assert "0x" not in digest_value
    assert digest_value == canonical_digest(stable_projection)
    assert digest_value != canonical_digest(leaked_projection)


def _json_strings(value: JSONValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _json_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _json_strings(child)]
    return []
