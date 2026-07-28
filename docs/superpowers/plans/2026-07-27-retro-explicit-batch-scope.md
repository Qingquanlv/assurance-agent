# Retro Explicit Batch Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现设计 `docs/superpowers/specs/2026-07-27-retro-explicit-batch-scope-design.md`：自动 Retro 只分析 Orchestrator 明确传入的 Batch Change 集合；不论成员或 Retro stage 成功与否都形成可审计结果，并在 Improvement Ledger 暂不可写时通过 durable outbox 延迟闭环。

**Architecture:** Orchestrator 是 Batch membership 的唯一权威，Retro collect 把 manifest 绑定到 immutable window，再把缺失、损坏、非 terminal 与部分证据降级成 typed gap，而不是入口错误。Graph 内 recovery 处理可路由的 stage failure；`RetroSupervisor` 只补偿 Graph 外的 compile/dispatch/freeze/finalize failure，并且只能写固定 status/failure/outbox 路径。Eval 向 `qa/eval/**` 发布紧凑 projection，Retro 不再同步 raw `eval/out/**`。Benchmark 维护 durable batch manifest，并通过 canonical `aa retro --batch-manifest` 入口启动整个闭环。

**Tech Stack:** Python 3.11、uv、Click、Pydantic v2、YAML Graph DSL、JSONL event ledger、Bash benchmark harness、pytest、ruff、pyright、import-linter。

## Global Constraints

- 本计划以当前工作树中的 Retro v3 实现为基线，不以 bare `HEAD` 为基线。开始 Task 1 前先提交或另行保存已经 review 的相关改动；禁止用新 worktree 丢失当前未提交的 `retro_v3.py`、`slices.py`、`assemble.py`、Graph 与 benchmark 改动。
- 不读取、不迁移历史 `qa/retro/**`。普通 Batch Retro 只读本批 Change 原始证据、与本批关联的 `qa/eval/**` projection，以及全局 Improvement Ledger 的去重状态。
- 不改变 Problem fingerprint 或 Improvement fingerprint。相同 fingerprint 的跨批结果仍追加 `improvement_evidence_linked`。
- `completed_with_gaps` 与 `pending_reconcile` 是 Retro 观测结果，不覆盖 Full、Issue、archive、Eval 或 Benchmark 的原 verdict。
- Graph workspace 与 execution contract 继续 fail-closed。不得用扩大 `project:eval/**` write authorization 修复嵌套 SUT false positive。
- Supervisor 不是任意写入旁路；其持久化 API 只允许当前 `qa/retro/<retro-id>/{pipeline-failure.json,retro-status.json}`、`qa/improvements/outbox/<retro-id>.json` 和现有 Improvement reconciler。
- 新 JSON artifact 使用 canonical bytes（UTF-8、sorted keys、紧凑分隔符、单个尾换行）、SHA-256 digest、atomic replace 和 immutable/idempotent 写入。
- 每个 Task 先写失败测试，再做最小实现；Task 结束执行该 Task 的定向测试、`uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright` 和 `uv run lint-imports` 后单独 commit。禁止 `git add -A`，因为工作树含用户的其它改动和 benchmark 日志。
- 最终 Gate 额外执行 `uv run pytest -v` 与 `bash scripts/packaging_smoke_test.sh`。

## Dependency Order

```text
typed artifacts + Graph object params
  -> validated Batch window
  -> tolerant Issue/Workflow/Eval evidence collection
  -> deterministic fallback + outbox
  -> Graph recovery + RetroSupervisor
  -> CLI batch manifest
  -> Benchmark durable manifest
  -> acceptance / docs
```

---

### Task 1: 增加 Batch、pipeline failure 与 Retro status typed artifacts

**Files:**
- Create: `assurance_agent/artifacts/models/retro_batch.py`
- Modify: `assurance_agent/artifacts/canonical.py`（当前工作树已有，先固化为共享 API）
- Modify: `assurance_agent/artifacts/models/retro_v3.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `assurance_agent/retro/candidates.py`
- Modify: `assurance_agent/workflow/improvements/projection.py`
- Modify: `assurance_agent/workflow/improvements/ledger.py`
- Modify: `assurance_agent/workflow/improvements/review.py`
- Modify: `assurance_agent/workflow/graph/workspace.py`（只加 parity 保护与保留原因，不迁移 tree encoder）
- Modify: `assurance_agent/workflow/graph/schema_v2.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Test: `tests/unit/artifacts/test_retro_batch_models.py`
- Create: `tests/unit/artifacts/test_canonical.py`
- Test: `tests/unit/artifacts/test_registry.py`
- Test: `tests/unit/workflow/graph/test_schema_v2.py`
- Test: `tests/unit/workflow/graph/test_compiler.py`
- Test: `tests/unit/retro/test_candidates.py`
- Test: `tests/unit/workflow/improvements/test_projection.py`
- Test: `tests/unit/workflow/improvements/test_ledger.py`
- Test: `tests/unit/workflow/improvements/test_review.py`

**Interfaces:**

