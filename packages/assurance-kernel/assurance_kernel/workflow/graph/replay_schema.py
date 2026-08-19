"""Pure replayable assurance schema dependency and topology guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from assurance_kernel.verification.profiles import (
    LayerAssuranceProfile,
    get_layer_assurance_profile,
    iter_layer_assurance_profiles,
)
from assurance_kernel.workflow.graph.schema_v2 import GraphDef, WorkflowSchemaV2
from assurance_kernel.workflow.orchestration.dsl import (
    BoolOp,
    Call,
    Compare,
    Expr,
    Ident,
    Literal as DslLiteral,
    Member,
    Not,
    _walk,
    parse_expression,
)
from assurance_kernel.workflow.orchestration.schema import GateDef, derive_alias

if TYPE_CHECKING:
    from assurance_kernel.workflow.graph.historical_roles import DiscoveredHistoricalAssuranceRoles

WIRED_REPLAY_LAYERS = frozenset({"api", "e2e"})
_SPECIALTY_PARENT_PREFLIGHT_LAYERS = frozenset({"fuzz", "performance"})

WiringStatus = Literal["wired", "legacy_unwired", "partial"]

_APPLICABILITY_OPERATION = "operation:derive-plan-layer-applicability"
_DATA_KNOWLEDGE_PATH = "repo:.aa/data-knowledge.yaml"
_DATA_KNOWLEDGE_ALIAS = "data_knowledge"

_ASSURANCE_BRANCH_NODES = ("api", "e2e", "fuzz", "performance")

_PARAMS_ONLY_ROOTS = frozenset({"params"})
_REPLAYABLE_PLAN_ROOTS = frozenset({"params", "policy"})

_PARAMS_ONLY_BUILTINS = frozenset()
_REPLAYABLE_PLAN_BUILTINS = frozenset(
    {"plan_assurance_state", "capabilities_present", "check_failed", "defined", "len"}
)

_FORBIDDEN_REPLAY_BUILTINS = frozenset({"node", "gate", "file_exists"})


@dataclass(frozen=True, slots=True)
class LayerTopologySpec:
    layer: str
    plan_artifacts: tuple[str, ...]
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str


@dataclass(frozen=True, slots=True)
class PinnedLayerTopology:
    layer: str
    status: WiringStatus
    assurance_node_id: str | None
    branch_graph_id: str | None
    cycle_call_node_id: str | None
    cycle_graph_id: str | None
    applicability_node_id: str | None
    reviewer_node_id: str | None
    gate_node_id: str | None
    human_review_node_id: str | None
    knowledge_remediation_node_id: str | None
    codegen_precondition_node_id: str | None
    codegen_node_id: str | None
    diagnostics: tuple[str, ...]
    semantics_id: str = "legacy_unspecified"
    semantics_bound: bool = False


def validate_params_only_expression(
    text: str,
    param_names: frozenset[str] | set[str],
    *,
    locator: str = "expr",
) -> tuple[str, ...]:
    """Return validation errors for a params-only replayable selection predicate."""
    allowed_roots = _PARAMS_ONLY_ROOTS
    allowed_builtins = _PARAMS_ONLY_BUILTINS
    errors: list[str] = []
    try:
        expr = parse_expression(text)
    except Exception as exc:  # noqa: BLE001 - surface parse failures as locator errors
        return (f"{locator}: invalid expression: {exc}",)
    errors.extend(
        _dependency_errors(
            expr,
            locator=locator,
            allowed_roots=allowed_roots,
            allowed_builtins=allowed_builtins,
            declared_param_names=param_names,
        )
    )
    return tuple(errors)


def validate_replayable_plan_gate(gate: GateDef) -> tuple[str, ...]:
    """Return validation errors for a replayable wired plan-review gate definition."""
    allowed_aliases = frozenset(entry.alias for entry in gate.reads)
    allowed_roots = _REPLAYABLE_PLAN_ROOTS | allowed_aliases
    errors: list[str] = []
    for rule in gate.rules:
        locator = f"gate:{gate.id}:{rule.field}"
        try:
            expr = parse_expression(rule.expr)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{locator}: invalid expression: {exc}")
            continue
        errors.extend(
            _dependency_errors(
                expr,
                locator=locator,
                allowed_roots=allowed_roots,
                allowed_builtins=_REPLAYABLE_PLAN_BUILTINS,
            )
        )
    for cause, expression in gate.causes.items():
        locator = f"gate:{gate.id}:causes:{cause}"
        try:
            expr = parse_expression(expression)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{locator}: invalid expression: {exc}")
            continue
        errors.extend(
            _dependency_errors(
                expr,
                locator=locator,
                allowed_roots=allowed_roots,
                allowed_builtins=_REPLAYABLE_PLAN_BUILTINS,
            )
        )
    return tuple(errors)


def validate_wired_profile_topology(
    schema: WorkflowSchemaV2,
    profile: LayerAssuranceProfile,
) -> tuple[str, ...]:
    """Return structural errors for one wired layer's explicit assurance graph."""
    layer = profile.layer
    cycle_graph_id = f"{layer}-plan-cycle"
    branch_graph_id = f"{layer}-branch"
    locator_prefix = f"graph:{cycle_graph_id}"
    errors: list[str] = []

    if cycle_graph_id not in schema.graphs:
        return (f"{locator_prefix}: missing plan-cycle graph",)

    cycle = schema.graphs[cycle_graph_id]
    branch = schema.graphs.get(branch_graph_id)

    gate_owners = _gate_owner_nodes(cycle, profile.gate_id)
    if len(gate_owners) == 0:
        errors.append(f"{locator_prefix}: missing explicit gate owner for {profile.gate_id!r}")
    elif len(gate_owners) > 1:
        errors.append(
            f"{locator_prefix}: multiple explicit gate owners for {profile.gate_id!r}: "
            + ", ".join(sorted(gate_owners))
        )

    attached = _attached_reviewer_gate_nodes(cycle, profile.gate_id)
    if attached:
        errors.append(
            f"{locator_prefix}: attached reviewer gate on {', '.join(sorted(attached))}; "
            "use explicit builtin:gate owner"
        )

    applicability_nodes = _applicability_nodes(cycle, layer)
    if len(applicability_nodes) == 0:
        errors.append(f"{locator_prefix}: missing applicability operation for layer {layer!r}")
    elif len(applicability_nodes) > 1:
        errors.append(
            f"{locator_prefix}: multiple applicability operations: " + ", ".join(sorted(applicability_nodes))
        )

    review_nodes = _review_nodes(cycle, layer)
    if len(review_nodes) == 0:
        errors.append(f"{locator_prefix}: missing reviewer node")
    elif len(review_nodes) > 1:
        errors.append(f"{locator_prefix}: multiple reviewer nodes: " + ", ".join(sorted(review_nodes)))

    if len(gate_owners) == 1 and len(review_nodes) == 1:
        gate_owner = gate_owners[0]
        review = review_nodes[0]
        if not _has_edge(cycle, review, gate_owner):
            errors.append(f"{locator_prefix}: explicit gate must follow reviewer")

    if len(applicability_nodes) == 1 and len(review_nodes) == 1:
        applicability = applicability_nodes[0]
        review = review_nodes[0]
        route = _route_from(cycle, applicability)
        if route is None:
            errors.append(f"{locator_prefix}: applicability node must declare a route")
        else:
            applicable_target = route.cases.get("true")
            inapplicable_target = route.cases.get("false")
            if applicable_target not in review_nodes:
                errors.append(f"{locator_prefix}: applicable path must reach review")
            if len(gate_owners) == 1 and inapplicable_target != gate_owners[0]:
                errors.append(f"{locator_prefix}: inapplicable path must reach review-gate")
            if (
                len(gate_owners) == 1
                and applicable_target in review_nodes
                and not _has_edge(cycle, applicable_target, gate_owners[0])
            ):
                errors.append(f"{locator_prefix}: review must precede the plan gate on applicable path")

    if not _has_edge(cycle, "fix", "review"):
        errors.append(f"{locator_prefix}: fix must re-enter at review")

    knowledge_route = _route_from(cycle, "knowledge-remediation")
    if knowledge_route is None:
        errors.append(f"{locator_prefix}: missing knowledge-remediation route")
    elif knowledge_route.cases.get("fix_and_proceed") != "review":
        errors.append(f"{locator_prefix}: knowledge remediation fix_and_proceed must re-enter at review")

    gate = schema.gates.get(profile.gate_id)
    if gate is None:
        errors.append(f"gate:{profile.gate_id}: missing gate definition")
    else:
        errors.extend(_gate_read_errors(gate, profile))

    if branch is not None:
        errors.extend(_codegen_topology_errors(schema, branch, profile, branch_graph_id))

    return tuple(errors)


