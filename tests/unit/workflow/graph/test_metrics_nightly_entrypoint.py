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
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.driver.operations_catalog import default_operations
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
    run_nightly_metrics_pipeline_operation,
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
    "operation:run-nightly-metrics-pipeline": run_nightly_metrics_pipeline_operation,
}

COMPOSITE_NODE = "run-nightly-metrics-pipeline"
COMPOSITE_USES = "operation:run-nightly-metrics-pipeline"


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


def test_metrics_nightly_graph_is_a_single_pipeline_host() -> None:
    schema, _ = _compiled()
    graph = schema.graphs["metrics-nightly-workflow"]
    op_nodes = {name: node for name, node in graph.nodes.items() if node.uses.startswith("operation:")}
    assert set(op_nodes) == {COMPOSITE_NODE}
    assert op_nodes[COMPOSITE_NODE].uses == COMPOSITE_USES
    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert edges == {("START", COMPOSITE_NODE), (COMPOSITE_NODE, "END")}


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

    composite = contracts[COMPOSITE_USES]
    composite_writes = {str(path) for path in composite.writes}
    composite_auth = {str(path) for path in composite.authorization_writes}
    assert "change:inspect/metrics-nightly.json" in composite_writes
    assert "change:inspect/metrics-nightly-shortboards.json" in composite_writes
    assert "change:inspect/metrics.json" not in composite_writes
    assert "change:inspect/metrics.json" not in composite_auth
