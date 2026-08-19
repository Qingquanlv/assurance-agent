"""schema v2 编译产物模型：冻结拓扑 + 稳定 digest；Plan 语义模型；ledger 投影模型。

GraphRuntime 只接受 compiled model，不直接解释原始 YAML。compiled 拓扑一律使用
tuple（而非 list），使 digest 之后的执行顺序不可被调用方原地修改。
Plan 语义模型（RuntimeContext/ExecutableTask/PlanResult 等）是纯 planner 的
输入输出，不含 wall-clock 或线程调度顺序。投影模型（TaskProjection/
GraphProjection 等）是 strict ledger 的纯函数输出，frozen + extra="forbid"，
绝不反向覆盖 ledger。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from assurance_kernel.workflow.core.graph_events import TaskRecoveryRoutedEvent
from assurance_kernel.workflow.core.graph_types import ErrorKind
from assurance_kernel.workflow.graph.contracts import ResourceClaims
from assurance_kernel.workflow.graph.schema_v2 import (
    EdgeDef,
    NodeDef,
    RetryPolicyDef,
    RouteDef,
    TimeoutPolicyDef,
    WorkflowSchemaV2,
)
from assurance_kernel.workflow.orchestration.dsl import Expr


class CompiledNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    graph_id: str
    node_id: str
    declaration_index: int
    topology_rank: int
    definition: NodeDef  # Includes the optional, validated recovery declaration.
    incoming: tuple[EdgeDef, ...]
    outgoing: tuple[EdgeDef, ...]
    routes: tuple[RouteDef, ...]
    # 本 node 自己的保守资源 claim（``graph:<id>`` 为子图 footprint，无 catalog 时
    # 为 global:exclusive）；scheduler 据此做 wave 冲突判定，无需再退回全图 footprint。
    resources: ResourceClaims = Field(default_factory=ResourceClaims)
    exports: tuple[CompiledExport, ...] = ()


class CompiledGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    graph_id: str
    max_supersteps: int
    declaration_order: tuple[str, ...]
    nodes: dict[str, CompiledNode]
    sccs: tuple[tuple[str, ...], ...]
    artifact_symbols: dict[str, str]
    # 保守资源 footprint：全图 node claim 的并集（graph:<id> 递归展开）；
    # 无 catalog 编译时不可推导，为 global:exclusive。
    resource_footprint: ResourceClaims = Field(default_factory=ResourceClaims)


class CompiledEntrypoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    graph_id: str
    allow_expr: Expr | None
    param_overrides: dict[str, object]
    restart: str = "once"  # "once" | "repeatable"


class CompiledWorkflow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    # plan 指定的公开字段名；遮蔽 BaseModel.schema() 方法属有意为之。
    schema: WorkflowSchemaV2  # type: ignore[reportIncompatibleMethodOverride]
    digest: str
    entrypoints: dict[str, CompiledEntrypoint]
    graphs: dict[str, CompiledGraph]
    contract_digests: dict[str, str] = Field(default_factory=dict)
    ingest_catalog_digest: str = ""


# ---- Plan 语义模型（纯 planner 的输入输出；确定性，无 wall-clock）----


class RuntimeContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    _project_lock_scope_owner: object | None = PrivateAttr(default=None)
    _project_lock_scope_nonce: object | None = PrivateAttr(default=None)
    _held_project_lock_tokens: tuple[str, ...] = PrivateAttr(default=())
    _prepared_wave_lease_owner: object | None = PrivateAttr(default=None)
    _prepared_wave_lease_nonce: object | None = PrivateAttr(default=None)
    _prepared_wave_lease: object | None = PrivateAttr(default=None)
    _task_attempt_id: str | None = PrivateAttr(default=None)

    project_root: Path
    repo_root: Path
    change_dir: Path
    change_id: str
    params: dict[str, object] = Field(default_factory=dict)
    parent_session_id: str | None = None
    host_project_root: Path | None = None

    @property
    def resolved_host_root(self) -> Path:
        """Read-only real SUT root.

        Nested subgraphs remap ``project_root`` to the task workspace. Product
        source, ``.aa/config.yaml``, policy, and L1 live-reads must use this
        instead of the agent-writable sandbox.
        """
        if self.host_project_root is not None:
            return self.host_project_root
        return self.project_root

    @property
    def task_attempt_id(self) -> str | None:
        """Scheduler-owned attempt identity for the currently executing task."""
        return self._task_attempt_id

    def with_task_attempt_id(self, attempt_id: str) -> Self:
        """Bind an execution-only attempt ID without changing persisted task input."""
        if not attempt_id.strip():
            raise ValueError("task attempt_id must be non-empty")
        attempted = self.model_copy()
        attempted._task_attempt_id = attempt_id
        return attempted

    def inherited_project_lock_scope(
        self,
        owner: object,
    ) -> tuple[object, tuple[str, ...]] | None:
        """Return the opaque acquisition nonce and tokens to their scheduler."""
        if self._project_lock_scope_owner is not owner or self._project_lock_scope_nonce is None:
            return None
        return self._project_lock_scope_nonce, self._held_project_lock_tokens

    def with_project_lock_scope(
        self,
        owner: object,
        nonce: object,
        tokens: tuple[str, ...],
    ) -> Self:
        """Copy this context with a process-local, non-serializable lock lease."""
        locked = self.model_copy()
        locked._project_lock_scope_owner = owner
        locked._project_lock_scope_nonce = nonce
        locked._held_project_lock_tokens = tokens
        return locked

    def inherited_prepared_wave_lease(self, owner: object) -> object | None:
        """Return the opaque prepared-wave lease inherited from an ancestor scheduler."""
        if self._prepared_wave_lease_owner is not owner or self._prepared_wave_lease_nonce is None:
            return None
        return self._prepared_wave_lease

    def with_prepared_wave_lease(
        self,
        owner: object,
        nonce: object,
        lease: object,
    ) -> Self:
        """Copy this context with a process-local, non-serializable prepared-wave lease."""
        prepared = self.model_copy()
        prepared._prepared_wave_lease_owner = owner
        prepared._prepared_wave_lease_nonce = nonce
        prepared._prepared_wave_lease = lease
        return prepared

    def without_prepared_wave_lease(self) -> Self:
        """Copy this context with any prepared-wave lease cleared (single-use per superstep)."""
        cleared = self.model_copy()
        cleared._prepared_wave_lease_owner = None
        cleared._prepared_wave_lease_nonce = None
        cleared._prepared_wave_lease = None
        return cleared


class ResolvedArtifact(BaseModel):
    """一次 artifact 读取的冻结结果：解析后的 JSON 值 + 实际读取路径的内容 hash。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    value: object
    reads_sha256: dict[str, str]