def validate_replayable_assurance_schema(schema: WorkflowSchemaV2) -> tuple[str, ...]:
    """Validate replayable branch predicates, wired plan gates, and wired topology."""
    param_names = frozenset(schema.params)
    errors: list[str] = []

    assurance = schema.graphs.get("assurance")
    if assurance is None:
        errors.append("graph:assurance: missing assurance graph")
    else:
        for node_id in _ASSURANCE_BRANCH_NODES:
            node = assurance.nodes.get(node_id)
            if node is None:
                errors.append(f"graph:assurance.nodes.{node_id}: missing branch node")
                continue
            when = node.when
            if not when:
                errors.append(f"graph:assurance.nodes.{node_id}.when: missing selection predicate")
                continue
            errors.extend(
                validate_params_only_expression(
                    when,
                    param_names,
                    locator=f"graph:assurance.nodes.{node_id}.when",
                )
            )

    for layer in sorted(WIRED_REPLAY_LAYERS):
        profile = get_layer_assurance_profile(layer)
        gate = schema.gates.get(profile.gate_id)
        if gate is None:
            errors.append(f"gate:{profile.gate_id}: missing wired plan gate")
        else:
            errors.extend(validate_replayable_plan_gate(gate))
        errors.extend(validate_wired_profile_topology(schema, profile))

    return tuple(errors)


def validate_current_assurance_activation(
    schema: WorkflowSchemaV2,
) -> tuple[str, ...]:
    """Compatibility wrapper: deterministic string diagnostics for packaged compile.

    Until Task 15 activates the strict structured validator inside
    ``compile_packaged_workflow``, this retains the wired-classification path.
    Callers that need structured current-release diagnostics must use
    ``find_current_assurance_conformance_issues`` instead.
    """
    errors: list[str] = []
    for profile in iter_layer_assurance_profiles():
        spec = LayerTopologySpec(
            layer=profile.layer,
            plan_artifacts=profile.plan_artifacts,
            review_artifact=profile.review_artifact,
            review_alias=profile.review_alias,
            checks_artifact=profile.checks_artifact,
            gate_id=profile.gate_id,
        )
        topology = classify_pinned_layer_topology(schema, spec)
        if topology.status == "wired":
            continue
        errors.append(f"layer:{profile.layer}: assurance activation is {topology.status}")
        errors.extend(topology.diagnostics)
    return tuple(errors)


