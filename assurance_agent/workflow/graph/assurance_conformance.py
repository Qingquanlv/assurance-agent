"""Structured current four-layer assurance topology conformance (dark-shipped)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, LayerName
from assurance_agent.verification.profiles import (
    LayerAssuranceProfile,
    get_layer_assurance_profile,
    iter_layer_assurance_profiles,
)
from assurance_agent.workflow.graph.schema_v2 import GraphDef, WorkflowSchemaV2
from assurance_agent.workflow.graph.topology_analysis import (
    Assignment,
    build_cfg,
    dominates,
    expression_has_top_level_predicates,
    expression_matches_required,
    expressions_truth_equivalent,
    layer_selection_domain,
    paths_exist_avoiding,
)
from assurance_agent.workflow.orchestration.schema import GateDef, Verdict, derive_alias

AssuranceConformanceCode = Literal[
    "missing_unique_node",
    "missing_required_edge",
    "forbidden_bypass_edge",
    "route_case_mismatch",
    "selection_predicate_mismatch",
    "run_mode_predicate_mismatch",
    "gate_read_mismatch",
    "gate_rule_mismatch",
    "codegen_precondition_mismatch",
    "interrupt_binding_mismatch",
    "interrupt_checkpoint_mismatch",
    "interrupt_action_mismatch",
    "manual_revision_allowlist_mismatch",
    "remediation_return_mismatch",
    "generation_join_mismatch",
    "missing_validate",
]

_DATA_KNOWLEDGE_PATH = "repo:.aa/data-knowledge.yaml"
_DATA_KNOWLEDGE_ALIAS = "data_knowledge"
_ASSURANCE_GRAPH = "assurance"
_SPECIALTY_LAYERS = frozenset({"fuzz", "performance"})
_API_E2E_LAYERS = frozenset({"api", "e2e"})

_LAYER_SELECTION_MODES: dict[str, tuple[str, ...]] = {
    "api": ("full", "api-only", "plan-only", "review-plan", "codegen-only"),
    "e2e": ("full", "e2e-only", "plan-only", "review-plan", "codegen-only"),
    "fuzz": ("full", "plan-only", "review-plan", "codegen-only"),
    "performance": ("full", "plan-only", "review-plan", "codegen-only"),
}

_PLAN_GATE_ROUTE_CASES = (
    "pass",
    "skip",
    "needs_fix",
    "knowledge_remediation",
    "needs_human_review",
    "reject",
    "stop",
)

_CODEGEN_ROUTE_CASES = ("pass", "skip", "stop")

_SELECTION_PARAMS = frozenset({"test_types", "run_mode"})
_SELECTION_BUILTINS: frozenset[str] = frozenset()
_RUN_MODE_PARAMS = frozenset({"run_mode", "test_types"})


@dataclass(frozen=True, slots=True)
class AssuranceConformanceIssue:
    code: AssuranceConformanceCode
    layer: LayerName | None
    owner: str
    locator: str
    detail: str


@dataclass(frozen=True, slots=True)
class _LayerRoles:
    layer: LayerName
    profile: LayerAssuranceProfile
    assurance_node_id: str
    branch_graph_id: str
    branch: GraphDef
    cycle_call_node_id: str
    cycle_graph_id: str
    cycle: GraphDef
    applicability_node_id: str
    reviewer_node_id: str
    gate_node_id: str
    human_review_node_id: str
    knowledge_remediation_node_id: str
    fixer_node_id: str | None
    codegen_precondition_node_id: str
    codegen_node_id: str
    plan_node_id: str | None
    preflight_node_id: str | None


def find_current_assurance_conformance_issues(
    schema: WorkflowSchemaV2,
) -> tuple[AssuranceConformanceIssue, ...]:
    """Return sorted structured issues for the approved current assurance shape.

    Does not mutate ``schema`` and never returns a next-node suggestion.
    Unknown params/builtins and parse failures are structured mismatches.
    """
    issues: list[AssuranceConformanceIssue] = []
    issues.extend(_missing_validate_issues(schema))
    issues.extend(_generation_join_issues(schema))
    for profile in iter_layer_assurance_profiles():
        layer_issues, roles = _discover_layer_roles(schema, profile)
        issues.extend(layer_issues)
        if roles is None:
            continue
        issues.extend(_selection_issues(schema, roles))
        issues.extend(_run_mode_topology_issues(roles))
        issues.extend(_chain_edge_issues(roles))
        issues.extend(_route_issues(roles))
        issues.extend(_plan_gate_issues(schema, roles))
        issues.extend(_codegen_precondition_issues(schema, roles))
        issues.extend(_interrupt_issues(roles))
        issues.extend(_remediation_issues(roles))
        issues.extend(_bypass_issues(roles))
    return _sorted_issues(issues)


def render_assurance_conformance_issues(
    issues: tuple[AssuranceConformanceIssue, ...] | list[AssuranceConformanceIssue],
) -> tuple[str, ...]:
    """Deterministic string rendering for compatibility callers."""
    return tuple(f"{issue.locator}: [{issue.code}] {issue.detail}" for issue in _sorted_issues(issues))


def _sorted_issues(
    issues: list[AssuranceConformanceIssue] | tuple[AssuranceConformanceIssue, ...],
) -> tuple[AssuranceConformanceIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.code,
                issue.layer or "",
                issue.owner,
                issue.locator,
                issue.detail,
            ),
        )
    )


def _missing_validate_issues(schema: WorkflowSchemaV2) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    for graph_id, graph in schema.graphs.items():
        for nid, node in graph.nodes.items():
            if node.validate_ is None:
                issues.append(
                    _issue(
                        "missing_validate",
                        layer=None,
                        owner=nid,
                        locator=f"graphs.{graph_id}.nodes.{nid}.validate",
                        detail="packaged node must declare validate (use none if no reconciler)",
                    )
                )
    return issues


def _issue(
    code: AssuranceConformanceCode,
    *,
    layer: LayerName | None,
    owner: str,
    locator: str,
    detail: str,
) -> AssuranceConformanceIssue:
    return AssuranceConformanceIssue(
        code=code,
        layer=layer,
        owner=owner,
        locator=locator,
        detail=detail,
    )


def _discover_layer_roles(
    schema: WorkflowSchemaV2,
    profile: LayerAssuranceProfile,
) -> tuple[list[AssuranceConformanceIssue], _LayerRoles | None]:
    layer = profile.layer
    issues: list[AssuranceConformanceIssue] = []
    assurance = schema.graphs.get(_ASSURANCE_GRAPH)
    if assurance is None:
        issues.append(
            _issue(
                "missing_unique_node",
                layer=None,
                owner="assurance",
                locator="graph:assurance",
                detail="missing assurance graph",
            )
        )
        return issues, None

    branch_nodes = [
        node_id
        for node_id, node in assurance.nodes.items()
        if node.uses == f"graph:{layer}-branch" or node_id == layer
    ]
    if len(branch_nodes) != 1:
        issues.append(
            _issue(
                "missing_unique_node",
                layer=layer,
                owner="assurance",
                locator=f"graph:assurance.nodes.{layer}",
                detail=f"expected exactly one assurance branch node, found {len(branch_nodes)}",
            )
        )
        return issues, None
    assurance_node_id = branch_nodes[0]
    branch_node = assurance.nodes[assurance_node_id]
    if not branch_node.uses.startswith("graph:"):
        issues.append(
            _issue(
                "missing_unique_node",
                layer=layer,
                owner="assurance",
                locator=f"graph:assurance.nodes.{assurance_node_id}",
                detail="assurance branch must use graph:<id>",
            )
        )
        return issues, None
    branch_graph_id = branch_node.uses.removeprefix("graph:")
    branch = schema.graphs.get(branch_graph_id)
    if branch is None:
        issues.append(
            _issue(
                "missing_unique_node",
                layer=layer,
                owner="branch",
                locator=f"graph:{branch_graph_id}",
                detail="missing branch graph",
            )
        )
        return issues, None

    cycle_calls = [
        (node_id, node.uses.removeprefix("graph:"))
        for node_id, node in branch.nodes.items()
        if node.uses.startswith("graph:") and "cycle" in node.uses
    ]
    if len(cycle_calls) != 1:
        issues.append(
            _issue(
                "missing_unique_node",
                layer=layer,
                owner="review-cycle",
                locator=f"graph:{branch_graph_id}",
                detail=f"expected exactly one cycle-call node, found {len(cycle_calls)}",
            )
        )
        return issues, None
    cycle_call_node_id, cycle_graph_id = cycle_calls[0]
    cycle = schema.graphs.get(cycle_graph_id)
    if cycle is None:
        issues.append(
            _issue(
                "missing_unique_node",
                layer=layer,
                owner="review-cycle",
                locator=f"graph:{cycle_graph_id}",
                detail="missing plan-cycle graph",
            )
        )
        return issues, None

    applicability = _nodes_with_uses(cycle, "operation:derive-plan-layer-applicability", layer=layer)
    reviewers = [nid for nid, node in cycle.nodes.items() if node.uses == f"skill:aa-{layer}-plan-reviewer"]
    gates = [
        nid
        for nid, node in cycle.nodes.items()
        if node.uses == "builtin:gate" and node.with_.get("gate") == profile.gate_id
    ]
    human = [
        nid
        for nid, node in cycle.nodes.items()
        if nid == "human-review"
        or (node.uses == "builtin:interrupt" and "knowledge" not in nid and nid != "knowledge-remediation")
    ]
    knowledge = [
        nid
        for nid, node in cycle.nodes.items()
        if nid == "knowledge-remediation" or (node.uses == "builtin:interrupt" and "knowledge" in nid)
    ]
    author_reentry = f"skill:aa-{layer}-plan"
    fixers = [
        nid
        for nid, node in cycle.nodes.items()
        if node.uses.startswith("skill:") and ("fixer" in node.uses or node.uses == author_reentry)
    ]
    codegen_gate_id = f"{layer}-codegen-precondition-gate"
    prechecks = [
        nid
        for nid, node in branch.nodes.items()
        if node.uses == "builtin:gate" and node.with_.get("gate") == codegen_gate_id
    ]
    codegen = [
        nid
        for nid, node in branch.nodes.items()
        if node.uses.startswith("skill:") and "codegen" in node.uses and "fixer" not in node.uses
    ]
    plans = [
        nid
        for nid, node in branch.nodes.items()
        if node.uses.startswith("skill:") and node.uses.endswith("-plan")
    ]
    preflights = [
        nid
        for nid, node in branch.nodes.items()
        if "preflight" in nid or node.uses == "operation:derive-plan-layer-applicability"
    ]

    def require_one(name: str, owner: str, found: list[str], locator: str) -> str | None:
        if len(found) != 1:
            issues.append(
                _issue(
                    "missing_unique_node",
                    layer=layer,
                    owner=owner,
                    locator=locator,
                    detail=f"expected exactly one {name}, found {len(found)}",
                )
            )
            return None
        return found[0]

    applicability_id = require_one("applicability", "applicability", applicability, f"graph:{cycle_graph_id}")
    reviewer_id = require_one("reviewer", "reviewer", reviewers, f"graph:{cycle_graph_id}")
    gate_id = require_one("plan gate owner", "plan-gate", gates, f"graph:{cycle_graph_id}")
    human_id = require_one("human-review interrupt", "human-review", human, f"graph:{cycle_graph_id}")
    knowledge_id = require_one(
        "knowledge remediation interrupt",
        "knowledge-remediation",
        knowledge,
        f"graph:{cycle_graph_id}",
    )
    precheck_id = require_one(
        "codegen precondition",
        "codegen-precheck",
        prechecks,
        f"graph:{branch_graph_id}",
    )
    codegen_id = require_one("codegen skill", "codegen", codegen, f"graph:{branch_graph_id}")

    if (
        applicability_id is None
        or reviewer_id is None
        or gate_id is None
        or human_id is None
        or knowledge_id is None
        or precheck_id is None
        or codegen_id is None
    ):
        return issues, None

    # Duplicate owner probes: a second codegen/precheck/reviewer already failed uniqueness.
    return issues, _LayerRoles(
        layer=layer,
        profile=profile,
        assurance_node_id=assurance_node_id,
        branch_graph_id=branch_graph_id,
        branch=branch,
        cycle_call_node_id=cycle_call_node_id,
        cycle_graph_id=cycle_graph_id,
        cycle=cycle,
        applicability_node_id=applicability_id,
        reviewer_node_id=reviewer_id,
        gate_node_id=gate_id,
        human_review_node_id=human_id,
        knowledge_remediation_node_id=knowledge_id,
        fixer_node_id=fixers[0] if len(fixers) == 1 else None,
        codegen_precondition_node_id=precheck_id,
        codegen_node_id=codegen_id,
        plan_node_id=plans[0] if len(plans) == 1 else None,
        preflight_node_id=preflights[0] if len(preflights) == 1 else None,
    )


def _nodes_with_uses(graph: GraphDef, uses: str, *, layer: str | None = None) -> list[str]:
    found: list[str] = []
    for nid, node in graph.nodes.items():
        if node.uses != uses:
            continue
        if layer is not None and node.with_.get("layer") not in (None, layer):
            continue
        found.append(nid)
    return found


def _selection_issues(schema: WorkflowSchemaV2, roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    assurance = schema.graphs[_ASSURANCE_GRAPH]
    node = assurance.nodes[roles.assurance_node_id]
    locator = f"graph:assurance.nodes.{roles.assurance_node_id}.when"
    if not node.when:
        return [
            _issue(
                "selection_predicate_mismatch",
                layer=roles.layer,
                owner="assurance",
                locator=locator,
                detail="missing selection predicate",
            )
        ]
    modes = _LAYER_SELECTION_MODES[roles.layer]
    mode_list = "[" + ",".join(repr(mode) for mode in modes) + "]"
    required = f"'{roles.layer}' in params.test_types and params.run_mode in {mode_list}"
    run_modes = tuple(schema.params["run_mode"].values or ())
    layers = list(LAYER_NAMES)
    domain = layer_selection_domain(layers=layers, run_modes=[str(m) for m in run_modes])
    if not expression_matches_required(
        node.when,
        required,
        domain,
        allowed_params=_SELECTION_PARAMS,
        allowed_builtins=_SELECTION_BUILTINS,
    ):
        return [
            _issue(
                "selection_predicate_mismatch",
                layer=roles.layer,
                owner="assurance",
                locator=locator,
                detail="selection predicate is not truth-equivalent to the release-domain requirement",
            )
        ]
    return []


def _run_mode_topology_issues(roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    codegen = roles.branch.nodes[roles.codegen_node_id]
    locator = f"graph:{roles.branch_graph_id}.nodes.{roles.codegen_node_id}.when"
    if not codegen.when:
        issues.append(
            _issue(
                "run_mode_predicate_mismatch",
                layer=roles.layer,
                owner="codegen",
                locator=locator,
                detail="codegen node missing run-mode predicate",
            )
        )
        return issues

    if roles.layer == "api":
        required = "params.run_mode in ['full','api-only','codegen-only']"
    elif roles.layer == "e2e":
        required = "params.run_mode in ['full','e2e-only','codegen-only']"
    else:
        required = "params.run_mode in ['full','codegen-only']"

    run_modes = (
        "full",
        "api-only",
        "e2e-only",
        "plan-only",
        "codegen-only",
        "review-plan",
        "case-only",
        "review-case",
    )
    domain: tuple[Assignment, ...] = tuple({"run_mode": mode} for mode in run_modes)
    if not expression_matches_required(
        codegen.when,
        required,
        domain,
        allowed_params=_RUN_MODE_PARAMS,
        allowed_builtins=_SELECTION_BUILTINS,
    ):
        issues.append(
            _issue(
                "run_mode_predicate_mismatch",
                layer=roles.layer,
                owner="codegen",
                locator=locator,
                detail="codegen run-mode predicate diverges on the closed release domain",
            )
        )

    # plan-only / review-plan must not reach codegen via ordinary/route edges under those modes.
    # Structural proxy: edge from review-cycle to codegen-precheck must be generation-mode guarded.
    for edge in roles.branch.edges:
        if edge.from_ == roles.cycle_call_node_id and edge.to == roles.codegen_precondition_node_id:
            when = edge.when or ""
            if "plan-only" in when.replace(" ", "") or "review-plan" in when.replace(" ", ""):
                # Only fail when the guard affirmatively enables plan-only/review-plan.
                if _edge_allows_modes(when, ("plan-only", "review-plan")):
                    issues.append(
                        _issue(
                            "run_mode_predicate_mismatch",
                            layer=roles.layer,
                            owner="codegen-precheck",
                            locator=f"graph:{roles.branch_graph_id}.edges.{edge.from_}->{edge.to}",
                            detail="plan-only/review-plan must not reach codegen precheck",
                        )
                    )
    return issues


def _edge_allows_modes(when: str, modes: tuple[str, ...]) -> bool:
    domain = tuple({"run_mode": mode} for mode in modes)
    try:
        return any(
            expressions_truth_equivalent(when, "true", (assignment,), allowed_params=_RUN_MODE_PARAMS)
            for assignment in domain
        )
    except Exception:  # noqa: BLE001
        return True


def _chain_edge_issues(roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    cycle = roles.cycle
    checks_output = f"change:{roles.profile.checks_artifact}"
    reviewer = cycle.nodes[roles.reviewer_node_id]
    if checks_output not in reviewer.outputs:
        issues.append(
            _issue(
                "missing_required_edge",
                layer=roles.layer,
                owner="reviewer",
                locator=f"graph:{roles.cycle_graph_id}.nodes.{roles.reviewer_node_id}.outputs",
                detail=f"reviewer outputs must include {checks_output}",
            )
        )
    if not _has_edge(cycle, roles.reviewer_node_id, roles.gate_node_id):
        issues.append(
            _issue(
                "missing_required_edge",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"graph:{roles.cycle_graph_id}",
                detail="explicit plan gate must follow reviewer",
            )
        )
    if roles.fixer_node_id is not None and not _has_edge(cycle, roles.fixer_node_id, roles.reviewer_node_id):
        issues.append(
            _issue(
                "remediation_return_mismatch",
                layer=roles.layer,
                owner="fixer",
                locator=f"graph:{roles.cycle_graph_id}.nodes.{roles.fixer_node_id}",
                detail="automatic fixer must return to review",
            )
        )
    if roles.layer in _SPECIALTY_LAYERS:
        if roles.preflight_node_id is None:
            issues.append(
                _issue(
                    "missing_unique_node",
                    layer=roles.layer,
                    owner="preflight",
                    locator=f"graph:{roles.branch_graph_id}",
                    detail="specialty layer requires parent applicability preflight",
                )
            )
        else:
            issues.extend(_specialty_preflight_edge_issues(roles))
    return issues


def _specialty_preflight_edge_issues(roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    """Reject unconditional/broadened specialty parent-preflight cycle edges."""
    assert roles.preflight_node_id is not None
    issues: list[AssuranceConformanceIssue] = []
    required = (
        f"params.run_mode == 'codegen-only' and node('{roles.preflight_node_id}').value.applicable == true"
    )
    domain: tuple[Assignment, ...] = tuple(
        {
            "run_mode": mode,
            "_nodes": {
                roles.preflight_node_id: {"value": {"applicable": applicable}},
            },
        }
        for mode in ("full", "codegen-only", "plan-only", "review-plan")
        for applicable in (True, False)
    )
    found_direct = False
    for edge in roles.branch.edges:
        if edge.from_ != roles.preflight_node_id or edge.to != roles.cycle_call_node_id:
            continue
        found_direct = True
        when = edge.when or "true"
        if not expression_matches_required(
            when,
            required,
            domain,
            allowed_params=_RUN_MODE_PARAMS,
            allowed_builtins=frozenset({"node"}),
        ):
            issues.append(
                _issue(
                    "run_mode_predicate_mismatch",
                    layer=roles.layer,
                    owner="preflight",
                    locator=(
                        f"graph:{roles.branch_graph_id}.edges."
                        f"{roles.preflight_node_id}->{roles.cycle_call_node_id}"
                    ),
                    detail="parent preflight cycle edge must stay codegen-only+applicable",
                )
            )
    if not found_direct:
        issues.append(
            _issue(
                "missing_required_edge",
                layer=roles.layer,
                owner="preflight",
                locator=f"graph:{roles.branch_graph_id}",
                detail="parent preflight must provide a guarded direct cycle path",
            )
        )
    return issues


def _route_issues(roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    applicability_route = _route_from(roles.cycle, roles.applicability_node_id)
    if applicability_route is None:
        issues.append(
            _issue(
                "route_case_mismatch",
                layer=roles.layer,
                owner="applicability",
                locator=f"graph:{roles.cycle_graph_id}.routes.{roles.applicability_node_id}",
                detail="applicability node must declare a route",
            )
        )
    else:
        if applicability_route.cases.get("true") != roles.reviewer_node_id:
            issues.append(
                _issue(
                    "route_case_mismatch",
                    layer=roles.layer,
                    owner="applicability",
                    locator=f"graph:{roles.cycle_graph_id}.routes.{roles.applicability_node_id}",
                    detail="applicable path must reach review",
                )
            )
        if applicability_route.cases.get("false") != roles.gate_node_id:
            issues.append(
                _issue(
                    "route_case_mismatch",
                    layer=roles.layer,
                    owner="applicability",
                    locator=f"graph:{roles.cycle_graph_id}.routes.{roles.applicability_node_id}",
                    detail="inapplicable path must reach review-gate",
                )
            )

    gate_route = _route_from(roles.cycle, roles.gate_node_id)
    expected_gate = {
        "pass": "END",
        "skip": "END",
        "needs_fix": roles.fixer_node_id or "fix",
        "knowledge_remediation": roles.knowledge_remediation_node_id,
        "needs_human_review": roles.human_review_node_id,
        "reject": "STOP",
        "stop": "STOP",
    }
    if roles.layer in _SPECIALTY_LAYERS:
        # Specialty layers route needs_fix to human-review (audited plan tree change).
        expected_gate["needs_fix"] = roles.human_review_node_id
    if gate_route is None:
        issues.append(
            _issue(
                "route_case_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"graph:{roles.cycle_graph_id}.routes.{roles.gate_node_id}",
                detail="plan gate must declare a route",
            )
        )
    else:
        for case in _PLAN_GATE_ROUTE_CASES:
            expected = expected_gate.get(case)
            actual = gate_route.cases.get(case)
            if expected is not None and actual != expected:
                issues.append(
                    _issue(
                        "route_case_mismatch",
                        layer=roles.layer,
                        owner="plan-gate",
                        locator=f"graph:{roles.cycle_graph_id}.routes.{roles.gate_node_id}.cases.{case}",
                        detail=f"plan gate case {case!r} must target {expected!r}, got {actual!r}",
                    )
                )

    codegen_route = _route_from(roles.branch, roles.codegen_precondition_node_id)
    expected_codegen = {"pass": roles.codegen_node_id, "skip": "END", "stop": "STOP"}
    if codegen_route is None:
        issues.append(
            _issue(
                "route_case_mismatch",
                layer=roles.layer,
                owner="codegen-precheck",
                locator=f"graph:{roles.branch_graph_id}.routes.{roles.codegen_precondition_node_id}",
                detail="codegen precondition must declare a route",
            )
        )
    else:
        for case in _CODEGEN_ROUTE_CASES:
            expected = expected_codegen[case]
            actual = codegen_route.cases.get(case)
            if actual != expected:
                issues.append(
                    _issue(
                        "route_case_mismatch",
                        layer=roles.layer,
                        owner="codegen-precheck",
                        locator=(
                            f"graph:{roles.branch_graph_id}.routes."
                            f"{roles.codegen_precondition_node_id}.cases.{case}"
                        ),
                        detail=f"codegen precondition case {case!r} must target {expected!r}, got {actual!r}",
                    )
                )
    return issues


def _plan_gate_issues(schema: WorkflowSchemaV2, roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    gate = schema.gates.get(roles.profile.gate_id)
    locator = f"gate:{roles.profile.gate_id}"
    if gate is None:
        return [
            _issue(
                "gate_read_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=locator,
                detail="missing plan gate definition",
            )
        ]

    issues.extend(_gate_read_issues(gate, roles.profile, owner="plan-gate", layer=roles.layer))

    if gate.invalid_json != Verdict.STOP:
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.invalid_json",
                detail="invalid_json must stop (fail-closed)",
            )
        )
    if gate.missing_field_is != Verdict.STOP:
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.missing_field_is",
                detail="missing_field_is must stop (fail-closed)",
            )
        )
    if gate.missing_file_is is not None and gate.missing_file_is != Verdict.STOP:
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.missing_file_is",
                detail="missing_file_is must stop when declared (fail-closed)",
            )
        )
    if gate.default != Verdict.STOP:
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.default",
                detail="default must be fail-closed stop",
            )
        )

    rules = {rule.field: rule.expr for rule in gate.rules}
    required_fields = (
        "stop_when",
        "skip_when",
        "needs_fix_when",
        "needs_human_review_when",
        "reject_when",
        "pass_when",
    )
    for field in required_fields:
        if field not in rules:
            issues.append(
                _issue(
                    "gate_rule_mismatch",
                    layer=roles.layer,
                    owner="plan-gate",
                    locator=f"{locator}.{field}",
                    detail=f"missing required rule {field}",
                )
            )

    review_alias = roles.profile.review_alias
    checks_alias = derive_alias(roles.profile.checks_artifact)
    layer = roles.layer
    state_call = f"plan_assurance_state({checks_alias}, {review_alias}, {_DATA_KNOWLEDGE_ALIAS}, '{layer}')"
    policy_block_reject = (
        f"({state_call} == 'applicable' and ("
        f"(check_failed({checks_alias}, 'l1_path') and policy.plan_checks.l1_path == 'block') or "
        f"(check_failed({checks_alias}, 'shared_factory') and policy.plan_checks.shared_factory == 'block') or "
        f"(check_failed({checks_alias}, 'assert_ideal') and policy.plan_checks.assert_ideal == 'block') or "
        f"(check_failed({checks_alias}, 'capability_keys') and policy.plan_checks.capability_keys == 'block')"
        f"))"
    )

    # Presence alone is insufficient: empty/whitespace expressions must fail atom checks.
    stop_when = rules.get("stop_when", "")
    if "stop_when" in rules and not expression_has_top_level_predicates(
        stop_when,
        op="or",
        required=(f"{state_call} == 'invalid'",),
        exact=True,
    ):
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.stop_when",
                detail="stop_when must be exactly the invalid plan_assurance_state atom",
            )
        )

    skip_when = rules.get("skip_when", "")
    if "skip_when" in rules and not expression_has_top_level_predicates(
        skip_when,
        op="and",
        required=(f"{state_call} == 'not_applicable'",),
        exact=True,
    ):
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.skip_when",
                detail="skip_when must be exactly the not_applicable plan_assurance_state atom",
            )
        )

    reject_when = rules.get("reject_when", "")
    if "reject_when" in rules and not expression_has_top_level_predicates(
        reject_when,
        op="or",
        required=(
            f"{review_alias}.decision == 'reject'",
            f"{review_alias}.codegen_readiness == 'not_ready'",
            policy_block_reject,
        ),
        exact=True,
    ):
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.reject_when",
                detail="reject_when must keep the closed reject/not_ready/policy-block OR set",
            )
        )

    pass_when = rules.get("pass_when", "")
    if "pass_when" in rules and not expression_has_top_level_predicates(
        pass_when,
        op="and",
        required=(
            f"{state_call} == 'applicable'",
            f"{review_alias}.decision == 'pass'",
            f"{review_alias}.codegen_readiness in ['ready','ready_with_warnings']",
            f"capabilities_present({review_alias}, {_DATA_KNOWLEDGE_ALIAS})",
            "policy.coverage_floor.risk_high > 0",
            "policy.coverage_floor.risk_medium > 0",
        ),
    ):
        issues.append(
            _issue(
                "gate_rule_mismatch",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"{locator}.pass_when",
                detail=(
                    "pass_when missing required applicable/decision/codegen_readiness/"
                    "capabilities_present/policy-floor atoms"
                ),
            )
        )

    # Precedence: declaration order among rule fields must keep stop/skip before pass.
    field_order = [rule.field for rule in gate.rules]
    if "pass_when" in field_order and "stop_when" in field_order:
        if field_order.index("pass_when") < field_order.index("stop_when"):
            issues.append(
                _issue(
                    "gate_rule_mismatch",
                    layer=roles.layer,
                    owner="plan-gate",
                    locator=locator,
                    detail="pass_when must not precede stop_when (first-true precedence)",
                )
            )
    if "pass_when" in field_order and "skip_when" in field_order:
        if field_order.index("pass_when") < field_order.index("skip_when"):
            issues.append(
                _issue(
                    "gate_rule_mismatch",
                    layer=roles.layer,
                    owner="plan-gate",
                    locator=locator,
                    detail="pass_when must not precede skip_when (first-true precedence)",
                )
            )
    return issues


def _codegen_precondition_issues(
    schema: WorkflowSchemaV2,
    roles: _LayerRoles,
) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    gate_id = f"{roles.layer}-codegen-precondition-gate"
    gate = schema.gates.get(gate_id)
    locator = f"gate:{gate_id}"
    if gate is None:
        return [
            _issue(
                "codegen_precondition_mismatch",
                layer=roles.layer,
                owner="codegen-precheck",
                locator=locator,
                detail="missing codegen precondition gate",
            )
        ]

    issues.extend(
        _gate_read_issues(
            gate,
            roles.profile,
            owner="codegen-precheck",
            layer=roles.layer,
            code="codegen_precondition_mismatch",
        )
    )

    rules = {rule.field: rule.expr for rule in gate.rules}
    for field in ("skip_when", "stop_when", "pass_when"):
        if field not in rules:
            issues.append(
                _issue(
                    "codegen_precondition_mismatch",
                    layer=roles.layer,
                    owner="codegen-precheck",
                    locator=f"{locator}.{field}",
                    detail=f"missing {field}",
                )
            )

    review_alias = roles.profile.review_alias
    checks_alias = derive_alias(roles.profile.checks_artifact)
    state_call = (
        f"plan_assurance_state({checks_alias}, {review_alias}, {_DATA_KNOWLEDGE_ALIAS}, '{roles.layer}')"
    )
    cycle_status_ok = f"node('{roles.cycle_call_node_id}').status == 'succeeded'"
    cycle_status_bad = f"node('{roles.cycle_call_node_id}').status != 'succeeded'"

    skip_when = rules.get("skip_when", "")
    if "skip_when" in rules and not expression_has_top_level_predicates(
        skip_when,
        op="and",
        required=(f"{state_call} == 'not_applicable'",),
        exact=True,
    ):
        issues.append(
            _issue(
                "codegen_precondition_mismatch",
                layer=roles.layer,
                owner="codegen-precheck",
                locator=f"{locator}.skip_when",
                detail="skip_when must be exactly the not_applicable plan_assurance_state atom",
            )
        )

    stop_when = rules.get("stop_when", "")
    if "stop_when" in rules and not expression_has_top_level_predicates(
        stop_when,
        op="or",
        required=(
            cycle_status_bad,
            f"{state_call} == 'invalid'",
            f"not file_exists('{_DATA_KNOWLEDGE_PATH}')",
        ),
        exact=True,
    ):
        issues.append(
            _issue(
                "codegen_precondition_mismatch",
                layer=roles.layer,
                owner="codegen-precheck",
                locator=f"{locator}.stop_when",
                detail="stop_when must keep the closed child-status/invalid/L1-missing OR set",
            )
        )

    pass_when = rules.get("pass_when", "")
    if "pass_when" in rules and not expression_has_top_level_predicates(
        pass_when,
        op="and",
        required=(
            cycle_status_ok,
            f"{state_call} == 'applicable'",
            f"gate('{roles.profile.gate_id}').verdict == 'pass'",
            f"capabilities_present({review_alias}, {_DATA_KNOWLEDGE_ALIAS})",
            f"file_exists('{_DATA_KNOWLEDGE_PATH}')",
        ),
    ):
        issues.append(
            _issue(
                "codegen_precondition_mismatch",
                layer=roles.layer,
                owner="codegen-precheck",
                locator=f"{locator}.pass_when",
                detail=(
                    "pass_when missing required child-success/applicable/gate-pass/"
                    "capabilities_present/file_exists atoms"
                ),
            )
        )
    return issues


def _gate_read_issues(
    gate: GateDef,
    profile: LayerAssuranceProfile,
    *,
    owner: str,
    layer: LayerName,
    code: AssuranceConformanceCode = "gate_read_mismatch",
) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    locator = f"gate:{gate.id}"
    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    expected = (
        (profile.review_artifact, profile.review_alias),
        (profile.checks_artifact, derive_alias(profile.checks_artifact)),
        (_DATA_KNOWLEDGE_PATH, _DATA_KNOWLEDGE_ALIAS),
    )
    for path, alias in expected:
        if path not in reads_by_path:
            issues.append(
                _issue(
                    code,
                    layer=layer,
                    owner=owner,
                    locator=f"{locator}.reads",
                    detail=f"must read {path!r}",
                )
            )
        elif reads_by_path[path] != alias:
            issues.append(
                _issue(
                    code,
                    layer=layer,
                    owner=owner,
                    locator=f"{locator}.reads",
                    detail=f"alias for {path!r} must be {alias!r}, got {reads_by_path[path]!r}",
                )
            )
    if len({entry.alias for entry in gate.reads}) != len(gate.reads):
        issues.append(
            _issue(
                code,
                layer=layer,
                owner=owner,
                locator=f"{locator}.reads",
                detail="gate read aliases must be unique",
            )
        )
    return issues


def _interrupt_issues(roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    for node_id, owner in (
        (roles.human_review_node_id, "human-review"),
        (roles.knowledge_remediation_node_id, "knowledge-remediation"),
    ):
        node = roles.cycle.nodes[node_id]
        locator = f"graph:{roles.cycle_graph_id}.nodes.{node_id}.interrupt"
        interrupt = node.interrupt
        if interrupt is None:
            issues.append(
                _issue(
                    "interrupt_binding_mismatch",
                    layer=roles.layer,
                    owner=owner,
                    locator=locator,
                    detail="missing interrupt definition",
                )
            )
            continue
        if interrupt.bind != "audited_gate_read":
            issues.append(
                _issue(
                    "interrupt_binding_mismatch",
                    layer=roles.layer,
                    owner=owner,
                    locator=f"{locator}.bind",
                    detail=f"bind must be audited_gate_read, got {interrupt.bind!r}",
                )
            )
        if interrupt.checkpoint != roles.profile.gate_id:
            issues.append(
                _issue(
                    "interrupt_checkpoint_mismatch",
                    layer=roles.layer,
                    owner=owner,
                    locator=f"{locator}.checkpoint",
                    detail=f"checkpoint must be {roles.profile.gate_id!r}",
                )
            )
        expected_actions = ["fix_and_proceed", "accept_risk", "stop"]
        if list(interrupt.actions) != expected_actions:
            issues.append(
                _issue(
                    "interrupt_action_mismatch",
                    layer=roles.layer,
                    owner=owner,
                    locator=f"{locator}.actions",
                    detail=f"actions must be {expected_actions}, got {list(interrupt.actions)}",
                )
            )
        if roles.layer in _SPECIALTY_LAYERS and owner == "human-review":
            revision = interrupt.manual_revision
            if revision is None:
                issues.append(
                    _issue(
                        "manual_revision_allowlist_mismatch",
                        layer=roles.layer,
                        owner=owner,
                        locator=f"{locator}.manual_revision",
                        detail="specialty human-review requires exact manual_revision allowlist",
                    )
                )
            else:
                expected_paths = {
                    f"change:plans/{roles.layer}-plan.md",
                    f"change:plans/{roles.layer}-codegen-plan.md",
                    f"change:plans/{roles.layer}-codegen-mapping.json",
                }
                if set(revision.paths) != expected_paths or revision.action != "fix_and_proceed":
                    issues.append(
                        _issue(
                            "manual_revision_allowlist_mismatch",
                            layer=roles.layer,
                            owner=owner,
                            locator=f"{locator}.manual_revision",
                            detail="manual_revision allowlist drifted from the exact release set",
                        )
                    )
        elif interrupt.manual_revision is not None and roles.layer in _API_E2E_LAYERS:
            # API/E2E human-review has no manual_revision in the approved shape.
            issues.append(
                _issue(
                    "manual_revision_allowlist_mismatch",
                    layer=roles.layer,
                    owner=owner,
                    locator=f"{locator}.manual_revision",
                    detail="api/e2e human-review must not broaden manual_revision allowlist",
                )
            )
    return issues


def _remediation_issues(roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    knowledge_route = _route_from(roles.cycle, roles.knowledge_remediation_node_id)
    if knowledge_route is None or knowledge_route.cases.get("fix_and_proceed") != roles.reviewer_node_id:
        issues.append(
            _issue(
                "remediation_return_mismatch",
                layer=roles.layer,
                owner="knowledge-remediation",
                locator=f"graph:{roles.cycle_graph_id}.routes.{roles.knowledge_remediation_node_id}",
                detail="knowledge remediation must return to review",
            )
        )
    if roles.fixer_node_id is not None:
        # API/E2E automatic fixer edge return checked in _chain_edge_issues.
        human_route = _route_from(roles.cycle, roles.human_review_node_id)
        if roles.layer in _API_E2E_LAYERS and human_route is not None:
            if human_route.cases.get("fix_and_proceed") != roles.fixer_node_id:
                issues.append(
                    _issue(
                        "remediation_return_mismatch",
                        layer=roles.layer,
                        owner="human-review",
                        locator=f"graph:{roles.cycle_graph_id}.routes.{roles.human_review_node_id}",
                        detail="human-review fix_and_proceed must enter the automatic fixer",
                    )
                )
        if roles.layer in _SPECIALTY_LAYERS and human_route is not None:
            if human_route.cases.get("fix_and_proceed") != roles.reviewer_node_id:
                issues.append(
                    _issue(
                        "remediation_return_mismatch",
                        layer=roles.layer,
                        owner="human-review",
                        locator=f"graph:{roles.cycle_graph_id}.routes.{roles.human_review_node_id}",
                        detail="specialty fix_and_proceed must return to review",
                    )
                )
    return issues


def _bypass_issues(roles: _LayerRoles) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    branch_cfg = build_cfg(roles.branch)

    # No path into codegen that avoids the precondition owner.
    if paths_exist_avoiding(
        branch_cfg,
        "START",
        roles.codegen_node_id,
        avoid={roles.codegen_precondition_node_id},
    ):
        issues.append(
            _issue(
                "forbidden_bypass_edge",
                layer=roles.layer,
                owner="codegen",
                locator=f"graph:{roles.branch_graph_id}",
                detail="codegen is reachable while bypassing the precondition gate",
            )
        )

    # Precondition must dominate codegen.
    if not dominates(branch_cfg, roles.codegen_precondition_node_id, roles.codegen_node_id):
        issues.append(
            _issue(
                "forbidden_bypass_edge",
                layer=roles.layer,
                owner="codegen-precheck",
                locator=f"graph:{roles.branch_graph_id}",
                detail="codegen precondition must dominate codegen",
            )
        )

    # Direct review-cycle / plan / skip into codegen.
    for src in (roles.cycle_call_node_id, roles.plan_node_id):
        if src is None:
            continue
        if any(edge.src == src and edge.dst == roles.codegen_node_id for edge in branch_cfg.edges):
            issues.append(
                _issue(
                    "forbidden_bypass_edge",
                    layer=roles.layer,
                    owner="codegen",
                    locator=f"graph:{roles.branch_graph_id}.edges.{src}->{roles.codegen_node_id}",
                    detail=f"direct edge from {src!r} to codegen is forbidden",
                )
            )

    incoming_to_gate = {edge.from_ for edge in roles.cycle.edges if edge.to == roles.gate_node_id}
    if incoming_to_gate - {roles.reviewer_node_id}:
        issues.append(
            _issue(
                "forbidden_bypass_edge",
                layer=roles.layer,
                owner="plan-gate",
                locator=f"graph:{roles.cycle_graph_id}",
                detail="plan gate is reachable while bypassing reviewer on the applicable path",
            )
        )
    return issues


def _generation_join_issues(schema: WorkflowSchemaV2) -> list[AssuranceConformanceIssue]:
    issues: list[AssuranceConformanceIssue] = []
    assurance = schema.graphs.get(_ASSURANCE_GRAPH)
    if assurance is None:
        return issues
    join_nodes = [
        (nid, node)
        for nid, node in assurance.nodes.items()
        if node.uses == "builtin:join" or nid == "generation-join"
    ]
    if len(join_nodes) != 1:
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator="graph:assurance",
                detail=f"expected exactly one generation-join, found {len(join_nodes)}",
            )
        )
        return issues
    join_id, join_node = join_nodes[0]
    locator = f"graph:assurance.nodes.{join_id}"
    join = join_node.join
    if join is None:
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=locator,
                detail="generation-join missing join definition",
            )
        )
        return issues
    expected_sources = list(LAYER_NAMES)
    if list(join.sources) != expected_sources:
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=f"{locator}.join.sources",
                detail=f"sources must be {expected_sources}, got {list(join.sources)}",
            )
        )
    if join.mode != "all_active":
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=f"{locator}.join.mode",
                detail=f"mode must be all_active, got {join.mode!r}",
            )
        )
    if join.cancel_remaining:
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=f"{locator}.join.cancel_remaining",
                detail="cancel_remaining must be false",
            )
        )

    # Guarded successors and no direct branch-to-execution bypass.
    for layer in LAYER_NAMES:
        if any(edge.from_ == layer and edge.to == "execution" for edge in assurance.edges):
            issues.append(
                _issue(
                    "forbidden_bypass_edge",
                    layer=layer,
                    owner="generation-join",
                    locator=f"graph:assurance.edges.{layer}->execution",
                    detail="direct active-branch-to-execution edge bypasses generation-join",
                )
            )
        if not any(edge.from_ == layer and edge.to == join_id for edge in assurance.edges):
            issues.append(
                _issue(
                    "missing_required_edge",
                    layer=layer,
                    owner="generation-join",
                    locator=f"graph:assurance.edges.{layer}->{join_id}",
                    detail="each layer branch must edge into generation-join",
                )
            )

    join_to_execution = [edge for edge in assurance.edges if edge.from_ == join_id and edge.to == "execution"]
    join_to_end = [edge for edge in assurance.edges if edge.from_ == join_id and edge.to == "END"]
    run_tests_domain: tuple[Assignment, ...] = ({"run_tests": True}, {"run_tests": False})
    if len(join_to_execution) != 1:
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=f"{locator}->execution",
                detail="generation-join must have one guarded edge to execution",
            )
        )
    elif not expression_matches_required(
        join_to_execution[0].when or "",
        "params.run_tests == true",
        run_tests_domain,
        allowed_params=frozenset({"run_tests"}),
        allowed_builtins=_SELECTION_BUILTINS,
    ):
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=f"{locator}->execution",
                detail="generation-join -> execution guard must be truth-equivalent to params.run_tests == true",
            )
        )
    if len(join_to_end) != 1:
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=f"{locator}->END",
                detail="generation-join must have one guarded edge to END",
            )
        )
    elif not expression_matches_required(
        join_to_end[0].when or "",
        "params.run_tests == false",
        run_tests_domain,
        allowed_params=frozenset({"run_tests"}),
        allowed_builtins=_SELECTION_BUILTINS,
    ):
        issues.append(
            _issue(
                "generation_join_mismatch",
                layer=None,
                owner="generation-join",
                locator=f"{locator}->END",
                detail="generation-join -> END guard must be truth-equivalent to params.run_tests == false",
            )
        )
    return issues


def _has_edge(graph: GraphDef, src: str, dst: str) -> bool:
    return any(edge.from_ == src and edge.to == dst for edge in graph.edges)


def _route_from(graph: GraphDef, node_id: str):
    for route in graph.routes:
        if route.from_ == node_id:
            return route
    return None


def with_approved_api_e2e_capability_atoms(schema: WorkflowSchemaV2) -> WorkflowSchemaV2:
    """Return a model_copy of ``schema`` with approved dark-ship plan/codegen atoms.

    Used by dark-ship tests; does not edit packaged YAML. Adds API/E2E codegen
    ``capabilities_present`` atoms and plan-gate policy-floor atoms when absent.
    """
    gates = dict(schema.gates)
    for layer in ("api", "e2e"):
        profile = get_layer_assurance_profile(layer)
        gate_id = f"{layer}-codegen-precondition-gate"
        gate = gates.get(gate_id)
        if gate is None:
            continue
        rules = list(gate.rules)
        updated_rules = []
        atom = f"capabilities_present({profile.review_alias}, data_knowledge)"
        for rule in rules:
            if rule.field == "pass_when" and "capabilities_present" not in rule.expr:
                expr = rule.expr.rstrip()
                updated_rules.append(rule.model_copy(update={"expr": f"{expr} and {atom}"}))
            else:
                updated_rules.append(rule)
        gates[gate_id] = gate.model_copy(update={"rules": updated_rules})

    policy_floor_atoms = (
        "policy.coverage_floor.risk_high > 0",
        "policy.coverage_floor.risk_medium > 0",
    )
    for profile in iter_layer_assurance_profiles():
        gate = gates.get(profile.gate_id)
        if gate is None:
            continue
        updated_rules = []
        for rule in gate.rules:
            if rule.field != "pass_when":
                updated_rules.append(rule)
                continue
            expr = " ".join(rule.expr.split())
            for atom in policy_floor_atoms:
                if not expression_has_top_level_predicates(expr, op="and", required=(atom,)):
                    expr = f"{expr} and {atom}"
            updated_rules.append(rule.model_copy(update={"expr": expr}))
        gates[profile.gate_id] = gate.model_copy(update={"rules": updated_rules})
    return schema.model_copy(update={"gates": gates})