```python
class RetroBatchMember(BaseModel):
    change_id: str
    execution_status: Literal[
        "completed", "failed", "stopped", "hard_timeout",
        "cancelled", "running", "not_started",
    ]
    evidence_availability: Literal["complete", "partial", "absent"]

class RetroBatchScope(BaseModel):
    schema_version: Literal["1"]
    batch_id: str
    status: Literal["complete", "incomplete"]
    members: tuple[RetroBatchMember, ...]

class RetroPipelineFailure(BaseModel):
    schema_version: Literal["1"]
    failure_id: str
    retro_id: str
    batch_id: str | None
    stage: str
    node_id: str | None
    error_kind: str
    message_fingerprint: str
    runtime_event_id: str | None
    occurred_at: datetime

class RetroPipelineFailureDocument(BaseModel):
    schema_version: Literal["1"]
    retro_id: str
    failures: tuple[RetroPipelineFailure, ...]

class RetroRunStatus(BaseModel):
    schema_version: Literal["1"]
    retro_id: str
    batch_id: str | None
    result: Literal["completed", "completed_with_gaps", "pending_reconcile"]
    improvement_ids: tuple[str, ...]
    outbox_id: str | None
    failure_ids: tuple[str, ...]

class RetroInvocationResult(BaseModel):
    status: RetroRunStatus | None
    result: Literal[
        "completed", "completed_with_gaps", "pending_reconcile", "technical_failure"
    ]
```

- `RetroWindow` 增加 `batch_scope: RetroBatchScope | None = None`；`RetroContextV3.window` 原样冻结该对象。
- `RetroSourceDescriptor.kind` 增加 `batch_manifest` 与 `retro_pipeline_failure`，使 synthetic gap/failure ID 能进入现有 `ImprovementSourceRefs.workflow_evidence_ids` 的可解析 namespace；不为 Review Subject 另造 source-ref 类型。
- `ParamDef.type` 增加 `"object"`。compiler 只接受 JSON-compatible mapping，拒绝 list/scalar；default 也执行同一验证。
- Registry 注册 `qa/retro/<retro-id>/window.json`、`pipeline-failure.json`、`retro-status.json` 和 `qa/improvements/outbox/<retro-id>.json` 的 path-specific schema。`technical_failure` 只存在于无法落盘时返回给调用方的 `RetroInvocationResult`，绝不能写进 `retro-status.json`。
- 当前工作树已有 `artifacts/canonical.py`，但 `retro/candidates.py`、Improvement projection/ledger/review 仍有同格式实现。先用 byte-parity tests 把这些“sorted keys + compact separators + 单尾换行”的调用迁到 `canonical_json_bytes/sha256_bytes`。`workflow/graph/workspace.py` 的 tree-object bytes **没有尾换行且决定 tree ID**，明确保留本地 encoder，不做行为迁移。

- [ ] **Step 1: 写模型失败测试。** 覆盖未知 status、重复成员、非 canonical 排序、`complete` 携带 partial member、空 batch ID、failure 中原始 exception payload、failure document 的排序/去重，以及持久化 status 拒绝 `technical_failure`。
- [ ] **Step 2: 写 canonical helper parity 测试。** 固定 projection/candidate/review/ledger fixture 在迁移前后 bytes 与 digest 完全一致；单独锁定 workspace tree encoder 无尾换行、tree ID 不变。
- [ ] **Step 3: 写 Graph 参数失败测试。** `type: object` 接受 mapping/default；字符串、数组和包含非 JSON 值的 mapping 在 compile 时失败。
- [ ] **Step 4: 运行** `uv run pytest tests/unit/artifacts/test_retro_batch_models.py tests/unit/artifacts/test_canonical.py tests/unit/artifacts/test_registry.py tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_compiler.py tests/unit/retro/test_candidates.py tests/unit/workflow/improvements/test_projection.py tests/unit/workflow/improvements/test_ledger.py tests/unit/workflow/improvements/test_review.py -v`；预期因新模型/`object` param/parity migration 尚不存在而 FAIL。
- [ ] **Step 5: 实现模型、共享 canonical helper 迁移、registry 与 compiler 支持。** canonical 排序定义为按 `change_id` 升序，错误消息包含稳定 reason code，不包含整个输入对象；workspace tree encoder 保持独立并补说明。
- [ ] **Step 6: 重跑定向测试和全局质量门禁并通过。** Commit：`git commit -m "feat: add retro batch and closure artifacts"`。

---

### Task 2: 将显式 Batch scope 绑定到 Retro window

**Files:**
- Modify: `assurance_agent/retro/window.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Test: `tests/unit/retro/test_window.py`
- Test: `tests/unit/workflow/graph/test_retro_ops.py`
- Test: `tests/unit/workflow/graph/test_retro_workflow.py`

**Interfaces:**

```python
class RetroWindowSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    change_ids: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    last: int | None = None
    batch_scope: RetroBatchScope | None = None

def validate_batch_selection(selection: RetroWindowSelection) -> RetroBatchScope | None:
    """Validate identity equality and ordering before any evidence read."""