def validate_historical_replay_surface(
    schema: WorkflowSchemaV2,
    *,
    historical_roles: "DiscoveredHistoricalAssuranceRoles | None" = None,
) -> tuple[str, ...]:
    """Validate syntax, graph integrity, and replay-safe dependencies only.

    When ``historical_roles`` is provided, selection/branch lookup uses the
    discovered manifest rather than current node IDs.
    """
    param_names = frozenset(schema.params)
    errors: list[str] = []

    if historical_roles is not None:
        assurance = schema.graphs.get(historical_roles.assurance_graph_id)
        if assurance is None:
            return (f"graph:{historical_roles.assurance_graph_id}: missing assurance graph",)
        for layer_roles in historical_roles.layers:
            node_id = layer_roles.selection_event_node_id
            node = assurance.nodes.get(node_id)
            if node is None:
                errors.append(
                    f"graph:{historical_roles.assurance_graph_id}.nodes.{node_id}: missing branch node"
                )
                continue
            if not node.uses.startswith("graph:"):
                errors.append(
                    f"graph:{historical_roles.assurance_graph_id}.nodes.{node_id}: branch must use graph:<id>"
                )
                continue
            branch_id = node.uses.removeprefix("graph:")
            if branch_id != layer_roles.branch_graph_id:
                errors.append(
                    f"graph:{historical_roles.assurance_graph_id}.nodes.{node_id}: "
                    f"branch graph {branch_id!r} != discovered {layer_roles.branch_graph_id!r}"
                )
            if branch_id not in schema.graphs:
                errors.append(
                    f"graph:{historical_roles.assurance_graph_id}.nodes.{node_id}: "
                    f"missing branch graph {branch_id!r}"
                )
            when = node.when
            if not when:
                errors.append(
                    f"graph:{historical_roles.assurance_graph_id}.nodes.{node_id}.when: "
                    "missing selection predicate"
                )
                continue
            errors.extend(
                validate_params_only_expression(
                    when,
                    param_names,
                    locator=f"graph:{historical_roles.assurance_graph_id}.nodes.{node_id}.when",
                )
            )
    else:
        assurance = schema.graphs.get("assurance")
        if assurance is None:
            return ("graph:assurance: missing assurance graph",)

        for node_id in _ASSURANCE_BRANCH_NODES:
            node = assurance.nodes.get(node_id)
            if node is None:
                errors.append(f"graph:assurance.nodes.{node_id}: missing branch node")
                continue
            if not node.uses.startswith("graph:"):
                errors.append(f"graph:assurance.nodes.{node_id}: branch must use graph:<id>")
                continue
            branch_id = node.uses.removeprefix("graph:")
            if branch_id not in schema.graphs:
                errors.append(f"graph:assurance.nodes.{node_id}: missing branch graph {branch_id!r}")
            when = node.when
            if not when:
                errors.append(f"graph:assurance.nodes.{node_id}.when: missing selection predicate")
                continue
            errors.extend(
                validate_params_only_expression(
                    when,
                    param_names,
                    locator=f"graph:assurance.nodes.{node_id}.when",
                )
            )

    for profile in iter_layer_assurance_profiles():
        gate = schema.gates.get(profile.gate_id)
        if gate is not None:
            errors.extend(validate_replayable_plan_gate(gate))

    return tuple(errors)


def classify_pinned_layer_topology(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
) -> PinnedLayerTopology:
    """Compatibility alias for the frozen unbound display classifier (v5 epoch)."""
    return classify_pinned_layer_topology_v5(schema, topology_spec)


def classify_pinned_layer_topology_v4(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
) -> PinnedLayerTopology:
    """Frozen v4 unbound display classification (including known gaps)."""
    result = _classify_pinned_layer_topology_legacy(schema, topology_spec)
    return _with_semantics(result, semantics_id="legacy_v4_unbound", semantics_bound=False)


def classify_pinned_layer_topology_v5(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
) -> PinnedLayerTopology:
    """Frozen v5 unbound display classification (byte-stable, including false negatives)."""
    result = _classify_pinned_layer_topology_legacy(schema, topology_spec)
    return _with_semantics(result, semantics_id="legacy_v5_unbound", semantics_bound=False)


def classify_pinned_layer_topology_v6(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
    *,
    historical_roles: "DiscoveredHistoricalAssuranceRoles",
) -> PinnedLayerTopology:
    """V6 semantic role/CFG classifier; replay authorization evidence only."""
    from assurance_kernel.workflow.graph.historical_topology_v6 import (
        LayerTopologySpecView,
        classify_historical_layer_topology_v6,
    )

    classified = classify_historical_layer_topology_v6(
        schema,
        LayerTopologySpecView(
            layer=topology_spec.layer,
            review_artifact=topology_spec.review_artifact,
            review_alias=topology_spec.review_alias,
            checks_artifact=topology_spec.checks_artifact,
            gate_id=topology_spec.gate_id,
        ),
        historical_roles=historical_roles,
    )
    roles = classified.roles
    return PinnedLayerTopology(
        layer=classified.layer,
        status=classified.status,
        assurance_node_id=None if roles is None else roles.selection_event_node_id,
        branch_graph_id=None if roles is None else roles.branch_graph_id,
        cycle_call_node_id=None if roles is None else roles.cycle_call_node_id,
        cycle_graph_id=None if roles is None else roles.cycle_graph_id,
        applicability_node_id=None if roles is None else roles.applicability_node_id,
        reviewer_node_id=None if roles is None else roles.reviewer_node_id,
        gate_node_id=None if roles is None else roles.plan_gate_node_id,
        human_review_node_id=None,
        knowledge_remediation_node_id=None,
        codegen_precondition_node_id=None if roles is None else roles.precondition_node_id,
        codegen_node_id=None if roles is None else roles.codegen_node_id,
        diagnostics=classified.diagnostics,
        semantics_id=classified.semantics_id,
        semantics_bound=classified.semantics_bound,
    )


def _with_semantics(
    topology: PinnedLayerTopology,
    *,
    semantics_id: str,
    semantics_bound: bool,
) -> PinnedLayerTopology:
    return PinnedLayerTopology(
        layer=topology.layer,
        status=topology.status,
        assurance_node_id=topology.assurance_node_id,
        branch_graph_id=topology.branch_graph_id,
        cycle_call_node_id=topology.cycle_call_node_id,
        cycle_graph_id=topology.cycle_graph_id,
        applicability_node_id=topology.applicability_node_id,
        reviewer_node_id=topology.reviewer_node_id,
        gate_node_id=topology.gate_node_id,
        human_review_node_id=topology.human_review_node_id,
        knowledge_remediation_node_id=topology.knowledge_remediation_node_id,
        codegen_precondition_node_id=topology.codegen_precondition_node_id,
        codegen_node_id=topology.codegen_node_id,
        diagnostics=topology.diagnostics,
        semantics_id=semantics_id,
        semantics_bound=semantics_bound,
    )


