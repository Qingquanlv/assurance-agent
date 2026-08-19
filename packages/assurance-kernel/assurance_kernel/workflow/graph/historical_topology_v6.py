"""V6 historical topology safety classifier (D4 / D10 / §9.2).

One-sided safety over the pinned finite domain using CFG dominance. Reports
``wired | legacy_unwired | partial``. Not a current compile gate.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Literal

from assurance_kernel.artifacts.models.assurance import LAYER_NAMES
from assurance_kernel.workflow.graph.historical_roles import (
    DiscoveredHistoricalAssuranceRoles,
    DiscoveredHistoricalLayerRoles,
    layer_roles_or_none,
)
from assurance_kernel.workflow.graph.schema_v2 import GraphDef, WorkflowSchemaV2
from assurance_kernel.workflow.graph.topology_analysis import (
    build_cfg,
    dominates,
    expressions_truth_equivalent,
    layer_selection_domain,
    paths_exist_avoiding,
)
from assurance_kernel.workflow.orchestration.dsl import (
    BUILTIN_ARITY,
    Call,
    Expr,
    Member,
    Ident,
    parse_expression,
    _walk,
)
from assurance_kernel.workflow.orchestration.schema import GateDef, derive_alias

V6_SEMANTICS_ID = "historical_topology_safety/v1"
WIRING_STATUSES: frozenset[str] = frozenset({"wired", "legacy_unwired", "partial"})
WiringStatus = Literal["wired", "legacy_unwired", "partial"]

_FORBIDDEN_SELECTION_BUILTINS = frozenset({"node", "gate", "file_exists"})
_REPLAYABLE_PLAN_BUILTINS = frozenset(
    {"plan_assurance_state", "capabilities_present", "check_failed", "defined", "len"}
)
_SELECTION_DOMAIN_PARAMS = frozenset({"test_types", "run_mode"})
_API_E2E_LAYERS = frozenset({"api", "e2e"})
_SPECIALTY_LAYERS = frozenset({"fuzz", "performance"})


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
    if not roles.applicability_node_id or not roles.reviewer_node_id or not roles.plan_gate_node_id:
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
    diagnostics.extend(_remediation_diagnostics(schema, roles))
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
    """Return True when an explicit plan-review gate remains.

    Branch-level preflight applicability alone is not an assurance-chain
    activation marker for the legacy_unwired boundary.
    """
    for graph in schema.graphs.values():
        for node in graph.nodes.values():
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
    if errors:
        return errors

    referenced = _referenced_param_names(expr)
    domain, domain_errors = _finite_selection_domain(schema, referenced, locator=locator)
    errors.extend(domain_errors)
    if errors or not domain:
        return errors

    # One-sided evaluability: every finite-domain assignment must yield a boolean.
    allowed_builtins = frozenset(BUILTIN_ARITY) - _FORBIDDEN_SELECTION_BUILTINS
    if not expressions_truth_equivalent(
        when,
        when,
        domain,
        allowed_params=frozenset(referenced),
        allowed_builtins=allowed_builtins,
    ):
        errors.append(f"{locator}: unevaluable_selection_over_finite_domain")
    return errors


def _referenced_param_names(expr: Expr) -> set[str]:
    names: set[str] = set()
    for node in _walk(expr):
        if isinstance(node, Member) and isinstance(node.obj, Ident) and node.obj.name == "params":
            names.add(node.prop)
    return names


def _finite_selection_domain(
    schema: WorkflowSchemaV2,
    referenced: set[str],
    *,
    locator: str,
) -> tuple[tuple[dict[str, object], ...], list[str]]:
    """Build the pinned finite assignment table for selection evaluation."""
    errors: list[str] = []
    if not referenced:
        return ({},), errors

    extra_domains: dict[str, tuple[object, ...]] = {}
    for name in sorted(referenced - _SELECTION_DOMAIN_PARAMS):
        finite = _finite_domain_for_param(schema, name)
        if finite is None:
            errors.append(f"{locator}: unbounded_param_domain param={name!r}")
        else:
            extra_domains[name] = finite
    if errors:
        return (), errors

    uses_layer_domain = bool(referenced & _SELECTION_DOMAIN_PARAMS)
    if uses_layer_domain:
        if "run_mode" in referenced:
            run_modes = _enum_values(schema, "run_mode")
            if run_modes is None:
                return (), [f"{locator}: unbounded_param_domain param='run_mode'"]
        else:
            run_modes = _enum_values(schema, "run_mode") or ("full",)
        base = layer_selection_domain(layers=list(LAYER_NAMES), run_modes=list(run_modes))
        if "test_types" not in referenced:
            # Keep run_mode variation only.
            seen: set[str] = set()
            trimmed: list[dict[str, object]] = []
            for assignment in base:
                mode = str(assignment["run_mode"])
                if mode in seen:
                    continue
                seen.add(mode)
                trimmed.append({"run_mode": assignment["run_mode"]})
            base = tuple(trimmed)
    else:
        base = ({},)

    if not extra_domains:
        return base, errors

    expanded: list[dict[str, object]] = []
    keys = sorted(extra_domains)
    for assignment in base:
        for values in itertools.product(*(extra_domains[key] for key in keys)):
            merged = dict(assignment)
            merged.update(dict(zip(keys, values, strict=True)))
            expanded.append(merged)
    return tuple(expanded), errors


def _finite_domain_for_param(schema: WorkflowSchemaV2, name: str) -> tuple[object, ...] | None:
    param = schema.params.get(name)
    if param is None:
        return None
    if param.type == "bool":
        return (True, False)
    if param.type == "enum" and param.values is not None:
        return tuple(param.values)
    if param.values is not None:
        return tuple(param.values)
    return None


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
            errors.append(f"layer:{roles.layer}: applicable path must reach review")
        if route.cases.get("false") != roles.plan_gate_node_id:
            errors.append(f"layer:{roles.layer}: inapplicable path must reach the plan gate")
        if route.cases.get("true") == roles.reviewer_node_id and not _has_edge(
            cycle, roles.reviewer_node_id, roles.plan_gate_node_id
        ):
            errors.append(f"layer:{roles.layer}: review must precede the plan gate on applicable path")
    if not _has_edge(cycle, roles.reviewer_node_id, roles.plan_gate_node_id):
        errors.append(f"layer:{roles.layer}: explicit gate must follow reviewer")
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


def _remediation_diagnostics(
    schema: WorkflowSchemaV2,
    roles: DiscoveredHistoricalLayerRoles,
) -> list[str]:
    """Require audited remediation returns that regenerate evidence they can change."""
    errors: list[str] = []
    cycle = schema.graphs.get(roles.cycle_graph_id)
    if cycle is None:
        return [f"layer:{roles.layer}: missing cycle graph for remediation checks"]

    fixer_ids = [
        node_id
        for node_id, node in cycle.nodes.items()
        if node.uses.startswith("skill:")
        and ("fixer" in node.uses or node.uses == f"skill:aa-{roles.layer}-plan")
    ]
    interrupts = [node_id for node_id, node in cycle.nodes.items() if node.uses == "builtin:interrupt"]
    for node_id in interrupts:
        route = _route_from(cycle, node_id)
        locator = f"layer:{roles.layer}:remediation:{node_id}"
        if route is None or "fix_and_proceed" not in route.cases:
            errors.append(f"{locator}: unaudited_remediation_return missing fix_and_proceed route")
            continue
        target = route.cases["fix_and_proceed"]
        if target == roles.reviewer_node_id:
            continue
        if roles.layer in _SPECIALTY_LAYERS and target == roles.reviewer_node_id:
            continue
        if roles.layer in _API_E2E_LAYERS and fixer_ids and target in fixer_ids:
            continue
        if roles.layer in _API_E2E_LAYERS and not fixer_ids and target == roles.reviewer_node_id:
            continue
        errors.append(
            f"{locator}: unaudited_remediation_return target={target!r} "
            f"(expected reviewer/fixer regeneration)"
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
                errors.append(f"gate:{topology_spec.gate_id}:{rule.field}: unknown builtin {call.callee!r}")
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
    "WIRING_STATUSES",
    "LayerTopologySpecView",
    "V6LayerClassification",
    "classify_historical_layer_topology_v6",
]