class ArtifactReader(Protocol):
    """按 tree 读取逻辑路径上的 JSON artifact；缺读/解析失败/hash 漂移时抛出异常。"""

    def read_json(self, tree_id: str, logical_path: str) -> ResolvedArtifact:
        raise NotImplementedError


class BudgetConsumption(BaseModel):
    """planner 给 budget consumer task 的标记：scheduler 据此原子 staging 预算事件。

    ``consumption_id`` 按 ``(invocation_id, checkpoint_ns, budget_id, task_id)``
    经 canonical SHA-256 派生——retry 重计划同一 task，同一成功只消耗一个单位。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    budget_id: str
    consumption_id: str


class EvidenceBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    alias: str
    producer_task_id: str
    symbol: str
    source_sha256: str


class CompiledExport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: str
    from_node: str
    output: str


class RecoveryContext(BaseModel):
    """Frozen typed context delivered only to a dedicated recovery operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_task_id: str
    source_node_id: str
    generation_ordinal: int
    error_kind: ErrorKind
    message: str
    attempts_used: int
    recovery_event: TaskRecoveryRoutedEvent


class ExecutableTask(BaseModel):
    """一次逻辑 node invocation 的可执行单元；retry 时 task_id 不变。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    structural_path: str
    input: object
    input_sha256: str
    contract_digest: str
    retryable_errors: tuple[ErrorKind, ...]
    retry_policy: RetryPolicyDef
    timeout_policy: TimeoutPolicyDef
    target: str
    resources: ResourceClaims
    task_key: str | None = None
    budget: BudgetConsumption | None = None
    evidence_bindings: tuple[EvidenceBinding, ...] = ()
    # scheduler 确定性 wave 选择键；planner 尚未回填时默认为 0，退化为 task_id 序。
    topology_rank: int = 0
    declaration_index: int = 0
    # Retry-only feedback from the previous failed attempt. Kept off the input
    # payload so input_sha256 / task_id stay stable across attempts; AgentHandler
    # injects contract-violation kinds into the prompt so the agent can fix them.
    prior_failure: str | None = None
    prior_error_kind: ErrorKind | None = None
    # Sticky across retries, preserving the exact contract-class failures that
    # occurred so model escalation remains both stable and policy-selective.
    contract_failure_kinds_seen: tuple[ErrorKind, ...] = ()
    recovery: RecoveryContext | None = None
    # Execution-time resolved evidence values keyed by declared alias. Injected
    # by the scheduler from committed producer frozen_outputs; kept off the input
    # payload so input_sha256 / task_id stay stable (mirrors prior_failure).
    resolved_evidence: dict[str, object] = Field(default_factory=dict)


class PlanResult(BaseModel):
    """一次纯 Plan 的输出：ready tasks、待持久化的 strict events 与终局判定。"""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    superstep_id: str
    checkpoint_id: str
    tasks: tuple[ExecutableTask, ...]
    strict_events: tuple[BaseModel, ...] = ()
    terminal: Literal["end", "stop", "fail", "interrupt"] | None = None
    reason: str | None = None


class WaveResult(BaseModel):
    """一次 Execute/Update wave 的冻结结果：成功/失败/中断与 pending write-set。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    superstep_id: str
    succeeded: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    interrupted: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    pending_write_set_ids: tuple[str, ...] = ()
    retry_at: str | None = None


