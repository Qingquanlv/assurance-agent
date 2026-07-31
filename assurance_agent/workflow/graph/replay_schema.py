"""Pure replayable assurance schema dependency and topology guards."""

from __future__ import annotations

from assurance_agent.verification.profiles import LayerAssuranceProfile, get_layer_assurance_profile
from assurance_agent.workflow.graph.schema_v2 import GraphDef, WorkflowSchemaV2
from assurance_agent.workflow.orchestration.dsl import Call, Expr, Ident, _walk, parse_expression
from assurance_agent.workflow.orchestration.schema import GateDef, derive_alias

WIRED_REPLAY_LAYERS = frozenset({"api", "e2e"})

_APPLICABILITY_OPERATION = "operation:derive-plan-layer-applicability"
_MECHANICAL_OPERATION = "operation:verify-plan-mechanical"
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

    mechanical_nodes = _mechanical_nodes(cycle, profile)
    if len(mechanical_nodes) == 0:
        errors.append(f"{locator_prefix}: missing reviewed mechanical producer")
    elif len(mechanical_nodes) > 1:
        errors.append(
            f"{locator_prefix}: multiple reviewed mechanical producers: "
            + ", ".join(sorted(mechanical_nodes))
        )

    if len(gate_owners) == 1 and len(mechanical_nodes) == 1:
        gate_owner = gate_owners[0]
        mechanical = mechanical_nodes[0]
        if not _has_edge(cycle, mechanical, gate_owner):
            errors.append(f"{locator_prefix}: explicit gate must follow mechanical producer")

    if len(applicability_nodes) == 1 and len(mechanical_nodes) == 1:
        applicability = applicability_nodes[0]
        mechanical = mechanical_nodes[0]
        route = _route_from(cycle, applicability)
        if route is None:
            errors.append(f"{locator_prefix}: applicability node must declare a route")
        else:
            applicable_target = route.cases.get("true")
            inapplicable_target = route.cases.get("false")
            review_nodes = _review_nodes(cycle, layer)
            if not review_nodes:
                errors.append(f"{locator_prefix}: missing reviewer node")
            elif applicable_target not in review_nodes:
                errors.append(f"{locator_prefix}: applicable path must reach review before mechanical")
            if inapplicable_target != mechanical:
                errors.append(f"{locator_prefix}: inapplicable path must reach mechanical producer")
            if applicable_target in review_nodes and not _has_edge(cycle, applicable_target, mechanical):
                errors.append(f"{locator_prefix}: review must precede mechanical on applicable path")

    if not _has_edge(cycle, "fix", "review"):
        errors.append(f"{locator_prefix}: fix must re-enter at review")

    knowledge_route = _route_from(cycle, "knowledge-remediation")
    if knowledge_route is None:
        errors.append(f"{locator_prefix}: missing knowledge-remediation route")
    elif knowledge_route.cases.get("fix_and_proceed") != "mechanical-plan-checks":
        errors.append(
            f"{locator_prefix}: knowledge remediation fix_and_proceed must re-enter at mechanical producer"
        )

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


def _mechanical_nodes(graph: GraphDef, profile: LayerAssuranceProfile) -> list[str]:
    expected_output = f"change:{profile.checks_artifact}"
    nodes: list[str] = []
    for node_id, node in graph.nodes.items():
        if node.uses != _MECHANICAL_OPERATION:
            continue
        if node.with_.get("layer") != profile.layer:
            continue
        if node.with_.get("require_review") is not True:
            continue
        if expected_output not in node.outputs:
            continue
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