class BatchScopeContractError(AaError):
    error_kind: Literal["batch_scope_invalid"] = "batch_scope_invalid"
    reason_code: str

def resolve_retro_window(
    project_root: Path,
    selection: RetroWindowSelection,
) -> ResolvedRetroWindow:
    """Resolve exactly explicit members and retain missing/non-terminal members."""
```

- Batch mode requires `change_ids == tuple(member.change_id for member in members)` byte-for-byte after canonical validation. It is mutually exclusive with `last/since/until`.
- `RetroWindowSelection` 保持现有 frozen Pydantic model 与 `model_validator`；只把隐式 `last=10` 改为 `None` 并加入 batch field，不改成 dataclass。`selection_from_options()` 的人工 `last` 由 CLI 明确传入。
- `RetroSelectionSnapshot.mode` 继续使用现有 `"change_ids"`，不新增平行 `"batch"` mode；`RetroWindow.batch_scope is not None` 是自动 Batch 与人工显式 Change 集合的权威区别。
- Resolution never drops a missing or non-terminal member. It records availability downgrade from manifest `complete` to observed `partial/absent`; it never upgrades manifest availability.
- Runtime only fills a safe `retro_id`; it must not manufacture `retro_last=10`, infer directories or synthesize Batch membership.
- Workflow schema adds `batch_scope: {type: object, default: {}}` and passes it unchanged to `retro-collect-v3`.

- [ ] **Step 1: 写失败测试。** 两个显式 members（一个 archived terminal、一个 missing/running）都出现在 resolved window；旧 Change 不混入；11 个成员不截断；snapshot mode 为 `change_ids` 且 window 保留完整 batch scope；集合不一致抛带 `error_kind=batch_scope_invalid` 的 `BatchScopeContractError`；manifest availability 只允许降级。
- [ ] **Step 2: 运行** `uv run pytest tests/unit/retro/test_window.py tests/unit/workflow/graph/test_retro_ops.py tests/unit/workflow/graph/test_retro_workflow.py -v`；预期 FAIL。
- [ ] **Step 3: 实现 Batch parser、validator 与 exact selection。** 保持人工 `--change/--last/--since/--until` 的现有路径不变。
- [ ] **Step 4: 更新 Graph params 与 handler。** 为 contract failure 返回结构化 `error_kind=batch_scope_invalid`，不从 exception message 子串推断错误类型；Task 6/8 必须捕获该类型并转入 batch-contract fallback，Task 2 本身不得把它当正常 collect 结果。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: bind retro windows to explicit batch scope"`。

---

### Task 3: 将 Issue 与 Workflow 读取改成 Batch-tolerant evidence collection

**Files:**
- Modify: `assurance_agent/workflow/issues/history_models.py`
- Modify: `assurance_agent/workflow/issues/history.py`
- Modify: `assurance_agent/retro/workflow_history.py`
- Modify: `assurance_agent/retro/slices.py`
- Modify: `assurance_agent/artifacts/models/retro_v3.py`
- Test: `tests/unit/workflow/issues/test_history.py`
- Test: `tests/unit/retro/test_workflow_history.py`
- Test: `tests/unit/retro/test_slices.py`

**Interfaces:**

```python
class IssueWindowSelection(BaseModel):
    change_ids: tuple[str, ...]
    allow_member_gaps: bool = False

class BatchMemberEvidenceGapSignal(_SignalBase):
    signal_type: Literal["batch_member_evidence_gap"]
    change_id: str
    execution_status: str
    domain: Literal["issue", "workflow", "eval"]
    reason_code: Literal[
        "workspace_missing", "non_terminal", "ledger_missing",
        "ledger_corrupt", "digest_drift", "projection_missing",
        "projection_corrupt",
    ]
```

- `allow_member_gaps=False` 保留人工/旧调用方的 strict 行为；Batch collect 传 `True`。
- 每个 Change 独立捕获 missing/corrupt/digest errors，拒绝消费该 member 的不可信 domain 内容，并继续其它 member/domain。
- project Problem ledger 缺失或损坏时 Issue domain 整体 incomplete，但 Workflow/Eval 仍继续。
- `running/not_started` 不读取可能仍在写入的 Change ledger，直接生成 gap。
- 每个 gap 的稳定 `signal_id` 同时写入 `source_refs.workflow_evidence_ids`，由 Batch manifest 的 `RetroSourceDescriptor.evidence_ids` 解析；把该类加入现有 `Signal` discriminated union，不创建第二套 source-ref shape。
- `RetroContext.integrity.reasons` 使用稳定格式 `batch_member_evidence_gap:<change-id>:<execution-status>:<domain>:<reason-code>`。

