# Improvement Automatic Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现设计 `docs/superpowers/specs/2026-07-27-improvement-automatic-review-design.md`：对本轮 Retro 新生成、证据完整的低风险 process Improvement 执行只读自动审查，并且只有确定性 Graph Gate 与锁内 apply 同时通过时才执行 `proposed -> approved`；其它结论留在人工 review queue。

**Architecture:** Improvement reconciler 在 proposal/evidence-link 时发布内容寻址的 immutable Review Subject，并把 digest 绑定到 Ledger event。只读 `aa-improvement-reviewer` 仅消费一个 frozen subject、输出 typed assessment。workflow validator 机械构造 gate input，named Gate fail-closed 地计算 auto eligibility，apply 在 registry lock 内重读 projection/subject/assessment 并写 strict event。外层 `retro-orchestration-workflow` 只 fan-out 本轮 receipt 指向的 proposed Improvement；每个 child failure 自行记录，Retro 已完成的 status 不回滚，也没有任何 delivery edge。

**Tech Stack:** Python 3.11、uv、Pydantic v2、JSONL Improvement event ledger、YAML Graph DSL、skill registry、Click CLI、pytest、ruff、pyright、import-linter。

## Global Constraints

- 本计划依赖 `docs/superpowers/plans/2026-07-27-retro-explicit-batch-scope.md` 的 typed receipt/outbox drain、`retro-status.json` finalizer 与 canonical Supervisor 入口。Task 1–6 可独立完成；本计划 Task 7 的外层 Retro 串联必须在 Retro 批次计划 Task 7–9 完成后实施。
- 当前工作树含未提交的 Retro v3 与 Improvement reconciler 改动。实施前先建立可恢复 checkpoint；禁止从 bare `HEAD` 建 worktree 后漏掉当前 reviewed changes。
- Skill 没有授权能力：不得输出或写入 `auto_eligible`、Ledger state、delivery event；它只能写当前 review ID 的 `assessment.json` 和 `summary.md`。
- 系统绝不自动 `reject`、`request_rework`、`supersede`、evaluate、export、apply、rollback。Reviewer 的 `reject` 只记录为 `reject_advice`。
- 不修改 Improvement fingerprint；`subject_sha256` 不是 fingerprint 的组成部分。
- 不扫描、不迁移、不读取历史 `qa/retro/**` 为旧 Improvement 补 subject。旧 proposal 无 subject 时继续人工 review。
- Subject digest 不含 ledger version、review attempt、assessment 或时间戳；记录 Auto Review event 不得改变 subject digest。
- Gate 和 apply 都 fail-closed。未知枚举、字段缺失、digest drift、stale version、重复 terminal review 或解析失败都不能退化成 pass。
- 新 artifacts 使用 canonical JSON、content digest、atomic immutable write。Review ID/event idempotency key 稳定派生自 `improvement_id + subject_sha256 + policy_version + attempt`。
- 每个 Task 先写失败测试，再做最小实现；Task 结束执行定向测试、`uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright`、`uv run lint-imports` 并单独 commit。禁止 `git add -A`。
- 最终 Gate 额外执行 `uv run pytest -v` 与 `bash scripts/packaging_smoke_test.sh`。

## Lifecycle Boundary

```text
Retro proposer
  -> Improvement reconciler + immutable Review Subject
  -> retro-status final
  -> selector(current receipts only)
  -> read-only reviewer
  -> deterministic Gate
       eligible -> locked auto-approval event
       otherwise -> recorded advice/error, state remains proposed
  -> batch summary

No edge to evaluate/export/apply/rollback.
```

---

### Task 1: 增加 Auto Review artifacts、strict events 与 projection fold

**Files:**
- Create: `assurance_agent/artifacts/models/improvement_review.py`
- Modify: `assurance_agent/artifacts/models/improvements.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `assurance_agent/workflow/improvements/events.py`
- Modify: `assurance_agent/workflow/improvements/projection.py`
- Test: `tests/unit/artifacts/test_improvement_auto_review_models.py`
- Test: `tests/unit/artifacts/test_registry.py`
- Test: `tests/unit/workflow/improvements/test_events.py`
- Test: `tests/unit/workflow/improvements/test_projection.py`

**Interfaces:**

```python
class AutoReviewFinding(BaseModel):
    finding_id: str
    severity: Literal["info", "low", "medium", "high", "critical", "blocking"]
    category: str
    message: str
    source_refs: tuple[str, ...]

