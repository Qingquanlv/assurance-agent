"""Integration coverage for Improvement delivery graphs, contracts, and CLI entrypoints."""

from __future__ import annotations

from pathlib import Path

from assurance_agent import resources
from assurance_agent.commands.workflow_cmd import _ENTRYPOINT_CHOICE
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts, parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2, parse_workflow_v2

DELIVERY_OPS = (
    "operation:load-improvement-delivery",
    "operation:evaluate-memory-improvement",
    "operation:apply-memory-improvement",
    "operation:rollback-memory-improvement",
    "operation:export-change-improvement",
    "operation:record-change-improvement-applied",
    "operation:export-knowledge-improvement",
    "operation:record-knowledge-improvement-applied",
)

ENTRYPOINTS = (
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)


def test_delivery_entrypoints_and_operations_are_registered() -> None:
    schema = load_workflow_v2(Path.cwd(), Path("assurance_agent/_resources/schemas/workflow-schema.yaml"))
    contracts = load_execution_contracts(Path.cwd())
    compiled = compile_workflow(schema, contracts)

    for name in ENTRYPOINTS:
        assert name in compiled.entrypoints
        assert name in _ENTRYPOINT_CHOICE.choices

    ops = default_operations()
    for target in DELIVERY_OPS:
        assert target in ops
        assert target in contracts.contracts


def test_delivery_contracts_are_narrow_and_separate() -> None:
    raw = resources.read_text("schemas/execution-contracts.yaml")
    catalog = parse_execution_contracts(raw)

    memory_eval = catalog.contracts["operation:evaluate-memory-improvement"]
    memory_apply = catalog.contracts["operation:apply-memory-improvement"]
    change_export = catalog.contracts["operation:export-change-improvement"]
    knowledge_export = catalog.contracts["operation:export-knowledge-improvement"]

    assert any(".aa/memory" in path for path in memory_apply.writes)
    assert not any(".aa/memory" in path for path in memory_eval.writes)
    assert any("drafts" in path for path in change_export.writes)
    assert any("knowledge-delta" in path for path in knowledge_export.writes)
    assert not any("data-knowledge" in path for path in knowledge_export.writes)

    for target in (
        "operation:evaluate-memory-improvement",
        "operation:apply-memory-improvement",
        "operation:export-change-improvement",
        "operation:export-knowledge-improvement",
        "operation:record-change-improvement-applied",
        "operation:record-knowledge-improvement-applied",
        "operation:rollback-memory-improvement",
    ):
        contract = catalog.contracts[target]
        assert "project:qa/improvements/**" in contract.synchronized
        assert "project:improvement-registry" in contract.exclusive


def test_delivery_graphs_load_first_then_branch_on_delivery() -> None:
    raw = resources.read_text("schemas/workflow-schema.yaml")
    schema = parse_workflow_v2(raw)

    evaluate = schema.graphs["improvement-evaluate-workflow"]
    assert "load-improvement-delivery" in evaluate.nodes
    assert any(
        edge.from_ == "START" and edge.to == "load-improvement-delivery" for edge in evaluate.edges
    )
    assert evaluate.routes[0].cases["memory_patch"] == "evaluate-memory"
    assert evaluate.routes[0].default == "STOP"

    export = schema.graphs["improvement-export-workflow"]
    assert export.routes[0].cases["change_draft"] == "export-change"
    assert export.routes[0].cases["knowledge_delta"] == "export-knowledge"

    apply_graph = schema.graphs["improvement-apply-workflow"]
    assert apply_graph.routes[0].cases["memory_patch"] == "apply-memory"
    assert apply_graph.routes[0].cases["change_draft"] == "record-change-applied"
    assert apply_graph.routes[0].cases["knowledge_delta"] == "record-knowledge-applied"

    rollback = schema.graphs["improvement-rollback-workflow"]
    assert rollback.routes[0].cases["memory_patch"] == "rollback-memory"
