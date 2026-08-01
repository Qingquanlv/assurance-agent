"""V4/v5 topology compatibility audit and typed live-resume guard (D10 / D14).

A topology receipt proves v6-audited topology safety only. It never binds
``runtime_commit_safety/v1``. Remaining commit-safety-bearing work on a
v1–v5 root stops with ``legacy_commit_safety_semantics_unbound``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_agent.artifacts.models.assurance import LAYER_NAMES
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.exceptions import AaError
from assurance_agent.verification.generated_files import get_generated_files_contract
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.core.graph_events import TopologySafetyCompatibilityRecordedEvent
from assurance_agent.workflow.graph.compiler import CompiledWorkflow
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.definition_pinning import (
    topology_semantics_snapshot_relpath,
)
from assurance_agent.workflow.graph.historical_roles import (
    DiscoveredHistoricalAssuranceRoles,
    layer_roles_or_none,
)
from assurance_agent.workflow.graph.historical_topology_v6 import (
    LayerTopologySpecView,
    V6LayerClassification,
    classify_historical_layer_topology_v6,
)
from assurance_agent.workflow.graph.models import GraphProjection
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2
from assurance_agent.workflow.graph.topology_semantics import (
    topology_safety_semantics_bytes,
    topology_safety_semantics_digest,
    topology_safety_semantics_object_digest,
)

LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND = "legacy_commit_safety_semantics_unbound"
TOPOLOGY_AUDIT_UNSAFE = "legacy_topology_audit_unsafe"
TOPOLOGY_PROFILE_UNRECONSTRUCTABLE = "legacy_topology_profile_unreconstructable"
TOPOLOGY_RECEIPT_CORRUPT = "topology_compatibility_receipt_corrupt"

AuditResult = Literal["wired", "partial", "legacy_unwired", "blocked"]
RemainingWorkClass = Literal["none", "report_terminal_only", "commit_safety_bearing"]
ResumeCompatibilityReason = Literal[
    "legacy_commit_safety_semantics_unbound",
    "legacy_topology_audit_unsafe",
    "legacy_topology_profile_unreconstructable",
    "topology_compatibility_receipt_corrupt",
]


class ResumeCompatibilityError(AaError):
    """Typed resume barrier failure; callers must read ``decision``, not parse text."""

    def __init__(self, decision: ResumeCompatibilityDecision) -> None:
        self.decision = decision
        self.reason_code = decision.reason or "resume_compatibility_blocked"
        super().__init__(self.reason_code)


class TopologyCompatibilityReceiptV1(StrictWireModel):
    schema_version: Literal["1"]
    receipt_id: str
    root_invocation_id: str
    event_schema_version: int
    graph_digest: str
    ingest_catalog_digest: str
    contract_digests: dict[str, str]
    assurance_profile_digest: str
    discovered_roles_digest: str
    topology_safety_semantics_object_id: str
    topology_safety_semantics_digest: str
    audit_result: Literal["wired"]
    selected_layers: tuple[str, ...]
    reachable_layers: tuple[str, ...]
    per_layer_results: dict[str, str]
    reachable_set_digest: str
    source_sequence: int


class ResumeCompatibilityDecision(StrictWireModel):
    schema_version: Literal["1"]
    allowed: bool
    reason: ResumeCompatibilityReason | None = None
    audit_triggered: bool = False
    requires_receipt: bool = False
    receipt_id: str | None = None
    remaining_work_class: RemainingWorkClass = "none"
    event_schema_version: int = 0
    root_invocation_id: str = ""


@dataclass(frozen=True, slots=True)
class RemainingWorkAssessment:
    work_class: RemainingWorkClass
    reachable_node_ids: frozenset[str]
    reachable_layers: tuple[str, ...]
    reachable_set_digest: str
    commit_safety_targets: tuple[str, ...]
    audit_trigger_roles: tuple[str, ...]


def stage_audit_topology_semantics(change_dir: Path) -> tuple[str, str, bytes]:
    """Stage current v6 topology-semantics bytes for a legacy audit receipt."""
    object_id = topology_safety_semantics_object_digest()
    digest = topology_safety_semantics_digest()
    data = topology_safety_semantics_bytes()
    rel = topology_semantics_snapshot_relpath(object_id)
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing != data:
            raise ResumeCompatibilityError(
                ResumeCompatibilityDecision(
                    schema_version="1",
                    allowed=False,
                    reason=TOPOLOGY_RECEIPT_CORRUPT,
                    audit_triggered=True,
                )
            )
        return object_id, digest, data
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return object_id, digest, data


def assess_remaining_work(
    *,
    schema: WorkflowSchemaV2,
    compiled: CompiledWorkflow,
    contracts: ExecutionContractCatalog,
    historical_roles: DiscoveredHistoricalAssuranceRoles | None,
    projection: GraphProjection,
    selected_layers: Sequence[str],
) -> RemainingWorkAssessment:
    """Classify remaining reachable work without trusting frozen display status."""
    selected = tuple(layer for layer in LAYER_NAMES if layer in set(selected_layers))
    pending_nodes = _pending_or_open_node_ids(projection)
    role_codegen: dict[str, str] = {}
    role_triggers: list[str] = []
    if historical_roles is not None:
        for layer in selected or LAYER_NAMES:
            roles = layer_roles_or_none(historical_roles, layer)
            if roles is None:
                continue
            if roles.codegen_node_id:
                role_codegen[layer] = roles.codegen_node_id

    reachable_nodes = set(pending_nodes)
    # Include discovered codegen roles that are not yet succeeded for selected layers.
    for layer, node_id in role_codegen.items():
        if not _node_succeeded(projection, node_id):
            reachable_nodes.add(node_id)
            role_triggers.append(f"reachable:codegen:{layer}:{node_id}")

    commit_targets: list[str] = []
    for node_id in sorted(reachable_nodes):
        uses = _uses_for_node(compiled, node_id)
        if uses is None:
            # Ambiguous / unknown node identity triggers audit rather than exempting.
            role_triggers.append(f"ambiguous:node:{node_id}")
            continue
        if _is_commit_safety_bearing_target(uses, contracts):
            commit_targets.append(uses)
        elif _contract_writes_private_assurance_root(uses, contracts):
            role_triggers.append(f"contract_write:{uses}")
            commit_targets.append(uses)

    # Succeeded-but-uncommitted assurance write / effect seams.
    for task in projection.tasks.values():
        if task.status != "succeeded":
            continue
        uses = _uses_for_node(compiled, task.node_id) or ""
        if not _is_commit_safety_bearing_target(uses, contracts) and not _looks_like_assurance_codegen(
            uses
        ):
            continue
        if not task.outputs_committed or task.durable_effects:
            commit_targets.append(uses or task.node_id)
            role_triggers.append(f"uncommitted:{task.task_id}")

    reachable_layers = tuple(
        layer for layer in (selected or LAYER_NAMES) if layer in role_codegen and role_codegen[layer] in reachable_nodes
    )
    digest = _digest_sorted(
        {
            "nodes": sorted(reachable_nodes),
            "layers": list(reachable_layers),
            "commit_targets": sorted(set(commit_targets)),
        }
    )
    if commit_targets:
        work_class: RemainingWorkClass = "commit_safety_bearing"
    elif reachable_nodes:
        work_class = "report_terminal_only"
    else:
        work_class = "none"
    return RemainingWorkAssessment(
        work_class=work_class,
        reachable_node_ids=frozenset(reachable_nodes),
        reachable_layers=reachable_layers,
        reachable_set_digest=digest,
        commit_safety_targets=tuple(sorted(set(commit_targets))),
        audit_trigger_roles=tuple(sorted(set(role_triggers))),
    )


def audit_topology_for_resume(
    *,
    schema: WorkflowSchemaV2,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
    selected_layers: Sequence[str],
    reachable_layers: Sequence[str],
) -> tuple[AuditResult, dict[str, str], tuple[V6LayerClassification, ...]]:
    """Run v6 classifier for audit; never trusts frozen v4/v5 display status."""
    layers = tuple(dict.fromkeys([*selected_layers, *reachable_layers]))
    if not layers:
        layers = tuple(LAYER_NAMES)
    results: list[V6LayerClassification] = []
    per_layer: dict[str, str] = {}
    for layer in layers:
        if layer not in LAYER_NAMES:
            per_layer[layer] = "partial"
            continue
        profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
        spec = LayerTopologySpecView(
            layer=profile.layer,
            review_artifact=profile.review_artifact,
            review_alias=profile.review_alias,
            checks_artifact=profile.checks_artifact,
            gate_id=profile.gate_id,
        )
        classified = classify_historical_layer_topology_v6(
            schema, spec, historical_roles=historical_roles
        )
        results.append(classified)
        per_layer[layer] = classified.status
    statuses = {item.status for item in results}
    if "partial" in statuses:
        return "partial", per_layer, tuple(results)
    if statuses and statuses <= {"wired"}:
        return "wired", per_layer, tuple(results)
    if statuses <= {"legacy_unwired"} or not statuses:
        return "legacy_unwired", per_layer, tuple(results)
    return "partial", per_layer, tuple(results)


def build_topology_compatibility_receipt(
    *,
    projection: GraphProjection,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
    topology_object_id: str,
    topology_digest: str,
    selected_layers: Sequence[str],
    reachable_layers: Sequence[str],
    per_layer_results: Mapping[str, str],
    reachable_set_digest: str,
    source_sequence: int,
) -> TopologyCompatibilityReceiptV1:
    """Build a bound receipt; ``receipt_id`` digests the identity payload."""
    payload = {
        "schema_version": "1",
        "root_invocation_id": projection.invocation_id
        if projection.parent_invocation_id is None
        else (projection.parent_invocation_id or projection.invocation_id),
        "event_schema_version": projection.event_schema_version,
        "graph_digest": projection.graph_digest,
        "ingest_catalog_digest": projection.ingest_catalog_digest,
        "contract_digests": dict(sorted(projection.contract_digests.items())),
        "assurance_profile_digest": projection.assurance_profile_digest,
        "discovered_roles_digest": historical_roles.canonical_digest,
        "topology_safety_semantics_object_id": topology_object_id,
        "topology_safety_semantics_digest": topology_digest,
        "audit_result": "wired",
        "selected_layers": list(selected_layers),
        "reachable_layers": list(reachable_layers),
        "per_layer_results": dict(sorted(per_layer_results.items())),
        "reachable_set_digest": reachable_set_digest,
        "source_sequence": source_sequence,
    }
    root_id = (
        projection.invocation_id
        if projection.parent_invocation_id is None
        else projection.parent_invocation_id
    )
    payload["root_invocation_id"] = root_id
    receipt_id = _digest_sorted(payload)
    return TopologyCompatibilityReceiptV1(
        schema_version="1",
        receipt_id=receipt_id,
        root_invocation_id=root_id,
        event_schema_version=projection.event_schema_version,
        graph_digest=projection.graph_digest,
        ingest_catalog_digest=projection.ingest_catalog_digest,
        contract_digests=dict(sorted(projection.contract_digests.items())),
        assurance_profile_digest=projection.assurance_profile_digest,
        discovered_roles_digest=historical_roles.canonical_digest,
        topology_safety_semantics_object_id=topology_object_id,
        topology_safety_semantics_digest=topology_digest,
        audit_result="wired",
        selected_layers=tuple(selected_layers),
        reachable_layers=tuple(reachable_layers),
        per_layer_results=dict(sorted(per_layer_results.items())),
        reachable_set_digest=reachable_set_digest,
        source_sequence=source_sequence,
    )


def receipt_to_event(
    receipt: TopologyCompatibilityReceiptV1,
    *,
    checkpoint_ns: str,
) -> TopologySafetyCompatibilityRecordedEvent:
    return TopologySafetyCompatibilityRecordedEvent(
        type="topology_safety_compatibility_recorded",
        invocation_id=receipt.root_invocation_id,
        checkpoint_ns=checkpoint_ns,
        receipt_id=receipt.receipt_id,
        event_schema_version=receipt.event_schema_version,
        graph_digest=receipt.graph_digest,
        ingest_catalog_digest=receipt.ingest_catalog_digest,
        contract_digests=dict(receipt.contract_digests),
        assurance_profile_digest=receipt.assurance_profile_digest,
        discovered_roles_digest=receipt.discovered_roles_digest,
        topology_safety_semantics_object_id=receipt.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=receipt.topology_safety_semantics_digest,
        audit_result=receipt.audit_result,
        selected_layers=list(receipt.selected_layers),
        reachable_layers=list(receipt.reachable_layers),
        per_layer_results=dict(receipt.per_layer_results),
        reachable_set_digest=receipt.reachable_set_digest,
        source_sequence=receipt.source_sequence,
    )


def event_to_receipt(event: TopologySafetyCompatibilityRecordedEvent) -> TopologyCompatibilityReceiptV1:
    if event.audit_result != "wired":
        raise ResumeCompatibilityError(
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=False,
                reason=TOPOLOGY_RECEIPT_CORRUPT,
                audit_triggered=True,
                requires_receipt=True,
                receipt_id=event.receipt_id,
                root_invocation_id=event.invocation_id,
                event_schema_version=event.event_schema_version,
            )
        )
    return TopologyCompatibilityReceiptV1(
        schema_version="1",
        receipt_id=event.receipt_id,
        root_invocation_id=event.invocation_id,
        event_schema_version=event.event_schema_version,
        graph_digest=event.graph_digest,
        ingest_catalog_digest=event.ingest_catalog_digest,
        contract_digests=dict(sorted(event.contract_digests.items())),
        assurance_profile_digest=event.assurance_profile_digest,
        discovered_roles_digest=event.discovered_roles_digest,
        topology_safety_semantics_object_id=event.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=event.topology_safety_semantics_digest,
        audit_result="wired",
        selected_layers=tuple(event.selected_layers),
        reachable_layers=tuple(event.reachable_layers),
        per_layer_results=dict(sorted(event.per_layer_results.items())),
        reachable_set_digest=event.reachable_set_digest,
        source_sequence=event.source_sequence,
    )


def verify_receipt_bindings(
    receipt: TopologyCompatibilityReceiptV1,
    *,
    projection: GraphProjection,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
    topology_object_id: str,
    topology_digest: str,
    reachable_set_digest: str,
) -> None:
    """Exact reuse requires byte-identical bindings; drift is corruption."""
    root_id = (
        projection.invocation_id
        if projection.parent_invocation_id is None
        else projection.parent_invocation_id
    )
    expected = build_topology_compatibility_receipt(
        projection=projection.model_copy(
            update={"invocation_id": root_id, "parent_invocation_id": None}
        )
        if projection.parent_invocation_id is not None
        else projection,
        historical_roles=historical_roles,
        topology_object_id=topology_object_id,
        topology_digest=topology_digest,
        selected_layers=receipt.selected_layers,
        reachable_layers=receipt.reachable_layers,
        per_layer_results=receipt.per_layer_results,
        reachable_set_digest=reachable_set_digest,
        source_sequence=receipt.source_sequence,
    )
    if (
        receipt.receipt_id != expected.receipt_id
        or receipt.model_dump(mode="json") != expected.model_dump(mode="json")
    ):
        # Allow source_sequence to be the originally recorded sequence when
        # reachable digest/bindings otherwise match the recorded identity.
        if (
            receipt.root_invocation_id == root_id
            and receipt.graph_digest == projection.graph_digest
            and receipt.discovered_roles_digest == historical_roles.canonical_digest
            and receipt.topology_safety_semantics_object_id == topology_object_id
            and receipt.topology_safety_semantics_digest == topology_digest
            and receipt.reachable_set_digest == reachable_set_digest
            and receipt.assurance_profile_digest == projection.assurance_profile_digest
            and dict(sorted(receipt.contract_digests.items()))
            == dict(sorted(projection.contract_digests.items()))
            and receipt.ingest_catalog_digest == projection.ingest_catalog_digest
            and receipt.event_schema_version == projection.event_schema_version
            and receipt.audit_result == "wired"
        ):
            return
        raise ResumeCompatibilityError(
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=False,
                reason=TOPOLOGY_RECEIPT_CORRUPT,
                audit_triggered=True,
                requires_receipt=True,
                receipt_id=receipt.receipt_id,
                root_invocation_id=root_id,
                event_schema_version=projection.event_schema_version,
            )
        )


def evaluate_resume_compatibility(
    *,
    projection: GraphProjection,
    compiled: CompiledWorkflow,
    contracts: ExecutionContractCatalog,
    historical_roles: DiscoveredHistoricalAssuranceRoles | None,
    existing_receipt: TopologyCompatibilityReceiptV1 | None,
    profile_reconstructable: bool,
    change_dir: Path | None = None,
) -> tuple[ResumeCompatibilityDecision, TopologyCompatibilityReceiptV1 | None]:
    """Evaluate legacy resume authorization.

    Returns ``(decision, new_receipt_to_append_or_none)``. Exact existing receipt
    reuse yields ``new_receipt is None``.
    """
    if projection.event_schema_version >= 6:
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=True,
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
                remaining_work_class="none",
            ),
            None,
        )
    if projection.event_schema_version < 4:
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=True,
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
                remaining_work_class="none",
            ),
            None,
        )

    selected = _selected_layers(projection.params)
    assessment = assess_remaining_work(
        schema=compiled.schema,
        compiled=compiled,
        contracts=contracts,
        historical_roles=historical_roles,
        projection=projection,
        selected_layers=selected,
    )

    # Terminal / no remaining work: no receipt required.
    if projection.terminal is not None or assessment.work_class == "none":
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=True,
                audit_triggered=False,
                requires_receipt=False,
                remaining_work_class="none",
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
                receipt_id=existing_receipt.receipt_id if existing_receipt else None,
            ),
            None,
        )

    assurance_triggers = tuple(
        item
        for item in assessment.audit_trigger_roles
        if item.startswith(("reachable:codegen:", "contract_write:", "uncommitted:"))
    )
    audit_needed = bool(assurance_triggers) or assessment.work_class == "commit_safety_bearing"
    if not audit_needed:
        # Non-assurance roots, or proven absence of reachable assurance-codegen roles.
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=True,
                audit_triggered=False,
                requires_receipt=False,
                remaining_work_class=assessment.work_class,
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
            ),
            None,
        )

    if historical_roles is None:
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=False,
                reason=TOPOLOGY_AUDIT_UNSAFE,
                audit_triggered=True,
                requires_receipt=True,
                remaining_work_class=assessment.work_class,
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
            ),
            None,
        )

    if not profile_reconstructable:
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=False,
                reason=TOPOLOGY_PROFILE_UNRECONSTRUCTABLE,
                audit_triggered=True,
                requires_receipt=True,
                remaining_work_class=assessment.work_class,
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
            ),
            None,
        )

    audit_result, per_layer, _ = audit_topology_for_resume(
        schema=compiled.schema,
        historical_roles=historical_roles,
        selected_layers=selected,
        reachable_layers=assessment.reachable_layers,
    )
    if audit_result != "wired":
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=False,
                reason=TOPOLOGY_AUDIT_UNSAFE,
                audit_triggered=True,
                requires_receipt=True,
                remaining_work_class=assessment.work_class,
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
            ),
            None,
        )

    if change_dir is None:
        object_id = topology_safety_semantics_object_digest()
        topo_digest = topology_safety_semantics_digest()
    else:
        object_id, topo_digest, _ = stage_audit_topology_semantics(change_dir)

    if existing_receipt is not None:
        verify_receipt_bindings(
            existing_receipt,
            projection=projection,
            historical_roles=historical_roles,
            topology_object_id=object_id,
            topology_digest=topo_digest,
            reachable_set_digest=assessment.reachable_set_digest,
        )
        receipt = existing_receipt
        new_receipt = None
    else:
        receipt = build_topology_compatibility_receipt(
            projection=projection
            if projection.parent_invocation_id is None
            else projection.model_copy(
                update={
                    "invocation_id": projection.parent_invocation_id,
                    "parent_invocation_id": None,
                }
            ),
            historical_roles=historical_roles,
            topology_object_id=object_id,
            topology_digest=topo_digest,
            selected_layers=selected,
            reachable_layers=assessment.reachable_layers,
            per_layer_results=per_layer,
            reachable_set_digest=assessment.reachable_set_digest,
            source_sequence=projection.event_seq,
        )
        new_receipt = receipt

    # Topology receipt is necessary but never sufficient for commit-safety work.
    if assessment.work_class == "commit_safety_bearing":
        return (
            ResumeCompatibilityDecision(
                schema_version="1",
                allowed=False,
                reason=LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
                audit_triggered=True,
                requires_receipt=True,
                receipt_id=receipt.receipt_id,
                remaining_work_class="commit_safety_bearing",
                event_schema_version=projection.event_schema_version,
                root_invocation_id=_root_id(projection),
            ),
            new_receipt,
        )

    return (
        ResumeCompatibilityDecision(
            schema_version="1",
            allowed=True,
            audit_triggered=True,
            requires_receipt=True,
            receipt_id=receipt.receipt_id,
            remaining_work_class="report_terminal_only",
            event_schema_version=projection.event_schema_version,
            root_invocation_id=_root_id(projection),
        ),
        new_receipt,
    )


def decision_blocks_assurance_recovery(decision: ResumeCompatibilityDecision) -> bool:
    """True when write/publication/effect recovery that needs current semantics must stop."""
    return (not decision.allowed) and decision.reason == LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND


def _root_id(projection: GraphProjection) -> str:
    return projection.parent_invocation_id or projection.invocation_id


def _selected_layers(params: Mapping[str, object]) -> tuple[str, ...]:
    raw = params.get("test_types")
    if isinstance(raw, (list, tuple)):
        values = [str(item) for item in raw]
    elif isinstance(raw, str) and raw.strip():
        values = [raw.strip()]
    else:
        values = ["api", "e2e"]
    return tuple(layer for layer in LAYER_NAMES if layer in set(values))


def _pending_or_open_node_ids(projection: GraphProjection) -> set[str]:
    open_statuses = {"pending", "running", "failed", "abandoned"}
    nodes: set[str] = set()
    for task in projection.tasks.values():
        if task.status in open_statuses:
            nodes.add(task.node_id)
        elif task.status == "succeeded" and not task.outputs_committed:
            nodes.add(task.node_id)
    return nodes


def _node_succeeded(projection: GraphProjection, node_id: str) -> bool:
    for task in projection.tasks.values():
        if task.node_id == node_id and task.status == "succeeded" and task.outputs_committed:
            return True
    return False


def _uses_for_node(compiled: CompiledWorkflow, node_id: str) -> str | None:
    for graph in compiled.graphs.values():
        node = graph.nodes.get(node_id)
        if node is not None:
            return node.definition.uses
    for graph in compiled.schema.graphs.values():
        node = graph.nodes.get(node_id)
        if node is not None:
            return node.uses
    return None


def _looks_like_assurance_codegen(uses: str) -> bool:
    if not uses.startswith("skill:aa-"):
        return False
    body = uses.removeprefix("skill:aa-")
    return body.endswith("-codegen") or body.endswith("-codegen-fixer")


def _is_commit_safety_bearing_target(uses: str, contracts: ExecutionContractCatalog) -> bool:
    if _looks_like_assurance_codegen(uses):
        return True
    if uses in {
        "operation:allocate-healing-attempt",
        "operation:record-fixer-approval",
        "operation:record-api-heal-apply",
        "operation:record-e2e-heal-apply",
        "operation:combine-fixer-safety",
    }:
        return True
    contract = contracts.contracts.get(uses)
    if contract is None:
        return False
    if contract.precommit_validator is not None:
        return True
    if contract.durable_effects:
        return True
    return False


def _contract_writes_private_assurance_root(uses: str, contracts: ExecutionContractCatalog) -> bool:
    contract = contracts.contracts.get(uses)
    if contract is None:
        return False
    private_roots: set[str] = set()
    for layer in LAYER_NAMES:
        root = get_generated_files_contract(layer).private_test_root
        private_roots.add(root)
        private_roots.add(f"repo:{root}")
        private_roots.add(f"{root}/")
    for path in (*contract.writes, *contract.authorization_writes):
        for root in private_roots:
            if path == root or path.startswith(f"{root.rstrip('/')}/") or f"/{root.strip('/')}" in path:
                return True
    return False


def _digest_sorted(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND",
    "TOPOLOGY_AUDIT_UNSAFE",
    "TOPOLOGY_PROFILE_UNRECONSTRUCTABLE",
    "TOPOLOGY_RECEIPT_CORRUPT",
    "AuditResult",
    "RemainingWorkAssessment",
    "RemainingWorkClass",
    "ResumeCompatibilityDecision",
    "ResumeCompatibilityError",
    "ResumeCompatibilityReason",
    "TopologyCompatibilityReceiptV1",
    "assess_remaining_work",
    "audit_topology_for_resume",
    "build_topology_compatibility_receipt",
    "decision_blocks_assurance_recovery",
    "evaluate_resume_compatibility",
    "event_to_receipt",
    "receipt_to_event",
    "stage_audit_topology_semantics",
    "verify_receipt_bindings",
]