- [ ] **Step 1: 写失败测试。** complete+missing+corrupt 三成员中完整 member 仍生成 evidence；missing/corrupt 各生成稳定 gap；strict mode 仍抛原异常；running member 的 ledger 即使存在也不读取。
- [ ] **Step 2: 运行** `uv run pytest tests/unit/workflow/issues/test_history.py tests/unit/retro/test_workflow_history.py tests/unit/retro/test_slices.py -v`；预期 FAIL。
- [ ] **Step 3: 为 Issue reader 添加 opt-in tolerant mode。** 不在 reader 中吞掉 project-wide IO permission error；该类错误交给 Supervisor。
- [ ] **Step 4: 为 Workflow reader 添加逐成员隔离并在 slices 层统一物化 gap signal。** 排序键为 `(change_id, domain, reason_code)`，相同 gap 幂等去重；Agent 只补充领域分析，collect 产生的 deterministic gap signals 在 assemble 时不可被 Agent 输出覆盖。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: collect partial retro batch evidence"`。

---

### Task 4: 发布 compact Eval projection 并移除 Retro 对 raw eval 的依赖

**Files:**
- Create: `assurance_agent/artifacts/models/eval_projection.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `assurance_agent/eval/report.py`
- Modify: `assurance_agent/eval/runner.py`
- Modify: `assurance_agent/eval/types.py`
- Modify: `assurance_agent/retro/eval_history.py`
- Modify: `assurance_agent/retro/slices.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Test: `tests/unit/eval/test_report.py`
- Test: `tests/unit/eval/test_runner.py`
- Test: `tests/unit/retro/test_eval_history.py`
- Test: `tests/integration/test_eval_cli.py`

**Interfaces:**

```python
class EvalRunProjection(BaseModel):
    schema_version: Literal["1"]
    run_id: str
    suite: str
    verdict: EvalVerdict
    started_at: str
    completed_at: str
    source_change_ids: tuple[str, ...]
    failure_signature: str | None
    sample_ids: tuple[str, ...]
    raw_report_sha256: str

class EvalProjectionConflict(AaError):
    run_id: str

def write_run_report(
    run_dir: Path,
    manifest: RunManifest,
    metrics: SuiteMetrics,
    gate: EvalGateResult,
    *,
    projection_root: Path | None = None,
) -> None:
    """Write raw report and, when supplied, immutable qa/eval projection."""
```

- projection path 是 `<sut>/qa/eval/runs/<run-id>/report.json`。
- 同一 run ID 相同 canonical bytes 是 replay；不同 bytes 抛本 Task 明确定义的 `EvalProjectionConflict`。
- `failure_signature` 只由稳定 failure kind/sample IDs 派生，不复制日志或异常文本。
- Batch mode 只读取 `source_change_ids` 与 Batch members 相交的 projection；空关联 benchmark metrics 不参与 Retro。
- execution contract 从 `project:eval/**` 改为只读/同步 `project:qa/eval/**`。

- [ ] **Step 1: 写失败测试。** raw report 与 compact projection 同时落盘；projection 不含 samples/SUT/log/token；相同写幂等、不同写冲突；只选择与 Batch 相交 run；损坏 projection 生成 Eval gap 而不阻断其它域。
- [ ] **Step 2: 运行** `uv run pytest tests/unit/eval/test_report.py tests/unit/eval/test_runner.py tests/unit/retro/test_eval_history.py tests/integration/test_eval_cli.py -v`；预期 FAIL。
- [ ] **Step 3: 实现 typed projection 和双写。** runner 把 `sut_dir` 作为 `projection_root`；raw eval schema 保持现有兼容性。
- [ ] **Step 4: 切换 Retro reader 与 execution contract。** 删除 collect 对 raw `eval/out/**` 的 reads/synchronized claim。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: project compact eval evidence for retro"`。

---

### Task 5: 修正 workspace 顶层 Issue ledger 例外

**Files:**
- Modify: `assurance_agent/workflow/graph/workspace.py`
- Test: `tests/unit/workflow/graph/test_workspace.py`
- Test: `tests/unit/workflow/graph/test_read_isolation.py`
- Test: `tests/unit/eval/test_eval_import_replay.py`

**Interfaces:**

```python
def _is_top_level_issue_ledger(rel: PurePosixPath) -> bool:
    parts = rel.parts
    return (
        len(parts) == 5
        and parts[0] == "qa"
        and parts[1] in {"changes", "archive"}
        and parts[3:] == ("issues", "events.jsonl")
    )
```

- `_is_excluded_rel` 只有命中上述 exact shape 才覆盖 `qa/changes/**`/`qa/archive/**` 排除规则。
- 位于 `eval/**`、task workspace、sample SUT 或任意 excluded ancestor 下的同名 suffix 继续 excluded。
- 将现有仅覆盖 `qa/changes/...` 的例外扩展到 `qa/archive/...` 是 design §9 明确要求的 capture 可见性扩展，不是 incidental cleanup；两类合法路径分别加测试。
- `_walk()` 在 capture 与 freeze 的 current-tree 重扫中使用该 predicate。`materialize()` 不重新执行 predicate，只物化已经过滤的 tree manifest；测试应证明 excluded path 从未进入 manifest，因此也不会被 materialize。synchronized ledger overlay 保持独立例外。
- 真实 project-root `_walk()` 会先剪枝顶层 `eval/`，因此嵌套 Eval SUT false positive 在当前遍历入口是潜在风险而非稳定复现；本 Task 修的是 predicate 本身，并用直接 predicate/tree fixture 锁定，raw Eval read removal 由 Task 4 完成。

