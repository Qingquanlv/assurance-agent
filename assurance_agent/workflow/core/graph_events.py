"""schema v2 严格 graph 事件模型（ledger 权威）。

每个事件都是 frozen + extra="forbid" 的 pydantic 模型，带 ``source: "graph"``
字面量；严格读路径（``core.events.read_events_strict``）用单个
``TypeAdapter(GraphEvent)`` 校验所有 graph payload。未知 type 或未声明字段
一律视为 ledger 完整性失败。

本模块只允许依赖 ``core.graph_types``，绝不 import ``workflow.graph``，
避免 core → graph 的向上依赖。
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from assurance_agent.workflow.core.graph_types import ErrorKind


class _GraphEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: Literal["graph"] = "graph"


class GraphInvocationStartedEvent(_GraphEvent):
    type: Literal["graph_invocation_started"]
    invocation_id: str
    entrypoint: str
    graph_id: str
    graph_digest: str
    event_schema_version: int = 1
    ir_digest: str = ""
    ingest_catalog_digest: str = ""
    contract_digests: dict[str, str]
    policy_digest: str = ""
    policy_origin: str = ""
    gate_semantics_digest: str = ""
    assurance_profile_digest: str = ""
    gate_semantics_object_id: str = ""
    topology_safety_semantics_object_id: str = ""
    topology_safety_semantics_digest: str = ""
    commit_safety_semantics_object_id: str = ""
    commit_safety_semantics_digest: str = ""
    params: dict[str, object]
    params_sha256: str
    root_tree_id: str
    max_parallel_tasks: int
    checkpoint_ns: str
    parent_invocation_id: str | None = None
    parent_task_id: str | None = None
    structural_path: str

    @model_validator(mode="after")
    def _require_v4_definition_binding(self) -> Self:
        if self.event_schema_version >= 4:
            if not self.policy_digest:
                raise ValueError("policy_digest is required for event_schema_version >= 4")
            if not self.policy_origin:
                raise ValueError("policy_origin is required for event_schema_version >= 4")
            if not self.gate_semantics_digest:
                raise ValueError("gate_semantics_digest is required for event_schema_version >= 4")
            if not self.assurance_profile_digest:
                raise ValueError("assurance_profile_digest is required for event_schema_version >= 4")
        v6_fields = (
            ("gate_semantics_object_id", self.gate_semantics_object_id),
            ("gate_semantics_digest", self.gate_semantics_digest),
            ("topology_safety_semantics_object_id", self.topology_safety_semantics_object_id),
            ("topology_safety_semantics_digest", self.topology_safety_semantics_digest),
            ("commit_safety_semantics_object_id", self.commit_safety_semantics_object_id),
            ("commit_safety_semantics_digest", self.commit_safety_semantics_digest),
        )
        new_fields = (
            self.gate_semantics_object_id,
            self.topology_safety_semantics_object_id,
            self.topology_safety_semantics_digest,
            self.commit_safety_semantics_object_id,
            self.commit_safety_semantics_digest,
        )
        if self.event_schema_version >= 6:
            for name, value in v6_fields:
                if not value:
                    raise ValueError(f"{name} is required for event_schema_version >= 6")
        else:
            # v1-v5 remain valid without object-ID/topology/commit-safety fields and
            # must not receive synthetic v6 semantic identities.
            if any(new_fields):
                if not all(new_fields):
                    raise ValueError("v6 semantic binding fields must be all-or-none")
                raise ValueError("v6 semantic binding fields require event_schema_version >= 6")
        return self


class NodeActivatedEvent(_GraphEvent):
    type: Literal["node_activated"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    generation_ordinal: int = 0
    activation_id: str
    input_sha256: str
    source_reads_sha256: dict[str, str]


class NodeSkippedEvent(_GraphEvent):
    type: Literal["node_skipped"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    generation_ordinal: int = 0
    expression: str
    input_sha256: str
    source_reads_sha256: dict[str, str]


class FanOutExpandedEvent(_GraphEvent):
    type: Literal["fan_out_expanded"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    generation_ordinal: int = 0
    source_reads_sha256: dict[str, str]
    items: list[object]
    task_keys: list[str]
    task_ids: list[str]


class SuperstepPlannedEvent(_GraphEvent):
    type: Literal["superstep_planned"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    checkpoint_id: str
    task_ids: list[str]


class TaskAttemptStartedEvent(_GraphEvent):
    type: Literal["task_attempt_started"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    attempt_id: str
    node_id: str
    input_sha256: str
    graph_digest: str
    contract_digest: str
    attempt_number: int
    lease_expires_at: str
    started_at: str
    input_snapshot_id: str | None = None
    runtime_context_sha256: str | None = None
    precommit_validator: str | None = None
    # Execution-contract / operation target (e.g. operation:allocate-healing-attempt).
    target: str | None = None


class TaskAttemptSucceededEvent(_GraphEvent):
    type: Literal["task_attempt_succeeded"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    attempt_id: str
    write_set_id: str | None = None
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    frozen_outputs: dict[str, object] = Field(default_factory=dict)
    gate_report: dict[str, object] | None = None
    state_updates: dict[str, object] = Field(default_factory=dict)
    value: object = None
    input_snapshot_id: str | None = None
    runtime_context_sha256: str | None = None
    candidate_validation_receipt_id: str | None = None
    # Full canonical inline intents (optional for historical events; empty default).
    durable_effects: list[dict[str, object]] = Field(default_factory=list)


class TaskSchedulingDeferredEvent(_GraphEvent):
    """Scheduling-level lock deferral: no attempt id/number/budget consumption."""

    type: Literal["task_scheduling_deferred"]
    deferral_id: str
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    node_id: str
    token: str
    reason: str
    deferral_ordinal: int
    retry_policy_digest: str
    next_retry_at: str


class TaskAttemptStoppedEvent(_GraphEvent):
    """业务 STOP（非失败）：child/operation stop 传播为父图终局前的 task 结算。"""

    type: Literal["task_attempt_stopped"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    attempt_id: str
    reason: str
    value: object = None


class TaskAttemptFailedEvent(_GraphEvent):
    type: Literal["task_attempt_failed"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    attempt_id: str
    error_kind: ErrorKind
    message: str
    next_retry_at: str | None = None


class TaskRecoveryRoutedEvent(_GraphEvent):
    type: Literal["task_recovery_routed"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    generation_ordinal: int
    task_id: str
    error_kind: ErrorKind
    message: str
    via: str
    continue_to: str


class TaskAttemptAbandonedEvent(_GraphEvent):
    type: Literal["task_attempt_abandoned"]
    invocation_id: str
    checkpoint_ns: str
    task_id: str
    attempt_id: str
    reason: str
    abandoned_at: str


class BudgetConsumedEvent(_GraphEvent):
    type: Literal["budget_consumed"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    budget_id: str
    consumption_id: str
    task_id: str


class GraphInterruptedEvent(_GraphEvent):
    type: Literal["graph_interrupted"]
    invocation_id: str
    checkpoint_ns: str
    interrupt_id: str
    node_id: str
    checkpoint: str
    actions: list[str]
    audited_reads_sha256: dict[str, str]
    artifact_view: str | None = None
    anchor: "ResumeAnchor | None" = None
    parent_anchor_ref: str | None = None
    revision_owner_invocation_id: str | None = None
    revision_base_tree_id: str | None = None
    revision_view: str | None = None
    revision_paths: list[str] | None = None
    revision_before_sha256: dict[str, str] | None = None
    source_gate_attempt_id: str | None = None
    source_gate_tree_id: str | None = None

    @model_validator(mode="after")
    def _source_pair_all_or_none(self) -> Self:
        has_attempt = self.source_gate_attempt_id is not None
        has_tree = self.source_gate_tree_id is not None
        if has_attempt != has_tree:
            raise ValueError("source gate pair fields must be all-or-none")
        return self


class ResumeAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    checkpoint_ns: str
    node_id: str
    interrupt_id: str


class ManualPlanRevisionEvent(_GraphEvent):
    type: Literal["manual_plan_revision"] = "manual_plan_revision"
    invocation_id: str
    checkpoint_ns: str
    revision_transition_id: str
    interrupt_id: str
    action: Literal["fix_and_proceed"]
    who: str
    reason: str
    audited_reads_sha256: dict[str, str]
    source_gate_attempt_id: str
    source_gate_tree_id: str
    base_tree_id: str
    target_tree_id: str
    logical_paths: list[str]
    before_sha256: dict[str, str]
    after_sha256: dict[str, str]
    resume_anchors: list[ResumeAnchor]


class GraphResumedEvent(_GraphEvent):
    type: Literal["graph_resumed"]
    invocation_id: str
    checkpoint_ns: str
    interrupt_id: str
    action: str
    reason: str
    who: str
    audited_reads_sha256: dict[str, str]
    anchor: ResumeAnchor | None = None
    parent_anchor_ref: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    revision_transition_id: str | None = None
    revision_ordinal: int | None = None
    revision_chain_length: int | None = None
    source_gate_attempt_id: str | None = None
    source_gate_tree_id: str | None = None

    @model_validator(mode="after")
    def _revision_and_source_invariants(self) -> Self:
        rev_fields = (
            self.revision_transition_id,
            self.revision_ordinal,
            self.revision_chain_length,
        )
        present = [field is not None for field in rev_fields]
        if any(present) and not all(present):
            raise ValueError("revision triple fields must be all-or-none")
        if all(present):
            ordinal = self.revision_ordinal
            length = self.revision_chain_length
            assert ordinal is not None and length is not None
            if length < 1:
                raise ValueError("revision_chain_length must be >= 1")
            if not (0 <= ordinal < length):
                raise ValueError("revision_ordinal out of bounds")
        has_attempt = self.source_gate_attempt_id is not None
        has_tree = self.source_gate_tree_id is not None
        if has_attempt != has_tree:
            raise ValueError("source gate pair fields must be all-or-none")
        return self


class SuperstepCommittedEvent(_GraphEvent):
    type: Literal["superstep_committed"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    checkpoint_id: str
    parent_checkpoint_id: str | None = None
    write_set_ids: list[str]
    target_tree_id: str
    state_values: dict[str, object]
    committed_task_ids: list[str] = Field(default_factory=list)


class DurableEffectAcknowledgedEvent(_GraphEvent):
    type: Literal["durable_effect_acknowledged"]
    root_invocation_id: str
    invocation_id: str
    checkpoint_ns: str
    task_id: str
    attempt_id: str
    effect_id: str
    kind: str
    reconciler_semantics_digest: str
    payload_sha256: str
    domain_source_sequence: int
    domain_event_digest: str


class DurableEffectIntegrityFailedEvent(_GraphEvent):
    type: Literal["durable_effect_integrity_failed"]
    invocation_id: str
    checkpoint_ns: str
    task_id: str
    attempt_id: str
    effect_id: str
    reason: str


class GraphTerminalEvent(_GraphEvent):
    type: Literal["graph_completed", "graph_stopped", "graph_failed"]
    invocation_id: str
    checkpoint_ns: str
    reason: str


class TaskImportedEvent(_GraphEvent):
    type: Literal["task_imported"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    structural_path: str
    task_key: str | None = None
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    gate_report: dict[str, object] | None = None


class CheckpointImportedEvent(_GraphEvent):
    type: Literal["checkpoint_imported"]
    invocation_id: str
    checkpoint_ns: str
    fixture_id: str
    fixture_digest: str
    manifest_sha256: str
    input_sha256: dict[str, str]


GraphEvent = Annotated[
    GraphInvocationStartedEvent
    | NodeActivatedEvent
    | NodeSkippedEvent
    | FanOutExpandedEvent
    | SuperstepPlannedEvent
    | TaskAttemptStartedEvent
    | TaskAttemptSucceededEvent
    | TaskAttemptStoppedEvent
    | TaskAttemptFailedEvent
    | TaskSchedulingDeferredEvent
    | TaskRecoveryRoutedEvent
    | TaskAttemptAbandonedEvent
    | BudgetConsumedEvent
    | GraphInterruptedEvent
    | ManualPlanRevisionEvent
    | GraphResumedEvent
    | SuperstepCommittedEvent
    | DurableEffectAcknowledgedEvent
    | DurableEffectIntegrityFailedEvent
    | GraphTerminalEvent
    | TaskImportedEvent
    | CheckpointImportedEvent,
    Field(discriminator="type"),
]

GRAPH_EVENT_ADAPTER: TypeAdapter[GraphEvent] = TypeAdapter(GraphEvent)

__all__ = [
    "GraphEvent",
    "GRAPH_EVENT_ADAPTER",
    "GraphInvocationStartedEvent",
    "NodeActivatedEvent",
    "NodeSkippedEvent",
    "FanOutExpandedEvent",
    "SuperstepPlannedEvent",
    "TaskAttemptStartedEvent",
    "TaskAttemptSucceededEvent",
    "TaskAttemptStoppedEvent",
    "TaskAttemptFailedEvent",
    "TaskSchedulingDeferredEvent",
    "TaskRecoveryRoutedEvent",
    "TaskAttemptAbandonedEvent",
    "BudgetConsumedEvent",
    "GraphInterruptedEvent",
    "ManualPlanRevisionEvent",
    "GraphResumedEvent",
    "ResumeAnchor",
    "SuperstepCommittedEvent",
    "DurableEffectAcknowledgedEvent",
    "DurableEffectIntegrityFailedEvent",
    "GraphTerminalEvent",
    "TaskImportedEvent",
    "CheckpointImportedEvent",
]
