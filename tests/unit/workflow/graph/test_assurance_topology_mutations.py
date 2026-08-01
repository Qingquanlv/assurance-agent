"""Dark-ship current assurance topology mutation corpus (§8.7)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.assurance_conformance import (
    AssuranceConformanceCode,
    find_current_assurance_conformance_issues,
    with_approved_api_e2e_capability_atoms,
)
from assurance_agent.workflow.graph.replay_schema import validate_current_assurance_activation
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2, load_workflow_v2
from assurance_agent.workflow.graph.topology_analysis import (
    reorder_commutative_and,
    reorder_list_literal,
)
from assurance_agent.workflow.orchestration.schema import Verdict


def _packaged() -> WorkflowSchemaV2:
    return load_workflow_v2(Path.cwd())


def _approved() -> WorkflowSchemaV2:
    return with_approved_api_e2e_capability_atoms(_packaged())


def _codes(schema: WorkflowSchemaV2) -> set[str]:
    return {issue.code for issue in find_current_assurance_conformance_issues(schema)}


def _replace_gate_rule(schema: WorkflowSchemaV2, gate_id: str, field: str, expr: str) -> WorkflowSchemaV2:
    gate = schema.gates[gate_id]
    rules = [rule.model_copy(update={"expr": expr}) if rule.field == field else rule for rule in gate.rules]
    return schema.model_copy(
        update={"gates": {**schema.gates, gate_id: gate.model_copy(update={"rules": rules})}}
    )


def _remove_gate_rule(schema: WorkflowSchemaV2, gate_id: str, field: str) -> WorkflowSchemaV2:
    gate = schema.gates[gate_id]
    rules = [rule for rule in gate.rules if rule.field != field]
    return schema.model_copy(
        update={"gates": {**schema.gates, gate_id: gate.model_copy(update={"rules": rules})}}
    )


def _reorder_gate_rules(schema: WorkflowSchemaV2, gate_id: str) -> WorkflowSchemaV2:
    gate = schema.gates[gate_id]
    # Move pass_when before stop_when to alter first-true precedence.
    rules = list(gate.rules)
    pass_idx = next(i for i, rule in enumerate(rules) if rule.field == "pass_when")
    stop_idx = next(i for i, rule in enumerate(rules) if rule.field == "stop_when")
    rules[pass_idx], rules[stop_idx] = rules[stop_idx], rules[pass_idx]
    # Ensure pass is first after swap if needed.
    pass_rule = next(rule for rule in rules if rule.field == "pass_when")
    others = [rule for rule in rules if rule.field != "pass_when"]
    return schema.model_copy(
        update={"gates": {**schema.gates, gate_id: gate.model_copy(update={"rules": [pass_rule, *others]})}}
    )


def _mutate_assurance_when(schema: WorkflowSchemaV2, layer: str, when: str) -> WorkflowSchemaV2:
    assurance = schema.graphs["assurance"]
    node = assurance.nodes[layer].model_copy(update={"when": when})
    graph = assurance.model_copy(update={"nodes": {**assurance.nodes, layer: node}})
    return schema.model_copy(update={"graphs": {**schema.graphs, "assurance": graph}})


def _mutate_branch_edge(
    schema: WorkflowSchemaV2,
    layer: str,
    *,
    fr: str,
    to: str,
    when: str | None = None,
) -> WorkflowSchemaV2:
    branch_id = f"{layer}-branch"
    branch = schema.graphs[branch_id]
    from assurance_agent.workflow.graph.schema_v2 import EdgeDef

    edge = EdgeDef.model_validate({"from": fr, "to": to, "when": when})
    graph = branch.model_copy(update={"edges": [*branch.edges, edge]})
    return schema.model_copy(update={"graphs": {**schema.graphs, branch_id: graph}})


def _mutate_route_case(
    schema: WorkflowSchemaV2,
    graph_id: str,
    from_node: str,
    case: str,
    target: str,
) -> WorkflowSchemaV2:
    graph = schema.graphs[graph_id]
    routes = []
    for route in graph.routes:
        if route.from_ == from_node:
            cases = {**route.cases, case: target}
            routes.append(route.model_copy(update={"cases": cases}))
        else:
            routes.append(route)
    return schema.model_copy(
        update={"graphs": {**schema.graphs, graph_id: graph.model_copy(update={"routes": routes})}}
    )


def _duplicate_node(schema: WorkflowSchemaV2, graph_id: str, node_id: str, new_id: str) -> WorkflowSchemaV2:
    graph = schema.graphs[graph_id]
    node = graph.nodes[node_id]
    return schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                graph_id: graph.model_copy(update={"nodes": {**graph.nodes, new_id: node}}),
            }
        }
    )


def _remove_node(schema: WorkflowSchemaV2, graph_id: str, node_id: str) -> WorkflowSchemaV2:
    graph = schema.graphs[graph_id]
    nodes = {nid: node for nid, node in graph.nodes.items() if nid != node_id}
    edges = [edge for edge in graph.edges if edge.from_ != node_id and edge.to != node_id]
    routes = [route for route in graph.routes if route.from_ != node_id]
    return schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                graph_id: graph.model_copy(update={"nodes": nodes, "edges": edges, "routes": routes}),
            }
        }
    )


def test_unmodified_packaged_stays_on_compatibility_validator() -> None:
    schema = _packaged()
    assert validate_current_assurance_activation(schema) == ()
    # Strict dark-ship validator requires approved API/E2E capability atoms.
    assert "codegen_precondition_mismatch" in _codes(schema)


def test_approved_target_has_no_conformance_issues() -> None:
    assert find_current_assurance_conformance_issues(_approved()) == ()


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (
            lambda s: _mutate_assurance_when(s, "api", "params.run_mode == 'full'"),
            "selection_predicate_mismatch",
        ),
        (
            lambda s: _mutate_assurance_when(
                s,
                "api",
                "'e2e' in params.test_types and params.run_mode in ['full','api-only','plan-only','review-plan','codegen-only']",
            ),
            "selection_predicate_mismatch",
        ),
        (
            lambda s: _mutate_assurance_when(
                s,
                "api",
                "'api' in params.test_types and params.run_mode in ['full','api-only','plan-only','review-plan','codegen-only','case-only']",
            ),
            "selection_predicate_mismatch",
        ),
        (
            lambda s: _mutate_assurance_when(
                s,
                "api",
                "'api' in params.test_types and params.run_mode in ['full','api-only','plan-only','review-plan','codegen-only'] and unknown_param == 1",
            ),
            "selection_predicate_mismatch",
        ),
        (
            lambda s: _mutate_assurance_when(
                s,
                "api",
                "'api' in params.test_types and params.run_mode in ['full','api-only','plan-only','review-plan','codegen-only'] and len(params.test_types) > 0",
            ),
            "selection_predicate_mismatch",
        ),
    ],
)
def test_selection_truth_table_mutations(
    mutator: Callable[[WorkflowSchemaV2], WorkflowSchemaV2],
    expected: AssuranceConformanceCode,
) -> None:
    issues = find_current_assurance_conformance_issues(mutator(_approved()))
    assert expected in {issue.code for issue in issues}


def test_positive_control_commutative_selection_reorder_passes() -> None:
    schema = _approved()
    assurance = schema.graphs["assurance"]
    when = assurance.nodes["api"].when
    assert when is not None
    reordered = reorder_commutative_and(when)
    mutated = _mutate_assurance_when(schema, "api", reordered)
    assert find_current_assurance_conformance_issues(mutated) == ()


def test_positive_control_list_literal_reorder_passes() -> None:
    schema = _approved()
    assurance = schema.graphs["assurance"]
    when = assurance.nodes["api"].when
    assert when is not None
    reordered = reorder_list_literal(when)
    mutated = _mutate_assurance_when(schema, "api", reordered)
    assert find_current_assurance_conformance_issues(mutated) == ()


def test_broadened_specialty_preflight_rejected() -> None:
    schema = _approved()
    branch = schema.graphs["fuzz-branch"]
    # Broaden: make START->review-cycle unconditional (remove applicability guard intent).
    edges = []
    for edge in branch.edges:
        if edge.from_ == "applicability-preflight" and edge.to == "review-cycle":
            edges.append(edge.model_copy(update={"when": None}))
        else:
            edges.append(edge)
    mutated = schema.model_copy(
        update={"graphs": {**schema.graphs, "fuzz-branch": branch.model_copy(update={"edges": edges})}}
    )
    assert "run_mode_predicate_mismatch" in _codes(mutated)


@pytest.mark.parametrize(
    ("fr", "to"),
    [
        ("review-cycle", "codegen"),
        ("plan", "codegen"),
    ],
)
def test_direct_bypass_into_codegen_rejected(fr: str, to: str) -> None:
    mutated = _mutate_branch_edge(_approved(), "api", fr=fr, to=to)
    assert "forbidden_bypass_edge" in _codes(mutated)


def test_codegen_enabled_in_plan_only_rejected() -> None:
    schema = _approved()
    branch = schema.graphs["api-branch"]
    node = branch.nodes["codegen"].model_copy(
        update={"when": "params.run_mode in ['full','api-only','codegen-only','plan-only']"}
    )
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "api-branch": branch.model_copy(update={"nodes": {**branch.nodes, "codegen": node}}),
            }
        }
    )
    assert "run_mode_predicate_mismatch" in _codes(mutated)


def test_codegen_precondition_reads_review_cycle_status_not_bound_child_rejected() -> None:
    gate_id = "api-codegen-precondition-gate"
    mutated = _replace_gate_rule(
        _approved(),
        gate_id,
        "pass_when",
        "node('review-cycle').status == 'succeeded' and plan_assurance_state(api_plan_checks, api_plan_review, data_knowledge, 'api') == 'applicable'",
    )
    # Missing capability/file/gate atoms.
    assert "codegen_precondition_mismatch" in _codes(mutated)


@pytest.mark.parametrize("field", ["skip_when", "stop_when", "pass_when"])
def test_codegen_empty_rule_expression_rejected(field: str) -> None:
    mutated = _replace_gate_rule(_approved(), "api-codegen-precondition-gate", field, "")
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "codegen_precondition_mismatch" and issue.locator.endswith(f".{field}")
        for issue in issues
    )


def test_codegen_stop_when_or_true_broadening_rejected() -> None:
    schema = _approved()
    gate_id = "api-codegen-precondition-gate"
    stop_when = next(rule.expr for rule in schema.gates[gate_id].rules if rule.field == "stop_when")
    mutated = _replace_gate_rule(schema, gate_id, "stop_when", f"{stop_when} or true")
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "codegen_precondition_mismatch" and issue.locator.endswith(".stop_when")
        for issue in issues
    )


def test_codegen_pass_when_nested_capability_spoof_rejected() -> None:
    """Substring-visible capabilities_present nested under OR must not satisfy structural atoms."""
    schema = _approved()
    gate_id = "api-codegen-precondition-gate"
    pass_when = next(rule.expr for rule in schema.gates[gate_id].rules if rule.field == "pass_when")
    spoofed = pass_when.replace(
        "capabilities_present(api_plan_review, data_knowledge)",
        "(capabilities_present(api_plan_review, data_knowledge) or true)",
    )
    assert "capabilities_present" in spoofed
    mutated = _replace_gate_rule(schema, gate_id, "pass_when", spoofed)
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "codegen_precondition_mismatch" and issue.locator.endswith(".pass_when")
        for issue in issues
    )


def test_missing_gate_reads_or_wrong_alias_rejected() -> None:
    schema = _approved()
    profile = get_layer_assurance_profile("api")
    gate = schema.gates[profile.gate_id]
    reads = list(gate.reads)
    reads[0] = reads[0].model_copy(update={"alias": "wrong_alias"})
    mutated = schema.model_copy(
        update={"gates": {**schema.gates, profile.gate_id: gate.model_copy(update={"reads": reads})}}
    )
    assert "gate_read_mismatch" in _codes(mutated)


def test_plan_gate_invalid_json_not_stop_rejected() -> None:
    schema = _approved()
    profile = get_layer_assurance_profile("api")
    gate = schema.gates[profile.gate_id].model_copy(update={"invalid_json": Verdict.PASS})
    mutated = schema.model_copy(update={"gates": {**schema.gates, profile.gate_id: gate}})
    assert "gate_rule_mismatch" in _codes(mutated)


def test_plan_gate_skip_when_weakened_to_false_rejected() -> None:
    profile = get_layer_assurance_profile("api")
    mutated = _replace_gate_rule(_approved(), profile.gate_id, "skip_when", "false")
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "gate_rule_mismatch" and issue.locator.endswith(".skip_when") for issue in issues
    )


def test_plan_gate_reject_when_weakened_to_false_rejected() -> None:
    profile = get_layer_assurance_profile("api")
    mutated = _replace_gate_rule(_approved(), profile.gate_id, "reject_when", "false")
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "gate_rule_mismatch" and issue.locator.endswith(".reject_when") for issue in issues
    )


@pytest.mark.parametrize(
    "field",
    ["skip_when", "reject_when", "pass_when", "stop_when"],
)
def test_plan_gate_empty_rule_expression_rejected(field: str) -> None:
    profile = get_layer_assurance_profile("api")
    mutated = _replace_gate_rule(_approved(), profile.gate_id, field, "   ")
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(issue.code == "gate_rule_mismatch" and issue.locator.endswith(f".{field}") for issue in issues)


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("reject_when", "gate_rule_mismatch"),
        ("stop_when", "gate_rule_mismatch"),
    ],
)
def test_plan_gate_or_rule_broadened_with_true_rejected(field: str, code: str) -> None:
    profile = get_layer_assurance_profile("api")
    schema = _approved()
    gate = schema.gates[profile.gate_id]
    expr = next(rule.expr for rule in gate.rules if rule.field == field)
    mutated = _replace_gate_rule(schema, profile.gate_id, field, f"{expr} or true")
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(issue.code == code and issue.locator.endswith(f".{field}") for issue in issues)


@pytest.mark.parametrize(
    "removed_atom",
    [
        " and api_plan_review.decision == 'pass'",
        " and api_plan_review.codegen_readiness in ['ready','ready_with_warnings']",
        " and policy.coverage_floor.risk_high > 0",
        " and policy.coverage_floor.risk_medium > 0",
    ],
)
def test_plan_gate_pass_when_required_atom_removed_rejected(removed_atom: str) -> None:
    profile = get_layer_assurance_profile("api")
    schema = _approved()
    gate = schema.gates[profile.gate_id]
    pass_when = next(rule.expr for rule in gate.rules if rule.field == "pass_when")
    assert removed_atom in pass_when
    mutated = _replace_gate_rule(schema, profile.gate_id, "pass_when", pass_when.replace(removed_atom, ""))
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "gate_rule_mismatch" and issue.locator.endswith(".pass_when") for issue in issues
    )


def test_plan_gate_missing_field_is_not_stop_rejected() -> None:
    schema = _approved()
    profile = get_layer_assurance_profile("api")
    gate = schema.gates[profile.gate_id].model_copy(update={"missing_field_is": Verdict.PASS})
    mutated = schema.model_copy(update={"gates": {**schema.gates, profile.gate_id: gate}})
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "gate_rule_mismatch" and issue.locator.endswith(".missing_field_is") for issue in issues
    )


def test_plan_gate_missing_file_is_not_stop_rejected() -> None:
    schema = _approved()
    profile = get_layer_assurance_profile("api")
    gate = schema.gates[profile.gate_id].model_copy(update={"missing_file_is": Verdict.SKIP})
    mutated = schema.model_copy(update={"gates": {**schema.gates, profile.gate_id: gate}})
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(
        issue.code == "gate_rule_mismatch" and issue.locator.endswith(".missing_file_is") for issue in issues
    )


def test_plan_gate_pass_atom_removed_rejected() -> None:
    profile = get_layer_assurance_profile("api")
    mutated = _remove_gate_rule(_approved(), profile.gate_id, "pass_when")
    assert "gate_rule_mismatch" in _codes(mutated)


def test_plan_gate_precedence_order_change_rejected() -> None:
    profile = get_layer_assurance_profile("api")
    mutated = _reorder_gate_rules(_approved(), profile.gate_id)
    assert "gate_rule_mismatch" in _codes(mutated)


def test_plan_gate_pass_routed_to_human_review_rejected() -> None:
    mutated = _mutate_route_case(_approved(), "api-plan-cycle", "review-gate", "pass", "human-review")
    assert "route_case_mismatch" in _codes(mutated)


def test_precondition_pass_routed_wrong_target_rejected() -> None:
    mutated = _mutate_route_case(_approved(), "api-branch", "codegen-precheck", "pass", "END")
    assert "route_case_mismatch" in _codes(mutated)


@pytest.mark.parametrize(
    ("graph_id", "node_id", "new_id", "expected"),
    [
        ("api-plan-cycle", "review", "review-2", "missing_unique_node"),
        ("api-plan-cycle", "mechanical-plan-checks", "mechanical-2", "missing_unique_node"),
        ("api-plan-cycle", "review-gate", "review-gate-2", "missing_unique_node"),
        ("api-branch", "codegen-precheck", "codegen-precheck-2", "missing_unique_node"),
        ("api-branch", "codegen", "codegen-2", "missing_unique_node"),
    ],
)
def test_duplicate_owners_rejected(
    graph_id: str,
    node_id: str,
    new_id: str,
    expected: AssuranceConformanceCode,
) -> None:
    mutated = _duplicate_node(_approved(), graph_id, node_id, new_id)
    assert expected in _codes(mutated)


def test_fixer_returns_elsewhere_rejected() -> None:
    schema = _approved()
    cycle = schema.graphs["api-plan-cycle"]
    edges = [
        edge.model_copy(update={"to": "END"}) if edge.from_ == "fix" and edge.to == "review" else edge
        for edge in cycle.edges
    ]
    mutated = schema.model_copy(
        update={"graphs": {**schema.graphs, "api-plan-cycle": cycle.model_copy(update={"edges": edges})}}
    )
    assert "remediation_return_mismatch" in _codes(mutated)


def test_knowledge_remediation_returns_elsewhere_rejected() -> None:
    mutated = _mutate_route_case(
        _approved(),
        "api-plan-cycle",
        "knowledge-remediation",
        "fix_and_proceed",
        "review",
    )
    assert "remediation_return_mismatch" in _codes(mutated)


def test_interrupt_bind_none_rejected() -> None:
    from assurance_agent.workflow.graph.schema_v2 import InterruptDef

    schema = _approved()
    cycle = schema.graphs["api-plan-cycle"]
    node = cycle.nodes["human-review"]
    assert node.interrupt is not None
    bad = InterruptDef.model_construct(
        reason=node.interrupt.reason,
        checkpoint=node.interrupt.checkpoint,
        bind="none",
        actions=list(node.interrupt.actions),
        manual_revision=None,
    )
    mutated_node = node.model_copy(update={"interrupt": bad})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "api-plan-cycle": cycle.model_copy(
                    update={"nodes": {**cycle.nodes, "human-review": mutated_node}}
                ),
            }
        }
    )
    assert "interrupt_binding_mismatch" in _codes(mutated)


def test_interrupt_wrong_checkpoint_rejected() -> None:
    schema = _approved()
    cycle = schema.graphs["api-plan-cycle"]
    node = cycle.nodes["human-review"]
    assert node.interrupt is not None
    interrupt = node.interrupt.model_copy(update={"checkpoint": "wrong-gate"})
    mutated_node = node.model_copy(update={"interrupt": interrupt})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "api-plan-cycle": cycle.model_copy(
                    update={"nodes": {**cycle.nodes, "human-review": mutated_node}}
                ),
            }
        }
    )
    assert "interrupt_checkpoint_mismatch" in _codes(mutated)


def test_interrupt_wrong_actions_rejected() -> None:
    schema = _approved()
    cycle = schema.graphs["api-plan-cycle"]
    node = cycle.nodes["human-review"]
    assert node.interrupt is not None
    interrupt = node.interrupt.model_copy(update={"actions": ["stop"]})
    mutated_node = node.model_copy(update={"interrupt": interrupt})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "api-plan-cycle": cycle.model_copy(
                    update={"nodes": {**cycle.nodes, "human-review": mutated_node}}
                ),
            }
        }
    )
    assert "interrupt_action_mismatch" in _codes(mutated)


def test_specialty_manual_revision_allowlist_broadened_rejected() -> None:
    schema = _approved()
    cycle = schema.graphs["fuzz-plan-cycle"]
    node = cycle.nodes["human-review"]
    assert node.interrupt is not None and node.interrupt.manual_revision is not None
    revision = node.interrupt.manual_revision.model_copy(
        update={
            "paths": [
                *node.interrupt.manual_revision.paths,
                "change:plans/fuzz-extra.md",
            ]
        }
    )
    interrupt = node.interrupt.model_copy(update={"manual_revision": revision})
    mutated_node = node.model_copy(update={"interrupt": interrupt})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "fuzz-plan-cycle": cycle.model_copy(
                    update={"nodes": {**cycle.nodes, "human-review": mutated_node}}
                ),
            }
        }
    )
    assert "manual_revision_allowlist_mismatch" in _codes(mutated)


def test_generation_join_mode_change_rejected() -> None:
    schema = _approved()
    assurance = schema.graphs["assurance"]
    node = assurance.nodes["generation-join"]
    assert node.join is not None
    join = node.join.model_copy(update={"mode": "any"})
    mutated_node = node.model_copy(update={"join": join})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "assurance": assurance.model_copy(
                    update={"nodes": {**assurance.nodes, "generation-join": mutated_node}}
                ),
            }
        }
    )
    assert "generation_join_mismatch" in _codes(mutated)


def test_generation_join_cancel_remaining_rejected() -> None:
    schema = _approved()
    assurance = schema.graphs["assurance"]
    node = assurance.nodes["generation-join"]
    assert node.join is not None
    join = node.join.model_copy(update={"cancel_remaining": True})
    mutated_node = node.model_copy(update={"join": join})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "assurance": assurance.model_copy(
                    update={"nodes": {**assurance.nodes, "generation-join": mutated_node}}
                ),
            }
        }
    )
    assert "generation_join_mismatch" in _codes(mutated)


def test_generation_join_source_removed_rejected() -> None:
    schema = _approved()
    assurance = schema.graphs["assurance"]
    node = assurance.nodes["generation-join"]
    assert node.join is not None
    join = node.join.model_copy(update={"sources": ["api", "e2e", "fuzz"]})
    mutated_node = node.model_copy(update={"join": join})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "assurance": assurance.model_copy(
                    update={"nodes": {**assurance.nodes, "generation-join": mutated_node}}
                ),
            }
        }
    )
    assert "generation_join_mismatch" in _codes(mutated)


def test_generation_join_bypassed_by_direct_execution_edge_rejected() -> None:
    from assurance_agent.workflow.graph.schema_v2 import EdgeDef

    schema = _approved()
    assurance = schema.graphs["assurance"]
    edge = EdgeDef.model_validate({"from": "api", "to": "execution"})
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "assurance": assurance.model_copy(update={"edges": [*assurance.edges, edge]}),
            }
        }
    )
    assert "forbidden_bypass_edge" in _codes(mutated)


def test_generation_join_guarded_successor_misrouted_rejected() -> None:
    schema = _approved()
    assurance = schema.graphs["assurance"]
    edges = [
        edge.model_copy(update={"when": "true"})
        if edge.from_ == "generation-join" and edge.to == "execution"
        else edge
        for edge in assurance.edges
    ]
    mutated = schema.model_copy(
        update={
            "graphs": {
                **schema.graphs,
                "assurance": assurance.model_copy(update={"edges": edges}),
            }
        }
    )
    issues = find_current_assurance_conformance_issues(mutated)
    assert any(issue.code == "generation_join_mismatch" and "execution" in issue.locator for issue in issues)


def test_layer_missing_chain_subset_rejected() -> None:
    mutated = _remove_node(_approved(), "api-plan-cycle", "review-gate")
    assert "missing_unique_node" in _codes(mutated)


def test_api_e2e_capability_atom_removal_rejected() -> None:
    schema = _approved()
    gate = schema.gates["api-codegen-precondition-gate"]
    rules = []
    for rule in gate.rules:
        if rule.field == "pass_when":
            expr = rule.expr.replace(
                "and capabilities_present(api_plan_review, data_knowledge)",
                "",
            )
            rules.append(rule.model_copy(update={"expr": expr}))
        else:
            rules.append(rule)
    mutated = schema.model_copy(
        update={
            "gates": {
                **schema.gates,
                "api-codegen-precondition-gate": gate.model_copy(update={"rules": rules}),
            }
        }
    )
    assert "codegen_precondition_mismatch" in _codes(mutated)


def test_fail_closed_default_weakening_rejected() -> None:
    schema = _approved()
    profile = get_layer_assurance_profile("api")
    gate = schema.gates[profile.gate_id].model_copy(update={"default": Verdict.PASS})
    mutated = schema.model_copy(update={"gates": {**schema.gates, profile.gate_id: gate}})
    assert "gate_rule_mismatch" in _codes(mutated)


def test_issues_are_sorted_and_immutable() -> None:
    mutated = _mutate_assurance_when(_approved(), "api", "true")
    issues = find_current_assurance_conformance_issues(mutated)
    assert issues == tuple(
        sorted(
            issues,
            key=lambda issue: (issue.code, issue.layer or "", issue.owner, issue.locator, issue.detail),
        )
    )
    assert isinstance(issues, tuple)