- [ ] **Step 1: 写参数化失败测试。** `qa/changes/...` 与新增的 `qa/archive/...` 两个合法顶层路径可见；直接 predicate/tree fixture 中的 `eval/out/runs/R/samples/S/sut/qa/changes/C/issues/events.jsonl`、`nested/qa/changes/...` 和多一层目录均不可见；freeze 不把 excluded 文件判为未授权删除；materialize 只复现 manifest 内容。
- [ ] **Step 2: 运行** `uv run pytest tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_read_isolation.py tests/unit/eval/test_eval_import_replay.py -v`；预期至少嵌套 SUT case FAIL。
- [ ] **Step 3: 用 exact predicate 替换 suffix 判断。** 不增加 Retro write 权限。
- [ ] **Step 4: 通过测试与质量门禁。** Commit：`git commit -m "fix: scope issue ledger workspace exception"`。

---

### Task 6: 对全证据缺失和 stage failure 生成确定性 process Candidate

**Files:**
- Create: `assurance_agent/retro/fallback.py`
- Modify: `assurance_agent/artifacts/models/retro_v3.py`
- Modify: `assurance_agent/retro/assemble.py`
- Modify: `assurance_agent/retro/candidates.py`
- Modify: `assurance_agent/workflow/retro_outputs.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Test: `tests/unit/retro/test_fallback.py`
- Test: `tests/unit/retro/test_assemble.py`
- Test: `tests/unit/workflow/graph/test_retro_ops.py`

**Interfaces:**

```python
def candidate_from_evidence_gaps(
    *, retro_id: str, batch_scope: RetroBatchScope,
    gaps: Sequence[BatchMemberEvidenceGapSignal], context_sha256: str,
) -> ImprovementCandidateV3:
    """Build one stable workflow_improvement without an LLM call."""

def candidate_from_pipeline_failure(
    failure: RetroPipelineFailure,
    *, context_sha256: str,
) -> ImprovementCandidateV3:
    """Target assurance-agent:retro:<stage>; intent uses stage + error_kind."""
```

- 所有 member 在 Issue/Workflow/Eval 都无领域 evidence 时不调三个 analyzer Agent，直接产生 evidence-gap Candidate。
- collect/assemble 之前失败时，Supervisor 先物化三个 canonical empty typed slices（共享同一 batch/window、integrity incomplete、source descriptor 指向 failure envelope），再构造最小但 schema-valid 的 `RetroContextV3`：三个 domain status 为 failed，并携带一个 `RetroPipelineFailureSignal`。已有可信 context 时复用并附加 failure signal。因而所有 fallback Candidate 都有真实 slice digests 和非空 `context_sha256`，可继续使用现有 reconciler/`ImprovementAcceptStatus`。
- 对 `batch_scope_invalid` 不把非法 manifest 重新包装成可信 `RetroBatchScope`；fallback window 使用 `mode="change_ids"`、空 trusted Change 集合和 `batch_scope=None`，只在 failure envelope 中保留通过安全标识校验的 optional batch ID/reason code。
- pipeline failure Candidate 的 fingerprint 输入不使用自由 message；`message_fingerprint` 只用于审计。
- incomplete context 继续允许 `prompt_improvement`、`fixture_improvement`、`test_improvement`、`workflow_improvement`，只禁止 `domain_knowledge`。
- proposer invalid output、assemble failure 和 analyzer exhausted failure 均使用同一 typed envelope；不递归启动 Retro。
- 将 `RetroPipelineFailureSignal` 加入现有 `Signal` discriminated union，字段包含稳定 `failure_id/stage/error_kind`；fallback Candidate 的 `signal_ids` 与 `ImprovementSourceRefs.workflow_evidence_ids` 均引用该 signal/failure namespace，满足 `ImprovementCandidateV3` 的既有追溯约束。

- [ ] **Step 1: 写失败测试。** 全 absent 产生一个 deterministic workflow Candidate 且 analyzer 调用数为零；collect 前 failure 生成三个可验 digest 的 empty slices、合法 context 和非空 context digest；相同 stage/error kind、不同 message 得到同一 intent/fingerprint；incomplete process Candidate 可接受，domain knowledge 被拒绝。
- [ ] **Step 2: 运行** `uv run pytest tests/unit/retro/test_fallback.py tests/unit/retro/test_assemble.py tests/unit/workflow/graph/test_retro_ops.py -v`；预期 FAIL。
- [ ] **Step 3: 实现纯 fallback builder。** 固定 target、rationale、verification suite 和 success criteria；`BatchScopeContractError` 生成 `batch_contract` stage、`batch_scope_invalid` kind 的 envelope/Candidate。gap/failure 先获得稳定 evidence ID，并经 Task 1 扩展的 `RetroSourceDescriptor` 进入现有 `ImprovementSourceRefs`，禁止无来源 Candidate。
- [ ] **Step 4: 接入 assemble/handler 的 no-agent 与 failure 分支。** 原有健康零信号 NOOP 仍保持 NOOP；只有 evidence gap 或 pipeline failure 才产生 fallback Candidate。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: create deterministic retro fallback candidates"`。