def _classify_pinned_layer_topology_legacy(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
) -> PinnedLayerTopology:
    """Frozen pre-v6 name-coupled classifier body — do not improve."""
    layer = topology_spec.layer
    empty = _empty_topology(layer)
    diagnostics: list[str] = []

    assurance = schema.graphs.get("assurance")
    if assurance is None:
        return _finalize_topology(
            empty,
            status="partial",
            diagnostics=("graph:assurance: missing assurance graph",),
        )

    assurance_nodes = [
        node_id
        for node_id, node in assurance.nodes.items()
        if node_id == layer and node.uses.startswith("graph:")
    ]
    if len(assurance_nodes) != 1:
        if not assurance_nodes:
            # Probe construct-named graphs for legacy marker absence when present.
            return _classify_without_assurance_binding(schema, topology_spec, empty)
        return _finalize_topology(
            empty,
            status="partial",
            diagnostics=(f"layer:{layer}: expected exactly one assurance branch node",),
            assurance_node_id=assurance_nodes[0],
        )

    assurance_node_id = assurance_nodes[0]
    branch_graph_id = assurance.nodes[assurance_node_id].uses.removeprefix("graph:")
    branch = schema.graphs.get(branch_graph_id)
    if branch is None:
        return _finalize_topology(
            empty,
            status="partial",
            diagnostics=(f"layer:{layer}: missing branch graph {branch_graph_id!r}",),
            assurance_node_id=assurance_node_id,
            branch_graph_id=branch_graph_id,
        )

    cycle_calls = [
        (node_id, node.uses.removeprefix("graph:"))
        for node_id, node in branch.nodes.items()
        if node.uses.startswith("graph:")
    ]
    if len(cycle_calls) != 1:
        # Still probe cycle markers if a unique construct-named cycle exists.
        if len(cycle_calls) == 0:
            return _classify_discovered(
                schema,
                topology_spec,
                empty,
                assurance_node_id=assurance_node_id,
                branch_graph_id=branch_graph_id,
                branch=branch,
                cycle_call_node_id=None,
                cycle_graph_id=None,
                cycle=None,
                extra_diagnostics=(f"layer:{layer}: expected exactly one cycle-call graph binding",),
            )
        return _finalize_topology(
            empty,
            status="partial",
            diagnostics=(f"layer:{layer}: expected exactly one cycle-call graph binding",),
            assurance_node_id=assurance_node_id,
            branch_graph_id=branch_graph_id,
        )

    cycle_call_node_id, cycle_graph_id = cycle_calls[0]
    cycle = schema.graphs.get(cycle_graph_id)
    if cycle is None:
        return _finalize_topology(
            empty,
            status="partial",
            diagnostics=(f"layer:{layer}: missing cycle graph {cycle_graph_id!r}",),
            assurance_node_id=assurance_node_id,
            branch_graph_id=branch_graph_id,
            cycle_call_node_id=cycle_call_node_id,
            cycle_graph_id=cycle_graph_id,
        )

    return _classify_discovered(
        schema,
        topology_spec,
        empty,
        assurance_node_id=assurance_node_id,
        branch_graph_id=branch_graph_id,
        branch=branch,
        cycle_call_node_id=cycle_call_node_id,
        cycle_graph_id=cycle_graph_id,
        cycle=cycle,
        extra_diagnostics=tuple(diagnostics),
    )


