"""Strict historical binding for assurance replay: root, definitions, and evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.artifacts.models.review import PlanReview
from assurance_agent.artifacts.policy import PolicyError, load_policy_snapshot_bytes
from assurance_agent.change_location import archive_root
from assurance_agent.knowledge.capabilities import plan_review_route
from assurance_agent.verification.gate_state import plan_assurance_state
from assurance_agent.verification.profile_manifest import assurance_profile_digest
from assurance_agent.verification.profiles import LayerAssuranceProfile, get_layer_assurance_profile
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GRAPH_EVENT_ADAPTER,
    GraphInvocationStartedEvent,
    SuperstepCommittedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import ResourcePath
from assurance_agent.workflow.graph.definition_pinning import (
    is_definition_binding_replayable,
    policy_snapshot_relpath,
)
from assurance_agent.workflow.graph.models import CompiledWorkflow
from assurance_agent.workflow.graph.replay_schema import (
    validate_params_only_expression,
    validate_replayable_assurance_schema,
)
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2
from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression
from assurance_agent.workflow.graph.workspace import TreeStore
from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_digest
from assurance_agent.workflow.orchestration.plan_check_replay import (
    BoundArtifact,
    bind_json_artifact,
    bind_yaml_artifact,
    evaluate_bound_plan_gate,
)

ReplayReasonCode = Literal[
    "root_invocation_unbound",
    "pinned_schema_missing",
    "pinned_schema_digest_mismatch",
    "policy_snapshot_missing",
    "policy_digest_mismatch",
    "policy_origin_mismatch",
    "gate_semantics_mismatch",
    "assurance_profile_mismatch",
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
]

_LAYER_CYCLE_GRAPH = {"api": "api-plan-cycle", "e2e": "e2e-plan-cycle"}
_WIRED_LAYERS = frozenset(_LAYER_CYCLE_GRAPH)
_ASSURANCE_BRANCH_NODES = ("api", "e2e", "fuzz", "performance")
_MECHANICAL_NODE = "mechanical-plan-checks"
_GATE_NODE = "review-gate"
_SCHEMA_DIR = ".graph-runtime/schemas"


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
    layer_bindings: dict[str, LayerInvocationBinding]
    sequenced_events: tuple[SequencedEvent, ...]


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


def validate_pinned_layer_selection(schema: WorkflowSchemaV2) -> tuple[str, ...]:
    """Validate pinned assurance branch predicates are params-only replayable."""
    param_names = frozenset(schema.params)
    errors: list[str] = []
    assurance = schema.graphs.get("assurance")
    if assurance is None:
        return ("graph:assurance: missing assurance graph",)
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
    return tuple(errors)


def evaluate_layer_selection(
    schema: WorkflowSchemaV2,
    params: Mapping[str, object],
) -> tuple[LayerSelectionFact, ...]:
    """Evaluate pinned assurance branch predicates from frozen params."""
    selection_errors = validate_pinned_layer_selection(schema)
    if selection_errors:
        raise ReplayBindingError("ambiguous_graph_wiring", "; ".join(selection_errors))
    assurance = schema.graphs["assurance"]
    facts: list[LayerSelectionFact] = []
    scope = Scope({"params": dict(params)})
    for layer in _ASSURANCE_BRANCH_NODES:
        when = assurance.nodes[layer].when
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
) -> None:
    """Require activation/skip events to agree with predicate classification when present."""
    for fact in selections:
        observed: bool | None = None
        for item in events:
            payload = item.payload
            if payload.get("invocation_id") != assurance_invocation_id:
                continue
            if payload.get("node_id") != fact.layer:
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
    del store
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

    compiled, policy = _load_pinned_definitions(change_dir, root_started, schema_root=schema_root)
    schema_errors = validate_replayable_assurance_schema(compiled.schema)
    if schema_errors:
        raise ReplayBindingError(
            "ambiguous_graph_wiring",
            "; ".join(schema_errors),
        )

    assurance = _bind_assurance_invocation(
        sequenced,
        root_started=root_started,
        compiled=compiled,
    )
    layer_bindings = _bind_layer_invocations(
        sequenced,
        assurance=assurance,
        compiled=compiled,
    )
    return FrozenDefinitionBinding(
        change_id=change_id,
        root_invocation_id=root_invocation_id,
        assurance_invocation_id=assurance.invocation_id,
        graph_digest=root_started.graph_digest,
        gate_definition_source="pinned_schema",
        baseline_policy_digest=root_started.policy_digest,
        policy_source="pinned_runtime_snapshot",
        policy_origin=root_started.policy_origin,
        gate_semantics_digest=root_started.gate_semantics_digest,
        assurance_profile_digest=root_started.assurance_profile_digest,
        compiled=compiled,
        policy=policy,
        assurance_params=dict(assurance.params),
        layer_bindings=layer_bindings,
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
    if layer not in _WIRED_LAYERS:
        raise ReplayBindingError("invalid_evidence", f"layer {layer!r} is not wired for replay binding")
    layer_binding = binding.layer_bindings.get(layer)
    if layer_binding is None:
        raise ReplayBindingError("ambiguous_graph_wiring", f"no bound invocation for layer {layer!r}")

    profile = get_layer_assurance_profile(layer)
    object_store = store or TreeStore(change_dir)
    gate_attempt = _select_gate_attempt(binding.sequenced_events, layer_binding.invocation_id, profile)
    mechanical_attempt = _select_mechanical_attempt(
        binding.sequenced_events,
        layer_binding.invocation_id,
        profile,
        gate_attempt=gate_attempt,
    )
    gate_report = gate_attempt.gate_report
    if gate_report is None:
        raise ReplayBindingError("gate_evidence_unbound", f"missing gate report for layer {layer}")

    checks = _recover_checks_artifact(
        profile=profile,
        gate_report=gate_report,
        gate_tree_id=gate_attempt.target_tree_id,
        change_dir=change_dir,
        store=object_store,
        binding=binding,
    )
    applicable = _is_applicable(checks_doc=checks.model)
    review, data_knowledge = _recover_optional_artifacts(
        profile=profile,
        gate_report=gate_report,
        gate_tree_id=gate_attempt.target_tree_id,
        applicable=applicable,
        change_dir=change_dir,
        store=object_store,
        binding=binding,
    )
    if applicable:
        if review is None:
            raise ReplayBindingError("missing_evidence", f"missing review artifact for applicable layer {layer}")
        if data_knowledge is None:
            raise ReplayBindingError("missing_evidence", f"missing L1 artifact for applicable layer {layer}")
    else:
        review = None
        data_knowledge = None

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
    _assert_baseline_gate(gate_report, baseline, profile)
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
    *,
    schema_root: Path | None,
) -> tuple[CompiledWorkflow, Policy]:
    schema_path = change_dir / _SCHEMA_DIR / f"{started.graph_digest}.json"
    if not schema_path.exists():
        raise ReplayBindingError(
            "pinned_schema_missing",
            f"pinned schema missing at {schema_path}",
        )
    schema_bytes = schema_path.read_bytes()
    try:
        schema = WorkflowSchemaV2.model_validate(json.loads(schema_bytes))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"pinned schema at {schema_path} is invalid: {exc}",
        ) from exc
    root = schema_root or Path.cwd()
    from assurance_agent.workflow.graph.contracts import load_execution_contracts

    compiled = compile_workflow(schema, load_execution_contracts(root))
    if compiled.digest != started.graph_digest:
        raise ReplayBindingError(
            "pinned_schema_digest_mismatch",
            f"pinned schema digest mismatch for {schema_path}: "
            f"expected {started.graph_digest}, compiled {compiled.digest}",
        )

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
    current_semantics = gate_semantics_digest()
    if started.gate_semantics_digest != current_semantics:
        raise ReplayBindingError(
            "gate_semantics_mismatch",
            f"gate semantics digest mismatch: recorded {started.gate_semantics_digest}, current {current_semantics}",
        )
    current_profile = assurance_profile_digest()
    if started.assurance_profile_digest != current_profile:
        raise ReplayBindingError(
            "assurance_profile_mismatch",
            f"assurance profile digest mismatch: recorded {started.assurance_profile_digest}, current {current_profile}",
        )
    return compiled, snap.policy


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
        params=dict(started.params),
        root_tree_id=started.root_tree_id,
        current_tree_id=started.root_tree_id,
    )


def _bind_assurance_invocation(
    events: Sequence[SequencedEvent],
    *,
    root_started: GraphInvocationStartedEvent,
    compiled: CompiledWorkflow,
) -> GraphInvocationStartedEvent:
    root_task_id = f"{root_started.structural_path}:assurance"
    expected_path = f"{root_started.structural_path}/assurance/assurance"
    matches = _matching_started_events(
        events,
        parent_invocation_id=root_started.invocation_id,
        parent_task_id=root_task_id,
        graph_id="assurance",
        structural_path=expected_path,
    )
    if len(matches) != 1:
        raise ReplayBindingError(
            "ambiguous_assurance_invocation",
            f"expected exactly one assurance invocation, found {len(matches)}",
        )
    return matches[0]


def _bind_layer_invocations(
    events: Sequence[SequencedEvent],
    *,
    assurance: GraphInvocationStartedEvent,
    compiled: CompiledWorkflow,
) -> dict[str, LayerInvocationBinding]:
    bindings: dict[str, LayerInvocationBinding] = {}
    for layer, branch_graph in (("api", "api-branch"), ("e2e", "e2e-branch")):
        branch_task_id = f"{assurance.structural_path}:{layer}"
        branch_path = f"{assurance.structural_path}/{layer}/{branch_graph}"
        branch_matches = _matching_started_events(
            events,
            parent_invocation_id=assurance.invocation_id,
            parent_task_id=branch_task_id,
            graph_id=branch_graph,
            structural_path=branch_path,
        )
        if len(branch_matches) != 1:
            raise ReplayBindingError(
                "ambiguous_graph_wiring",
                f"expected exactly one {branch_graph} invocation for layer {layer}, found {len(branch_matches)}",
            )
        branch = branch_matches[0]
        cycle_graph = _LAYER_CYCLE_GRAPH[layer]
        cycle_task_id = f"{branch.structural_path}:review-cycle"
        cycle_path = f"{branch.structural_path}/review-cycle/{cycle_graph}"
        cycle_matches = _matching_started_events(
            events,
            parent_invocation_id=branch.invocation_id,
            parent_task_id=cycle_task_id,
            graph_id=cycle_graph,
            structural_path=cycle_path,
        )
        if len(cycle_matches) != 1:
            raise ReplayBindingError(
                "ambiguous_graph_wiring",
                f"expected exactly one {cycle_graph} invocation for layer {layer}, found {len(cycle_matches)}",
            )
        cycle = cycle_matches[0]
        bindings[layer] = LayerInvocationBinding(
            layer=layer,
            invocation_id=cycle.invocation_id,
            parent_invocation_id=branch.invocation_id,
            parent_task_id=cycle_task_id,
            graph_id=cycle_graph,
            structural_path=cycle.structural_path,
            params=dict(cycle.params),
        )
    return bindings


def _matching_started_events(
    events: Sequence[SequencedEvent],
    *,
    parent_invocation_id: str,
    parent_task_id: str,
    graph_id: str,
    structural_path: str,
) -> list[GraphInvocationStartedEvent]:
    matches: list[GraphInvocationStartedEvent] = []
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
            and event.parent_task_id == parent_task_id
            and event.graph_id == graph_id
            and event.structural_path == structural_path
        ):
            matches.append(event)
    return matches


def _select_gate_attempt(
    events: Sequence[SequencedEvent],
    invocation_id: str,
    profile: LayerAssuranceProfile,
) -> CommittedAttempt:
    attempts = _committed_attempts(events, invocation_id)
    gate_attempts = [
        item
        for item in attempts
        if item.node_id == _GATE_NODE
        and isinstance(item.gate_report, dict)
        and item.gate_report.get("gate_id") == profile.gate_id
    ]
    if not gate_attempts:
        raise ReplayBindingError("no_successful_gate_evidence", f"no committed gate for {profile.layer}")
    terminal_seq = _terminal_seq(events, invocation_id)
    eligible = [item for item in gate_attempts if item.commit_seq <= terminal_seq]
    if not eligible:
        raise ReplayBindingError("gate_evidence_unbound", f"no gate attempt before terminal for {profile.layer}")
    return eligible[-1]


def _select_mechanical_attempt(
    events: Sequence[SequencedEvent],
    invocation_id: str,
    profile: LayerAssuranceProfile,
    *,
    gate_attempt: CommittedAttempt,
) -> CommittedAttempt:
    attempts = _committed_attempts(events, invocation_id)
    gate_reads = gate_attempt.gate_report.get("reads_sha256") if gate_attempt.gate_report else None
    if not isinstance(gate_reads, dict):
        raise ReplayBindingError("gate_evidence_unbound", "gate report missing reads_sha256")
    expected_checks_digest = (
        gate_reads.get(profile.checks_artifact)
        or gate_reads.get(normalize_logical_path(f"change:{profile.checks_artifact}"))
        or gate_reads.get(f"change:{profile.checks_artifact}")
    )
    if expected_checks_digest is None:
        raise ReplayBindingError("gate_evidence_unbound", f"gate report missing checks digest for {profile.layer}")

    candidates: list[CommittedAttempt] = []
    for item in attempts:
        if item.node_id != _MECHANICAL_NODE:
            continue
        if item.commit_seq > gate_attempt.commit_seq:
            continue
        outputs = item.outputs_sha256 or {}
        digest = (
            outputs.get(normalize_logical_path(f"change:{profile.checks_artifact}"))
            or outputs.get(f"change:{profile.checks_artifact}")
            or outputs.get(profile.checks_artifact)
        )
        if digest != expected_checks_digest:
            continue
        candidates.append(item)
    if not candidates:
        raise ReplayBindingError(
            "mechanical_producer_unbound",
            f"no committed mechanical producer matching gate reads for {profile.layer}",
        )
    return candidates[-1]


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
    profile: LayerAssuranceProfile,
    gate_report: dict[str, object],
    gate_tree_id: str,
    change_dir: Path,
    store: TreeStore,
    binding: FrozenDefinitionBinding,
) -> BoundArtifact[PlanCheckDocument]:
    reads = gate_report.get("reads_sha256")
    if not isinstance(reads, dict):
        raise ReplayBindingError("gate_evidence_unbound", "gate report missing reads_sha256")
    checks_path = normalize_logical_path(f"change:{profile.checks_artifact}")
    checks_digest = (
        reads.get(profile.checks_artifact)
        or reads.get(checks_path)
        or reads.get(f"change:{profile.checks_artifact}")
    )
    if not isinstance(checks_digest, str):
        raise ReplayBindingError("missing_evidence", f"missing checks digest for {profile.layer}")
    checks_bytes = _read_bound_bytes(
        store,
        gate_tree_id,
        checks_path,
        expected_digest=checks_digest,
        change_dir=change_dir,
        binding=binding,
    )
    checks_model = PlanCheckDocument.model_validate(json.loads(checks_bytes))
    return cast(
        BoundArtifact[PlanCheckDocument],
        bind_json_artifact(
            logical_path=profile.checks_artifact,
            model=checks_model,
            raw_bytes=checks_bytes,
        ),
    )


def _recover_optional_artifacts(
    *,
    profile: LayerAssuranceProfile,
    gate_report: dict[str, object],
    gate_tree_id: str,
    applicable: bool,
    change_dir: Path,
    store: TreeStore,
    binding: FrozenDefinitionBinding,
) -> tuple[BoundArtifact[PlanReview] | None, BoundArtifact[DataKnowledge] | None]:
    if not applicable:
        return None, None
    reads = gate_report.get("reads_sha256")
    if not isinstance(reads, dict):
        raise ReplayBindingError("gate_evidence_unbound", "gate report missing reads_sha256")

    review_path = normalize_logical_path(f"change:{profile.review_artifact}")
    review_digest = (
        reads.get(profile.review_artifact)
        or reads.get(review_path)
        or reads.get(f"change:{profile.review_artifact}")
    )
    if not isinstance(review_digest, str):
        raise ReplayBindingError("missing_evidence", f"missing review digest for {profile.layer}")
    review_bytes = _read_bound_bytes(
        store,
        gate_tree_id,
        review_path,
        expected_digest=review_digest,
        change_dir=change_dir,
        binding=binding,
    )
    review_model = PlanReview.model_validate(json.loads(review_bytes))
    review = cast(
        BoundArtifact[PlanReview],
        bind_json_artifact(
            logical_path=profile.review_artifact,
            model=review_model,
            raw_bytes=review_bytes,
        ),
    )

    l1_path = normalize_logical_path("repo:.aa/data-knowledge.yaml")
    l1_digest = reads.get(l1_path) or reads.get("repo:.aa/data-knowledge.yaml")
    if not isinstance(l1_digest, str):
        raise ReplayBindingError("missing_evidence", f"missing L1 digest for {profile.layer}")
    l1_bytes = _read_bound_bytes(
        store,
        gate_tree_id,
        l1_path,
        expected_digest=l1_digest,
        change_dir=change_dir,
        binding=binding,
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
    change_dir: Path,
    binding: FrozenDefinitionBinding,
) -> bytes:
    normalized = normalize_logical_path(logical_path)
    try:
        payload = store.read_bytes(tree_id, normalized)
    except FileNotFoundError:
        payload = _read_fallback_bytes(change_dir, binding.change_id, normalized, expected_digest=expected_digest)
    except OSError as exc:
        raise ReplayBindingError("missing_evidence", f"cannot read {normalized}: {exc}") from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_digest:
        raise ReplayBindingError(
            "gate_evidence_drift",
            f"digest mismatch for {normalized}: expected {expected_digest}, got {actual}",
        )
    return payload


def _read_fallback_bytes(
    change_dir: Path,
    change_id: str,
    logical_path: str,
    *,
    expected_digest: str,
) -> bytes:
    parsed = ResourcePath.parse(logical_path)
    candidates: list[Path] = []
    if parsed.root == "change":
        candidates.append(change_dir / parsed.pattern)
        candidates.append(archive_root(change_dir.parents[2]) / change_id / parsed.pattern)
    elif parsed.root == "repo":
        project_root = change_dir.parents[2]
        candidates.append(project_root / parsed.pattern)
    elif parsed.root == "project":
        project_root = change_dir.parents[2]
        candidates.append(project_root / parsed.pattern)
    for path in candidates:
        if not path.exists():
            continue
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() == expected_digest:
            return payload
    raise ReplayBindingError("missing_evidence", f"artifact missing for {logical_path}")


def _is_applicable(*, checks_doc: PlanCheckDocument) -> bool:
    if checks_doc.applicability is None:
        return False
    return bool(checks_doc.applicability.applicable)


def _assert_baseline_gate(
    recorded: dict[str, object],
    baseline: object,
    profile: LayerAssuranceProfile,
) -> None:
    from assurance_agent.workflow.orchestration.gates import FrozenGateReport

    if not isinstance(baseline, FrozenGateReport):
        raise ReplayBindingError("baseline_gate_mismatch", "baseline evaluation failed")
    if recorded.get("gate_id") != profile.gate_id:
        raise ReplayBindingError("baseline_gate_mismatch", "gate id mismatch")
    if recorded.get("verdict") != baseline.verdict.value:
        raise ReplayBindingError(
            "baseline_gate_mismatch",
            f"verdict {recorded.get('verdict')!r} != {baseline.verdict.value!r}",
        )
    recorded_value = recorded.get("value")
    recorded_verdict = recorded.get("verdict")
    if (
        recorded_value is not None
        and recorded_verdict is not None
        and recorded_value != recorded_verdict
        and recorded_value != baseline.verdict.value
    ):
        raise ReplayBindingError(
            "baseline_gate_mismatch",
            f"value {recorded_value!r} != baseline {baseline.verdict.value!r}",
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


__all__ = [
    "BoundLayerReplayInputs",
    "CommittedAttempt",
    "FrozenDefinitionBinding",
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
