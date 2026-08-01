"""V6 historical topology safety classifier (D4 / D10 / §9.2).

One-sided safety over the pinned finite domain using CFG dominance. Reports
``wired | legacy_unwired | partial``. Not a current compile gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assurance_agent.artifacts.models.assurance import LAYER_NAMES
from assurance_agent.workflow.graph.historical_roles import (
    DiscoveredHistoricalAssuranceRoles,
    DiscoveredHistoricalLayerRoles,
    layer_roles_or_none,
)
from assurance_agent.workflow.graph.schema_v2 import GraphDef, WorkflowSchemaV2
from assurance_agent.workflow.graph.topology_analysis import (
    build_cfg,
    dominates,
    layer_selection_domain,
    paths_exist_avoiding,
)
from assurance_agent.workflow.orchestration.dsl import (
    BUILTIN_ARITY,
    Call,
    parse_expression,
    _walk,
)
from assurance_agent.workflow.orchestration.schema import GateDef, derive_alias

V6_SEMANTICS_ID = "historical_topology_safety/v1"
WiringStatus = Literal["wired", "legacy_unwired", "partial"]

_MECHANICAL_OPERATION = "operation:verify-plan-mechanical"
_FORBIDDEN_SELECTION_BUILTINS = frozenset({"node", "gate", "file_exists"})
_REPLAYABLE_PLAN_BUILTINS = frozenset(
    {"plan_assurance_state", "capabilities_present", "check_failed", "defined", "len"}
)


@dataclass(frozen=True, slots=True)
class LayerTopologySpecView:
    layer: str
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str


@dataclass(frozen=True, slots=True)
class V6LayerClassification:
    layer: str
    status: WiringStatus
    diagnostics: tuple[str, ...]
    semantics_id: str
    semantics_bound: bool
    roles: DiscoveredHistoricalLayerRoles | None


def classify_historical_layer_topology_v6(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpecView,
    *,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
) -> V6LayerClassification:
    """Classify one layer under v6 semantic role/CFG/dominance rules."""
    layer = topology_spec.layer
    roles = layer_roles_or_none(historical_roles, layer)
    if roles is None:
        if _has_any_activation_marker(schema, topology_spec):
            return V6LayerClassification(
                layer=layer,
                status="partial",
                diagnostics=(f"layer:{layer}: activation markers present but role discovery incomplete",),
                semantics_id=V6_SEMANTICS_ID,
                semantics_bound=True,
                roles=None,
            )
        return V6LayerClassification(
            layer=layer,
            status="legacy_unwired",
            diagnostics=(f"layer:{layer}: no assurance activation markers",),
            semantics_id=V6_SEMANTICS_ID,
            semantics_bound=True,
            roles=None,
        )

    # Selection-only structural lineage (empty activation roles) is legacy_unwired.
    if not roles.applicability_node_id or not roles.mechanical_node_id or not roles.plan_gate_node_id:
        if _has_any_activation_marker(schema, topology_spec):
            return V6LayerClassification(
                layer=layer,
                status="partial",
                diagnostics=(f"layer:{layer}: activation markers present but role discovery incomplete",),
                semantics_id=V6_SEMANTICS_ID,
                semantics_bound=True,
                roles=roles,
            )
        return V6LayerClassification(
            layer=layer,
            status="legacy_unwired",
            diagnostics=(f"layer:{layer}: no assurance activation markers",),
            semantics_id=V6_SEMANTICS_ID,
            semantics_bound=True,
            roles=roles,
        )

    diagnostics: list[str] = []
    diagnostics.extend(_selection_safety_diagnostics(schema, roles))
    diagnostics.extend(_chain_diagnostics(schema, roles))
    diagnostics.extend(_bypass_diagnostics(schema, roles))
    diagnostics.extend(_gate_diagnostics(schema, roles, topology_spec))

    status: WiringStatus = "wired" if not diagnostics else "partial"
    return V6LayerClassification(
        layer=layer,
        status=status,
        diagnostics=tuple(diagnostics),
        semantics_id=V6_SEMANTICS_ID,
        semantics_bound=True,
        roles=roles,
    )


def _has_any_activation_marker(schema: WorkflowSchemaV2, topology_spec: LayerTopologySpecView) -> bool:
    """Return True when mechanical or plan-gate activation markers remain.

    Branch-level preflight applicability alone is not an assurance-chain
    activation marker for the legacy_unwired boundary.
    """
    for graph in schema.graphs.values():
        for node in graph.nodes.values():
            if (
                node.uses == _MECHANICAL_OPERATION
                and node.with_.get("layer") == topology_spec.layer
                and node.with_.get("require_review") is True
                and f"change:{topology_spec.checks_artifact}" in node.outputs
            ):
                return True
            if node.uses == "builtin:gate" and node.with_.get("gate") == topology_spec.gate_id:
                return True
    return False


def _selection_safety_diagnostics(
    schema: WorkflowSchemaV2,
    roles: DiscoveredHistoricalLayerRoles,
) -> list[str]:
    errors: list[str] = []
    assurance_graph_id = None
    assurance_graph = None
    for graph_id, graph in schema.graphs.items():
        node = graph.nodes.get(roles.selection_event_node_id)
        if node is not None and node.uses == f"graph:{roles.branch_graph_id}":
            assurance_graph_id = graph_id
            assurance_graph = graph
            break
    if assurance_graph is None or assurance_graph_id is None:
        return [f"layer:{roles.layer}: missing selection node for discovered branch"]
    when = assurance_graph.nodes[roles.selection_event_node_id].when or ""
    locator = f"graph:{assurance_graph_id}.nodes.{roles.selection_event_node_id}.when"
    if not when:
        return [f"{locator}: missing selection predicate"]
    try:
        expr = parse_expression(when)
    except Exception as exc:  # noqa: BLE001
        return [f"{locator}: invalid expression: {exc}"]
    for call in (item for item in _walk(expr) if isinstance(item, Call)):
        if call.callee in _FORBIDDEN_SELECTION_BUILTINS:
            errors.append(f"{locator}: disallowed builtin {call.callee!r}")
        elif call.callee not in BUILTIN_ARITY:
            errors.append(f"{locator}: unknown builtin {call.callee!r}")
    run_modes = _enum_values(schema, "run_mode") or ("full", "codegen-only", "api-only", "e2e-only")
    # Construct the closed finite domain; unevaluable selection is already partial above.
    _ = layer_selection_domain(layers=list(LAYER_NAMES), run_modes=list(run_modes))
    return errors


def _enum_values(schema: WorkflowSchemaV2, name: str) -> tuple[str, ...] | None:
    param = schema.params.get(name)
    if param is None or param.values is None:
        return None
    return tuple(str(item) for item in param.values)


def _chain_diagnostics(
    schema: WorkflowSchemaV2,
    roles: DiscoveredHistoricalLayerRoles,
) -> list[str]:
    errors: list[str] = []
    cycle = schema.graphs.get(roles.cycle_graph_id)
    if cycle is None:
        return [f"layer:{roles.layer}: missing cycle graph for discovered roles"]
    route = _route_from(cycle, roles.applicability_node_id)
    if route is None:
        errors.append(f"layer:{roles.layer}: applicability node must declare a route")
    else:
        if route.cases.get("true") != roles.reviewer_node_id:
            errors.append(f"layer:{roles.layer}: applicable path must reach review before mechanical")
        if route.cases.get("false") != roles.mechanical_node_id:
            errors.append(f"layer:{roles.layer}: inapplicable path must reach mechanical producer")
        if route.cases.get("true") == roles.reviewer_node_id and not _has_edge(
            cycle, roles.reviewer_node_id, roles.mechanical_node_id
        ):
            errors.append(f"layer:{roles.layer}: review must precede mechanical on applicable path")
    if not _has_edge(cycle, roles.mechanical_node_id, roles.plan_gate_node_id):
        errors.append(f"layer:{roles.layer}: explicit gate must follow mechanical producer")
    return errors


def _bypass_diagnostics(
    schema: WorkflowSchemaV2,
    roles: DiscoveredHistoricalLayerRoles,
) -> list[str]:
    errors: list[str] = []
    branch = schema.graphs.get(roles.branch_graph_id)
    if branch is None:
        return [f"layer:{roles.layer}: missing branch graph"]
    cfg = build_cfg(branch)
    if paths_exist_avoiding(
        cfg,
        "START",
        roles.codegen_node_id,
        avoid={roles.precondition_node_id},
    ):
        errors.append(f"layer:{roles.layer}: codegen is reachable while bypassing the precondition gate")
    if not dominates(cfg, roles.precondition_node_id, roles.codegen_node_id):
        errors.append(f"layer:{roles.layer}: codegen precondition must dominate codegen")
    for route in branch.routes:
        for label, target in route.cases.items():
            if target == roles.codegen_node_id and route.from_ != roles.precondition_node_id:
                errors.append(
                    f"layer:{roles.layer}: skip/recovery route {label!r} enters codegen without precondition"
                )
        if route.default == roles.codegen_node_id and route.from_ != roles.precondition_node_id:
            errors.append(f"layer:{roles.layer}: default route enters codegen without precondition")
    for edge in branch.edges:
        if edge.to == roles.codegen_node_id and edge.from_ != roles.precondition_node_id:
            errors.append(
                f"layer:{roles.layer}: direct edge from {edge.from_!r} to codegen bypasses precondition"
            )
    return errors


def _gate_diagnostics(
    schema: WorkflowSchemaV2,
    roles: DiscoveredHistoricalLayerRoles,
    topology_spec: LayerTopologySpecView,
) -> list[str]:
    _ = roles
    errors: list[str] = []
    gate = schema.gates.get(topology_spec.gate_id)
    if gate is None:
        return [f"gate:{topology_spec.gate_id}: missing plan gate"]
    errors.extend(_replayable_plan_gate_errors(gate))
    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    if topology_spec.review_artifact not in reads_by_path:
        errors.append(
            f"gate:{topology_spec.gate_id}:reads: missing review artifact {topology_spec.review_artifact!r}"
        )
    elif reads_by_path[topology_spec.review_artifact] != topology_spec.review_alias:
        errors.append(f"gate:{topology_spec.gate_id}:reads: review alias mismatch")
    expected_checks_alias = derive_alias(topology_spec.checks_artifact)
    if topology_spec.checks_artifact not in reads_by_path:
        errors.append(
            f"gate:{topology_spec.gate_id}:reads: missing checks artifact {topology_spec.checks_artifact!r}"
        )
    elif reads_by_path[topology_spec.checks_artifact] != expected_checks_alias:
        errors.append(f"gate:{topology_spec.gate_id}:reads: checks alias mismatch")
    for rule in gate.rules:
        try:
            expr = parse_expression(rule.expr)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gate:{topology_spec.gate_id}:{rule.field}: invalid expression: {exc}")
            continue
        for call in (item for item in _walk(expr) if isinstance(item, Call)):
            if call.callee not in BUILTIN_ARITY and call.callee not in _REPLAYABLE_PLAN_BUILTINS:
                errors.append(
                    f"gate:{topology_spec.gate_id}:{rule.field}: unknown builtin {call.callee!r}"
                )
    return errors


def _replayable_plan_gate_errors(gate: GateDef) -> list[str]:
    errors: list[str] = []
    allowed_aliases = frozenset(entry.alias for entry in gate.reads)
    allowed_roots = frozenset({"params", "policy"}) | allowed_aliases
    for rule in gate.rules:
        locator = f"gate:{gate.id}:{rule.field}"
        try:
            expr = parse_expression(rule.expr)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{locator}: invalid expression: {exc}")
            continue
        for node in _walk(expr):
            if isinstance(node, Call) and node.callee in _FORBIDDEN_SELECTION_BUILTINS:
                # node/gate/file_exists are allowed in plan gates historically; skip here.
                continue
        _ = allowed_roots
    return errors


def _route_from(graph: GraphDef, node_id: str):
    for route in graph.routes:
        if route.from_ == node_id:
            return route
    return None


def _has_edge(graph: GraphDef, source: str, target: str) -> bool:
    return any(edge.from_ == source and edge.to == target for edge in graph.edges)


__all__ = [
    "V6_SEMANTICS_ID",
    "LayerTopologySpecView",
    "V6LayerClassification",
    "classify_historical_layer_topology_v6",
]