class ImprovementAutoReviewAssessment(BaseModel):
    schema_version: Literal["1"]
    review_type: Literal["improvement"]
    review_id: str
    improvement_id: str
    expected_improvement_version: int
    subject_sha256: str
    decision: Literal["pass", "changes_requested", "needs_human_review", "reject"]
    findings: tuple[AutoReviewFinding, ...]
    evidence_traceability: Literal["complete", "incomplete", "invalid"]
    scope_readiness: Literal["ready", "not_ready", "ambiguous"]
    verification_readiness: Literal["ready", "not_ready"]
    delivery_safety: Literal["ready", "not_ready", "needs_human_review"]
    human_review_required: bool

class LastAutoReview(BaseModel):
    review_id: str
    subject_sha256: str
    assessment_sha256: str
    policy_version: str
    verdict: Literal[
        "auto_approved", "changes_requested", "needs_human_review",
        "reject_advice", "review_error",
    ]

class ImprovementAutoReviewStatus(BaseModel):
    schema_version: Literal["1"]
    review_id: str
    improvement_id: str
    result: Literal["approved", "escalated", "review_error", "stale", "idempotent"]
    ledger_event_id: str | None

class ImprovementAutoReviewBatchSummary(BaseModel):
    schema_version: Literal["1"]
    retro_id: str
    review_ids: tuple[str, ...]
    approved: int
    escalated: int
    errors: int
    stale: int
```

- `ImprovementProjection` 增加默认字段：`review_subject_sha256: str | None = None`、`approval_source: Literal["none", "human", "automatic"] = "none"`、`last_auto_review: LastAutoReview | None = None`。
- Registry 注册 content-addressed subject、每个 review 的 assessment/gate-input/status 与每轮 Retro 的 Auto Review batch summary；`summary.md` 只做展示，不参与 schema/Gate。
- `ImprovementProposedEvent` 与 `ImprovementEvidenceLinkedEvent` 增加可选 `review_subject_sha256`，保持旧事件 replay。
- 新 strict events：`ImprovementAutoReviewApprovedEvent` 和 `ImprovementAutoReviewRecordedEvent`；未知 extra fields 拒绝。
- auto-approved fold 将 state 设为 `approved`、source 设为 `automatic`；recorded 只更新 audit 字段并增加 version；human approval 设为 `human`；离开 approved 后 active source 重置为 `none`。

- [ ] **Step 1: 写模型/事件失败测试。** 覆盖四种 assessment decision、finding 严重度、非法 digest、未知字段、两个新 event 的必填字段和 recorded verdict enum。
- [ ] **Step 2: 写 replay 失败测试。** 旧 events 无 subject 仍 replay；human/automatic approval source 正确；recorded 不改 state/subject/proposal；auto event version 自增但 digest 不变。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/artifacts/test_improvement_auto_review_models.py tests/unit/artifacts/test_registry.py tests/unit/workflow/improvements/test_events.py tests/unit/workflow/improvements/test_projection.py -v`；预期 FAIL。
- [ ] **Step 4: 实现 models、registry、event union 与 projection fold。** `_with_version` 只在实际离开 approved 时重置 active source，不抹历史 event。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: add improvement auto review ledger events"`。

---

### Task 2: 由 reconciler 生成 immutable content-addressed Review Subject

**Files:**
- Create: `assurance_agent/workflow/improvements/review_subject.py`
- Modify: `assurance_agent/artifacts/models/improvement_review.py`
- Modify: `assurance_agent/workflow/improvements/reconciler.py`
- Modify: `assurance_agent/workflow/improvements/events.py`
- Test: `tests/unit/workflow/improvements/test_review_subject.py`
- Test: `tests/unit/workflow/improvements/test_reconciler.py`
- Test: `tests/unit/workflow/improvements/test_reconcile_v3.py`