def _dependency_errors(
    expr: Expr,
    *,
    locator: str,
    allowed_roots: frozenset[str] | set[str],
    allowed_builtins: frozenset[str] | set[str],
    declared_param_names: frozenset[str] | set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for root in sorted(_collect_root_idents(expr)):
        if root in _FORBIDDEN_REPLAY_BUILTINS:
            continue
        if root not in allowed_roots:
            if root == "state":
                errors.append(f"{locator}: disallowed identifier 'state'")
            else:
                errors.append(f"{locator}: disallowed identifier {root!r}")
    for call in _collect_calls(expr):
        if call.callee in _FORBIDDEN_REPLAY_BUILTINS:
            errors.append(f"{locator}: disallowed builtin {call.callee!r}")
        elif call.callee not in allowed_builtins:
            errors.append(f"{locator}: disallowed builtin {call.callee!r}")
    _ = declared_param_names
    return errors


def _collect_root_idents(expr: Expr) -> set[str]:
    return {node.name for node in _walk(expr) if isinstance(node, Ident)}


def _collect_calls(expr: Expr) -> list[Call]:
    return [node for node in _walk(expr) if isinstance(node, Call)]


def _gate_owner_nodes(graph: GraphDef, gate_id: str) -> list[str]:
    owners: list[str] = []
    for node_id, node in graph.nodes.items():
        if node.uses == "builtin:gate" and node.with_.get("gate") == gate_id:
            owners.append(node_id)
    return owners


def _attached_reviewer_gate_nodes(graph: GraphDef, gate_id: str) -> list[str]:
    attached: list[str] = []
    for node_id, node in graph.nodes.items():
        if node.gate == gate_id and node.uses != "builtin:gate":
            attached.append(node_id)
    return attached


def _applicability_nodes(graph: GraphDef, layer: str) -> list[str]:
    nodes: list[str] = []
    for node_id, node in graph.nodes.items():
        if node.uses == _APPLICABILITY_OPERATION and node.with_.get("layer") == layer:
            nodes.append(node_id)
    return nodes


def _review_nodes(graph: GraphDef, layer: str) -> list[str]:
    suffix = f"aa-{layer}-plan-reviewer"
    return [
        node_id
        for node_id, node in graph.nodes.items()
        if node.uses == f"skill:{suffix}" or node.uses.endswith(f":aa-{layer}-plan-reviewer")
    ]


def _route_from(graph: GraphDef, node_id: str):
    for route in graph.routes:
        if route.from_ == node_id:
            return route
    return None


def _has_edge(graph: GraphDef, source: str, target: str) -> bool:
    return any(edge.from_ == source and edge.to == target for edge in graph.edges)


def _gate_read_errors(gate: GateDef, profile: LayerAssuranceProfile) -> list[str]:
    locator = f"gate:{gate.id}:reads"
    errors: list[str] = []
    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    reads_by_alias = {entry.alias: entry.path for entry in gate.reads}

    if profile.review_artifact not in reads_by_path:
        errors.append(f"{locator}: gate reads must include review artifact {profile.review_artifact!r}")
    elif reads_by_path[profile.review_artifact] != profile.review_alias:
        errors.append(
            f"{locator}: review alias must be {profile.review_alias!r}, "
            f"got {reads_by_path[profile.review_artifact]!r}"
        )

    if profile.checks_artifact not in reads_by_path:
        errors.append(f"{locator}: gate reads must include checks artifact {profile.checks_artifact!r}")
    else:
        expected_checks_alias = derive_alias(profile.checks_artifact)
        if reads_by_path[profile.checks_artifact] != expected_checks_alias:
            errors.append(
                f"{locator}: checks alias must be {expected_checks_alias!r}, "
                f"got {reads_by_path[profile.checks_artifact]!r}"
            )

    if _DATA_KNOWLEDGE_PATH not in reads_by_path:
        errors.append(f"{locator}: gate reads must include L1 path {_DATA_KNOWLEDGE_PATH!r}")
    elif reads_by_path[_DATA_KNOWLEDGE_PATH] != _DATA_KNOWLEDGE_ALIAS:
        errors.append(
            f"{locator}: L1 alias must be {_DATA_KNOWLEDGE_ALIAS!r}, "
            f"got {reads_by_path[_DATA_KNOWLEDGE_PATH]!r}"
        )

    if len(reads_by_alias) != len(gate.reads):
        errors.append(f"{locator}: gate read aliases must be unique")

    return errors


def _codegen_topology_errors(
    schema: WorkflowSchemaV2,
    branch: GraphDef,
    profile: LayerAssuranceProfile,
    branch_graph_id: str,
) -> list[str]:
    errors: list[str] = []
    codegen_gate_id = f"{profile.layer}-codegen-precondition-gate"
    locator = f"graph:{branch_graph_id}"
    owners = _gate_owner_nodes(branch, codegen_gate_id)
    if len(owners) == 0:
        errors.append(f"{locator}: missing codegen precondition gate owner")
    elif len(owners) > 1:
        errors.append(f"{locator}: multiple codegen precondition gate owners")

    gate = schema.gates.get(codegen_gate_id)
    if gate is None:
        errors.append(f"gate:{codegen_gate_id}: missing codegen precondition gate")
        return errors

    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    expected_checks_alias = derive_alias(profile.checks_artifact)
    if profile.checks_artifact not in reads_by_path:
        errors.append(
            f"gate:{codegen_gate_id}:reads: codegen gate must read checks artifact "
            f"{profile.checks_artifact!r}"
        )
    elif reads_by_path[profile.checks_artifact] != expected_checks_alias:
        errors.append(
            f"gate:{codegen_gate_id}:reads: checks alias must be {expected_checks_alias!r}, "
            f"got {reads_by_path[profile.checks_artifact]!r}"
        )
    return errors


def _empty_topology(layer: str) -> PinnedLayerTopology:
    return PinnedLayerTopology(
        layer=layer,
        status="partial",
        assurance_node_id=None,
        branch_graph_id=None,
        cycle_call_node_id=None,
        cycle_graph_id=None,
        applicability_node_id=None,
        reviewer_node_id=None,
        gate_node_id=None,
        human_review_node_id=None,
        knowledge_remediation_node_id=None,
        codegen_precondition_node_id=None,
        codegen_node_id=None,
        diagnostics=(),
    )


def _finalize_topology(
    base: PinnedLayerTopology,
    *,
    status: WiringStatus,
    diagnostics: tuple[str, ...],
    assurance_node_id: str | None = None,
    branch_graph_id: str | None = None,
    cycle_call_node_id: str | None = None,
    cycle_graph_id: str | None = None,
    applicability_node_id: str | None = None,
    reviewer_node_id: str | None = None,
    gate_node_id: str | None = None,
    human_review_node_id: str | None = None,
    knowledge_remediation_node_id: str | None = None,
    codegen_precondition_node_id: str | None = None,
    codegen_node_id: str | None = None,
) -> PinnedLayerTopology:
    return PinnedLayerTopology(
        layer=base.layer,
        status=status,
        assurance_node_id=assurance_node_id,
        branch_graph_id=branch_graph_id,
        cycle_call_node_id=cycle_call_node_id,
        cycle_graph_id=cycle_graph_id,
        applicability_node_id=applicability_node_id,
        reviewer_node_id=reviewer_node_id,
        gate_node_id=gate_node_id,
        human_review_node_id=human_review_node_id,
        knowledge_remediation_node_id=knowledge_remediation_node_id,
        codegen_precondition_node_id=codegen_precondition_node_id,
        codegen_node_id=codegen_node_id,
        diagnostics=diagnostics,
    )


def _classify_without_assurance_binding(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
    empty: PinnedLayerTopology,
) -> PinnedLayerTopology:
    layer = topology_spec.layer
    # Best-effort legacy probe for construct-named graphs when assurance binding is absent.
    for branch_id in (f"{layer}-branch", f"{layer}-pinned-branch"):
        branch = schema.graphs.get(branch_id)
        if branch is None:
            continue
        cycle_calls = [
            (node_id, node.uses.removeprefix("graph:"))
            for node_id, node in branch.nodes.items()
            if node.uses.startswith("graph:")
        ]
        if len(cycle_calls) != 1:
            continue
        cycle_call_node_id, cycle_graph_id = cycle_calls[0]
        cycle = schema.graphs.get(cycle_graph_id)
        return _classify_discovered(
            schema,
            topology_spec,
            empty,
            assurance_node_id=None,
            branch_graph_id=branch_id,
            branch=branch,
            cycle_call_node_id=cycle_call_node_id,
            cycle_graph_id=cycle_graph_id,
            cycle=cycle,
            extra_diagnostics=(f"layer:{layer}: missing assurance branch node",),
        )
    return _finalize_topology(
        empty,
        status="legacy_unwired",
        diagnostics=(f"layer:{layer}: no assurance branch binding",),
    )


def _classify_discovered(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
    empty: PinnedLayerTopology,
    *,
    assurance_node_id: str | None,
    branch_graph_id: str | None,
    branch: GraphDef | None,
    cycle_call_node_id: str | None,
    cycle_graph_id: str | None,
    cycle: GraphDef | None,
    extra_diagnostics: tuple[str, ...] = (),
) -> PinnedLayerTopology:
    layer = topology_spec.layer
    diagnostics = list(extra_diagnostics)

    applicability_ids = _applicability_nodes(cycle, layer) if cycle is not None else []
    review_ids = _review_nodes(cycle, layer) if cycle is not None else []
    gate_ids = _gate_owner_nodes(cycle, topology_spec.gate_id) if cycle is not None else []
    marker_count = sum(1 for group in (applicability_ids, review_ids, gate_ids) if group)

    discovered = {
        "assurance_node_id": assurance_node_id,
        "branch_graph_id": branch_graph_id,
        "cycle_call_node_id": cycle_call_node_id,
        "cycle_graph_id": cycle_graph_id,
        "applicability_node_id": applicability_ids[0] if len(applicability_ids) == 1 else None,
        "reviewer_node_id": review_ids[0] if len(review_ids) == 1 else None,
        "gate_node_id": gate_ids[0] if len(gate_ids) == 1 else None,
    }

    if marker_count == 0:
        # Shallow attached reviewer / branch codegen gates do not count as activation.
        return _finalize_topology(
            empty,
            status="legacy_unwired",
            diagnostics=tuple(diagnostics)
            if diagnostics
            else (f"layer:{layer}: no assurance activation markers",),
            **discovered,
        )

    if cycle is None or branch is None or cycle_call_node_id is None:
        diagnostics.append(f"layer:{layer}: incomplete branch/cycle discovery")
        return _finalize_topology(empty, status="partial", diagnostics=tuple(diagnostics), **discovered)

    review_ids = _review_nodes(cycle, layer)
    human_ids = _interrupt_nodes(cycle)
    knowledge_ids = [node_id for node_id in human_ids if "knowledge" in node_id]
    # Prefer explicit knowledge-remediation node id when present.
    knowledge_node_id = (
        "knowledge-remediation"
        if "knowledge-remediation" in cycle.nodes
        else (knowledge_ids[0] if len(knowledge_ids) == 1 else None)
    )
    human_review_node_id = (
        "human-review"
        if "human-review" in cycle.nodes
        else next((node_id for node_id in human_ids if node_id != knowledge_node_id), None)
    )

    codegen_gate_id = f"{layer}-codegen-precondition-gate"
    codegen_precondition_ids = _gate_owner_nodes(branch, codegen_gate_id)
    codegen_ids = [
        node_id
        for node_id, node in branch.nodes.items()
        if "codegen" in node_id and node.uses.startswith("skill:")
    ]

    discovered.update(
        {
            "reviewer_node_id": review_ids[0] if len(review_ids) == 1 else None,
            "human_review_node_id": human_review_node_id,
            "knowledge_remediation_node_id": knowledge_node_id,
            "codegen_precondition_node_id": (
                codegen_precondition_ids[0] if len(codegen_precondition_ids) == 1 else None
            ),
            "codegen_node_id": codegen_ids[0] if len(codegen_ids) == 1 else None,
        }
    )

    diagnostics.extend(
        _complete_wiring_diagnostics(
            schema,
            topology_spec,
            branch=branch,
            cycle=cycle,
            cycle_call_node_id=cycle_call_node_id,
            applicability_ids=applicability_ids,
            review_ids=review_ids,
            gate_ids=gate_ids,
            human_review_node_id=human_review_node_id,
            knowledge_node_id=knowledge_node_id,
            codegen_precondition_ids=codegen_precondition_ids,
            codegen_ids=codegen_ids,
        )
    )

    status: WiringStatus = "wired" if not diagnostics else "partial"
    return _finalize_topology(empty, status=status, diagnostics=tuple(diagnostics), **discovered)


def _interrupt_nodes(graph: GraphDef) -> list[str]:
    return [node_id for node_id, node in graph.nodes.items() if node.uses == "builtin:interrupt"]


def _complete_wiring_diagnostics(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
    *,
    branch: GraphDef,
    cycle: GraphDef,
    cycle_call_node_id: str,
    applicability_ids: list[str],
    review_ids: list[str],
    gate_ids: list[str],
    human_review_node_id: str | None,
    knowledge_node_id: str | None,
    codegen_precondition_ids: list[str],
    codegen_ids: list[str],
) -> list[str]:
    layer = topology_spec.layer
    locator = f"layer:{layer}"
    errors: list[str] = []

    if len(applicability_ids) != 1:
        errors.append(f"{locator}: expected exactly one applicability operation")
    if len(review_ids) != 1:
        errors.append(f"{locator}: expected exactly one reviewer")
    if len(gate_ids) != 1:
        errors.append(f"{locator}: expected exactly one explicit gate owner")
    elif review_ids:
        attached = _attached_reviewer_gate_nodes(cycle, topology_spec.gate_id)
        if attached:
            errors.append(f"{locator}: reviewer must not attach the plan gate")

    if len(applicability_ids) == 1 and len(review_ids) == 1:
        applicability = applicability_ids[0]
        review = review_ids[0]
        route = _route_from(cycle, applicability)
        if route is None:
            errors.append(f"{locator}: applicability node must declare a route")
        else:
            if route.cases.get("true") != review:
                errors.append(f"{locator}: applicable path must reach review")
            if len(gate_ids) == 1 and route.cases.get("false") != gate_ids[0]:
                errors.append(f"{locator}: inapplicable path must reach review-gate")
            if (
                route.cases.get("true") == review
                and len(gate_ids) == 1
                and not _has_edge(cycle, review, gate_ids[0])
            ):
                errors.append(f"{locator}: review must precede the plan gate on applicable path")

    if len(review_ids) == 1 and len(gate_ids) == 1:
        if not _has_edge(cycle, review_ids[0], gate_ids[0]):
            errors.append(f"{locator}: explicit gate must follow reviewer")

    errors.extend(
        _recovery_shape_errors(
            cycle,
            topology_spec,
            gate_ids=gate_ids,
            review_ids=review_ids,
            human_review_node_id=human_review_node_id,
        )
    )
    errors.extend(
        _knowledge_remediation_errors(
            cycle,
            locator=locator,
            gate_ids=gate_ids,
            review_ids=review_ids,
            knowledge_node_id=knowledge_node_id,
        )
    )

    if len(codegen_precondition_ids) != 1:
        errors.append(f"{locator}: expected exactly one codegen precondition owner")
    if len(codegen_ids) != 1:
        errors.append(f"{locator}: expected exactly one codegen skill node")

    if len(codegen_precondition_ids) == 1 and cycle_call_node_id is not None:
        errors.extend(
            _codegen_ast_errors(
                schema,
                topology_spec,
                codegen_gate_id=f"{layer}-codegen-precondition-gate",
                cycle_call_node_id=cycle_call_node_id,
            )
        )

    plan_gate = schema.gates.get(topology_spec.gate_id)
    if plan_gate is None:
        errors.append(f"gate:{topology_spec.gate_id}: missing plan gate")
    else:
        # Reuse profile-shaped gate read checks via a temporary profile-like adapter.
        errors.extend(_topology_gate_read_errors(plan_gate, topology_spec))
        errors.extend(validate_replayable_plan_gate(plan_gate))

    if layer in _SPECIALTY_PARENT_PREFLIGHT_LAYERS:
        errors.extend(
            _parent_preflight_errors(
                branch,
                layer=layer,
                cycle_call_node_id=cycle_call_node_id,
            )
        )

    return errors


def _recovery_shape_errors(
    cycle: GraphDef,
    topology_spec: LayerTopologySpec,
    *,
    gate_ids: list[str],
    review_ids: list[str],
    human_review_node_id: str | None,
) -> list[str]:
    locator = f"layer:{topology_spec.layer}"
    if len(gate_ids) != 1 or len(review_ids) != 1:
        return [f"{locator}: cannot validate recovery shape without gate and reviewer"]

    gate_route = _route_from(cycle, gate_ids[0])
    if gate_route is None:
        return [f"{locator}: missing review-gate route"]

    needs_fix_target = gate_route.cases.get("needs_fix")
    review = review_ids[0]

    # Automatic author/fixer shape: needs_fix -> skill node, skill -> review.
    if needs_fix_target is not None and needs_fix_target in cycle.nodes:
        fix_node = cycle.nodes[needs_fix_target]
        if fix_node.uses.startswith("skill:"):
            if _has_edge(cycle, needs_fix_target, review):
                return []
            return [f"{locator}: automatic fixer must re-enter at review"]

    # Human-only shape: needs_fix -> human-review, fix_and_proceed -> review + allowlist.
    if human_review_node_id is None or needs_fix_target != human_review_node_id:
        return [f"{locator}: needs_fix must reach human-review for human-only recovery"]

    human = cycle.nodes[human_review_node_id]
    interrupt = human.interrupt
    if interrupt is None or interrupt.manual_revision is None:
        return [f"{locator}: human-only interrupt must declare manual_revision"]
    if interrupt.manual_revision.action != "fix_and_proceed":
        return [f"{locator}: manual_revision.action must be fix_and_proceed"]
    expected_paths = tuple(f"change:{path}" for path in topology_spec.plan_artifacts)
    mapping_json = f"change:plans/{topology_spec.layer}-codegen-mapping.json"
    mapping_yaml = f"change:plans/{topology_spec.layer}-codegen-mapping.yaml"
    actual_paths = tuple(interrupt.manual_revision.paths)
    if actual_paths not in {expected_paths, (*expected_paths, mapping_json), (*expected_paths, mapping_yaml)}:
        return [f"{locator}: manual_revision.paths must exactly match plan artifacts"]

    human_route = _route_from(cycle, human_review_node_id)
    if human_route is None or human_route.cases.get("fix_and_proceed") != review:
        return [f"{locator}: human-only fix_and_proceed must return to review"]
    return []


def _knowledge_remediation_errors(
    cycle: GraphDef,
    *,
    locator: str,
    gate_ids: list[str],
    review_ids: list[str],
    knowledge_node_id: str | None,
) -> list[str]:
    errors: list[str] = []
    if len(gate_ids) != 1:
        return [f"{locator}: cannot validate knowledge remediation without gate"]
    if knowledge_node_id is None:
        return [f"{locator}: missing knowledge-remediation interrupt"]
    if len(review_ids) != 1:
        return [f"{locator}: cannot validate knowledge remediation without reviewer"]

    gate_route = _route_from(cycle, gate_ids[0])
    if gate_route is None or gate_route.cases.get("knowledge_remediation") != knowledge_node_id:
        errors.append(f"{locator}: review-gate.knowledge_remediation must reach knowledge interrupt")

    knowledge_route = _route_from(cycle, knowledge_node_id)
    if knowledge_route is None:
        errors.append(f"{locator}: missing knowledge-remediation route")
    elif knowledge_route.cases.get("fix_and_proceed") != review_ids[0]:
        errors.append(f"{locator}: knowledge remediation fix_and_proceed must re-enter at review")
    return errors


def _parent_preflight_errors(
    branch: GraphDef,
    *,
    layer: str,
    cycle_call_node_id: str,
) -> list[str]:
    locator = f"layer:{layer}"
    preflight_ids = _applicability_nodes(branch, layer)
    if len(preflight_ids) != 1:
        return [f"{locator}: missing cases-only parent preflight"]
    preflight = preflight_ids[0]
    if not _has_edge(branch, "START", preflight):
        return [f"{locator}: parent preflight must be reached from START"]

    route = _route_from(branch, preflight)
    if route is None:
        return [f"{locator}: parent preflight must declare a route"]

    plan_ids = [node_id for node_id, node in branch.nodes.items() if node.uses == f"skill:aa-{layer}-plan"]
    if not plan_ids:
        return [f"{locator}: parent branch must declare a plan node"]

    if route.cases.get("false") != cycle_call_node_id:
        return [f"{locator}: parent preflight false path must reach cycle"]
    if route.cases.get("true") not in plan_ids:
        return [f"{locator}: parent preflight true path must reach plan"]
    plan_id = route.cases["true"]
    if not _has_edge(branch, plan_id, cycle_call_node_id):
        return [f"{locator}: plan must reach cycle after parent preflight"]
    if not any(edge.from_ == preflight and edge.to == cycle_call_node_id for edge in branch.edges):
        return [f"{locator}: parent preflight must provide a direct cycle path for codegen-only"]
    return []


def _topology_gate_read_errors(gate: GateDef, spec: LayerTopologySpec) -> list[str]:
    locator = f"gate:{gate.id}:reads"
    errors: list[str] = []
    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    if spec.review_artifact not in reads_by_path:
        errors.append(f"{locator}: gate reads must include review artifact {spec.review_artifact!r}")
    elif reads_by_path[spec.review_artifact] != spec.review_alias:
        errors.append(
            f"{locator}: review alias must be {spec.review_alias!r}, "
            f"got {reads_by_path[spec.review_artifact]!r}"
        )
    if spec.checks_artifact not in reads_by_path:
        errors.append(f"{locator}: gate reads must include checks artifact {spec.checks_artifact!r}")
    else:
        expected_checks_alias = derive_alias(spec.checks_artifact)
        if reads_by_path[spec.checks_artifact] != expected_checks_alias:
            errors.append(
                f"{locator}: checks alias must be {expected_checks_alias!r}, "
                f"got {reads_by_path[spec.checks_artifact]!r}"
            )
    if _DATA_KNOWLEDGE_PATH not in reads_by_path:
        errors.append(f"{locator}: gate reads must include L1 path {_DATA_KNOWLEDGE_PATH!r}")
    elif reads_by_path[_DATA_KNOWLEDGE_PATH] != _DATA_KNOWLEDGE_ALIAS:
        errors.append(
            f"{locator}: L1 alias must be {_DATA_KNOWLEDGE_ALIAS!r}, "
            f"got {reads_by_path[_DATA_KNOWLEDGE_PATH]!r}"
        )
    return errors


def _codegen_ast_errors(
    schema: WorkflowSchemaV2,
    topology_spec: LayerTopologySpec,
    *,
    codegen_gate_id: str,
    cycle_call_node_id: str,
) -> list[str]:
    locator = f"gate:{codegen_gate_id}"
    gate = schema.gates.get(codegen_gate_id)
    if gate is None:
        return [f"{locator}: missing codegen precondition gate"]

    errors: list[str] = []
    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    checks_alias = derive_alias(topology_spec.checks_artifact)
    for path, alias in (
        (topology_spec.review_artifact, topology_spec.review_alias),
        (topology_spec.checks_artifact, checks_alias),
        (_DATA_KNOWLEDGE_PATH, _DATA_KNOWLEDGE_ALIAS),
    ):
        if path not in reads_by_path:
            errors.append(f"{locator}:reads: must include {path!r}")
        elif reads_by_path[path] != alias:
            errors.append(f"{locator}:reads: alias for {path!r} must be {alias!r}")

    rules = {rule.field: rule.expr for rule in gate.rules}
    for field in ("skip_when", "stop_when", "pass_when"):
        if field not in rules:
            errors.append(f"{locator}: missing {field}")

    if "skip_when" in rules:
        errors.extend(
            _require_top_level_predicates(
                rules["skip_when"],
                locator=f"{locator}:skip_when",
                op="and",
                required=_skip_predicates(topology_spec),
                allow_extra=True,
            )
        )
    if "stop_when" in rules:
        errors.extend(
            _require_top_level_predicates(
                rules["stop_when"],
                locator=f"{locator}:stop_when",
                op="or",
                required=_stop_predicates(topology_spec, cycle_call_node_id),
                allow_extra=True,
            )
        )
    if "pass_when" in rules:
        errors.extend(
            _require_top_level_predicates(
                rules["pass_when"],
                locator=f"{locator}:pass_when",
                op="and",
                required=_pass_predicates(topology_spec, cycle_call_node_id),
                allow_extra=True,
            )
        )
    return errors


def _skip_predicates(spec: LayerTopologySpec) -> tuple[Expr, ...]:
    checks_alias = derive_alias(spec.checks_artifact)
    return (
        Compare(
            "==",
            Call(
                "plan_assurance_state",
                (
                    Ident(checks_alias),
                    Ident(spec.review_alias),
                    Ident(_DATA_KNOWLEDGE_ALIAS),
                    DslLiteral(spec.layer),
                ),
            ),
            DslLiteral("not_applicable"),
        ),
    )


def _stop_predicates(spec: LayerTopologySpec, cycle_call_node_id: str) -> tuple[Expr, ...]:
    checks_alias = derive_alias(spec.checks_artifact)
    return (
        Compare(
            "!=",
            Member(Call("node", (DslLiteral(cycle_call_node_id),)), "status"),
            DslLiteral("succeeded"),
        ),
        Compare(
            "==",
            Call(
                "plan_assurance_state",
                (
                    Ident(checks_alias),
                    Ident(spec.review_alias),
                    Ident(_DATA_KNOWLEDGE_ALIAS),
                    DslLiteral(spec.layer),
                ),
            ),
            DslLiteral("invalid"),
        ),
        Not(Call("file_exists", (DslLiteral(_DATA_KNOWLEDGE_PATH),))),
    )


def _pass_predicates(spec: LayerTopologySpec, cycle_call_node_id: str) -> tuple[Expr, ...]:
    checks_alias = derive_alias(spec.checks_artifact)
    predicates: list[Expr] = [
        Compare(
            "==",
            Member(Call("node", (DslLiteral(cycle_call_node_id),)), "status"),
            DslLiteral("succeeded"),
        ),
        Compare(
            "==",
            Call(
                "plan_assurance_state",
                (
                    Ident(checks_alias),
                    Ident(spec.review_alias),
                    Ident(_DATA_KNOWLEDGE_ALIAS),
                    DslLiteral(spec.layer),
                ),
            ),
            DslLiteral("applicable"),
        ),
        Compare(
            "==",
            Member(Call("gate", (DslLiteral(spec.gate_id),)), "verdict"),
            DslLiteral("pass"),
        ),
    ]
    if spec.layer in _SPECIALTY_PARENT_PREFLIGHT_LAYERS:
        predicates.append(
            Call(
                "capabilities_present",
                (Ident(spec.review_alias), Ident(_DATA_KNOWLEDGE_ALIAS)),
            )
        )
    predicates.append(Call("file_exists", (DslLiteral(_DATA_KNOWLEDGE_PATH),)))
    return tuple(predicates)


def _require_top_level_predicates(
    text: str,
    *,
    locator: str,
    op: str,
    required: tuple[Expr, ...],
    allow_extra: bool,
) -> list[str]:
    _ = allow_extra
    try:
        expr = parse_expression(text)
    except Exception as exc:  # noqa: BLE001
        return [f"{locator}: invalid expression: {exc}"]

    # Reject permissive top-level OR around a pass/skip conjunction.
    if op == "and" and isinstance(expr, BoolOp) and expr.op == "or":
        return [f"{locator}: required predicates must not sit under a permissive or"]

    parts = _flatten_boolop(expr, op)
    missing = [predicate for predicate in required if not any(_expr_equal(part, predicate) for part in parts)]
    if missing:
        return [f"{locator}: missing required hard predicate ({len(missing)} absent)"]
    return []


def _flatten_boolop(expr: Expr, op: str) -> list[Expr]:
    if isinstance(expr, BoolOp) and expr.op == op:
        return _flatten_boolop(expr.left, op) + _flatten_boolop(expr.right, op)
    return [expr]


def _expr_equal(left: Expr, right: Expr) -> bool:
    return left == right
