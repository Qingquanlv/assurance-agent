"""schema v2 严格 graph 事件模型（ledger 权威）。

每个事件都是 frozen + extra="forbid" 的 pydantic 模型，带 ``source: "graph"``
字面量；严格读路径（``core.events.read_events_strict``）用单个
``TypeAdapter(GraphEvent)`` 校验所有 graph payload。未知 type 或未声明字段
一律视为 ledger 完整性失败。

本模块只允许依赖 ``core.graph_types``，绝不 import ``workflow.graph``，
避免 core → graph 的向上依赖。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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
    params: dict[str, object]
    params_sha256: str
    root_tree_id: str
    max_parallel_tasks: int
    checkpoint_ns: str
    parent_invocation_id: str | None = None
    parent_task_id: str | None = None
    structural_path: str


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


class ResumeAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    checkpoint_ns: str
    node_id: str
    interrupt_id: str


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
    | TaskAttemptAbandonedEvent
    | BudgetConsumedEvent
    | GraphInterruptedEvent
    | GraphResumedEvent
    | SuperstepCommittedEvent
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
    "TaskAttemptAbandonedEvent",
    "BudgetConsumedEvent",
    "GraphInterruptedEvent",
    "GraphResumedEvent",
    "ResumeAnchor",
    "SuperstepCommittedEvent",
    "GraphTerminalEvent",
    "TaskImportedEvent",
    "CheckpointImportedEvent",
]