**Interfaces:**

```python
class ImprovementReviewSubject(BaseModel):
    schema_version: Literal["1"]
    improvement_id: str
    kind: ImprovementKind
    delivery: ImprovementDelivery
    target: str
    rationale: str
    proposed_change: str
    verification: ImprovementVerification
    risk: ImprovementRisk
    confidence: ImprovementConfidence
    source_refs: Mapping[str, SourceReference]
    signal_evidence: tuple[SignalEvidence, ...]
    source_manifest: Mapping[str, SourceManifestEntry]
    provenance: ImprovementReviewProvenance

def build_review_subject(
    candidate: ImprovementCandidateV3,
    context: RetroContextV3,
    *, improvement_id: str,
) -> tuple[ImprovementReviewSubject, str, bytes]:
    """Resolve only candidate signal/source refs and return canonical digest-bound bytes."""

def publish_review_subject(
    project_root: Path,
    subject_sha256: str,
    canonical_bytes: bytes,
) -> Path:
    """Write qa/improvements/review-subjects/<digest>.json immutably."""
```

- `signal_evidence` 只含 Candidate `signal_ids` 指向的 signals；gap/failure Candidate 只含其结构化 envelope。
- Subject 不复制无关 signal、raw log、Eval sample/SUT、secret、review timestamp 或 ledger version。
- reconciler 在 Improvement registry lock 内先发布 subject，再 append proposal/evidence-link event，并在 event 上写 digest。
- Subject 写成功但 ledger append 失败允许 orphan；相同重试必须引用相同 bytes。Ledger 不得引用缺失或 digest 不匹配 subject。
- evidence-link 若语义证据改变则生成新 subject；完全相同 subject 不产生新 digest。

- [ ] **Step 1: 写失败测试。** 相同 semantic input 在不同 ledger version/time 下 digest 相同；一个引用 signal 改变后 digest 改变；无关 signal 改变不影响 digest；source ref 无法解析时 reconcile 在写 event 前失败。
- [ ] **Step 2: 写顺序/原子性测试。** 观测 publish 发生在 append 前；append failure 留下安全 orphan；不同 bytes 写同 digest 冲突；event 从不引用不存在 subject。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/workflow/improvements/test_review_subject.py tests/unit/workflow/improvements/test_reconciler.py tests/unit/workflow/improvements/test_reconcile_v3.py -v`；预期 FAIL。
- [ ] **Step 4: 实现 subject builder/store 并接入 reconciliation plan。** 复用共享 canonical SHA helper，禁止在第四处复制序列化逻辑。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: freeze improvement review subjects during reconcile"`。

---

### Task 3: 新增只读 `aa-improvement-reviewer` skill 与输出校验

**Files:**
- Create: `assurance_agent/_resources/skills/aa-improvement-reviewer/SKILL.md`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/workflow/graph/handlers/agent.py`
- Create: `assurance_agent/workflow/improvements/reviewer_output.py`
- Test: `tests/unit/test_skills_content.py`
- Test: `tests/integration/test_cli_skill_refresh.py`
- Test: `tests/unit/workflow/improvements/test_reviewer_output.py`
- Test: `tests/unit/workflow/graph/test_read_isolation.py`

**Skill Contract:**

```text
reads:
  project:qa/improvements/review-subjects/${params.subject_sha256}.json
writes:
  project:qa/improvements/reviews/${params.review_id}/assessment.json
  project:qa/improvements/reviews/${params.review_id}/summary.md
```

```python
def complete_improvement_reviewer_outputs(
    *, subject_path: Path, assessment_path: Path, summary_path: Path,
    expected_review_id: str, expected_improvement_id: str,
    expected_version: int, expected_subject_sha256: str,
) -> ImprovementAutoReviewAssessment:
    """Validate identity, schema and subject binding before freeze."""
