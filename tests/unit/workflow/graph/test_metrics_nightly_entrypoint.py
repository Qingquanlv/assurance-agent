"""M2 Task 1: metrics-nightly entrypoint, graph order, contracts, PR isolation."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.models.metrics import (
    ARTIFACT_REL_BY_CADENCE,
    MetricsDocument,
    NIGHTLY_METRICS_REL,
    PR_METRICS_REL,
)
from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.commands.workflow_cmd import _ENTRYPOINT_CHOICE
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.metrics.adversarial_yield import collect_adversarial_yield_operation
from assurance_agent.workflow.metrics.c_layer import materialize_c_layer_metrics_operation
from assurance_agent.workflow.metrics.nightly import (
    aggregate_nightly_metrics_operation,
    compute_assertion_strength_operation,
    compute_baseline_drift_operation,
    evaluate_retrospective_shortboards_operation,
    load_latest_pr_metrics_operation,
    run_mutation_sample_operation,
)
from assurance_agent.workflow.metrics.quarantine import materialize_quarantine_projection_operation

SCHEMA_REL = Path("assurance_agent/_resources/schemas/workflow-schema.yaml")

NIGHTLY_OPS = {
    "operation:load-latest-pr-metrics": load_latest_pr_metrics_operation,
    "operation:run-mutation-sample": run_mutation_sample_operation,
    "operation:compute-assertion-strength": compute_assertion_strength_operation,
    "operation:compute-baseline-drift": compute_baseline_drift_operation,
    "operation:collect-adversarial-yield": collect_adversarial_yield_operation,
    "operation:materialize-quarantine-projection": materialize_quarantine_projection_operation,
    "operation:materialize-c-layer-metrics": materialize_c_layer_metrics_operation,
    "operation:aggregate-nightly-metrics": aggregate_nightly_metrics_operation,
    "operation:evaluate-retrospective-shortboards": evaluate_retrospective_shortboards_operation,
}

GRAPH_NODE_ORDER = (
    "load-latest-pr-metrics",
    "run-mutation-sample",
    "compute-assertion-strength",
    "compute-baseline-drift",
    "collect-adversarial-yield",
    "materialize-quarantine-projection",
    "materialize-c-layer-metrics",
    "aggregate-nightly-metrics",
    "evaluate-retrospective-shortboards",
)


def _compiled():
    schema = load_workflow_v2(Path.cwd(), SCHEMA_REL)
    contracts = load_execution_contracts(Path.cwd())
    return schema, compile_workflow(schema, contracts)


def test_nightly_metrics_path_is_registered_as_independent_must_compat() -> None:
    """§9: nightly has its own path so the PR document is written once."""
    pr = match_artifact(PR_METRICS_REL)
    nightly = match_artifact(NIGHTLY_METRICS_REL)
    assert pr is not None
    assert nightly is not None
    assert pr.artifact_type == "metrics_document"
    assert nightly.artifact_type == "metrics_nightly_document"
    assert pr.artifact_type != nightly.artifact_type
    assert nightly.model is MetricsDocument
    assert nightly.compat == "must_compat"
    assert pr.pattern != nightly.pattern


def test_cadence_to_artifact_path_is_closed_and_distinct() -> None:
    assert ARTIFACT_REL_BY_CADENCE["pr"] == "inspect/metrics.json"
    assert ARTIFACT_REL_BY_CADENCE["nightly"] == "inspect/metrics-nightly.json"
    assert set(ARTIFACT_REL_BY_CADENCE) == {"pr", "nightly"}


def test_metrics_nightly_entrypoint_is_repeatable_for_explicit_change() -> None:
    """Nightly is keyed by CLI ``--change`` (always required); allow cannot see context."""
    schema, compiled = _compiled()
    assert "metrics-nightly" in schema.entrypoints
    ep = schema.entrypoints["metrics-nightly"]
    assert ep.graph == "metrics-nightly-workflow"
    assert ep.restart == "repeatable"
    # Entrypoint allow expressions only bind ``params`` (compiler + runtime Scope);
    # explicit change_id is the workflow CLI ``--change`` flag, not a params gate.
    assert ep.allow is None
    assert compiled.entrypoints["metrics-nightly"].restart == "repeatable"
    assert compiled.entrypoints["metrics-nightly"].graph_id == "metrics-nightly-workflow"


def test_workflow_cli_choice_includes_metrics_nightly() -> None:
    assert "metrics-nightly" in set(_ENTRYPOINT_CHOICE.choices)


def test_metrics_nightly_graph_order_is_load_collect_aggregate_retro() -> None:
    schema, _ = _compiled()
    graph = schema.graphs["metrics-nightly-workflow"]
    assert set(GRAPH_NODE_ORDER) <= set(graph.nodes)

    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert ("START", "load-latest-pr-metrics") in edges
    assert ("load-latest-pr-metrics", "run-mutation-sample") in edges
    assert ("run-mutation-sample", "compute-assertion-strength") in edges
    assert ("compute-assertion-strength", "compute-baseline-drift") in edges
    assert ("compute-baseline-drift", "collect-adversarial-yield") in edges
    assert ("collect-adversarial-yield", "materialize-quarantine-projection") in edges
    assert ("materialize-quarantine-projection", "materialize-c-layer-metrics") in edges
    assert ("materialize-c-layer-metrics", "aggregate-nightly-metrics") in edges
    assert ("aggregate-nightly-metrics", "evaluate-retrospective-shortboards") in edges
    assert ("evaluate-retrospective-shortboards", "END") in edges

    assert graph.nodes["load-latest-pr-metrics"].uses == "operation:load-latest-pr-metrics"
    assert graph.nodes["run-mutation-sample"].uses == "operation:run-mutation-sample"
    assert graph.nodes["compute-assertion-strength"].uses == "operation:compute-assertion-strength"
    assert graph.nodes["compute-baseline-drift"].uses == "operation:compute-baseline-drift"
    assert graph.nodes["collect-adversarial-yield"].uses == "operation:collect-adversarial-yield"
    assert (
        graph.nodes["materialize-quarantine-projection"].uses == "operation:materialize-quarantine-projection"
    )
    assert graph.nodes["materialize-c-layer-metrics"].uses == "operation:materialize-c-layer-metrics"
    assert graph.nodes["aggregate-nightly-metrics"].uses == "operation:aggregate-nightly-metrics"
    assert (
        graph.nodes["evaluate-retrospective-shortboards"].uses
        == "operation:evaluate-retrospective-shortboards"
    )


def test_nightly_operations_registered_under_exact_keys() -> None:
    ops = default_operations()
    for key, fn in NIGHTLY_OPS.items():
        assert key in ops
        assert ops[key] is fn


def test_nightly_contracts_cover_real_surfaces_and_never_write_pr_metrics() -> None:
    contracts = load_execution_contracts(Path.cwd()).contracts
    for target in NIGHTLY_OPS:
        assert target in contracts, target
        writes = {str(path) for path in contracts[target].writes}
        auth = {str(path) for path in contracts[target].authorization_writes}
        assert "change:inspect/metrics.json" not in writes
        assert "change:inspect/metrics.json" not in auth

    aggregate = contracts["operation:aggregate-nightly-metrics"]
    assert "change:inspect/metrics-nightly.json" in {str(p) for p in aggregate.writes}
    assert "change:inspect/metrics-nightly.json" in {str(p) for p in aggregate.authorization_writes}
    assert "change:inspect/metrics.json" in {
        str(p) for p in contracts["operation:load-latest-pr-metrics"].reads
    }

    baseline = contracts["operation:compute-baseline-drift"]
    baseline_history = "project:.aa/baseline/performance/**"
    assert baseline_history in {str(path) for path in baseline.reads}
    assert baseline_history in {str(path) for path in baseline.writes}
    assert baseline_history in {str(path) for path in baseline.authorization_writes}
    assert baseline_history in {str(path) for path in baseline.synchronized}


def test_nightly_graph_wires_adversarial_yield_collector_before_aggregate() -> None:
    """M3: B3 collector is pinned before aggregate-nightly-metrics."""
    schema, _ = _compiled()
    graph = schema.graphs["metrics-nightly-workflow"]
    uses = {node.uses for node in graph.nodes.values()}
    assert "operation:collect-adversarial-yield" in uses
    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert ("collect-adversarial-yield", "materialize-quarantine-projection") in edges
    assert ("materialize-quarantine-projection", "materialize-c-layer-metrics") in edges
    assert ("materialize-c-layer-metrics", "aggregate-nightly-metrics") in edges