# ---- ledger 投影模型（strict events 的唯一权威视图）----


TaskStatus = Literal["pending", "running", "succeeded", "failed", "abandoned", "interrupted", "stopped"]


GenerationStatus = Literal[
    "activated",
    "skipped",
    "running",
    "succeeded",
    "failed",
    "abandoned",
    "interrupted",
    "stopped",
]


class NodeGeneration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    generation_ordinal: int
    status: GenerationStatus = "activated"
    reached: bool | None = None
    activation_id: str | None = None
    aggregate_task_id: str | None = None
    frozen_outputs: dict[str, object] = Field(default_factory=dict)
    outputs_committed: bool = False
    gate_report: dict[str, object] | None = None
    value: object = None
    fan_out_expansion_id: str | None = None


class NodeHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    latest_generation_ordinal: int = -1
    generations_by_ordinal: dict[int, NodeGeneration] = Field(default_factory=dict)


class TaskProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    node_id: str
    status: TaskStatus
    generation_ordinal: int | None = None
    task_key: str | None = None
    fan_out_child: bool = False
    fan_out_aggregate: bool = False
    attempts_used: int = 0
    latest_attempt_id: str | None = None
    write_set_id: str | None = None
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    frozen_outputs: dict[str, object] = Field(default_factory=dict)
    outputs_committed: bool = False
    gate_report: dict[str, object] | None = None
    state_updates: dict[str, object] = Field(default_factory=dict)
    value: object = None
    error_kind: ErrorKind | None = None
    contract_failure_kinds_seen: tuple[ErrorKind, ...] = ()
    # 失败 attempt 的原始 message（如 subgraph 失败时子图自身的终止原因、或
    # handler 抛出的异常详情）。之前只投影 error_kind，terminal-reason 拼字符串
    # 时无从得知具体原因（尤其 subgraph 节点：任何子图失败都折叠成笼统的
    # "internal"）。ledger event 一直带着 ``message``，这里补上投影字段，让
    # planner 的终止原因可以把它拼进去，不必回挖 ledger jsonl 才能定位根因。
    error: str | None = None
    next_retry_at: str | None = None
    # 最近 attempt 的 lease 到期时刻（来自 task_attempt_started）；lease 文件
    # 丢失时恢复分类退回此字段判断，绝不凭空放宽或收紧。
    lease_expires_at: str | None = None
    input_snapshot_id: str | None = None
    runtime_context_sha256: str | None = None
    candidate_validation_receipt_id: str | None = None
    precommit_validator: str | None = None
    # Execution-contract / operation target stamped from task_attempt_started.
    target: str | None = None
    durable_effects: tuple[dict[str, object], ...] = ()
    acknowledged_effect_ids: tuple[str, ...] = ()
    deferral_ordinal: int = 0
    latest_deferral_id: str | None = None


class FanOutExpansion(BaseModel):
    """一次冻结的 fan-out 展开：items、display keys、structural child IDs 与 source hashes。

    展开只在零 child 进入 ledger 时可重放；重放前对 ``source_reads_sha256``
    做 drift 检查，任何漂移 fail closed（``fan_out_source_drift``），绝不重算
    不同的 item 列表。display key 只用于展示/prompt，永不成为 state key。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[object, ...]
    task_keys: tuple[str, ...]
    task_ids: tuple[str, ...]
    source_reads_sha256: dict[str, str]


class InterruptProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interrupt_id: str
    checkpoint_ns: str
    node_id: str
    checkpoint: str
    actions: tuple[str, ...]
    audited_reads_sha256: dict[str, str]
    artifact_view: str | None = None
    resolved_action: str | None = None
    revision_owner_invocation_id: str | None = None
    revision_base_tree_id: str | None = None
    revision_view: str | None = None
    revision_paths: tuple[str, ...] | None = None
    revision_before_sha256: dict[str, str] | None = None
    source_gate_attempt_id: str | None = None
    source_gate_tree_id: str | None = None


class RecoveryProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    node_id: str
    generation_ordinal: int
    error_kind: ErrorKind
    message: str
    via: str
    continue_to: str


# ---- task 执行结果（TaskHandler → NodeRunner → scheduler 的唯一返回通道）----


class TaskResult(BaseModel):
    """一次物理 task 执行的冻结结果；handler 只返回它，绝不直接写 strict events。

    ``write_set_id``/``outputs_sha256`` 由已自行冻结 write-set 的 handler（agent、
    subgraph）填写；其余 write-capable handler 留空，由 scheduler 在成功事务中
    冻结并持久化（Task 10）。``gate_report`` 是 gate handler 的冻结裁决载体，
    planner 只读其中的 ``value``/``verdict`` 键。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["succeeded", "failed", "interrupted", "stopped"]
    value: object = None
    state_updates: dict[str, object] = Field(default_factory=dict)
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    gate_report: dict[str, object] | None = None
    write_set_id: str | None = None
    frozen_outputs: dict[str, object] = Field(default_factory=dict)
    candidate_outputs: dict[str, object] = Field(default_factory=dict)
    error_kind: ErrorKind | None = None
    error: str | None = None
    interrupt: InterruptProjection | None = None
    durable_effects: tuple[dict[str, object], ...] = ()


class GraphProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    checkpoint_ns: str
    parent_invocation_id: str | None = None
    parent_task_id: str | None = None
    structural_path: str
    graph_digest: str
    event_schema_version: int = 6
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
    capability_catalog_digest: str = ""
    product_id: str = ""
    params: dict[str, object]
    root_tree_id: str
    current_tree_id: str
    latest_checkpoint_id: str | None = None
    event_seq: int = 0
    supersteps: int = 0
    state_values: dict[str, object] = Field(default_factory=dict)
    tasks: dict[str, TaskProjection] = Field(default_factory=dict)
    budgets: dict[str, int] = Field(default_factory=dict)
    fan_out_expansions: dict[str, FanOutExpansion] = Field(default_factory=dict)
    node_histories: dict[str, NodeHistory] = Field(default_factory=dict)
    interrupts: dict[str, InterruptProjection] = Field(default_factory=dict)
    recoveries: dict[str, RecoveryProjection] = Field(default_factory=dict)
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None
    # Append-only v4/v5 topology audit receipt id (D10); never rewrites the root.
    topology_compatibility_receipt_id: str | None = None
    # D18 supersede audit id when this invocation (or its root) was superseded.
    supersede_id: str | None = None


class WorkflowStateProjection(BaseModel):
    """``workflow-state.yaml`` 兼容视图：仅人工/报告用途，运行时决策从不读它。

    每个字段都只能从 ``GraphProjection`` 派生（``project_workflow_state``）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None
    latest_checkpoint_id: str | None = None
    event_seq: int = 0
    # node 摘要：node_id -> 该 node 产生的 task_id（稳定排序）。
    nodes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    tasks: dict[str, TaskProjection] = Field(default_factory=dict)
    pending_interrupts: tuple[InterruptProjection, ...] = ()
    budgets: dict[str, int] = Field(default_factory=dict)


# ---- GraphRuntime 公开命令/结果模型（Task 11）----


class ResumeCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interrupt_id: str
    action: str = Field(min_length=1)
    reason: str
    who: str
    # Optional structured resume payload, forwarded onto the first-layer
    # graph_resumed event verbatim. Payload-model validation (payload_model_id /
    # payload_model_schema_digest) is a later increment; passthrough only for now.
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("action")
    @classmethod
    def action_is_nonblank(cls, action: str) -> str:
        if not action.strip():
            raise ValueError("action must not be blank")
        return action


GraphLifecycleStatus = Literal["running", "interrupted", "completed", "stopped", "failed"]


class GraphStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    status: GraphLifecycleStatus
    checkpoint_id: str | None
    event_seq: int
    superstep: int
    running_tasks: tuple[str, ...]
    pending_tasks: tuple[str, ...]
    pending_write_sets: tuple[str, ...]
    pending_interrupts: tuple[InterruptProjection, ...]
    next_retry_at: str | None
    budgets: dict[str, int]
    terminal_reason: str | None
    recovery_state: Literal["revision_resume_recovery_pending"] | None = None
    unacknowledged_durable_effects: tuple[str, ...] = ()


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    status: GraphStatus
    exit_code: Literal[0, 20, 30, 40]
    reason: str


class ImportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    checkpoint_id: str
    imported_tasks: tuple[str, ...]


# ---- 显式 checkpoint import manifest（Task 13）----


class ImportedGate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    verdict: str
    reads_sha256: dict[str, str]


class ImportedTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    graph: str
    node: str
    task_key: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    gate: ImportedGate | None = None


class ImportedBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    budget_id: str
    consumption_id: str
    task_path: str


class ImportManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["2"]
    entrypoint: str
    source_kind: Literal["eval-fixture", "benchmark-seed", "v1-artifact-import"]
    fixture_id: str
    fixture_digest: str
    inputs: dict[str, str] = Field(default_factory=dict)
    completed: tuple[ImportedTask, ...] = ()
    budgets: tuple[ImportedBudget, ...] = ()
