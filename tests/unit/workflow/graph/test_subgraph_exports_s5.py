"""S5：subgraph exports 编译与 apply_subgraph_exports。"""

from __future__ import annotations

import textwrap

import pytest

from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.frozen_output import FrozenOutput, frozen_outputs_wire
from assurance_agent.workflow.graph.models import CompiledExport, GraphProjection, NodeGeneration, NodeHistory
from assurance_agent.workflow.graph.node_history import node_history_key
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.subgraph_exports import SubgraphExportError, apply_subgraph_exports


def test_compiler_attaches_exports_to_graph_node() -> None:
    schema = parse_workflow_v2(
        textwrap.dedent(
            """
            schema_version: "2"
            name: export-test
            entrypoints:
              full:
                graph: main
            graphs:
              child:
                max_supersteps: 4
                exports:
                  api_plan_review:
                    from: review
                    output: api_plan_review
                nodes:
                  review:
                    uses: skill:noop
                edges:
                  - {from: START, to: review}
                  - {from: review, to: END}
              main:
                max_supersteps: 4
                nodes:
                  run-child:
                    uses: graph:child
                edges:
                  - {from: START, to: run-child}
                  - {from: run-child, to: END}
            """
        )
    )
    compiled = compile_workflow(schema)
    node = compiled.graphs["main"].nodes["run-child"]
    assert node.exports == (
        CompiledExport(symbol="api_plan_review", from_node="review", output="api_plan_review"),
    )


def test_apply_subgraph_exports_forwards_committed_frozen_output() -> None:
    fo = FrozenOutput(
        value={"decision": "approve"},
        source_path="change:review/api-plan-review.json",
        source_sha256="deadbeef",
        model_id="review@1",
        model_schema_digest="schema",
        catalog_symbol="api_plan_review",
    )
    wire = frozen_outputs_wire({"api_plan_review": fo})
    checkpoint_ns = "inv-1/parent/child-inv"
    key = node_history_key(checkpoint_ns, "child", "review")
    projection = GraphProjection(
        invocation_id="child-inv",
        entrypoint="child",
        checkpoint_ns=checkpoint_ns,
        structural_path="main/run-child/child",
        graph_digest="gd",
        contract_digests={},
        params={},
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        node_histories={
            key: NodeHistory(
                latest_generation_ordinal=0,
                generations_by_ordinal={
                    0: NodeGeneration(
                        generation_ordinal=0,
                        status="succeeded",
                        outputs_committed=True,
                        frozen_outputs=wire,
                    )
                },
            )
        },
    )
    exported = apply_subgraph_exports(
        child_projection=projection,
        child_graph_id="child",
        export_defs=(CompiledExport(symbol="api_plan_review", from_node="review", output="api_plan_review"),),
    )
    assert exported["api_plan_review"].source_sha256 == "deadbeef"
    assert exported["api_plan_review"].value == {"decision": "approve"}


def test_apply_subgraph_exports_fails_without_committed_generation() -> None:
    checkpoint_ns = "inv-1/parent/child-inv"
    key = node_history_key(checkpoint_ns, "child", "review")
    projection = GraphProjection(
        invocation_id="child-inv",
        entrypoint="child",
        checkpoint_ns=checkpoint_ns,
        structural_path="main/run-child/child",
        graph_digest="gd",
        contract_digests={},
        params={},
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        node_histories={
            key: NodeHistory(
                latest_generation_ordinal=0,
                generations_by_ordinal={
                    0: NodeGeneration(generation_ordinal=0, status="succeeded", outputs_committed=False),
                },
            )
        },
    )
    with pytest.raises(SubgraphExportError, match="no committed success"):
        apply_subgraph_exports(
            child_projection=projection,
            child_graph_id="child",
            export_defs=(CompiledExport(symbol="x", from_node="review", output="api_plan_review"),),
        )