---

### Task 7: 增加 Improvement reconcile outbox 与幂等 drain

**Files:**
- Create: `assurance_agent/workflow/improvements/outbox.py`
- Modify: `assurance_agent/workflow/improvements/reconciler.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Test: `tests/unit/workflow/improvements/test_outbox.py`
- Test: `tests/unit/workflow/improvements/test_reconciler.py`
- Test: `tests/integration/test_retro_improvement_workflow.py`

**Interfaces:**

```python
class ImprovementOutboxEntry(BaseModel):
    schema_version: Literal["1"]
    retro_id: str
    candidate_sha256: str
    context_sha256: str
    context: RetroContextV3
    candidate: ImprovementCandidateV3
    pipeline_failure: RetroPipelineFailure | None

def enqueue_reconcile(project_root: Path, entry: ImprovementOutboxEntry) -> Path:
    """Atomically persist immutable pending reconcile work."""

def drain_reconcile_outbox(project_root: Path) -> tuple[ImprovementAcceptStatus, ...]:
    """Under registry locking, reconcile sorted entries exactly once."""
```

- 复用现有 `reconciler.py` 的 `ImprovementAcceptStatus`，不引入第二种 receipt。其权威字段保持 `result/improvement_ids/event_ids`，并保留现有 `retro_id/context_sha256/candidate_batch_digest/idempotency_key/error`；Auto Review selector 根据本次 operation 明确返回的 accepted statuses 中 `improvement_ids` 重读 projection，status 本身不复制 subject/version。
- 每轮 Retro collect 前 drain，按 outbox filename 排序。entry 自包含已校验 context/candidate，drain 不回读历史 `qa/retro/**`。
- ledger lock/temporary IO failure 写 outbox 并把 Retro result 设为 `pending_reconcile`。
- 成功 drain 后记录 `ImprovementAcceptStatus`，再删除或标记 outbox；重放不得产生第二组 proposal/evidence-link events。
- candidate schema/ref/fingerprint contract error 不是临时 ledger failure；它转成 `reconcile_contract_error` pipeline failure Candidate，且只允许一次 fallback，避免递归。

- [ ] **Step 1: 写失败测试。** ledger 暂不可写时 outbox 原子落盘；下一轮 drain 只产生一组 events；在 commit 后 crash、清理前重放仍幂等；两个 outbox 按稳定顺序处理。
- [ ] **Step 2: 运行** `uv run pytest tests/unit/workflow/improvements/test_outbox.py tests/unit/workflow/improvements/test_reconciler.py tests/integration/test_retro_improvement_workflow.py -v`；预期 FAIL。
- [ ] **Step 3: 实现 outbox model/store/drain。** 复用现有 Improvement registry lock 和 reconciler，不复制 fingerprint 计算。
- [ ] **Step 4: 接入 Retro handler 和 status 输入。** 正常 reconcile status 与本轮成功 drain 返回的 `ImprovementAcceptStatus` 分开保留、合并为显式 status tuple 交给后续 Auto Review selector；不得通过扫描 accept-status 文件或 review queue 重建“本轮”集合。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: add durable improvement reconcile outbox"`。

---

### Task 8: 实现 Graph recovery、Retro status finalizer 与 Graph 外 Supervisor

**Files:**
- Create: `assurance_agent/retro/supervisor.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/commands/retro_cmd.py`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Test: `tests/unit/retro/test_supervisor.py`
- Test: `tests/unit/workflow/graph/test_retro_workflow.py`
- Test: `tests/unit/workflow/graph/test_task_runner.py`
- Test: `tests/integration/test_retro_cli.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class RetroInvocation:
    project_root: Path
    shell_change_id: str
    retro_id: str
    params: Mapping[str, object]

def run_retro_supervised(
    invocation: RetroInvocation,
    *, graph_runner: GraphRunner,
    preflight_failure: RetroPipelineFailure | None = None,
) -> RetroInvocationResult:
    """Run Graph, compensate outer failures, and always attempt status persistence."""
```

- Graph 内每个 analyzer 用 settled recovery node；collect/assemble/propose/reconcile 失败先写 `pipeline-failure.json`，走 deterministic fallback/reconcile，再写 status。
- Graph 外 compile/dispatch/workspace-freeze/finalize error 由 Supervisor 使用结构化 exception kind 生成相同 envelope。不得用 `"graph_definition_changed" in message` 一类子串判断。
- Supervisor 是 phase-aware：补偿前先读取并校验当前 `retro-status.json`。若 inner Retro status 已 final，则后续 selector/fanout/summarize 的失败属于 `post_retro_auto_review`，不得改写 `pipeline-failure.json`、outbox 或 `retro-status.json`，只交给 Auto Review batch recovery/命令结果。只有 status 尚不存在时才执行 Retro fallback。
- `retro-status.result`：无 gap/failure/outbox 为 `completed`；有 gap/fallback 为 `completed_with_gaps`；仍有 outbox 为 `pending_reconcile`。连 status/outbox 都不可写时不伪造 status 文件，只由 invocation result 返回 `technical_failure`。
- `commands/retro_cmd.py` 和 `commands/workflow_cmd.py` 的 retro entrypoint 都调用同一 Supervisor，不 monkey-patch planner/scheduler/runtime。

- [ ] **Step 1: 写 Graph 拓扑失败测试。** collect forbidden_write、analyzer timeout、assemble exception、proposer invalid output、reconcile lock failure 都能抵达 status node；健康路径仍一次 reconcile。
- [ ] **Step 2: 写 Supervisor 失败测试。** status 前的 compile/dispatch/freeze error 各自形成 envelope+fallback；structured `error_kind` 改 message 后不改变路由；parsed manifest contract error 可作为 `preflight_failure` 进入同一 fallback；status final 后模拟 selector/fanout/summarize dispatch failure，断言 status 与 pipeline-failure bytes 不变；完全只读项目返回 technical_failure。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/retro/test_supervisor.py tests/unit/workflow/graph/test_retro_workflow.py tests/unit/workflow/graph/test_task_runner.py tests/integration/test_retro_cli.py -v`；预期 FAIL。
- [ ] **Step 4: 实现 status/failure operations 与 Graph recovery。** 使用现有 `recover: {errors, via, continue_to}` 表达 exhausted failure；普通 route 使用互补 `when` 或既有 default route，不引入 DSL 中不存在的 `else`。settled join 等待成功节点或 recovery 的 `continue_to` terminal node。
- [ ] **Step 5: 实现窄接口 Supervisor 并接入两个命令。** 它只调用 Graph runner、fallback reconciler 和固定 artifact writer。
- [ ] **Step 6: 通过测试与质量门禁。** Commit：`git commit -m "feat: always finalize retro pipeline failures"`。

---

### Task 9: 为 CLI 增加 canonical `--batch-manifest` 入口

**Files:**
- Modify: `assurance_agent/commands/retro_cmd.py`
- Modify: `assurance_agent/cli.py`
- Modify: `tests/integration/test_retro_cli.py`
- Modify: `docs/schemas.md`

**CLI Contract:**

```text
aa retro --batch-manifest <path> --retro-id <id> [--json]
aa retro --change <id>... | --last <n> | --since <ts> [--until <ts>]
```

- `--batch-manifest` 与 `--change/--last/--since/--until` 互斥。
- manifest 读取后构造 exact `change_ids` 和 `batch_scope`，调用 Task 8 Supervisor。文件不存在/不可读属于无法取得任何 Batch document 的 CLI invocation error；JSON 已解析但 schema、排序、唯一性、status/集合约束失败时，构造 `batch_scope_invalid` preflight failure 并走 deterministic fallback，不作为入口阻断。
- 自动路径不提供 manifest 时不回退 `last=10`；这是调用方 contract error。
- JSON output 至少包含 retro ID、batch ID、status result、Improvement IDs、outbox ID 和 failure IDs；secret/error raw payload 不输出。

- [ ] **Step 1: 写 CLI 失败测试。** 合法 manifest 成功；已解析 manifest 的集合重复/乱序/status 不一致各自得到 `completed_with_gaps`、一个 stable batch-contract process Candidate 和零领域 Agent；文件缺失及和 `--last` 混用得到稳定非零 invocation error；缺成员 workspace 仍返回 completed_with_gaps；自动调用无 manifest 不会隐式选历史。
- [ ] **Step 2: 运行** `uv run pytest tests/integration/test_retro_cli.py -v`；预期 FAIL。
- [ ] **Step 3: 实现 option、manifest loader 与 JSON renderer。** 人工模式兼容现有命令。
- [ ] **Step 4: 更新 schema 文档与帮助快照。** 明确 shell Change 不属于 Batch membership。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: add explicit batch manifest retro cli"`。

---

### Task 10: Benchmark 使用 durable Batch manifest 驱动 Retro

**Files:**
- Modify: `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh`
- Modify: `tests/unit/benchmark/test_cursor_loop_helpers.py`

**Manifest Lifecycle:**

```text
<run-dir>/batch-manifest.json
  batch_id = RUNSTAMP
  members  = BENCHMARK_ITEMS mapped to deterministic Change IDs
```

- run 开始先根据 `RUNSTAMP + BENCHMARK_ITEMS` 原子创建 manifest；resume 要求已有 manifest 的 batch ID 与完整成员集合完全相同。
- item 状态映射：正常完成为 `completed/complete`；workflow failure 为 `failed/complete|partial`；停止为 `stopped/partial`；hard timeout 为 `hard_timeout/partial|absent`；取消为 `cancelled/partial|absent`；尚未启动为 `not_started/absent`。
- 每个 item terminal/timeout 后通过 temp file + `os.replace` 原子更新一个 member；不得用目录 mtime 或 Change ID 名称反推成员。
- 循环结束无条件调用 `uv run aa retro --batch-manifest "$BATCH_MANIFEST" --retro-id "$RETRO_ID" --json`。
- 删除自动路径的 `retro_last=10`、`select_latest_retro_dir` 和 shell Change selection。`retro_shell_change_id` 只传给 runtime carrier。
- Benchmark exit code 只基于原 Full/archive 结果；summary 单独展示 Retro status，不因 `completed_with_gaps`/`pending_reconcile` 改写 item verdict。

- [ ] **Step 1: 写 helper 失败测试。** 首次创建、逐成员原子更新、resume 相同成员成功、成员漂移失败、五类异常状态映射、0/2/11 个 item 精确保持。
- [ ] **Step 2: 写 shell source assertions。** 自动调用只出现 `--batch-manifest`，不出现 `retro_last`/latest-directory selection；Retro 调用位于所有 item 结果收集之后且不受 success gate 包围。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/benchmark/test_cursor_loop_helpers.py -v`；预期 FAIL。
- [ ] **Step 4: 实现 manifest helpers 和 loop integration。** 使用已有 Python helper 执行 JSON canonical/atomic update；不在 shell 中拼 JSON。
- [ ] **Step 5: 更新 loop summary。** 输出 batch ID/status、每个 member 的 execution/evidence 状态、Retro result、Improvement IDs/outbox ID。
- [ ] **Step 6: 通过测试与质量门禁。** Commit：`git commit -m "feat: drive benchmark retro with batch manifests"`。

---

### Task 11: 端到端验收、架构约束与文档收口

**Files:**
- Modify: `tests/integration/test_retro_v3_golden.py`
- Modify: `tests/integration/test_retro_improvement_workflow.py`
- Modify: `tests/integration/test_retro_cli.py`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_workspace.py`
- Modify: `tests/unit/benchmark/test_cursor_loop_helpers.py`
- Modify: `.importlinter` only if a new intentional facade edge cannot be eliminated
- Modify: `docs/schemas.md`
- Modify: `CONTEXT.md`

**Acceptance Matrix:**

| Case | Required outcome |
|---|---|
| PASS + final_status=FAIL | same explicit window; failure is Workflow evidence |
| archive rejected | Change retained; archive cause visible |
| complete + partial + absent | available domains analyzed; typed gaps frozen |
| all absent | zero domain Agents; deterministic process Candidate |
| nested Eval SUT | never materialized; no forbidden_write |
| unrelated Eval run | excluded by source_change_ids |
| repeated fingerprint | evidence link only; one Improvement |
| collect/proposer failure | stable fallback Improvement; no recursive Retro |
| ledger unavailable | pending outbox; next drain exactly once |
| benchmark resume | identical Batch membership and reproducible window |

- [ ] **Step 1: 扩充 golden fixture。** 使用 benchmark User/Role 的真实、脱敏 fixture，而不是测试内临时合成；保留 22→3 patterns、三域 signals、review queue 和健康 NOOP 验收。
- [ ] **Step 2: 添加“不读历史”哨兵。** 在旧 `qa/retro/**` 和 raw `eval/out/**` 放置会导致解析失败的哨兵文件，Batch run 仍必须成功且 read trace 不含这些路径。
- [ ] **Step 3: 添加 canonical Graph/contract assertions。** 确认 recovery edges、object params、`qa/eval/**` read set、固定 Supervisor write paths 和无 broad eval authorization。
- [ ] **Step 4: 运行核心验收：**
  - `uv run pytest tests/integration/test_retro_v3_golden.py tests/integration/test_retro_improvement_workflow.py tests/integration/test_retro_cli.py -v`
  - `uv run pytest tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_workspace.py tests/unit/benchmark/test_cursor_loop_helpers.py -v`
- [ ] **Step 5: 运行完整 CI：** `uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright`、`uv run lint-imports`、`uv run pytest -v`、`bash scripts/packaging_smoke_test.sh`；全部预期退出 0。
- [ ] **Step 6: 审核 import seam。** `commands -> retro supervisor -> workflow public runner` 保持单向；若能通过 facade 消除新豁免则不改 `.importlinter`。任何保留豁免必须精确到模块并在文件注释说明所有权。
- [ ] **Step 7: 更新文档。** 记录 Batch ownership、status 三态、唯一 technical_failure、Eval projection 与 Benchmark verdict 隔离。
- [ ] **Step 8: Commit：** `git commit -m "test: verify explicit batch retro closure"`。

## Completion Criteria

- 自动 Retro 没有 `last N`、mtime 或目录扫描的 Batch membership 推断。
- 每个实际创建的 Batch 都能得到 `completed`、`completed_with_gaps`、`pending_reconcile` 或唯一不可补偿的 `technical_failure`。
- 任一成员/领域/stage 失败都不会在 proposer 前静默断链。
- raw Eval SUT 不进入 Graph workspace；顶层 Issue ledger 例外不再误匹配嵌套路径。
- 新 Candidate 进入 proposed Improvement/review queue；相同 fingerprint 只追加 evidence；ledger 短暂失败通过 outbox 幂等恢复。
- Retro 不批准、交付或评估 Improvement；自动 Review 由独立计划在 Retro status final 后串联。