```

- Skill 明确检查 traceability、scope ownership、verification、delivery safety、重复/替代歧义与风险低估。
- Skill 输出 decision，但禁止 `auto_eligible`、canonical state/action、Ledger event 或 delivery 指令。
- Agent read isolation 禁止该节点读取 `qa/retro/**`、`qa/issues/**`、`.aa/memory/**`、源码、测试、其它 Improvement subject/review。
- assessment identity/version/subject 与 params 不一致返回 `invalid_output` 并走 agent retry；summary 不作为 Gate 输入。

- [ ] **Step 1: 写 skill 文本和 registry 失败测试。** 检查 required sections、禁止字段、只读范围、assessment/summary path schemas。
- [ ] **Step 2: 写 output completion 失败测试。** identity/version/digest mismatch、unknown enum、extra `auto_eligible`、丢失 assessment、越界 output 都 fail-closed。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/test_skills_content.py tests/integration/test_cli_skill_refresh.py tests/unit/workflow/improvements/test_reviewer_output.py tests/unit/workflow/graph/test_read_isolation.py -v`；预期 FAIL。
- [ ] **Step 4: 实现 skill、completion hook、registry 与 execution contract。** 保持 workflow 层只依赖 artifacts 模型，避免 `handlers/agent.py -> retro` 新 import seam。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: add read-only improvement reviewer skill"`。

---

### Task 4: 增加 safe dynamic Gate paths 与确定性 eligibility input

**Files:**
- Create: `assurance_agent/workflow/improvements/auto_review.py`
- Modify: `assurance_agent/workflow/orchestration/gates.py`
- Modify: `assurance_agent/workflow/orchestration/schema.py`
- Modify: `assurance_agent/workflow/graph/handlers/gate.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Test: `tests/unit/workflow/graph/test_gate_reference_resolution.py`
- Test: `tests/unit/workflow/test_gate_causes.py`
- Create: `tests/unit/workflow/improvements/test_auto_review.py`

**Interfaces:**

```python
def resolve_view_path(
    template: str,
    *, change_id: str,
    params: Mapping[str, JsonValue],
) -> PurePosixPath:
    """Expand <change-id> and allowlisted ${params.<name>} scalar path segments."""

class AutoReviewGateInput(BaseModel):
    schema_version: Literal["1"]
    review_id: str
    improvement_id: str
    expected_improvement_version: int
    subject_sha256: str
    assessment_sha256: str
    policy_version: Literal["1"]
    projection_state: str
    projection_subject_matches: bool
    semantic_subject_matches: bool
    kind_allowed: bool
    delivery_allowed: bool
    risk_low: bool
    confidence_high: bool
    reviewer_pass: bool
    human_review_required: bool
    source_refs_resolve: bool
    has_blocking_findings: bool
    verification_ready: bool
    ambiguity_free: bool
    prior_terminal_review: bool
    explicit_retry_allowed: bool

def build_auto_review_gate_input(
    project_root: Path,
    *, review_id: str, improvement_id: str,
    subject_sha256: str, expected_version: int,
    policy_version: str, attempt: int,
) -> AutoReviewGateInput:
    """Mechanically derive facts from projection, subject and assessment."""
```

- validator 写 `qa/improvements/reviews/<review-id>/gate-input.json`；Reviewer contract 没有该路径的 write permission。
- named Gate 读取 dynamic review path，显式 AND 所有 eligibility facts。它不信任 assessment 自报 risk/confidence/eligibility。
- path params 只接受单个安全 segment：非空 ASCII/identifier-digest 字符集，拒绝 `/`、`\\`、`.`、`..`、percent traversal 和未声明 param。
- Gate pass 后 apply 仍锁内重做 version/subject/digest/terminal-review 校验，避免 TOCTOU。

- [ ] **Step 1: 写 path resolution 失败测试。** 正常 review/digest 展开；slash、dotdot、absolute、missing param、list/object param 全拒绝；现有 static paths 与 `<change-id>` 行为不变。
- [ ] **Step 2: 写 eligibility table 测试。** 14 条设计条件逐条翻转时 `auto_approve=False`；全部满足才为 True；domain knowledge、knowledge_delta、empty suite、blocking/high/critical finding 与 prior terminal review 明确覆盖。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/workflow/graph/test_gate_reference_resolution.py tests/unit/workflow/test_gate_causes.py tests/unit/workflow/improvements/test_auto_review.py -v`；预期 FAIL。
- [ ] **Step 4: 实现安全插值、mechanical gate input 与 named Gate schema。** reason code 为稳定枚举，summary message 不参与判定。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: add deterministic improvement auto review gate"`。

---

### Task 5: 实现 Auto Review selector、locked apply、record 与 error recovery operations

**Files:**
- Modify: `assurance_agent/workflow/improvements/auto_review.py`
- Modify: `assurance_agent/workflow/improvements/events.py`
- Modify: `assurance_agent/workflow/improvements/projection.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Test: `tests/unit/workflow/improvements/test_auto_review.py`
- Test: `tests/unit/workflow/graph/test_retro_ops.py`
- Test: `tests/unit/workflow/improvements/test_projection.py`

**Interfaces:**

```python
class AutoReviewItem(BaseModel):
    improvement_id: str
    subject_sha256: str
    expected_improvement_version: int
    review_id: str
    policy_version: Literal["1"]
    attempt: int

def select_auto_review_items(
    projection: ImprovementProjection,
    receipts: Sequence[ImprovementAcceptReceipt],
    *, policy_version: str = "1",
) -> tuple[AutoReviewItem, ...]:
    """Select current receipts only; never scan the review queue."""

def apply_auto_review_result(
    project_root: Path,
    item: AutoReviewItem,
    *, gate_verdict: Literal["auto_approve", "record"],
) -> AutoReviewApplyResult:
    """Lock, reread, validate all bindings, then append at most one event."""
```

- selector 只接受本轮 accept/drain receipts 指向、当前 `proposed`、有 subject 且同 subject/policy 无 non-error terminal assessment 的 Improvement。
- dry-run、零 Candidate、尚未 drain 的 pending outbox 返回空 tuple。
- `decision=reject` 映射 `reject_advice`；changes_requested/needs_human_review 保持对应 audit verdict；Reviewer/subject/validator failure 映射 `review_error`。
- stale apply 不写 decision event；写 review-local status `stale`。如果新 subject 仍 proposed，可由后续显式 selection 创建新 review。
- 同一 `improvement_id+subject+policy+attempt` 并发只允许一条 canonical terminal event；另一方得到 idempotent 或 stale。

- [ ] **Step 1: 写 selector 失败测试。** current receipt、old queue item、old proposal no subject、dry-run、pending outbox、已审同 subject、新 subject、显式 retry 各自覆盖。
- [ ] **Step 2: 写 apply 并发/映射失败测试。** pass eligible 自动批准；reject 不写 reject event；changes requested 不进 needs_rework；stale version/digest drift 保持 proposed；两个并发 apply 仅一条 event。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/workflow/improvements/test_auto_review.py tests/unit/workflow/graph/test_retro_ops.py tests/unit/workflow/improvements/test_projection.py -v`；预期 FAIL。
- [ ] **Step 4: 实现纯 selector 和 synchronized operations。** registry lock 内重建 projection，event 使用 expected version 和稳定 idempotency key。
- [ ] **Step 5: 实现 review-local `status.json`。** result 为 `approved|escalated|review_error|stale|idempotent`，不成为 Ledger authority。
- [ ] **Step 6: 通过测试与质量门禁。** Commit：`git commit -m "feat: apply bounded improvement auto review decisions"`。

---

### Task 6: 建立单条 Auto Review Graph 与 standalone repeatable entrypoint

**Files:**
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Test: `tests/integration/test_improvement_auto_review_workflow.py`
- Test: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Test: `tests/unit/workflow/graph/test_contracts.py`
- Test: `tests/unit/workflow/graph/test_task_runner.py`

**Graph:**

```text
load-improvement-auto-review
  -> agent:aa-improvement-reviewer
  -> validate-improvement-review-assessment
  -> improvement-auto-review-gate
       pass -> apply-auto-approval -> write-review-status -> END
       stop -> record-auto-review -> write-review-status -> END
  agent/validation failure -> record-review-error -> write-review-status -> END
```

- 新 repeatable entrypoint `improvement-auto-review` 必须显式接收 `improvement_id`、`subject_sha256`、`expected_improvement_version`、`review_id`、`policy_version`、`attempt`。
- retry 只有在上一结果 `review_error` 时允许 attempt 增加；同 attempt 重放幂等。普通 selector 固定 attempt 1。
- child Graph 没有 human interrupt；所有 recovery edge 都落到 status terminal。
- execution contract 将 Agent 的窄 read/write 与 apply 的 `project:qa/improvements/**` synchronized/exclusive 权限分离。

- [ ] **Step 1: 写 Graph 失败测试。** pass、advice、timeout、invalid JSON、unknown enum、subject missing、digest drift、stale apply 都走到 END 并产生正确 status；只有 pass+eligible 改 state。
- [ ] **Step 2: 写 contract 测试。** Reviewer 无 Ledger write，validator/Gate 只读 subject/assessment/projection，apply 独占 registry；review paths 使用 safe params。
- [ ] **Step 3: 运行** `uv run pytest tests/integration/test_improvement_auto_review_workflow.py tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_task_runner.py -v`；预期 FAIL。
- [ ] **Step 4: 添加 graph、entrypoint 与 contracts。** named Gate 的 stop causes 映射到稳定 reason codes。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: add repeatable improvement auto review workflow"`。

---

### Task 7: 在 Retro final 之后增加外层 fan-out orchestration

**Depends on:** Retro Explicit Batch Scope Plan Tasks 7–9。

**Files:**
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Modify: `assurance_agent/commands/retro_cmd.py`
- Test: `tests/integration/test_retro_auto_review_orchestration.py`
- Test: `tests/unit/workflow/graph/test_retro_workflow.py`
- Test: `tests/unit/workflow/graph/test_canonical_schema_v2.py`

**Outer Graph:**

```text
retro-orchestration-workflow
  -> graph:retro-workflow
  -> select-current-retro-auto-review-items
  -> fanout graph:improvement-auto-review-cycle
  -> summarize-auto-review-batch
  -> END
```

- `retro-workflow` 必须先写 final `retro-status.json`；selector 不能在此节点前运行。
- fanout item 是明确 `{improvement_id, subject_sha256, expected_improvement_version, review_id, policy_version, attempt}`，不得扫描全局 queue/目录。
- `pending_reconcile` 且 outbox 未 drain、dry-run、NOOP 返回空 fanout；outbox 在本轮成功 drain 后其 receipt 可被 selector 接手。
- child timeout/invalid/conflict 在 child 内收口，parent summary 计数但不改变 `retro-status.result`。
- canonical `retro` entrypoint 指向 outer graph；inner `retro-workflow` 仍可作为受测子图，但 CLI/Benchmark 不手工串 shell 命令。

- [ ] **Step 1: 写顺序失败测试。** 观测 retro status 写入先于 selector；Reviewer 失败后 status bytes/digest 不变；两个 receipts 生成两个明确 child items。
- [ ] **Step 2: 写空选择失败测试。** dry-run、zero candidate、pending outbox、old queue item、old proposal no subject 均不启动 Agent；drain receipt 启动一次。
- [ ] **Step 3: 运行** `uv run pytest tests/integration/test_retro_auto_review_orchestration.py tests/unit/workflow/graph/test_retro_workflow.py tests/unit/workflow/graph/test_canonical_schema_v2.py -v`；预期 FAIL。
- [ ] **Step 4: 实现 selector operation、fanout mapping 和 batch summary。** summary path 使用当前 Retro ID，作为链接信息而非 Retro integrity/signal。
- [ ] **Step 5: 切换 canonical retro entrypoint 到 outer graph。** 删除 CLI/Benchmark 中任何显式 review 串联代码。
- [ ] **Step 6: 通过测试与质量门禁。** Commit：`git commit -m "feat: auto review improvements after retro finalization"`。

---

### Task 8: 增加人工接管信息与 automatic-approved 的受限恢复边

**Files:**
- Modify: `assurance_agent/workflow/improvements/transitions.py`
- Modify: `assurance_agent/workflow/improvements/review.py`
- Modify: `assurance_agent/artifacts/models/improvements.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Test: `tests/unit/workflow/improvements/test_review.py`
- Test: `tests/integration/test_improvement_review_workflow.py`

**Rules:**

```text
approved(approval_source=automatic) --human request_rework--> needs_rework
approved(approval_source=automatic) --human reject---------> rejected
```

- 只允许 audited human decision；当前 projection 必须仍 `approved`、source `automatic` 且 delivery 尚未开始。
- human-approved 的 approved 不开放这两条边。
- stale version 拒绝。evaluate/export/apply 已有事件时沿既有 rollback/supersede 路径，不抹回 rejected/rework。
- Human review context 展示 subject digest、last auto verdict、findings summary、assessment digest/path；apply 始终在锁内重读 projection。

- [ ] **Step 1: 写恢复边失败测试。** automatic-approved 可由人 request_rework/reject；human-approved 两者都拒绝；delivery started 拒绝；stale version 拒绝；stop 等原 action 不回归。
- [ ] **Step 2: 写 context 失败测试。** proposed/escalated 与 automatic-approved 都显示最新 assessment；assessment 缺失/digest mismatch 仅显示 warning，不改变 canonical fields。
- [ ] **Step 3: 运行** `uv run pytest tests/unit/workflow/improvements/test_review.py tests/integration/test_improvement_review_workflow.py -v`；预期 FAIL。
- [ ] **Step 4: 实现 state graph 的潜在边与 review validator 的条件授权。** 不能只在 `_ALLOWED` 静态集合放宽而绕过 approval source/delivery 检查。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: allow human override of automatic approvals"`。

---

### Task 9: CLI/status 展示 Auto Review 审计信息与显式 retry

**Files:**
- Modify: `assurance_agent/commands/improvement_cmd.py`
- Modify: `assurance_agent/commands/status_cmd.py` only if it already delegates Improvement rendering
- Modify: `assurance_agent/cli.py`
- Test: `tests/integration/test_improvement_cli.py`
- Test: `tests/integration/test_cli_status.py`
- Modify: `docs/schemas.md`

**CLI Contract:**

```text
aa improvement list [--json]
aa improvement show <improvement-id> [--json]
aa improvement auto-review <improvement-id> --attempt <n> [--json]
```

- list/show 展示 state、approval source、current subject digest、last verdict/policy/review ID、是否仍在 human queue、assessment/summary paths。
- auto-review 从 current projection 获取 subject/version，稳定派生 review ID；attempt 1 是默认首次审查，attempt >1 仅在上一同 subject/policy verdict 为 review_error 时允许。
- CLI 调用 standalone Graph，不在 Python 命令层重写 eligibility 或 event。
- JSON 输出把 assessment path/digest 作为审计引用，不嵌入可能包含长文本的 summary。

- [ ] **Step 1: 写 CLI 失败测试。** proposed escalated、automatic-approved、human-approved、old subjectless proposal、review_error retry、非法 retry 和 JSON shape 全覆盖。
- [ ] **Step 2: 运行** `uv run pytest tests/integration/test_improvement_cli.py tests/integration/test_cli_status.py -v`；预期 FAIL。
- [ ] **Step 3: 实现 renderer 和 Graph launcher。** 若 `aa status` 不拥有 Improvement 列表，不向其混入第二套读取逻辑；以 `aa improvement list/show` 为 canonical surface。
- [ ] **Step 4: 更新 schema/CLI 文档。** 明确 auto approval 不等于 delivery。
- [ ] **Step 5: 通过测试与质量门禁。** Commit：`git commit -m "feat: expose improvement auto review status"`。

---

### Task 10: 端到端验收、并发、架构与非目标锁定

**Files:**
- Modify: `tests/integration/test_improvement_auto_review_workflow.py`
- Modify: `tests/integration/test_retro_auto_review_orchestration.py`
- Modify: `tests/integration/test_improvement_review_workflow.py`
- Modify: `tests/integration/test_retro_improvement_workflow.py`
- Modify: `tests/unit/workflow/improvements/test_auto_review.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Modify: `tests/unit/test_skills_content.py`
- Modify: `.importlinter` only if a minimal intentional facade cannot remove a new edge
- Modify: `CONTEXT.md`

**Acceptance Matrix:**

| Case | Required canonical outcome |
|---|---|
| low risk + high confidence + pass | `improvement_auto_review_approved`, state approved |
| domain knowledge / knowledge delta | recorded escalation, state proposed |
| medium/high risk or low confidence | recorded escalation, state proposed |
| empty suite / blocking finding | recorded escalation, state proposed |
| reviewer reject | `reject_advice`, never rejected state |
| changes requested | recorded advice, never needs_rework |
| timeout / invalid JSON / unknown enum | review_error, state proposed |
| same subject replay | at most one terminal event |
| new evidence/new subject | eligible for a new review |
| concurrent evidence-link | old apply stale, no approval |
| automatic-approved + human override | needs_rework/rejected before delivery |
| human-approved + same override | rejected by validator |
| any automatic approval | no eval/export/apply event |
| Reviewer failure after Retro final | Retro status bytes unchanged |

- [ ] **Step 1: 添加完整 policy parameterization。** 每个 Gate 条件至少一个独立负例，并断言 exact reason code。
- [ ] **Step 2: 添加 concurrency barrier 测试。** 两个 auto review apply 同时读取旧 version，只有一个 append；evidence-link 在 Gate 后/apply 前发生时旧审查 stale。
- [ ] **Step 3: 添加 non-recursion 测试。** review_error 不产生新 Improvement、不启动第二个 Retro、不再次选择相同 subject attempt。
- [ ] **Step 4: 添加 delivery isolation 测试。** graph schema 中 auto approval terminal 不存在到 improvement-evaluate/export/apply/rollback 的 edge；Ledger 没有相关 event。
- [ ] **Step 5: 添加 read/write isolation 测试。** Reviewer 读取源码、历史 Retro、Issue ledger 或其它 subject 时 fail-closed；失败不能污染 Ledger/Retro artifacts。
- [ ] **Step 6: 运行核心验收：**
  - `uv run pytest tests/integration/test_improvement_auto_review_workflow.py tests/integration/test_retro_auto_review_orchestration.py tests/integration/test_improvement_review_workflow.py tests/integration/test_retro_improvement_workflow.py -v`
  - `uv run pytest tests/unit/workflow/improvements/test_auto_review.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/test_skills_content.py -v`
- [ ] **Step 7: 运行完整 CI：** `uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright`、`uv run lint-imports`、`uv run pytest -v`、`bash scripts/packaging_smoke_test.sh`；全部预期退出 0。
- [ ] **Step 8: 审核分层。** artifacts 不 import workflow；Graph handler 只调用 Improvement facade；Reviewer skill 不造成 workflow→retro 直接边。优先移动共享模型/纯 helper，而不是扩大 `.importlinter` seam。
- [ ] **Step 9: 更新 `CONTEXT.md`。** 记录 Reviewer/Gate/apply 权限、human override、retry 与 delivery isolation。
- [ ] **Step 10: Commit：** `git commit -m "test: verify bounded improvement auto review lifecycle"`。

## Completion Criteria

- 本轮 Retro receipt 指向的 eligible process Improvement 能自动从 proposed 进入 approved。
- Reviewer 永远不能直接改变 Ledger；Gate 不信任 Skill 自报 eligibility；apply 在锁内重新验证 stale/digest/policy。
- 非 pass、knowledge、风险/置信度/验证不足、歧义、错误和 stale 全部保持 proposed 并留在人工队列。
- 相同 subject/policy 不无限复审；只有 review_error 允许显式新 attempt，新 evidence 通过新 subject 重新审查。
- 人工能在 delivery 前撤销 automatic approval，但不能把 human approval 走同一恢复边。
- Retro status 先 final，Auto Review child failure 不反向改变 Retro/Benchmark verdict。
- 自动 Review 没有到 evaluate、export、apply 或 rollback 的任何隐式/显式执行边。
