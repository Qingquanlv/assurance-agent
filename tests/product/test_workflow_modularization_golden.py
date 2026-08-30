from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from graph_engine.frozen_json import thaw_json
from graph_engine.graph.input_projection import ObjectProjection, PredecessorPointerProjection

from tests.product.graph_inventory import load_workflow_module_ownership
from tests.product.product_runner import ProductRun, _PUBLIC_DIGEST, modular_product_composition
from tests.product.public_closure import (
    PUBLIC_CLOSURE_GOLDEN,
    PUBLIC_CLOSURE_SCENARIOS,
    declared_public_projection,
    jsonable_scenario,
    public_closure_trace,
    requires_terminal_output,
)

RELOCATION_DIFF = Path(__file__).resolve().parent / "fixtures" / "architectural-relocation-diff.yaml"
INTENTIONAL_SEMANTIC_DIFF: frozenset[str] = frozenset(
    {
        "entry",
        "execute",
        "generation",
        "generation-api",
        "generation-e2e",
        "generation-fuzz",
        "generation-performance",
        "healing-coverage-repair",
        "healing-fix-proposal",
        "improvement-apply",
    }
)
THIN_WRAPPER_ENTRYPOINTS = (
    "intake",
    "case",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)


def test_intentional_semantic_diff_is_the_intake_and_generation_correction() -> None:
    document = yaml.safe_load(RELOCATION_DIFF.read_text(encoding="utf-8"))
    assert document["intentional_semantic_diff"] == []
    assert INTENTIONAL_SEMANTIC_DIFF == frozenset(
        {
            "entry",
            "execute",
            "generation",
            "generation-api",
            "generation-e2e",
            "generation-fuzz",
            "generation-performance",
            "healing-coverage-repair",
            "healing-fix-proposal",
            "improvement-apply",
        }
    )


def test_architectural_relocation_diff_records_standalone_intake() -> None:
    document = yaml.safe_load(RELOCATION_DIFF.read_text(encoding="utf-8"))
    relocations = document["relocation"]
    assert len(relocations) == 1
    assert relocations[0]["id"] == "standalone-intake-review-loop"
    assert relocations[0]["entrypoint"] == "intake"


@pytest.mark.usefixtures("installed_sources")
def test_intake_entry_matches_relocated_full_prefix(installed_sources) -> None:
    del installed_sources
    assert "entry" in INTENTIONAL_SEMANTIC_DIFF
    ownership = load_workflow_module_ownership()
    relocation = ownership["product_to_intake_relocation"]
    assert "review-pass-gate" in relocation["prefix_nodes"]
    assert "review-fix-gate" in relocation["prefix_nodes"]
    assert "review-human-gate" in relocation["prefix_nodes"]


@pytest.mark.usefixtures("installed_sources")
def test_product_wrappers_are_one_public_call_and_end(installed_sources) -> None:
    from assurance_product.product import (
        AssuranceOpenCodeProductProvider,
        load_product_workflow_module,
    )

    del installed_sources
    root = load_product_workflow_module()
    assert root == AssuranceOpenCodeProductProvider.manifest().workflow_module
    for name in THIN_WRAPPER_ENTRYPOINTS:
        graph = root.graphs[f"product-{name}"]
        kinds = {node_id: node.kind for node_id, node in graph.nodes.items()}
        subgraphs = [node_id for node_id, kind in kinds.items() if kind == "subgraph"]
        ends = [node_id for node_id, kind in kinds.items() if kind == "end"]
        assert len(subgraphs) == 1
        assert len(ends) == 1
        assert len(graph.nodes) == 2
        caller = graph.nodes[subgraphs[0]]
        assert caller.graph_import is not None
        assert caller.graph is None
        assert [(edge.from_, edge.to) for edge in graph.edges] == [(subgraphs[0], ends[0])]


@pytest.mark.usefixtures("installed_sources")
def test_product_full_threads_prepare_output_into_generate() -> None:
    from assurance_product.product import load_product_workflow_module

    root = load_product_workflow_module()
    execute_tail = root.graphs["product-full"].nodes["execute-tail"]
    generation = root.graphs["product-execute"].nodes["generation"]
    assert isinstance(execute_tail.input_projection, ObjectProjection)
    assert isinstance(generation.input_projection, ObjectProjection)
    for field in ("artifacts", "decision"):
        pointer = execute_tail.input_projection.fields[field]
        assert isinstance(pointer, PredecessorPointerProjection)
        assert pointer.predecessor == "prepare"
        assert pointer.pointer == f"/{field}"
        assert field in generation.input_projection.fields


@pytest.mark.usefixtures("installed_sources")
def test_product_full_consumes_prepare_handler_terminal(installed_sources, tmp_path) -> None:
    modular = modular_product_composition(installed_sources)
    result = ProductRun(
        entrypoint="full",
        selected_test_families=("api",),
        review_decision="pass",
        healing_decision="allowed",
        engine_root=tmp_path / "consume-prepare",
        composition=modular,
    ).run_to_terminal()
    generation = next(
        item
        for item in result.projection.graph_instances
        if item.graph_id.endswith(".generation") or item.graph_id == "generation"
    )
    payload = thaw_json(generation.input)
    assert isinstance(payload, dict)
    assert payload["decision"] == "pass"
    assert payload["artifacts"] == [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}]


@pytest.mark.usefixtures("installed_sources")
def test_modular_public_closure_matches_frozen_traces(installed_sources, tmp_path) -> None:
    expected_document = json.loads(PUBLIC_CLOSURE_GOLDEN.read_text(encoding="utf-8"))
    expected_by_key = {
        json.dumps(item["scenario"], sort_keys=True): item for item in expected_document["scenarios"]
    }
    modular = modular_product_composition(installed_sources)
    for scenario in PUBLIC_CLOSURE_SCENARIOS:
        key = json.dumps(jsonable_scenario(scenario), sort_keys=True)
        expected = expected_by_key[key]
        projection = (
            declared_public_projection(modular, str(scenario["entrypoint"]))
            if expected["require_terminal_output"]
            else None
        )
        actual = public_closure_trace(
            modular,
            tmp_path / "mod" / str(hash(key)),
            scenario,
            public_projection=projection,
        )
        assert actual["dispatches"] == expected["trace"]["dispatches"]
        assert actual["interrupts"] == expected["trace"]["interrupts"]
        assert actual["effects"] == expected["trace"]["effects"]
        assert actual["terminal"] == expected["trace"]["terminal"]
        if expected["require_terminal_output"] or requires_terminal_output(scenario):
            assert actual["terminal_output"] == expected["trace"]["terminal_output"]
