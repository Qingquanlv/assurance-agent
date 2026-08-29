from __future__ import annotations

import pytest

from tests.product.composition_harness import request_for
from tests.product.graph_inventory import (
    INVENTORY_PATH,
    assert_closed_agent_aliases,
    collect_agent_triplets,
    expected_triplet_aliases,
    graph_capability_ids,
    load_graph_inventory,
)

_SLICE_GRAPHS = frozenset(
    {
        "entry",
        "intake",
        "explore",
        "case-design",
        "case-review",
        "case",
        "full",
        "generation",
        "generation-api",
        "generation-api-plan",
        "generation-api-plan-review",
        "generation-api-codegen",
        "generation-api-codegen-fix",
        "generation-e2e",
        "generation-e2e-plan",
        "generation-e2e-plan-review",
        "generation-e2e-codegen",
        "generation-e2e-codegen-fix",
        "generation-fuzz",
        "generation-fuzz-plan",
        "generation-fuzz-plan-review",
        "generation-fuzz-codegen",
        "generation-performance",
        "generation-performance-plan",
        "generation-performance-plan-review",
        "generation-performance-codegen",
        "execution-execute",
        "execution-run",
        "quality",
        "quality-fact-baseline",
        "quality-inspect",
        "quality-issue-triage",
        "quality-issue-analysis",
        "healing-fix-proposal",
        "healing-coverage-repair",
        "quality-report",
        "improvement-archive",
        "improvement-retro",
        "improvement-retro-eval-analysis",
        "improvement-retro-issue-analysis",
        "improvement-retro-workflow-analysis",
        "improvement-review",
        "retro",
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
        "execute",
        "archive",
        "issue-review",
        "issue-analyze",
        "issue-reconcile",
    }
)
_SLICE_PREPARE_IDS = (
    "assurance.intake.intake.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.case-review.prepare",
    "assurance.intake.case-design.prepare",
    "assurance.generation.api.codegen-fix.prepare",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.plan-review.prepare",
    "assurance.generation.api.plan.prepare",
    "assurance.generation.e2e.codegen-fix.prepare",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.plan-review.prepare",
    "assurance.generation.e2e.plan.prepare",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.plan-review.prepare",
    "assurance.generation.fuzz.plan.prepare",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.plan-review.prepare",
    "assurance.generation.performance.plan.prepare",
    "assurance.execution.execute.prepare",
    "assurance.execution.run.prepare",
    "assurance.healing.coverage-repair.prepare",
    "assurance.healing.fix-proposal.prepare",
    "assurance.quality.fact-baseline.prepare",
    "assurance.quality.inspect.prepare",
    "assurance.quality.issue-analysis.prepare",
    "assurance.quality.issue-triage.prepare",
    "assurance.quality.report.prepare",
    "assurance.improvement.archive.prepare",
    "assurance.improvement.improvement-review.prepare",
    "assurance.improvement.retro-eval-analysis.prepare",
    "assurance.improvement.retro-issue-analysis.prepare",
    "assurance.improvement.retro-workflow-analysis.prepare",
    "assurance.improvement.retro.prepare",
)
_PUBLIC_ENTRYPOINTS = (
    "intake",
    "case",
    "full",
    "execute",
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

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture
def compiled_product_workflow(installed_sources):
    from assurance_product.product import resolve_assurance_composition

    return resolve_assurance_composition(request_for("opencode", installed_sources)).workflow


def _named_graph(compiled_product_workflow, local_id: str):
    if local_id in compiled_product_workflow.graphs:
        return compiled_product_workflow.graphs[local_id]
    matches = [
        graph
        for graph_id, graph in compiled_product_workflow.graphs.items()
        if graph_id.endswith(f".{local_id}")
    ]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(local_id)


def _compiled_pre_modular_workflow():
    from assurance_product.product import load_canonical_workflow

    from tests.product.product_runner import resolve_product_workflow_composition

    return resolve_product_workflow_composition(load_canonical_workflow()).workflow


def test_canonical_workflow_is_loaded_from_yaml():
    from graph_engine.graph.input_projection import ObjectProjection, RootPointerProjection

    from assurance_product.product import load_canonical_workflow

    workflow = load_canonical_workflow()
    assert workflow.name == "assurance"
    assert "intake" in workflow.entrypoints
    assert "case" in workflow.entrypoints
    assert "full" in workflow.entrypoints
    assert set(workflow.entrypoints) >= set(_PUBLIC_ENTRYPOINTS)
    assert set(workflow.graphs) >= _SLICE_GRAPHS
    intake_prepare = workflow.graphs["intake"].nodes["prepare"].input_projection
    explore_prepare = workflow.graphs["explore"].nodes["prepare"].input_projection
    assert isinstance(intake_prepare, ObjectProjection)
    assert isinstance(explore_prepare, ObjectProjection)
    requirement = intake_prepare.fields["requirement"]
    assert isinstance(requirement, RootPointerProjection)
    assert requirement.pointer == "/requirement"
    assert "requirement" not in explore_prepare.fields
    case_design = workflow.graphs["case-design"]
    case_prepare = case_design.nodes["prepare"].input_projection
    case_finalize = case_design.nodes["finalize"].input_projection
    assert isinstance(case_prepare, ObjectProjection)
    assert isinstance(case_finalize, ObjectProjection)
    finalize_change_id = case_finalize.fields["change_id"]
    assert isinstance(finalize_change_id, RootPointerProjection)
    assert finalize_change_id.pointer == "/change_id"
    for projection in (case_prepare, case_finalize):
        selected = projection.fields["selected_test_families"]
        assert isinstance(selected, RootPointerProjection)
        assert selected.pointer == "/selected_test_families"
        case_delta_paths = projection.fields["case_delta_paths"]
        assert isinstance(case_delta_paths, RootPointerProjection)
        assert case_delta_paths.pointer == "/case_delta_paths"
    case_review_prepare = workflow.graphs["case-review"].nodes["prepare"].input_projection
    assert isinstance(case_review_prepare, ObjectProjection)
    review_case_paths = case_review_prepare.fields["case_delta_paths"]
    assert isinstance(review_case_paths, RootPointerProjection)
    assert review_case_paths.pointer == "/case_delta_paths"


def test_every_agent_node_is_one_closed_triplet():
    compiled = _compiled_pre_modular_workflow()
    triplets = collect_agent_triplets(compiled)
    assert triplets
    assert {triplet.prepare_id for triplet in triplets} == set(_SLICE_PREPARE_IDS)
    for triplet in triplets:
        assert triplet.aliases == expected_triplet_aliases(triplet.prepare_id)
        assert triplet.execute_input_from == triplet.prepare_node
        assert triplet.finalize_input_from == triplet.execute_node
    assert_closed_agent_aliases(compiled)


def test_full_case_review_routes_fixable_findings_back_to_case_design(
    compiled_product_workflow,
):
    graph = compiled_product_workflow.graphs.get("assurance.intake.workflow.graph.entry")
    if graph is None:
        graph = compiled_product_workflow.graphs["full"]
    nodes = set(graph.nodes)
    assert "review-round-advance" in nodes
    assert "human-review" in nodes
    assert "rejected" in nodes
    assert "exhausted" in nodes
    assert "review-pass-gate" not in nodes
    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert ("case-review", "generation") not in edges
    assert ("review-round-advance", "case-design") in edges or any(
        (edge[0] == "review-round-advance" and "case-design" in edge[1])
        or (edge[0] == "advance-join" and "case-design" in edge[1])
        for edge in edges
    )
    assert ("case-review", "human-review") in edges
    assert any(edge[1] == "rejected" for edge in edges)
    review = graph.nodes["case-review"]
    assert review.definition.routing is not None
    assert review.definition.routing.mode == "exclusive"


def test_case_review_reject_and_rework_are_distinct_routes(compiled_product_workflow):
    graph = compiled_product_workflow.graphs.get("assurance.intake.workflow.graph.entry")
    if graph is None:
        graph = compiled_product_workflow.graphs["full"]
    human = graph.nodes["human-review"]
    assert "reject" in human.definition.actions
    assert "request_rework" in human.definition.actions
    assert human.definition.routing is not None
    assert human.definition.routing.mode == "exclusive"
    edges = {(edge.from_, edge.to, edge.condition) for edge in graph.edges}
    reject_edges = {item for item in edges if item[0] == "human-review" and item[1] == "rejected"}
    rework_edges = {item for item in edges if item[0] == "human-review" and "review-round-advance" in item[1]}
    assert reject_edges
    assert rework_edges
    assert reject_edges != rework_edges


@pytest.mark.parametrize("family", ["api", "e2e", "fuzz", "performance"])
def test_generation_plan_review_routes_before_codegen(compiled_product_workflow, family):
    graph = _named_graph(compiled_product_workflow, f"generation-{family}")
    # Each review round activates plan/review plus the three mutually-exclusive
    # route gates. Keep the graph safety ceiling above the valid repair budget.
    assert graph.max_activations >= 64
    nodes = set(graph.nodes)
    assert {
        "plan-review-pass-gate",
        "plan-review-fix-gate",
        "plan-review-human-gate",
        "plan-human-review",
    }.issubset(nodes)
    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert ("plan-review", "codegen") not in edges
    assert ("plan-review-pass-gate", "codegen") in edges
    assert ("plan-review-fix-gate", "plan") in edges
    assert ("plan-review-human-gate", "plan-human-review") in edges
    assert ("plan-human-review", "codegen") in edges


@pytest.mark.parametrize("family", ["api", "e2e"])
def test_successful_codegen_does_not_unconditionally_run_fixer(compiled_product_workflow, family):
    graph = _named_graph(compiled_product_workflow, f"generation-{family}")
    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert ("codegen", "codegen-fix") not in edges
    assert ("codegen-pass-gate", "done") in edges
    assert ("codegen-fix-gate", "codegen-fix") in edges


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_workflow_compiles_under_both_product_providers(adapter, installed_sources):
    from assurance_product.product import (
        AssuranceCursorProductProvider,
        AssuranceOpenCodeProductProvider,
        resolve_assurance_composition,
    )

    composition = resolve_assurance_composition(request_for(adapter, installed_sources))
    provider = AssuranceOpenCodeProductProvider if adapter == "opencode" else AssuranceCursorProductProvider
    assert provider.manifest().workflow is None
    assert provider.manifest().workflow_module is not None
    assert set(composition.workflow.entrypoints) >= set(_PUBLIC_ENTRYPOINTS)
    capabilities = graph_capability_ids(composition.workflow)
    assert capabilities
    assert any("review-round.advance" in item for item in capabilities)
    feature_prepares = {
        capability
        for capability in capabilities
        if capability.endswith(".prepare") and capability.startswith("assurance.")
    }
    assert feature_prepares


def test_graph_inventory_records_this_slice(compiled_product_workflow):
    inventory = load_graph_inventory()
    actual_aliases = graph_capability_ids(compiled_product_workflow)
    assert any("review-round.advance" in item for item in actual_aliases)
    assert INVENTORY_PATH.is_file()
    for entrypoint in _PUBLIC_ENTRYPOINTS:
        assert inventory["entrypoints"][entrypoint]["nodes"]
        assert inventory["entrypoints"][entrypoint]["edges"]
