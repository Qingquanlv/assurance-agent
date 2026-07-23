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
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.contracts import ResourceClaims
from assurance_agent.workflow.graph.schema_v2 import (
    EdgeDef,
    NodeDef,
    RetryPolicyDef,
    RouteDef,
    TimeoutPolicyDef,
    WorkflowSchemaV2,
)
from assurance_agent.workflow.orchestration.dsl import Expr


class CompiledNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    graph_id: str
    node_id: str
    declaration_index: int
    topology_rank: int
    definition: NodeDef
    incoming: tuple[EdgeDef, ...]
    outgoing: tuple[EdgeDef, ...]
    routes: tuple[RouteDef, ...]
    # 本 node 自己的保守资源 claim（``graph:<id>`` 为子图 footprint，无 catalog 时
    # 为 global:exclusive）；scheduler 据此做 wave 冲突判定，无需再退回全图 footprint。
    resources: ResourceClaims = Field(default_factory=ResourceClaims)


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


class CompiledWorkflow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    # plan 指定的公开字段名；遮蔽 BaseModel.schema() 方法属有意为之。
    schema: WorkflowSchemaV2  # type: ignore[reportIncompatibleMethodOverride]
    digest: str
    entrypoints: dict[str, CompiledEntrypoint]
    graphs: dict[str, CompiledGraph]
    contract_digests: dict[str, str] = Field(default_factory=dict)


# ---- Plan 语义模型（纯 planner 的输入输出；确定性，无 wall-clock）----


class RuntimeContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    project_root: Path
    repo_root: Path
    change_dir: Path
    change_id: str
    params: dict[str, object] = Field(default_factory=dict)
    parent_session_id: str | None = None


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
    # scheduler 确定性 wave 选择键；planner 尚未回填时默认为 0，退化为 task_id 序。
    topology_rank: int = 0
    declaration_index: int = 0


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


class TaskProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    node_id: str
    status: TaskStatus
    attempts_used: int = 0
    latest_attempt_id: str | None = None
    write_set_id: str | None = None
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    gate_report: dict[str, object] | None = None
    state_updates: dict[str, object] = Field(default_factory=dict)
    value: object = None
    error_kind: ErrorKind | None = None
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
    error_kind: ErrorKind | None = None
    error: str | None = None
    interrupt: InterruptProjection | None = None


class GraphProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    checkpoint_ns: str
    parent_invocation_id: str | None = None
    parent_task_id: str | None = None
    structural_path: str
    graph_digest: str
    contract_digests: dict[str, str]
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
    interrupts: dict[str, InterruptProjection] = Field(default_factory=dict)
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None


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
    action: Literal["fix_and_proceed", "accept_risk", "stop"]
    reason: str
    who: str


class GraphStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    status: Literal["running", "interrupted", "completed", "stopped", "failed"]
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
