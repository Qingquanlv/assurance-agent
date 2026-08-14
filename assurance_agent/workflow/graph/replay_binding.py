"""Strict historical binding for assurance replay: definitions first, then wired evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.assurance import LAYER_NAMES
from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.artifacts.models.review import PlanReview
from assurance_agent.artifacts.policy import PolicyError, load_policy_snapshot_bytes
from assurance_agent.knowledge.capabilities import plan_review_route
from assurance_agent.verification.gate_state import plan_assurance_state
from assurance_agent.verification.profile_manifest import (
    AssuranceProfileManifest,
    AssuranceProfileManifestEntry,
    assurance_profile_bytes,
    assurance_profile_snapshot_relpath,
    parse_assurance_profile_snapshot,
)
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GRAPH_EVENT_ADAPTER,
    GraphInvocationStartedEvent,
    SuperstepCommittedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.graph.compiler import PinnedDefinitionRequest
from assurance_agent.workflow.graph.contracts import ResourcePath
from assurance_agent.workflow.graph.definition_pinning import (
    PinnedDefinitionError,
    is_definition_binding_replayable,
    load_pinned_execution_definition,
    policy_snapshot_relpath,
)
from assurance_agent.workflow.graph.historical_roles import (
    DiscoveredHistoricalAssuranceRoles,
    layer_roles_or_none,
)
from assurance_agent.workflow.graph.models import CompiledWorkflow
from assurance_agent.workflow.graph.replay_schema import (
    LayerTopologySpec,
    PinnedLayerTopology,
    classify_pinned_layer_topology_v4,
    classify_pinned_layer_topology_v5,
    classify_pinned_layer_topology_v6,
    validate_params_only_expression,
)
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2
from assurance_agent.workflow.graph.workspace import TreeStore
from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_digest
from assurance_agent.workflow.orchestration.plan_check_replay import (
    BoundArtifact,
    ProfileExecutableCompatibilityError,
    bind_json_artifact,
    bind_yaml_artifact,
    evaluate_bound_plan_gate,
    resolve_executable_layer_profile,
)

ReplayReasonCode = Literal[
    "root_invocation_unbound",
    "pinned_schema_missing",
    "pinned_schema_digest_mismatch",
    "pinned_schema_compile_failed",
    "pinned_ingest_catalog_missing",
    "pinned_ingest_catalog_invalid",
    "pinned_ingest_catalog_digest_mismatch",
    "pinned_contract_snapshot_missing",
    "pinned_contract_digest_mismatch",
    "pinned_contract_target_mismatch",
    "pinned_historical_roles_invalid",
    "policy_snapshot_missing",
    "policy_digest_mismatch",
    "policy_origin_mismatch",
    "gate_semantics_mismatch",
    "assurance_profile_mismatch",
    "profile_snapshot_missing",
    "profile_snapshot_digest_mismatch",
    "profile_definition_incompatible",
    "partial_assurance_wiring",
    "ambiguous_assurance_invocation",
    "ambiguous_graph_wiring",
    "selection_evidence_mismatch",
    "no_successful_gate_evidence",
    "mechanical_producer_unbound",
    "gate_evidence_unbound",
    "gate_evidence_drift",
    "invalid_checks",
    "missing_evidence",
    "invalid_evidence",
    "baseline_gate_mismatch",
    "baseline_route_mismatch",
    "topology_compatibility_receipt_corrupt",
]

_ASSURANCE_BRANCH_NODES = ("api", "e2e", "fuzz", "performance")
_SPECIALTY_LAYERS = frozenset({"fuzz", "performance"})


class ReplayBindingError(Exception):
    def __init__(self, reason_code: ReplayReasonCode, message: str) -> None:
        self.reason_code = reason_code
        self.message = message
        super().__init__(f"{reason_code}: {message}")


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    seq: int
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class CommittedAttempt:
    invocation_id: str
    task_id: str
    attempt_id: str
    superstep_id: str
    node_id: str
    contract_digest: str
    commit_seq: int
    target_tree_id: str
    gate_report: dict[str, object] | None = None
    outputs_sha256: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class LayerInvocationBinding:
    layer: str
    invocation_id: str
    parent_invocation_id: str
    parent_task_id: str
    graph_id: str
    structural_path: str
    params: dict[str, object]


@dataclass(frozen=True, slots=True)
class FrozenDefinitionBinding:
    change_id: str
    root_invocation_id: str
    assurance_invocation_id: str
    event_schema_version: int
    graph_digest: str
    gate_definition_source: Literal["pinned_schema"]
    baseline_policy_digest: str
    policy_source: Literal["pinned_runtime_snapshot"]
    policy_origin: str
    gate_semantics_digest: str
    assurance_profile_digest: str
    compiled: CompiledWorkflow
    policy: Policy
    assurance_params: dict[str, object]
    selected_layers: frozenset[str]
    profile_manifest: AssuranceProfileManifest | None
    layer_topology_specs: tuple[LayerTopologySpec, ...]
    profile_compatibility: dict[str, bool]
    gate_semantics_compatible: bool
    layer_topologies: dict[str, PinnedLayerTopology]
    historical_roles: DiscoveredHistoricalAssuranceRoles
    sequenced_events: tuple[SequencedEvent, ...]
    gate_semantics_object_id: str = ""
    topology_safety_semantics_object_id: str = ""
    topology_safety_semantics_digest: str = ""
    commit_safety_semantics_object_id: str = ""
    commit_safety_semantics_digest: str = ""


@dataclass(frozen=True, slots=True)
class LayerSelectionFact:
    layer: str
    selected: bool


@dataclass(frozen=True, slots=True)
class BoundLayerReplayInputs:
    layer: str
    params: dict[str, object]
    gate_report: dict[str, object]
    review: BoundArtifact[PlanReview] | None
    checks: BoundArtifact[PlanCheckDocument]
    data_knowledge: BoundArtifact[DataKnowledge] | None
    mechanical_execution_contract_digest: str
    gate_commit_tree_id: str
    gate_attempt: CommittedAttempt
    mechanical_attempt: CommittedAttempt
    baseline: object
    route: str


def validate_pinned_layer_selection(
    schema: WorkflowSchemaV2,
    *,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
) -> tuple[str, ...]:
    """Validate pinned assurance branch predicates are params-only replayable."""
    param_names = frozenset(schema.params)
    errors: list[str] = []
    assurance = schema.graphs.get(historical_roles.assurance_graph_id)
    if assurance is None:
        return (f"graph:{historical_roles.assurance_graph_id}: missing assurance graph",)
    by_layer = {item.layer: item for item in historical_roles.layers}
    for layer in _ASSURANCE_BRANCH_NODES:
        layer_roles = by_layer.get(layer)
        if layer_roles is None:
            errors.append(f"layer:{layer}: missing discovered selection role")
            continue
        node_id = layer_roles.selection_event_node_id
        node = assurance.nodes.get(node_id)
        if node is None:
            errors.append(f"graph:{historical_roles.assurance_graph_id}.nodes.{node_id}: missing branch node")
            continue
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
    return tuple(errors)


def evaluate_layer_selection(
    schema: WorkflowSchemaV2,
    params: Mapping[str, object],
    *,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
) -> tuple[LayerSelectionFact, ...]:
    """Evaluate pinned assurance branch predicates from frozen params and discovered roles."""
    selection_errors = validate_pinned_layer_selection(schema, historical_roles=historical_roles)
    if selection_errors:
        raise ReplayBindingError("ambiguous_graph_wiring", "; ".join(selection_errors))
    assurance = schema.graphs[historical_roles.assurance_graph_id]
    facts: list[LayerSelectionFact] = []
    scope = Scope({"params": dict(params)})
    for layer in _ASSURANCE_BRANCH_NODES:
        layer_roles = layer_roles_or_none(historical_roles, layer)
        if layer_roles is None:
            facts.append(LayerSelectionFact(layer=layer, selected=False))
            continue
        when = assurance.nodes[layer_roles.selection_event_node_id].when
        if not when:
            raise ReplayBindingError("ambiguous_graph_wiring", f"missing when for layer {layer}")
        expr = parse_expression(when)
        selected = evaluate(expr, scope) is True
        facts.append(LayerSelectionFact(layer=layer, selected=selected))
    return tuple(facts)


def assert_layer_selection_evidence(
    events: Sequence[SequencedEvent],
    *,
    assurance_invocation_id: str,
    selections: Sequence[LayerSelectionFact],
    historical_roles: DiscoveredHistoricalAssuranceRoles,
) -> None:
    """Require activation/skip events to agree with predicate classification when present."""
    for fact in selections:
        layer_roles = layer_roles_or_none(historical_roles, fact.layer)
        selection_node_id = layer_roles.selection_event_node_id if layer_roles is not None else fact.layer
        observed: bool | None = None
        for item in events:
            payload = item.payload
            if payload.get("invocation_id") != assurance_invocation_id:
                continue
            if payload.get("node_id") != selection_node_id:
                continue
            event_type = payload.get("type")
            if event_type == "node_activated":
                observed = True
                break
            if event_type == "node_skipped":
                observed = False
                break
        if observed is None:
            continue
        if observed != fact.selected:
            raise ReplayBindingError(
                "selection_evidence_mismatch",
                f"layer {fact.layer} predicate/event mismatch",
            )


def normalize_logical_path(path: str) -> str:
    """Canonicalize logical artifact paths for hash comparisons."""
    candidate = path if ":" in path else f"change:{path}"
    parsed = ResourcePath.parse(candidate)
    return f"{parsed.root}:{parsed.pattern}"


def bind_replay_definitions(
    *,
    change_dir: Path,
    change_id: str,
    root_invocation_id: str,
    expected_entrypoint: str,
    store: TreeStore | None = None,
    schema_root: Path | None = None,
) -> FrozenDefinitionBinding:
    """Bind replay to the caller-pinned root invocation and pinned definitions."""
    del store, schema_root
    try:
        raw_events = read_events_strict(change_dir)
    except LedgerIntegrityError as exc:
        raise ReplayBindingError("root_invocation_unbound", str(exc)) from exc

    sequenced = _sequenced_graph_events(raw_events)
    root_started = _require_root_started(
        sequenced,
        root_invocation_id=root_invocation_id,
        expected_entrypoint=expected_entrypoint,
    )
    if not _has_terminal(sequenced, root_invocation_id):
        raise ReplayBindingError(
            "root_invocation_unbound",
            f"root invocation {root_invocation_id} has no terminal event",
        )

    compiled, policy, historical_roles = _load_pinned_definitions(change_dir, root_started)
    assurance = _bind_assurance_invocation(
        sequenced,
        root_started=root_started,
        historical_roles=historical_roles,
    )
    selections = evaluate_layer_selection(
        compiled.schema,
        assurance.params,
        historical_roles=historical_roles,
    )
    assert_layer_selection_evidence(
        sequenced,
        assurance_invocation_id=assurance.invocation_id,
        selections=selections,
        historical_roles=historical_roles,
    )
    selected_layers = frozenset(fact.layer for fact in selections if fact.selected)

    gate_semantics_compatible = root_started.gate_semantics_digest == gate_semantics_digest()
    profile_manifest, layer_topology_specs, profile_compatibility, layer_topologies = (
        _bind_profile_and_topologies(
            change_dir=change_dir,
            root_started=root_started,
            schema=compiled.schema,
            historical_roles=historical_roles,
        )
    )
    _validate_topology_compatibility_receipts(
        sequenced,
        root_invocation_id=root_invocation_id,
        root_started=root_started,
        historical_roles=historical_roles,
    )

    return FrozenDefinitionBinding(
        change_id=change_id,
        root_invocation_id=root_invocation_id,
        assurance_invocation_id=assurance.invocation_id,
        event_schema_version=root_started.event_schema_version,
        graph_digest=root_started.graph_digest,
        gate_definition_source="pinned_schema",
        baseline_policy_digest=root_started.policy_digest,
        policy_source="pinned_runtime_snapshot",
        policy_origin=root_started.policy_origin,
        gate_semantics_digest=root_started.gate_semantics_digest,
        assurance_profile_digest=root_started.assurance_profile_digest,
        gate_semantics_object_id=root_started.gate_semantics_object_id,
        topology_safety_semantics_object_id=root_started.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=root_started.topology_safety_semantics_digest,
        commit_safety_semantics_object_id=root_started.commit_safety_semantics_object_id,
        commit_safety_semantics_digest=root_started.commit_safety_semantics_digest,
        compiled=compiled,
        policy=policy,
        assurance_params=dict(assurance.params),
        selected_layers=selected_layers,
        profile_manifest=profile_manifest,
        layer_topology_specs=layer_topology_specs,
        profile_compatibility=profile_compatibility,
        gate_semantics_compatible=gate_semantics_compatible,
        layer_topologies=layer_topologies,
        historical_roles=historical_roles,
        sequenced_events=tuple(sequenced),
    )


def recover_layer_inputs(
    binding: FrozenDefinitionBinding,
    *,
    layer: str,
    change_dir: Path,
    store: TreeStore | None = None,
) -> BoundLayerReplayInputs:
    """Recover committed gate/mechanical evidence and calibrate the frozen baseline."""
    topology = binding.layer_topologies.get(layer)
    if topology is None:
        raise ReplayBindingError("ambiguous_graph_wiring", f"no pinned topology for layer {layer!r}")
    if topology.status == "partial":
        raise ReplayBindingError("partial_assurance_wiring", "selected layer has partial pinned topology")
    if topology.status != "wired":
        raise ReplayBindingError("ambiguous_graph_wiring", "evidence recovery requires wired topology")
    if not binding.profile_compatibility.get(layer, False):
        raise ReplayBindingError("profile_definition_incompatible", "pinned profile cannot be executed")
    if not binding.gate_semantics_compatible:
        raise ReplayBindingError("gate_semantics_mismatch", "pinned gate evaluator is incompatible")
    return _recover_wired_layer(binding, topology, change_dir=change_dir, store=store)


def _recover_wired_layer(
    binding: FrozenDefinitionBinding,
    topology: PinnedLayerTopology,
    *,
    change_dir: Path,
    store: TreeStore | None,
) -> BoundLayerReplayInputs:
    layer = topology.layer
    spec = _spec_for_layer(binding, layer)
    root_started = _root_started_event(binding)
    assurance = _assurance_started_event(binding)
    layer_binding = _bind_layer_cycle_invocation(
        binding.sequenced_events,
        root_started=root_started,
        assurance=assurance,
        topology=topology,
        layer=layer,
    )

    object_store = store or TreeStore(change_dir)
    gate_node_id = topology.gate_node_id
    mechanical_node_id = topology.mechanical_node_id
    if gate_node_id is None or mechanical_node_id is None:
        raise ReplayBindingError(
            "ambiguous_graph_wiring", f"wired topology missing node ids for layer {layer}"
        )

    gate_attempt = _select_gate_attempt(
        binding,
        layer_binding.invocation_id,
        gate_node_id=gate_node_id,
        gate_id=spec.gate_id,
        cycle_graph_id=topology.cycle_graph_id or layer_binding.graph_id,
    )
    mechanical_attempt = _select_mechanical_attempt(
        binding,
        layer_binding.invocation_id,
        mechanical_node_id=mechanical_node_id,
        checks_artifact=spec.checks_artifact,
        cycle_graph_id=topology.cycle_graph_id or layer_binding.graph_id,
        gate_attempt=gate_attempt,
    )
    gate_report = gate_attempt.gate_report
    if gate_report is None:
        raise ReplayBindingError("gate_evidence_unbound", f"missing gate report for layer {layer}")

    checks = _recover_checks_artifact(
        checks_artifact=spec.checks_artifact,
        gate_report=gate_report,
        gate_tree_id=gate_attempt.target_tree_id,
        change_dir=change_dir,
        store=object_store,
        layer=layer,
    )
    applicable = _is_applicable(checks_doc=checks.model)
    review, data_knowledge = _recover_optional_artifacts(
        review_artifact=spec.review_artifact,
        gate_report=gate_report,
        gate_tree_id=gate_attempt.target_tree_id,
        applicable=applicable,
        change_dir=change_dir,
        store=object_store,
        layer=layer,
    )
    if applicable:
        if review is None:
            raise ReplayBindingError(
                "missing_evidence", f"missing review artifact for applicable layer {layer}"
            )
        if data_knowledge is None:
            raise ReplayBindingError("missing_evidence", f"missing L1 artifact for applicable layer {layer}")
    else:
        review = None
        data_knowledge = None

    pinned_entry = _pinned_manifest_entry(binding, layer)
    try:
        profile = resolve_executable_layer_profile(
            pinned_entry=pinned_entry,
            recorded_gate_semantics_digest=binding.gate_semantics_digest,
        )
    except ProfileExecutableCompatibilityError as exc:
        raise ReplayBindingError(cast(ReplayReasonCode, exc.reason_code), exc.message) from exc

    state = plan_assurance_state(
        checks.model.model_dump(mode="json"),
        review.model.model_dump(mode="json") if review is not None else {},
        data_knowledge.model.model_dump(mode="json") if data_knowledge is not None else {},
        layer,
        change_id=binding.change_id,
    )
    if state == "invalid":
        raise ReplayBindingError("invalid_checks", f"checks evidence failed validation for layer {layer}")
    if applicable and state != "applicable":
        raise ReplayBindingError("invalid_evidence", f"expected applicable checks for layer {layer}")
    if not applicable and state != "not_applicable":
        raise ReplayBindingError("invalid_evidence", f"expected inapplicable checks for layer {layer}")

    baseline = evaluate_bound_plan_gate(
        gates=binding.compiled.schema.gates,
        profile=profile,
        review=review,
        checks=checks,
        data_knowledge=data_knowledge,
        policy=binding.policy,
        change_id=binding.change_id,
        params=layer_binding.params,
    )
    _assert_baseline_gate(gate_report, baseline, gate_id=spec.gate_id)
    baseline_route = _route_from_baseline(baseline)
    recorded_route = plan_review_route({"gate": gate_report})
    if baseline_route != recorded_route:
        raise ReplayBindingError(
            "baseline_route_mismatch",
            f"baseline route {baseline_route!r} != recorded {recorded_route!r}",
        )
    _assert_route_events(binding.sequenced_events, layer_binding, recorded_route)

    return BoundLayerReplayInputs(
        layer=layer,
        params=dict(layer_binding.params),
        gate_report=dict(gate_report),
        review=review,
        checks=checks,
        data_knowledge=data_knowledge,
        mechanical_execution_contract_digest=mechanical_attempt.contract_digest,
        gate_commit_tree_id=gate_attempt.target_tree_id,
        gate_attempt=gate_attempt,
        mechanical_attempt=mechanical_attempt,
        baseline=baseline,
        route=recorded_route,
    )


def _sequenced_graph_events(raw_events: list[dict[str, object]]) -> list[SequencedEvent]:
    sequenced: list[SequencedEvent] = []
    for raw in raw_events:
        if raw.get("source") != "graph":
            continue
        seq = raw.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        payload = dict(raw)
        sequenced.append(SequencedEvent(seq=seq, payload=payload))
    sequenced.sort(key=lambda item: item.seq)
    return sequenced


def _validate_topology_compatibility_receipts(
    sequenced: Sequence[SequencedEvent],
    *,
    root_invocation_id: str,
    root_started: GraphInvocationStartedEvent,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
) -> None:
    """Exact receipt replay is idempotent; identity/payload drift is corruption."""
    from assurance_agent.workflow.core.graph_events import TopologySafetyCompatibilityRecordedEvent
    from assurance_agent.workflow.graph.resume_compatibility import event_to_receipt
    from assurance_agent.workflow.graph.topology_semantics import (
        topology_safety_semantics_digest,
        topology_safety_semantics_object_digest,
    )

    if root_started.event_schema_version < 4 or root_started.event_schema_version >= 6:
        return
    seen: dict[str, dict[str, object]] = {}
    for item in sequenced:
        raw = item.payload
        if raw.get("source") != "graph" or raw.get("type") != "topology_safety_compatibility_recorded":
            continue
        if raw.get("invocation_id") != root_invocation_id:
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                f"topology receipt belongs to another root: {raw.get('invocation_id')}",
            )
        payload = {k: v for k, v in raw.items() if k not in {"seq", "ts"}}
        try:
            event = TopologySafetyCompatibilityRecordedEvent.model_validate(payload)
            receipt = event_to_receipt(event)
        except Exception as exc:  # noqa: BLE001 — normalize fold/validation failures
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                str(exc),
            ) from exc
        dumped = receipt.model_dump(mode="json")
        prior = seen.get(receipt.receipt_id)
        if prior is not None and prior != dumped:
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                f"conflicting topology receipt payload for {receipt.receipt_id}",
            )
        seen[receipt.receipt_id] = dumped
        if receipt.graph_digest != root_started.graph_digest:
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                "topology receipt graph_digest does not match root",
            )
        if receipt.discovered_roles_digest != historical_roles.canonical_digest:
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                "topology receipt discovered_roles_digest does not match pinned roles",
            )
        if receipt.topology_safety_semantics_object_id != topology_safety_semantics_object_digest():
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                "topology receipt semantics object id mismatch",
            )
        if receipt.topology_safety_semantics_digest != topology_safety_semantics_digest():
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                "topology receipt semantics digest mismatch",
            )
        if len(seen) > 1:
            raise ReplayBindingError(
                "topology_compatibility_receipt_corrupt",
                "multiple distinct topology receipts for one root",
            )


def _require_root_started(
    events: Sequence[SequencedEvent],
    *,
    root_invocation_id: str,
    expected_entrypoint: str,
) -> GraphInvocationStartedEvent:
    started: GraphInvocationStartedEvent | None = None
    for item in events:
        payload = {k: v for k, v in item.payload.items() if k not in {"seq", "ts"}}
        if payload.get("type") != "graph_invocation_started":
            continue
        if payload.get("invocation_id") != root_invocation_id:
            continue
        try:
            event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise ReplayBindingError("root_invocation_unbound", str(exc)) from exc
        if not isinstance(event, GraphInvocationStartedEvent):
            continue
        if event.parent_invocation_id is not None:
            raise ReplayBindingError(
                "root_invocation_unbound",
                f"invocation {root_invocation_id} is not a root invocation",
            )
        if event.entrypoint != expected_entrypoint:
            raise ReplayBindingError(
                "root_invocation_unbound",
                f"entrypoint {event.entrypoint!r} != expected {expected_entrypoint!r}",
            )
        started = event
        break
    if started is None:
        raise ReplayBindingError(
            "root_invocation_unbound",
            f"root invocation {root_invocation_id} not found",
        )
    return started


def _has_terminal(events: Sequence[SequencedEvent], invocation_id: str) -> bool:
    for item in events:
        payload = item.payload
        if payload.get("invocation_id") != invocation_id:
            continue
        if payload.get("type") in {"graph_completed", "graph_stopped", "graph_failed"}:
            return True
    return False


def _load_pinned_definitions(
    change_dir: Path,
    started: GraphInvocationStartedEvent,
) -> tuple[CompiledWorkflow, Policy, DiscoveredHistoricalAssuranceRoles]:
    request = PinnedDefinitionRequest(
        graph_digest=started.graph_digest,
        ingest_catalog_digest=started.ingest_catalog_digest,
        contract_digests=tuple(sorted(started.contract_digests.items())),
        event_schema_version=started.event_schema_version,
        gate_semantics_digest=started.gate_semantics_digest,
        assurance_profile_digest=started.assurance_profile_digest,
        gate_semantics_object_id=started.gate_semantics_object_id,
        topology_safety_semantics_object_id=started.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=started.topology_safety_semantics_digest,
        commit_safety_semantics_object_id=started.commit_safety_semantics_object_id,
        commit_safety_semantics_digest=started.commit_safety_semantics_digest,
    )
    try:
        resolved = load_pinned_execution_definition(change_dir, request)
    except PinnedDefinitionError as exc:
        raise ReplayBindingError(cast(ReplayReasonCode, exc.reason_code), exc.message) from exc
    compiled = resolved.compiled

    policy_path = change_dir / policy_snapshot_relpath(started.policy_digest)
    if not policy_path.exists():
        raise ReplayBindingError(
            "policy_snapshot_missing",
            f"policy snapshot missing at {policy_path}",
        )
    policy_bytes = policy_path.read_bytes()
    actual_policy_digest = hashlib.sha256(policy_bytes).hexdigest()
    if actual_policy_digest != started.policy_digest:
        raise ReplayBindingError(
            "policy_digest_mismatch",
            f"policy snapshot digest mismatch for {policy_path}",
        )
    try:
        policy_origin = _coerce_policy_origin(started.policy_origin)
        snap = load_policy_snapshot_bytes(policy_bytes, origin=policy_origin)
    except PolicyError as exc:
        raise ReplayBindingError("policy_digest_mismatch", str(exc)) from exc
    if snap.origin != policy_origin:
        raise ReplayBindingError(
            "policy_origin_mismatch",
            f"policy origin {snap.origin!r} != recorded {started.policy_origin!r}",
        )
    if not is_definition_binding_replayable(
        _projection_from_started(started),
    ):
        raise ReplayBindingError("policy_origin_mismatch", "definition binding is not replayable")
    return compiled, snap.policy, resolved.historical_roles


def _bind_profile_and_topologies(
    *,
    change_dir: Path,
    root_started: GraphInvocationStartedEvent,
    schema: WorkflowSchemaV2,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
) -> tuple[
    AssuranceProfileManifest | None,
    tuple[LayerTopologySpec, ...],
    dict[str, bool],
    dict[str, PinnedLayerTopology],
]:
    version = root_started.event_schema_version

    def _classify(spec: LayerTopologySpec) -> PinnedLayerTopology:
        if version >= 6:
            _require_verified_v6_topology_semantics(change_dir, root_started)
            return classify_pinned_layer_topology_v6(schema, spec, historical_roles=historical_roles)
        if version >= 5:
            return classify_pinned_layer_topology_v5(schema, spec)
        return classify_pinned_layer_topology_v4(schema, spec)

    if version >= 5:
        manifest = _load_v5_profile_snapshot(change_dir, root_started.assurance_profile_digest)
        specs = _specs_from_manifest(manifest)
        topologies = {spec.layer: _classify(spec) for spec in specs}
        compatibility = _profile_compatibility_from_manifest(manifest)
        return manifest, specs, compatibility, topologies

    current_bytes = assurance_profile_bytes()
    current_digest = hashlib.sha256(current_bytes).hexdigest()
    digest_compatible = root_started.assurance_profile_digest == current_digest
    if digest_compatible:
        manifest = parse_assurance_profile_snapshot(current_bytes)
        specs = _specs_from_manifest(manifest)
        topologies = {spec.layer: _classify(spec) for spec in specs}
        topologies = _force_specialty_incomplete_without_snapshot(topologies)
        compatibility = {layer: True for layer in LAYER_NAMES}
        return manifest, specs, compatibility, topologies

    specs = _construct_provisional_specs()
    topologies = {spec.layer: _classify(spec) for spec in specs}
    topologies = _force_specialty_incomplete_without_snapshot(topologies)
    compatibility = {layer: False for layer in LAYER_NAMES}
    return None, specs, compatibility, topologies


def _force_specialty_incomplete_without_snapshot(
    topologies: dict[str, PinnedLayerTopology],
) -> dict[str, PinnedLayerTopology]:
    updated = dict(topologies)
    for layer in _SPECIALTY_LAYERS:
        topology = updated.get(layer)
        if topology is None:
            continue
        if topology.status == "wired":
            updated[layer] = PinnedLayerTopology(
                layer=topology.layer,
                status="partial",
                assurance_node_id=topology.assurance_node_id,
                branch_graph_id=topology.branch_graph_id,
                cycle_call_node_id=topology.cycle_call_node_id,
                cycle_graph_id=topology.cycle_graph_id,
                applicability_node_id=topology.applicability_node_id,
                reviewer_node_id=topology.reviewer_node_id,
                mechanical_node_id=topology.mechanical_node_id,
                gate_node_id=topology.gate_node_id,
                human_review_node_id=topology.human_review_node_id,
                knowledge_remediation_node_id=topology.knowledge_remediation_node_id,
                codegen_precondition_node_id=topology.codegen_precondition_node_id,
                codegen_node_id=topology.codegen_node_id,
                diagnostics=(
                    *topology.diagnostics,
                    f"layer:{layer}: complete activation without profile snapshot is incomplete",
                ),
                semantics_id=topology.semantics_id,
                semantics_bound=topology.semantics_bound,
            )
    return updated


def _load_v5_profile_snapshot(change_dir: Path, digest: str) -> AssuranceProfileManifest:
    path = change_dir / assurance_profile_snapshot_relpath(digest)
    if not path.exists():
        raise ReplayBindingError(
            "profile_snapshot_missing",
            f"assurance profile snapshot missing at {path}",
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReplayBindingError(
            "profile_snapshot_missing",
            f"cannot read assurance profile snapshot at {path}: {exc}",
        ) from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise ReplayBindingError(
            "profile_snapshot_digest_mismatch",
            f"assurance profile snapshot digest mismatch for {path}",
        )
    try:
        return parse_assurance_profile_snapshot(data)
    except ValueError as exc:
        raise ReplayBindingError("profile_snapshot_digest_mismatch", str(exc)) from exc


def _specs_from_manifest(manifest: AssuranceProfileManifest) -> tuple[LayerTopologySpec, ...]:
    return tuple(
        LayerTopologySpec(
            layer=entry.layer,
            plan_artifacts=entry.plan_artifacts,
            review_artifact=entry.review_artifact,
            review_alias=entry.review_alias,
            checks_artifact=entry.checks_artifact,
            gate_id=entry.gate_id,
        )
        for entry in manifest.profiles
    )


def _construct_provisional_specs() -> tuple[LayerTopologySpec, ...]:
    return tuple(
        LayerTopologySpec(
            layer=layer,
            plan_artifacts=(),
            review_artifact=(
                "review/plan-review.json" if layer == "e2e" else f"review/{layer}-plan-review.json"
            ),
            review_alias="plan_review" if layer == "e2e" else f"{layer}_plan_review",
            checks_artifact=f"review/{layer}-plan-checks.json",
            gate_id=f"{layer}-plan-review-gate",
        )
        for layer in LAYER_NAMES
    )


def _profile_compatibility_from_manifest(manifest: AssuranceProfileManifest) -> dict[str, bool]:
    current = parse_assurance_profile_snapshot(assurance_profile_bytes())
    current_by_layer = {entry.layer: entry for entry in current.profiles}
    result: dict[str, bool] = {}
    for entry in manifest.profiles:
        current_entry = current_by_layer.get(entry.layer)
        result[entry.layer] = current_entry is not None and _entry_canonical_bytes(
            entry
        ) == _entry_canonical_bytes(current_entry)
    for layer in LAYER_NAMES:
        result.setdefault(layer, False)
    return result


def _entry_canonical_bytes(entry: AssuranceProfileManifestEntry) -> bytes:
    return (
        json.dumps(entry.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _coerce_policy_origin(origin: str) -> Literal["project", "packaged_default"]:
    if origin == "project":
        return "project"
    if origin == "packaged_default":
        return "packaged_default"
    raise ReplayBindingError("policy_origin_mismatch", f"unknown policy origin {origin!r}")


def _projection_from_started(started: GraphInvocationStartedEvent):
    from assurance_agent.workflow.graph.models import GraphProjection

    return GraphProjection(
        invocation_id=started.invocation_id,
        entrypoint=started.entrypoint,
        checkpoint_ns=started.checkpoint_ns,
        parent_invocation_id=started.parent_invocation_id,
        parent_task_id=started.parent_task_id,
        structural_path=started.structural_path,
        graph_digest=started.graph_digest,
        event_schema_version=started.event_schema_version,
        ir_digest=started.ir_digest or started.graph_digest,
        ingest_catalog_digest=started.ingest_catalog_digest,
        contract_digests=dict(started.contract_digests),
        policy_digest=started.policy_digest,
        policy_origin=started.policy_origin,
        gate_semantics_digest=started.gate_semantics_digest,
        assurance_profile_digest=started.assurance_profile_digest,
        gate_semantics_object_id=started.gate_semantics_object_id,
        topology_safety_semantics_object_id=started.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=started.topology_safety_semantics_digest,
        commit_safety_semantics_object_id=started.commit_safety_semantics_object_id,
        commit_safety_semantics_digest=started.commit_safety_semantics_digest,
        params=dict(started.params),
        root_tree_id=started.root_tree_id,
        current_tree_id=started.root_tree_id,
    )


def _bind_assurance_invocation(
    events: Sequence[SequencedEvent],
    *,
    root_started: GraphInvocationStartedEvent,
    historical_roles: DiscoveredHistoricalAssuranceRoles,
) -> GraphInvocationStartedEvent:
    expected_path = (
        f"{root_started.structural_path}/"
        f"{historical_roles.assurance_call_node_id}/{historical_roles.assurance_graph_id}"
    )
    matches = _matching_started_events(
        events,
        root_started=root_started,
        parent_invocation_id=root_started.invocation_id,
        parent_node_id=historical_roles.assurance_call_node_id,
        parent_structural_path=root_started.structural_path,
        graph_id=historical_roles.assurance_graph_id,
        structural_path=expected_path,
    )
    if len(matches) != 1:
        raise ReplayBindingError(
            "ambiguous_assurance_invocation",
            f"expected exactly one assurance invocation, found {len(matches)}",
        )
    return matches[0]


def _bind_layer_cycle_invocation(
    events: Sequence[SequencedEvent],
    *,
    root_started: GraphInvocationStartedEvent,
    assurance: GraphInvocationStartedEvent,
    topology: PinnedLayerTopology,
    layer: str,
) -> LayerInvocationBinding:
    if topology.assurance_node_id is None or topology.branch_graph_id is None:
        raise ReplayBindingError(
            "ambiguous_graph_wiring", f"wired topology missing branch identity for layer {layer}"
        )
    if topology.cycle_call_node_id is None or topology.cycle_graph_id is None:
        raise ReplayBindingError(
            "ambiguous_graph_wiring", f"wired topology missing cycle identity for layer {layer}"
        )

    branch_path = f"{assurance.structural_path}/{topology.assurance_node_id}/{topology.branch_graph_id}"
    branch_matches = _matching_started_events(
        events,
        root_started=root_started,
        parent_invocation_id=assurance.invocation_id,
        parent_node_id=topology.assurance_node_id,
        parent_structural_path=assurance.structural_path,
        graph_id=topology.branch_graph_id,
        structural_path=branch_path,
    )
    if len(branch_matches) != 1:
        raise ReplayBindingError(
            "ambiguous_graph_wiring",
            f"expected exactly one {topology.branch_graph_id} invocation for layer {layer}, "
            f"found {len(branch_matches)}",
        )
    branch = branch_matches[0]
    cycle_path = f"{branch.structural_path}/{topology.cycle_call_node_id}/{topology.cycle_graph_id}"
    cycle_matches = _matching_started_events(
        events,
        root_started=root_started,
        parent_invocation_id=branch.invocation_id,
        parent_node_id=topology.cycle_call_node_id,
        parent_structural_path=branch.structural_path,
        graph_id=topology.cycle_graph_id,
        structural_path=cycle_path,
    )
    if len(cycle_matches) != 1:
        raise ReplayBindingError(
            "ambiguous_graph_wiring",
            f"expected exactly one {topology.cycle_graph_id} invocation for layer {layer}, "
            f"found {len(cycle_matches)}",
        )
    cycle = cycle_matches[0]
    return LayerInvocationBinding(
        layer=layer,
        invocation_id=cycle.invocation_id,
        parent_invocation_id=branch.invocation_id,
        parent_task_id=cycle.parent_task_id or "",
        graph_id=topology.cycle_graph_id,
        structural_path=cycle.structural_path,
        params=dict(cycle.params),
    )


def _matching_started_events(
    events: Sequence[SequencedEvent],
    *,
    root_started: GraphInvocationStartedEvent,
    parent_invocation_id: str,
    parent_node_id: str,
    parent_structural_path: str,
    graph_id: str,
    structural_path: str,
) -> list[GraphInvocationStartedEvent]:
    # Schema-v6 task IDs are content-derived hashes rather than the legacy
    # ``<structural-path>:<node-id>`` identity. Bind the child to the task IDs
    # actually started for the parent node. Old imported ledgers may not carry
    # those start events, so retain the structural identity only as a fallback.
    parent_task_ids = {
        str(item.payload["task_id"])
        for item in events
        if item.payload.get("type") == "task_attempt_started"
        and item.payload.get("invocation_id") == parent_invocation_id
        and item.payload.get("node_id") == parent_node_id
        and isinstance(item.payload.get("task_id"), str)
    }
    if not parent_task_ids:
        parent_task_ids = {f"{parent_structural_path}:{parent_node_id}"}
    matches: list[GraphInvocationStartedEvent] = []
    structural_hits = 0
    for item in events:
        payload = {k: v for k, v in item.payload.items() if k not in {"seq", "ts"}}
        if payload.get("type") != "graph_invocation_started":
            continue
        try:
            event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        except ValidationError:
            continue
        if not isinstance(event, GraphInvocationStartedEvent):
            continue
        if (
            event.parent_invocation_id == parent_invocation_id
            and event.parent_task_id in parent_task_ids
            and event.graph_id == graph_id
            and event.structural_path == structural_path
        ):
            structural_hits += 1
            if not _same_definition_epoch(event, root_started):
                raise ReplayBindingError(
                    "ambiguous_graph_wiring",
                    f"child invocation {event.invocation_id} has a different definition epoch",
                )
            matches.append(event)
    if structural_hits > len(matches):
        raise ReplayBindingError(
            "ambiguous_graph_wiring",
            f"structurally matching child for {graph_id} belongs to another definition epoch",
        )
    return matches


def _same_definition_epoch(
    child: GraphInvocationStartedEvent,
    root: GraphInvocationStartedEvent,
) -> bool:
    return (
        child.event_schema_version == root.event_schema_version
        and child.graph_digest == root.graph_digest
        and child.ingest_catalog_digest == root.ingest_catalog_digest
        and dict(child.contract_digests) == dict(root.contract_digests)
        and child.policy_digest == root.policy_digest
        and child.policy_origin == root.policy_origin
        and child.gate_semantics_digest == root.gate_semantics_digest
        and child.assurance_profile_digest == root.assurance_profile_digest
        and child.gate_semantics_object_id == root.gate_semantics_object_id
        and child.topology_safety_semantics_object_id == root.topology_safety_semantics_object_id
        and child.topology_safety_semantics_digest == root.topology_safety_semantics_digest
        and child.commit_safety_semantics_object_id == root.commit_safety_semantics_object_id
        and child.commit_safety_semantics_digest == root.commit_safety_semantics_digest
    )


def _require_verified_v6_topology_semantics(
    change_dir: Path,
    started: GraphInvocationStartedEvent,
) -> None:
    from assurance_agent.workflow.graph.definition_pinning import (
        topology_semantics_snapshot_relpath,
    )
    from assurance_agent.workflow.graph.topology_semantics import SEMANTICS_ID
    from assurance_agent.workflow.orchestration.gate_semantics import canonical_descriptor_bytes

    object_id = started.topology_safety_semantics_object_id
    semantic_digest = started.topology_safety_semantics_digest
    if not object_id or not semantic_digest:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            "v6 topology semantics binding is incomplete",
        )
    path = change_dir / topology_semantics_snapshot_relpath(object_id)
    if not path.exists():
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"topology semantics snapshot missing at {path}",
        )
    data = path.read_bytes()
    actual_object = hashlib.sha256(data).hexdigest()
    if actual_object != object_id:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"topology semantics object digest mismatch for {path}",
        )
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"topology semantics snapshot malformed: {exc}",
        ) from exc
    if not isinstance(payload, dict) or payload.get("semantics_id") != SEMANTICS_ID:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"unknown or missing topology semantics ID in {path}",
        )
    if payload.get("semantic_digest") != semantic_digest:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"topology semantics digest mismatch for {path}",
        )
    recomputed = canonical_descriptor_bytes(
        semantics_id=str(payload["semantics_id"]),
        schema_version=str(payload["schema_version"]),
        runtime_versions={str(k): str(v) for k, v in dict(payload["runtime_versions"]).items()},
        dependencies=[dict(item) for item in payload["dependencies"]],
        consumers=tuple(str(item) for item in payload["consumers"]),
        semantic_digest=str(payload["semantic_digest"]),
    )
    if recomputed != data:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"topology semantics snapshot bytes are not canonical for {path}",
        )


def _select_gate_attempt(
    binding: FrozenDefinitionBinding,
    invocation_id: str,
    *,
    gate_node_id: str,
    gate_id: str,
    cycle_graph_id: str,
) -> CommittedAttempt:
    attempts = _committed_attempts(binding.sequenced_events, invocation_id)
    expected_contract = _pinned_contract_digest(binding, cycle_graph_id, gate_node_id)
    gate_attempts = [
        item
        for item in attempts
        if item.node_id == gate_node_id
        and isinstance(item.gate_report, dict)
        and item.gate_report.get("gate_id") == gate_id
        and item.contract_digest == expected_contract
    ]
    if not gate_attempts:
        raise ReplayBindingError("no_successful_gate_evidence", f"no committed gate for {gate_id}")
    terminal_seq = _terminal_seq(binding.sequenced_events, invocation_id)
    eligible = [item for item in gate_attempts if item.commit_seq <= terminal_seq]
    if not eligible:
        raise ReplayBindingError("gate_evidence_unbound", f"no gate attempt before terminal for {gate_id}")
    return eligible[-1]


def _select_mechanical_attempt(
    binding: FrozenDefinitionBinding,
    invocation_id: str,
    *,
    mechanical_node_id: str,
    checks_artifact: str,
    cycle_graph_id: str,
    gate_attempt: CommittedAttempt,
) -> CommittedAttempt:
    attempts = _committed_attempts(binding.sequenced_events, invocation_id)
    gate_reads = gate_attempt.gate_report.get("reads_sha256") if gate_attempt.gate_report else None
    if not isinstance(gate_reads, dict):
        raise ReplayBindingError("gate_evidence_unbound", "gate report missing reads_sha256")
    expected_checks_digest = (
        gate_reads.get(checks_artifact)
        or gate_reads.get(normalize_logical_path(f"change:{checks_artifact}"))
        or gate_reads.get(f"change:{checks_artifact}")
    )
    if expected_checks_digest is None:
        raise ReplayBindingError(
            "gate_evidence_unbound", f"gate report missing checks digest for {checks_artifact}"
        )
    expected_contract = _pinned_contract_digest(binding, cycle_graph_id, mechanical_node_id)

    candidates: list[CommittedAttempt] = []
    for item in attempts:
        if item.node_id != mechanical_node_id:
            continue
        if item.contract_digest != expected_contract:
            continue
        if item.commit_seq > gate_attempt.commit_seq:
            continue
        outputs = item.outputs_sha256 or {}
        digest = (
            outputs.get(normalize_logical_path(f"change:{checks_artifact}"))
            or outputs.get(f"change:{checks_artifact}")
            or outputs.get(checks_artifact)
        )
        if digest != expected_checks_digest:
            continue
        candidates.append(item)
    if not candidates:
        raise ReplayBindingError(
            "mechanical_producer_unbound",
            f"no committed mechanical producer matching gate reads for {checks_artifact}",
        )
    return candidates[-1]


def _pinned_contract_digest(
    binding: FrozenDefinitionBinding,
    cycle_graph_id: str,
    node_id: str,
) -> str:
    graph = binding.compiled.schema.graphs.get(cycle_graph_id)
    if graph is None:
        raise ReplayBindingError(
            "ambiguous_graph_wiring", f"missing cycle graph {cycle_graph_id!r} in pinned schema"
        )
    node = graph.nodes.get(node_id)
    if node is None:
        raise ReplayBindingError(
            "ambiguous_graph_wiring", f"missing node {node_id!r} in pinned cycle {cycle_graph_id!r}"
        )
    digest = binding.compiled.contract_digests.get(node.uses)
    if digest is None:
        raise ReplayBindingError(
            "ambiguous_graph_wiring",
            f"missing pinned contract digest for {node.uses!r}",
        )
    return digest


def _committed_attempts(events: Sequence[SequencedEvent], invocation_id: str) -> list[CommittedAttempt]:
    started: dict[str, TaskAttemptStartedEvent] = {}
    succeeded: dict[str, TaskAttemptSucceededEvent] = {}
    abandoned: set[str] = set()
    failed: set[str] = set()
    commits: list[tuple[int, SuperstepCommittedEvent]] = []

    for item in events:
        payload = {k: v for k, v in item.payload.items() if k not in {"seq", "ts"}}
        if payload.get("invocation_id") != invocation_id:
            continue
        try:
            event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        except ValidationError:
            continue
        if isinstance(event, TaskAttemptStartedEvent):
            started[event.attempt_id] = event
        elif isinstance(event, TaskAttemptSucceededEvent):
            succeeded[event.attempt_id] = event
        elif payload.get("type") == "task_attempt_abandoned":
            attempt_id = payload.get("attempt_id")
            if isinstance(attempt_id, str):
                abandoned.add(attempt_id)
        elif payload.get("type") == "task_attempt_failed":
            attempt_id = payload.get("attempt_id")
            if isinstance(attempt_id, str):
                failed.add(attempt_id)
        elif isinstance(event, SuperstepCommittedEvent):
            commits.append((item.seq, event))

    committed: list[CommittedAttempt] = []
    for commit_seq, commit in commits:
        for attempt_id in sorted(succeeded):
            if attempt_id in abandoned or attempt_id in failed:
                continue
            start = started.get(attempt_id)
            success = succeeded.get(attempt_id)
            if start is None or success is None:
                continue
            if start.task_id not in commit.committed_task_ids:
                continue
            if (
                start.invocation_id != success.invocation_id
                or start.task_id != success.task_id
                or start.superstep_id != success.superstep_id
            ):
                continue
            committed.append(
                CommittedAttempt(
                    invocation_id=start.invocation_id,
                    task_id=start.task_id,
                    attempt_id=attempt_id,
                    superstep_id=start.superstep_id,
                    node_id=start.node_id,
                    contract_digest=start.contract_digest,
                    commit_seq=commit_seq,
                    target_tree_id=commit.target_tree_id,
                    gate_report=dict(success.gate_report) if success.gate_report else None,
                    outputs_sha256=dict(success.outputs_sha256),
                )
            )
    committed.sort(key=lambda item: item.commit_seq)
    return committed


def _terminal_seq(events: Sequence[SequencedEvent], invocation_id: str) -> int:
    for item in reversed(list(events)):
        payload = item.payload
        if payload.get("invocation_id") != invocation_id:
            continue
        if payload.get("type") in {"graph_completed", "graph_stopped", "graph_failed"}:
            return item.seq
    return max((item.seq for item in events), default=0)


def _recover_checks_artifact(
    *,
    checks_artifact: str,
    gate_report: dict[str, object],
    gate_tree_id: str,
    change_dir: Path,
    store: TreeStore,
    layer: str,
) -> BoundArtifact[PlanCheckDocument]:
    del change_dir
    reads = gate_report.get("reads_sha256")
    if not isinstance(reads, dict):
        raise ReplayBindingError("gate_evidence_unbound", "gate report missing reads_sha256")
    checks_path = normalize_logical_path(f"change:{checks_artifact}")
    checks_digest = (
        reads.get(checks_artifact) or reads.get(checks_path) or reads.get(f"change:{checks_artifact}")
    )
    if not isinstance(checks_digest, str):
        raise ReplayBindingError("missing_evidence", f"missing checks digest for {layer}")
    checks_bytes = _read_bound_bytes(
        store,
        gate_tree_id,
        checks_path,
        expected_digest=checks_digest,
    )
    checks_model = PlanCheckDocument.model_validate(json.loads(checks_bytes))
    return cast(
        BoundArtifact[PlanCheckDocument],
        bind_json_artifact(
            logical_path=checks_artifact,
            model=checks_model,
            raw_bytes=checks_bytes,
        ),
    )


def _recover_optional_artifacts(
    *,
    review_artifact: str,
    gate_report: dict[str, object],
    gate_tree_id: str,
    applicable: bool,
    change_dir: Path,
    store: TreeStore,
    layer: str,
) -> tuple[BoundArtifact[PlanReview] | None, BoundArtifact[DataKnowledge] | None]:
    del change_dir
    if not applicable:
        return None, None
    reads = gate_report.get("reads_sha256")
    if not isinstance(reads, dict):
        raise ReplayBindingError("gate_evidence_unbound", "gate report missing reads_sha256")

    review_path = normalize_logical_path(f"change:{review_artifact}")
    review_digest = (
        reads.get(review_artifact) or reads.get(review_path) or reads.get(f"change:{review_artifact}")
    )
    if not isinstance(review_digest, str):
        raise ReplayBindingError("missing_evidence", f"missing review digest for {layer}")
    review_bytes = _read_bound_bytes(
        store,
        gate_tree_id,
        review_path,
        expected_digest=review_digest,
    )
    review_model = PlanReview.model_validate(json.loads(review_bytes))
    review = cast(
        BoundArtifact[PlanReview],
        bind_json_artifact(
            logical_path=review_artifact,
            model=review_model,
            raw_bytes=review_bytes,
        ),
    )

    l1_path = normalize_logical_path("repo:.aa/data-knowledge.yaml")
    l1_digest = reads.get(l1_path) or reads.get("repo:.aa/data-knowledge.yaml")
    if not isinstance(l1_digest, str):
        raise ReplayBindingError("missing_evidence", f"missing L1 digest for {layer}")
    l1_bytes = _read_bound_bytes(
        store,
        gate_tree_id,
        l1_path,
        expected_digest=l1_digest,
    )
    l1_model = DataKnowledge.model_validate(yaml.safe_load(l1_bytes))
    data_knowledge = cast(
        BoundArtifact[DataKnowledge],
        bind_yaml_artifact(
            logical_path=l1_path,
            model=l1_model,
            raw_bytes=l1_bytes,
        ),
    )
    return review, data_knowledge


def _read_bound_bytes(
    store: TreeStore,
    tree_id: str,
    logical_path: str,
    *,
    expected_digest: str,
) -> bytes:
    normalized = normalize_logical_path(logical_path)
    try:
        payload = store.read_bytes(tree_id, normalized)
    except FileNotFoundError as exc:
        raise ReplayBindingError("missing_evidence", f"artifact missing for {normalized}") from exc
    except OSError as exc:
        raise ReplayBindingError("missing_evidence", f"cannot read {normalized}: {exc}") from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_digest:
        raise ReplayBindingError(
            "gate_evidence_drift",
            f"digest mismatch for {normalized}: expected {expected_digest}, got {actual}",
        )
    return payload


def _is_applicable(*, checks_doc: PlanCheckDocument) -> bool:
    if checks_doc.applicability is None:
        return False
    return bool(checks_doc.applicability.applicable)


def _assert_baseline_gate(
    recorded: dict[str, object],
    baseline: object,
    *,
    gate_id: str,
) -> None:
    from assurance_agent.workflow.orchestration.gates import FrozenGateReport

    if not isinstance(baseline, FrozenGateReport):
        raise ReplayBindingError("baseline_gate_mismatch", "baseline evaluation failed")
    if recorded.get("gate_id") != gate_id:
        raise ReplayBindingError("baseline_gate_mismatch", "gate id mismatch")
    if recorded.get("verdict") != baseline.verdict.value:
        raise ReplayBindingError(
            "baseline_gate_mismatch",
            f"verdict {recorded.get('verdict')!r} != {baseline.verdict.value!r}",
        )
    if recorded.get("matched_rule") != baseline.matched_rule:
        raise ReplayBindingError("baseline_gate_mismatch", "matched_rule mismatch")
    if recorded.get("reason") != baseline.reason:
        raise ReplayBindingError("baseline_gate_mismatch", "reason mismatch")
    recorded_details = _normalize_gate_details(recorded.get("details"))
    baseline_details = _normalize_gate_details(baseline.details)
    if recorded_details != baseline_details:
        raise ReplayBindingError("baseline_gate_mismatch", "details mismatch")
    recorded_reads = recorded.get("reads_sha256")
    if not isinstance(recorded_reads, dict):
        raise ReplayBindingError("baseline_gate_mismatch", "reads_sha256 missing")
    for key, digest in baseline.reads_sha256.items():
        normalized = normalize_logical_path(key)
        actual = recorded_reads.get(normalized) or recorded_reads.get(key)
        if actual != digest:
            raise ReplayBindingError("baseline_gate_mismatch", f"reads_sha256 mismatch for {key}")


def _normalize_gate_details(details: object) -> dict[str, object] | None:
    if details is None:
        return None
    if not isinstance(details, dict):
        raise ReplayBindingError("baseline_gate_mismatch", "details is not a mapping")
    normalized: dict[str, object] = {}
    for key, value in details.items():
        if key == "missing_capabilities" and isinstance(value, list):
            sorted_caps = sorted(str(item) for item in value)
            if sorted_caps:
                normalized[key] = sorted_caps
        else:
            normalized[key] = value
    return normalized if normalized else None


def _route_from_baseline(baseline: object) -> str:
    from assurance_agent.workflow.orchestration.gates import FrozenGateReport

    if not isinstance(baseline, FrozenGateReport):
        return "stop"
    details = dict(baseline.details) if baseline.details is not None else None
    return plan_review_route(
        {
            "gate": {
                "verdict": baseline.verdict.value,
                "matched_rule": baseline.matched_rule,
                "reason": baseline.reason,
                "details": details,
            }
        }
    )


def _assert_route_events(
    events: Sequence[SequencedEvent],
    layer_binding: LayerInvocationBinding,
    route: str,
) -> None:
    activation_nodes = {"review"} if route != "skip" else set()
    for item in events:
        payload = item.payload
        if payload.get("invocation_id") != layer_binding.invocation_id:
            continue
        if payload.get("type") == "node_activated" and payload.get("node_id") in activation_nodes:
            return
        if payload.get("type") == "node_skipped" and payload.get("node_id") == "review":
            if route == "skip":
                return
            raise ReplayBindingError("selection_evidence_mismatch", "review skipped but route is not skip")
    # Event absence is allowed when earlier termination prevented branch activation.


def _spec_for_layer(binding: FrozenDefinitionBinding, layer: str) -> LayerTopologySpec:
    for spec in binding.layer_topology_specs:
        if spec.layer == layer:
            return spec
    raise ReplayBindingError("ambiguous_graph_wiring", f"missing topology spec for layer {layer}")


def _pinned_manifest_entry(
    binding: FrozenDefinitionBinding,
    layer: str,
) -> AssuranceProfileManifestEntry:
    if binding.profile_manifest is None:
        raise ReplayBindingError(
            "profile_definition_incompatible",
            f"no pinned profile manifest entry for layer {layer}",
        )
    for entry in binding.profile_manifest.profiles:
        if entry.layer == layer:
            return entry
    raise ReplayBindingError(
        "profile_definition_incompatible",
        f"pinned profile manifest missing layer {layer}",
    )


def _root_started_event(binding: FrozenDefinitionBinding) -> GraphInvocationStartedEvent:
    for item in binding.sequenced_events:
        payload = {k: v for k, v in item.payload.items() if k not in {"seq", "ts"}}
        if payload.get("type") != "graph_invocation_started":
            continue
        if payload.get("invocation_id") != binding.root_invocation_id:
            continue
        event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        if isinstance(event, GraphInvocationStartedEvent):
            return event
    raise ReplayBindingError("root_invocation_unbound", "root started event missing from binding")


def _assurance_started_event(binding: FrozenDefinitionBinding) -> GraphInvocationStartedEvent:
    for item in binding.sequenced_events:
        payload = {k: v for k, v in item.payload.items() if k not in {"seq", "ts"}}
        if payload.get("type") != "graph_invocation_started":
            continue
        if payload.get("invocation_id") != binding.assurance_invocation_id:
            continue
        event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        if isinstance(event, GraphInvocationStartedEvent):
            return event
    raise ReplayBindingError("ambiguous_assurance_invocation", "assurance started event missing from binding")


__all__ = [
    "BoundLayerReplayInputs",
    "CommittedAttempt",
    "FrozenDefinitionBinding",
    "LayerInvocationBinding",
    "LayerSelectionFact",
    "ReplayBindingError",
    "ReplayReasonCode",
    "SequencedEvent",
    "assert_layer_selection_evidence",
    "bind_replay_definitions",
    "evaluate_layer_selection",
    "normalize_logical_path",
    "recover_layer_inputs",
    "validate_pinned_layer_selection",
]
