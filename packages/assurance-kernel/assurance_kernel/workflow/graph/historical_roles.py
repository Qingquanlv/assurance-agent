"""One semantic historical assurance-role discovery manifest (D10 / §9.1)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from assurance_kernel.artifacts.models.assurance import LAYER_NAMES, LayerName
from assurance_kernel.verification.profiles import (
    LayerAssuranceProfile,
    iter_layer_assurance_profiles,
)
from assurance_kernel.workflow.graph.schema_v2 import GraphDef, WorkflowSchemaV2
from assurance_kernel.workflow.orchestration.dsl import (
    Literal as DslLiteral,
    ListLit,
    parse_expression,
    _walk,
)
from assurance_kernel.workflow.orchestration.schema import derive_alias

_APPLICABILITY_OPERATION = "operation:derive-plan-layer-applicability"
_DATA_KNOWLEDGE_PATH = "repo:.aa/data-knowledge.yaml"
_DATA_KNOWLEDGE_ALIAS = "data_knowledge"

HistoricalRoleDiscoveryCode = Literal[
    "missing_assurance_lineage",
    "missing_unique_role",
    "duplicate_role",
    "ambiguous_alias",
    "disconnected_lineage",
    "ambiguous_layer_binding",
]


@dataclass(frozen=True, slots=True)
class HistoricalRoleDiscoveryIssue:
    code: HistoricalRoleDiscoveryCode
    layer: LayerName | None
    owner: str
    locator: str
    detail: str


@dataclass(frozen=True, slots=True)
class DiscoveredHistoricalLayerRoles:
    layer: LayerName
    selection_event_node_id: str
    branch_call_node_id: str
    branch_graph_id: str
    cycle_call_node_id: str
    cycle_graph_id: str
    applicability_node_id: str
    reviewer_node_id: str
    plan_gate_node_id: str
    precondition_node_id: str
    codegen_node_id: str
    artifact_aliases: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DiscoveredHistoricalAssuranceRoles:
    root_graph_id: str
    assurance_call_node_id: str
    assurance_graph_id: str
    layers: tuple[DiscoveredHistoricalLayerRoles, ...]
    canonical_digest: str


def discover_historical_assurance_roles(
    schema: WorkflowSchemaV2,
) -> tuple[DiscoveredHistoricalAssuranceRoles | None, tuple[HistoricalRoleDiscoveryIssue, ...]]:
    """Discover one immutable role manifest from pinned semantic references."""
    issues: list[HistoricalRoleDiscoveryIssue] = []
    parents = _graph_call_parents(schema)

    layer_payloads: list[DiscoveredHistoricalLayerRoles] = []
    assurance_graph_ids: set[str] = set()
    for profile in iter_layer_assurance_profiles():
        layer_roles, layer_issues = _discover_layer(schema, profile, parents)
        issues.extend(layer_issues)
        if layer_roles is None:
            continue
        layer_payloads.append(layer_roles)
        # The assurance graph is the unique parent of the branch call node.
        branch_parents = parents.get((layer_roles.branch_graph_id), ())
        for parent_graph_id, _node_id in branch_parents:
            assurance_graph_ids.add(parent_graph_id)

    if not layer_payloads:
        # Zero-marker / no activation: still try to recover assurance lineage for
        # legacy_unwired classification consumers via selection predicates alone.
        structural, structural_issues = _discover_assurance_from_selection(schema, parents)
        issues.extend(structural_issues)
        if structural is None:
            issues.append(
                HistoricalRoleDiscoveryIssue(
                    code="missing_assurance_lineage",
                    layer=None,
                    owner="assurance",
                    locator="graphs",
                    detail="no discoverable assurance lineage from semantic markers",
                )
            )
            return None, _sorted_issues(issues)
        return structural, _sorted_issues(issues)

    if len(assurance_graph_ids) != 1:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="missing_assurance_lineage",
                layer=None,
                owner="assurance",
                locator="graphs",
                detail=f"expected one assurance graph parent, found {sorted(assurance_graph_ids)}",
            )
        )
        return None, _sorted_issues(issues)
    assurance_graph_id = next(iter(assurance_graph_ids))

    root = _discover_root_call(schema, assurance_graph_id, parents)
    if root is None:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="missing_assurance_lineage",
                layer=None,
                owner="root",
                locator=f"graph:{assurance_graph_id}",
                detail="assurance graph is not called from a unique root entrypoint graph",
            )
        )
        return None, _sorted_issues(issues)
    root_graph_id, assurance_call_node_id = root

    # Normalize layer order to canonical LAYER_NAMES for stable digests.
    by_layer = {item.layer: item for item in layer_payloads}
    ordered = tuple(by_layer[layer] for layer in LAYER_NAMES if layer in by_layer)
    manifest = DiscoveredHistoricalAssuranceRoles(
        root_graph_id=root_graph_id,
        assurance_call_node_id=assurance_call_node_id,
        assurance_graph_id=assurance_graph_id,
        layers=ordered,
        canonical_digest="",
    )
    digest = _manifest_digest(manifest)
    return (
        DiscoveredHistoricalAssuranceRoles(
            root_graph_id=manifest.root_graph_id,
            assurance_call_node_id=manifest.assurance_call_node_id,
            assurance_graph_id=manifest.assurance_graph_id,
            layers=manifest.layers,
            canonical_digest=digest,
        ),
        _sorted_issues(issues),
    )


def layer_roles_or_none(
    roles: DiscoveredHistoricalAssuranceRoles,
    layer: str,
) -> DiscoveredHistoricalLayerRoles | None:
    for item in roles.layers:
        if item.layer == layer:
            return item
    return None


def _discover_layer(
    schema: WorkflowSchemaV2,
    profile: LayerAssuranceProfile,
    parents: dict[str, tuple[tuple[str, str], ...]],
) -> tuple[DiscoveredHistoricalLayerRoles | None, list[HistoricalRoleDiscoveryIssue]]:
    layer = profile.layer
    issues: list[HistoricalRoleDiscoveryIssue] = []

    cycle_hits = _graphs_with_activation(schema, profile)
    structural = None
    if not cycle_hits:
        structural = _structural_lineage_from_selection(schema, profile, parents)
        if structural is None:
            return None, issues
        (
            assurance_graph_id,
            branch_call_node_id,
            branch_graph_id,
            cycle_call_node_id,
            cycle_graph_id,
        ) = structural
        # Zero-marker / selection-only lineage: keep selection usable, leave activation empty.
        aliases, alias_issues = _artifact_aliases(schema, profile)
        if alias_issues:
            # Gate may still exist for the layer even when unwired; ignore soft alias gaps here.
            aliases = ()
        branch = schema.graphs[branch_graph_id]
        precondition_ids = _gate_owner_nodes(branch, f"{layer}-codegen-precondition-gate")
        codegen_ids = _codegen_nodes(branch, layer)
        return (
            DiscoveredHistoricalLayerRoles(
                layer=layer,
                selection_event_node_id=branch_call_node_id,
                branch_call_node_id=branch_call_node_id,
                branch_graph_id=branch_graph_id,
                cycle_call_node_id=cycle_call_node_id,
                cycle_graph_id=cycle_graph_id,
                applicability_node_id="",
                reviewer_node_id="",
                plan_gate_node_id="",
                precondition_node_id=precondition_ids[0] if len(precondition_ids) == 1 else "",
                codegen_node_id=codegen_ids[0] if len(codegen_ids) == 1 else "",
                artifact_aliases=aliases,
            ),
            issues,
        )

    if len(cycle_hits) != 1:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="disconnected_lineage",
                layer=layer,
                owner="review-cycle",
                locator="graphs",
                detail=f"expected one cycle graph for layer, found {sorted(cycle_hits)}",
            )
        )
        return None, issues
    cycle_graph_id = next(iter(cycle_hits))
    cycle = schema.graphs[cycle_graph_id]

    local_issues: list[HistoricalRoleDiscoveryIssue] = []
    applicability = _unique_node(
        _applicability_nodes(cycle, layer),
        layer=layer,
        owner="applicability",
        locator=f"graph:{cycle_graph_id}",
        name="applicability",
        issues=local_issues,
    )
    plan_gate = _unique_node(
        _gate_owner_nodes(cycle, profile.gate_id),
        layer=layer,
        owner="plan-gate",
        locator=f"graph:{cycle_graph_id}",
        name="plan gate owner",
        issues=local_issues,
    )
    reviewer = _unique_node(
        _reviewer_nodes(cycle, layer),
        layer=layer,
        owner="reviewer",
        locator=f"graph:{cycle_graph_id}",
        name="reviewer",
        issues=local_issues,
    )
    # Duplicate roles fail closed; mere absence is handled by callers/classifier.
    dup_issues = [issue for issue in local_issues if issue.code == "duplicate_role"]
    if dup_issues:
        issues.extend(dup_issues)
        return None, issues
    if applicability is None or plan_gate is None or reviewer is None:
        issues.extend(local_issues)
        return None, issues

    branch_parents = parents.get(cycle_graph_id, ())
    if len(branch_parents) != 1:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="disconnected_lineage",
                layer=layer,
                owner="branch",
                locator=f"graph:{cycle_graph_id}",
                detail=f"expected one branch caller of cycle, found {len(branch_parents)}",
            )
        )
        return None, issues
    branch_graph_id, cycle_call_node_id = branch_parents[0]
    branch = schema.graphs[branch_graph_id]

    precondition = _unique_node(
        _gate_owner_nodes(branch, f"{layer}-codegen-precondition-gate"),
        layer=layer,
        owner="codegen-precheck",
        locator=f"graph:{branch_graph_id}",
        name="codegen precondition",
        issues=issues,
    )
    codegen = _unique_node(
        _codegen_nodes(branch, layer),
        layer=layer,
        owner="codegen",
        locator=f"graph:{branch_graph_id}",
        name="codegen",
        issues=issues,
    )
    if precondition is None or codegen is None:
        return None, issues

    assurance_parents = parents.get(branch_graph_id, ())
    if len(assurance_parents) != 1:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="disconnected_lineage",
                layer=layer,
                owner="assurance",
                locator=f"graph:{branch_graph_id}",
                detail=f"expected one assurance caller of branch, found {len(assurance_parents)}",
            )
        )
        return None, issues
    assurance_graph_id, branch_call_node_id = assurance_parents[0]
    assurance = schema.graphs[assurance_graph_id]
    selection_node = assurance.nodes.get(branch_call_node_id)
    if selection_node is None or not selection_node.when:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="missing_unique_role",
                layer=layer,
                owner="selection",
                locator=f"graph:{assurance_graph_id}.nodes.{branch_call_node_id}",
                detail="branch call is missing a selection predicate",
            )
        )
        return None, issues
    bound_layers = _layers_mentioned_in_predicate(selection_node.when)
    if layer not in bound_layers and bound_layers:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="ambiguous_layer_binding",
                layer=layer,
                owner="selection",
                locator=f"graph:{assurance_graph_id}.nodes.{branch_call_node_id}.when",
                detail=f"selection predicate mentions {sorted(bound_layers)}, not {layer}",
            )
        )
        return None, issues

    aliases, alias_issues = _artifact_aliases(schema, profile)
    issues.extend(alias_issues)
    if alias_issues:
        return None, issues

    return (
        DiscoveredHistoricalLayerRoles(
            layer=layer,
            selection_event_node_id=branch_call_node_id,
            branch_call_node_id=branch_call_node_id,
            branch_graph_id=branch_graph_id,
            cycle_call_node_id=cycle_call_node_id,
            cycle_graph_id=cycle_graph_id,
            applicability_node_id=applicability,
            reviewer_node_id=reviewer,
            plan_gate_node_id=plan_gate,
            precondition_node_id=precondition,
            codegen_node_id=codegen,
            artifact_aliases=aliases,
        ),
        issues,
    )


def _structural_lineage_from_selection(
    schema: WorkflowSchemaV2,
    profile: LayerAssuranceProfile,
    parents: dict[str, tuple[tuple[str, str], ...]],
) -> tuple[str, str, str, str, str] | None:
    """Recover selection/branch/cycle when activation markers are absent."""
    candidates: list[tuple[str, str, str]] = []
    for graph_id, graph in schema.graphs.items():
        for node_id, node in graph.nodes.items():
            if not node.uses.startswith("graph:") or not node.when:
                continue
            mentioned = _layers_mentioned_in_predicate(node.when)
            if mentioned != {profile.layer}:
                continue
            branch_id = node.uses.removeprefix("graph:")
            if branch_id not in schema.graphs:
                continue
            candidates.append((graph_id, node_id, branch_id))
    if len(candidates) != 1:
        return None
    assurance_graph_id, branch_call_node_id, branch_graph_id = candidates[0]
    branch = schema.graphs[branch_graph_id]
    cycle_calls = [
        (node_id, node.uses.removeprefix("graph:"))
        for node_id, node in branch.nodes.items()
        if node.uses.startswith("graph:")
    ]
    if len(cycle_calls) != 1:
        return None
    cycle_call_node_id, cycle_graph_id = cycle_calls[0]
    if cycle_graph_id not in schema.graphs:
        return None
    _ = parents
    return (
        assurance_graph_id,
        branch_call_node_id,
        branch_graph_id,
        cycle_call_node_id,
        cycle_graph_id,
    )


def _discover_assurance_from_selection(
    schema: WorkflowSchemaV2,
    parents: dict[str, tuple[tuple[str, str], ...]],
) -> tuple[DiscoveredHistoricalAssuranceRoles | None, list[HistoricalRoleDiscoveryIssue]]:
    """Recover assurance/root identity when activation markers are absent."""
    issues: list[HistoricalRoleDiscoveryIssue] = []
    candidates: list[tuple[str, str, str]] = []
    for graph_id, graph in schema.graphs.items():
        for node_id, node in graph.nodes.items():
            if not node.uses.startswith("graph:") or not node.when:
                continue
            mentioned = _layers_mentioned_in_predicate(node.when)
            if len(mentioned) != 1:
                continue
            branch_id = node.uses.removeprefix("graph:")
            if branch_id not in schema.graphs:
                continue
            candidates.append((graph_id, node_id, next(iter(mentioned))))
    if not candidates:
        return None, issues
    assurance_ids = {item[0] for item in candidates}
    if len(assurance_ids) != 1:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="missing_assurance_lineage",
                layer=None,
                owner="assurance",
                locator="graphs",
                detail=f"ambiguous assurance graphs from selection predicates: {sorted(assurance_ids)}",
            )
        )
        return None, issues
    assurance_graph_id = next(iter(assurance_ids))
    root = _discover_root_call(schema, assurance_graph_id, parents)
    if root is None:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="missing_assurance_lineage",
                layer=None,
                owner="root",
                locator=f"graph:{assurance_graph_id}",
                detail="selection-only assurance graph has no unique root caller",
            )
        )
        return None, issues
    root_graph_id, assurance_call_node_id = root
    manifest = DiscoveredHistoricalAssuranceRoles(
        root_graph_id=root_graph_id,
        assurance_call_node_id=assurance_call_node_id,
        assurance_graph_id=assurance_graph_id,
        layers=(),
        canonical_digest="",
    )
    return (
        DiscoveredHistoricalAssuranceRoles(
            root_graph_id=manifest.root_graph_id,
            assurance_call_node_id=manifest.assurance_call_node_id,
            assurance_graph_id=manifest.assurance_graph_id,
            layers=(),
            canonical_digest=_manifest_digest(manifest),
        ),
        issues,
    )


def _discover_root_call(
    schema: WorkflowSchemaV2,
    assurance_graph_id: str,
    parents: dict[str, tuple[tuple[str, str], ...]],
) -> tuple[str, str] | None:
    all_parents = parents.get(assurance_graph_id, ())
    full = schema.entrypoints.get("full")
    if full is not None:
        matches = [
            (parent_graph_id, node_id)
            for parent_graph_id, node_id in all_parents
            if parent_graph_id == full.graph
        ]
        if len(matches) == 1:
            return matches[0]
    # Fall back to a unique parent when entrypoint metadata is sparse.
    if len(all_parents) == 1:
        return all_parents[0]
    return None


def _graphs_with_activation(schema: WorkflowSchemaV2, profile: LayerAssuranceProfile) -> set[str]:
    """Identify cycle graphs by the explicit plan-review gate owner.

    Branch-level preflight applicability and reviewer-only graphs are not enough:
    zero-marker legacy graphs must omit the layer so classification can report
    ``legacy_unwired``.
    """
    hits: set[str] = set()
    for graph_id, graph in schema.graphs.items():
        if _gate_owner_nodes(graph, profile.gate_id):
            hits.add(graph_id)
    return hits


def _graph_call_parents(schema: WorkflowSchemaV2) -> dict[str, tuple[tuple[str, str], ...]]:
    mapping: dict[str, list[tuple[str, str]]] = {}
    for graph_id, graph in schema.graphs.items():
        for node_id, node in graph.nodes.items():
            if not node.uses.startswith("graph:"):
                continue
            callee = node.uses.removeprefix("graph:")
            mapping.setdefault(callee, []).append((graph_id, node_id))
    return {callee: tuple(parents) for callee, parents in mapping.items()}


def _applicability_nodes(graph: GraphDef, layer: str) -> list[str]:
    return [
        node_id
        for node_id, node in graph.nodes.items()
        if node.uses == _APPLICABILITY_OPERATION and node.with_.get("layer") == layer
    ]


def _gate_owner_nodes(graph: GraphDef, gate_id: str) -> list[str]:
    return [
        node_id
        for node_id, node in graph.nodes.items()
        if node.uses == "builtin:gate" and node.with_.get("gate") == gate_id
    ]


def _reviewer_nodes(graph: GraphDef, layer: str) -> list[str]:
    suffix = f"aa-{layer}-plan-reviewer"
    return [
        node_id
        for node_id, node in graph.nodes.items()
        if node.uses == f"skill:{suffix}" or node.uses.endswith(f":{suffix}")
    ]


def _codegen_nodes(graph: GraphDef, layer: str) -> list[str]:
    suffix = f"aa-{layer}-codegen"
    return [
        node_id
        for node_id, node in graph.nodes.items()
        if (node.uses == f"skill:{suffix}" or node.uses.endswith(f":{suffix}")) and "fixer" not in node.uses
    ]


def _artifact_aliases(
    schema: WorkflowSchemaV2,
    profile: LayerAssuranceProfile,
) -> tuple[tuple[tuple[str, str], ...], list[HistoricalRoleDiscoveryIssue]]:
    issues: list[HistoricalRoleDiscoveryIssue] = []
    gate = schema.gates.get(profile.gate_id)
    if gate is None:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="missing_unique_role",
                layer=profile.layer,
                owner="plan-gate",
                locator=f"gate:{profile.gate_id}",
                detail="missing plan gate definition",
            )
        )
        return (), issues
    aliases = [(entry.path, entry.alias) for entry in gate.reads]
    by_alias: dict[str, list[str]] = {}
    for path, alias in aliases:
        by_alias.setdefault(alias, []).append(path)
    for alias, paths in sorted(by_alias.items()):
        if len(paths) > 1:
            issues.append(
                HistoricalRoleDiscoveryIssue(
                    code="ambiguous_alias",
                    layer=profile.layer,
                    owner="plan-gate",
                    locator=f"gate:{profile.gate_id}:reads",
                    detail=f"alias {alias!r} resolves to multiple paths {paths}",
                )
            )
    expected = {
        profile.review_artifact: profile.review_alias,
        profile.checks_artifact: derive_alias(profile.checks_artifact),
        _DATA_KNOWLEDGE_PATH: _DATA_KNOWLEDGE_ALIAS,
    }
    by_path = {path: alias for path, alias in aliases}
    for path, alias in expected.items():
        if path not in by_path:
            issues.append(
                HistoricalRoleDiscoveryIssue(
                    code="missing_unique_role",
                    layer=profile.layer,
                    owner="plan-gate",
                    locator=f"gate:{profile.gate_id}:reads",
                    detail=f"gate reads missing required path {path!r}",
                )
            )
        elif by_path[path] != alias:
            issues.append(
                HistoricalRoleDiscoveryIssue(
                    code="ambiguous_alias",
                    layer=profile.layer,
                    owner="plan-gate",
                    locator=f"gate:{profile.gate_id}:reads",
                    detail=f"path {path!r} alias expected {alias!r}, got {by_path[path]!r}",
                )
            )
    return tuple(sorted(aliases)), issues


def _unique_node(
    found: list[str],
    *,
    layer: LayerName,
    owner: str,
    locator: str,
    name: str,
    issues: list[HistoricalRoleDiscoveryIssue],
) -> str | None:
    if len(found) == 1:
        return found[0]
    if len(found) == 0:
        issues.append(
            HistoricalRoleDiscoveryIssue(
                code="missing_unique_role",
                layer=layer,
                owner=owner,
                locator=locator,
                detail=f"expected exactly one {name}, found 0",
            )
        )
        return None
    issues.append(
        HistoricalRoleDiscoveryIssue(
            code="duplicate_role",
            layer=layer,
            owner=owner,
            locator=locator,
            detail=f"expected exactly one {name}, found {len(found)}: {found}",
        )
    )
    return None


def _layers_mentioned_in_predicate(text: str) -> set[LayerName]:
    try:
        expr = parse_expression(text)
    except Exception:  # noqa: BLE001 - treat parse failure as no binding
        return set()
    mentioned: set[LayerName] = set()
    layer_set = set(LAYER_NAMES)
    for node in _walk(expr):
        if isinstance(node, DslLiteral) and isinstance(node.value, str) and node.value in layer_set:
            mentioned.add(node.value)  # type: ignore[arg-type]
        if isinstance(node, ListLit):
            for item in node.elements:
                if isinstance(item, DslLiteral) and isinstance(item.value, str) and item.value in layer_set:
                    mentioned.add(item.value)  # type: ignore[arg-type]
    return mentioned


def _manifest_digest(roles: DiscoveredHistoricalAssuranceRoles) -> str:
    payload = {
        "root_graph_id": roles.root_graph_id,
        "assurance_call_node_id": roles.assurance_call_node_id,
        "assurance_graph_id": roles.assurance_graph_id,
        "layers": [
            {
                "layer": item.layer,
                "selection_event_node_id": item.selection_event_node_id,
                "branch_call_node_id": item.branch_call_node_id,
                "branch_graph_id": item.branch_graph_id,
                "cycle_call_node_id": item.cycle_call_node_id,
                "cycle_graph_id": item.cycle_graph_id,
                "applicability_node_id": item.applicability_node_id,
                "reviewer_node_id": item.reviewer_node_id,
                "plan_gate_node_id": item.plan_gate_node_id,
                "precondition_node_id": item.precondition_node_id,
                "codegen_node_id": item.codegen_node_id,
                "artifact_aliases": list(item.artifact_aliases),
            }
            for item in roles.layers
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sorted_issues(
    issues: list[HistoricalRoleDiscoveryIssue],
) -> tuple[HistoricalRoleDiscoveryIssue, ...]:
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


def fixture_roles_from_schema(schema: WorkflowSchemaV2) -> DiscoveredHistoricalAssuranceRoles:
    """Build explicit fixture roles for direct compiler/classifier controls."""
    roles, issues = discover_historical_assurance_roles(schema)
    if roles is None:
        detail = "; ".join(f"{issue.code}:{issue.detail}" for issue in issues) or "discovery failed"
        raise ValueError(detail)
    return roles


__all__ = [
    "DiscoveredHistoricalAssuranceRoles",
    "DiscoveredHistoricalLayerRoles",
    "HistoricalRoleDiscoveryCode",
    "HistoricalRoleDiscoveryIssue",
    "discover_historical_assurance_roles",
    "fixture_roles_from_schema",
    "layer_roles_or_none",
]
