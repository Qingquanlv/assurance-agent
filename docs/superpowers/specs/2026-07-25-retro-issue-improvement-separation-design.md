# Retro / Issue Separation and Improvement Lifecycle Design

- 日期：2026-07-25
- 状态：已批准（2026-07-25）
- 范围：Issue 与 Retro 的职责拆分、只读历史查询 Interface、Retro evidence、Improvement Proposal 生命周期、clean-cut 兼容策略
- 关联设计：[Issue Lifecycle and Cross-Change Problem Tracking Design](./2026-07-25-issue-lifecycle-design.md)
- 实施计划：[Retro / Issue Separation and Improvement Lifecycle Implementation Plan](../plans/2026-07-25-retro-issue-improvement-separation.md)
- 基础计划：[Issue Lifecycle and Problem Ledger Implementation Plan](../plans/2026-07-25-issue-lifecycle-and-problem-ledger.md)

---

## 0. 决议摘要

1. Issue 继续在 full workflow 内执行：每次 authoritative execution 之后先运行现有 inspect，再收集 Observation、分析 Issue、reconcile Occurrence/Problem；每次 healing rerun 使用相同子图。
2. Retro 保持 project-scoped 独立 entrypoint，不加入 full workflow，不再次执行 Issue Analyzer。
3. Issue Module 唯一拥有 Observation、Occurrence、Problem、Problem assessment、状态、人工决定、resolution 与 regression。
4. Retro Analysis Module 只拥有跨 Change 派生信号和 Improvement Candidate；它不创建、合并、关闭或修改 Problem。
5. 在 Issue 与 Retro 之间新增只读 `IssueHistoryReader` Interface。Retro 不解析 Issue 文件布局，不自行 replay Ledger。
6. `operation:retro-collect` 是唯一可以读取 Issue/Workflow/Eval 权威历史的 Retro Adapter；LLM `skill:aa-retro` 只能读取当前 run 的 `context.json`。
7. `workflow_bug` 不再是 Retro finding。Issue 使用 `IssueClassification.workflow_issue` 表示问题；Retro 使用 `ImprovementKind.workflow_improvement` 表示改进行动。
8. 新建 project-scoped Improvement Ledger，作为跨 Retro run 的 Proposal 身份与 review/eval/apply 权威状态。`qa/retro/<retro-id>/**` 只是单次分析记录，不承担跨 run 状态。
9. clean cut：新流程不迁移、不扫描、不消费既有 `qa/retro/**`。当前 run 的节点可以读取本 run 已生成的文件；任何更早的 Retro run 都不是输入。
10. 删除基于历史 Retro 的 consumed-change 状态和汇总依赖。Retro window 来自显式范围或当前权威历史，跨 run 去重由 Improvement Ledger 完成。

---

## 1. 背景与问题

当前 Retro 同时承担四类职责：

1. 从 archive/change 文件重新提取失败、分类和人工决定；
2. 汇总跨 Change 趋势；
3. 生成 prompt、workflow 和 data-knowledge 提案；
4. 在每个 Retro run 内记录提案 review、eval、apply/export 状态。

Issue Lifecycle 设计引入 Change Issue Ledger 和 Project Problem Ledger 后，第 1 类职责已经有权威 owner。若继续按现有 implementation plan Task 15 直接扩展 `retro/archive_reader.py` 和 `retro/aggregator.py`，Retro 将理解以下 Issue Implementation 细节：

- Change/Project Ledger 的路径与 JSONL 格式；
- Observation、Occurrence 与 Problem 的关联；
- Problem projection、版本和人工决定事件；
- 分析失败、project sync pending 与损坏 Ledger 的语义。

这会形成一个浅 Module：Issue 的复杂度通过大 Interface 泄漏给 Retro，校验和解释逻辑需要在多个 caller 中重复实现。同时，当前 `workflow_bug -> issue_export` 会再次保存 title、severity、evidence 和 proposed change，与 `workflow_issue` Problem 形成两个可独立演进的问题记录。

本设计把“问题事实”和“改进行动”拆成两个生命周期，并在二者之间放置一个小而稳定的只读 Seam。

---

## 2. 目标与非目标

### 2.1 目标

1. 保留 Issue 在 full workflow 中的实时分析、报告和 archive 语义。
2. 让每类信息只有一个权威 owner。
3. 让 Retro 能分析跨 Change 的重复、误分类、人工纠正、resolution、regression、healing、skill 与 eval 趋势。
4. 阻止 Retro 产生第二套 Problem 身份、assessment 或状态。
5. 让 LLM 只看到冻结、可引用、digest-pinned 的 RetroContext，而不是开放的项目 Ledger。
6. 让 Improvement Proposal 在不同 Retro run 之间可去重、审阅、评测、应用和回滚，而无需读取历史 `qa/retro/**`。
7. 保留 prompt/memory、fixture/test/workflow change、data knowledge 三类实际落地能力。
8. 使 Issue、Retro Analysis 和 Improvement 的 Interface 都可以通过 in-memory Adapter 独立测试。

### 2.2 非目标

- 不改变 Issue 在 full workflow 中的位置。
- 不把 Retro 或 Improvement review 加入 full workflow。
- 不让 Issue Analyzer 提出或批准跨 Change Improvement。
- 不让 Retro 修改 Problem 状态或为 Problem 确认 assessment。
- 不自动修改产品代码或 Assurance Agent 源码。
- 不迁移历史 `qa/retro/**`，也不为其提供兼容 reader。
- 不把 `.aa/data-knowledge.yaml` 移入 Issue 或 Improvement Ledger。
- 不使用历史 Retro proposal 作为新 Retro 的分析输入。

---

## 3. 术语和 Ownership

### 3.1 Problem 与 Improvement 是两个维度

`IssueClassification` 回答“发生了什么问题”：

```text
product_bug | test_bug | test_data_issue | environment_issue |
coverage_gap | performance_issue | workflow_issue | unknown
```

`ImprovementKind` 回答“系统应该怎样改进”：

```text
prompt_improvement | fixture_improvement | test_improvement |
workflow_improvement | domain_knowledge
```

它们不是互斥枚举。一条 `product_bug` 可以暴露测试或 inspect 的系统性缺口，从而支持 `test_improvement` 或 `workflow_improvement`。一条 `workflow_issue` 也可以先作为 Problem 被立即跟踪，后续再由 Retro 聚合为 Improvement。

### 3.2 权威 owner

| 信息 | 权威 Module | Retro 是否持久化 |
|---|---|---|
| 单次测试失败、warning、anomaly、workaround | Execution / Change Issue | 只保存引用和冻结摘要 |
| Issue 分类、严重度、根因、affected surface | Issue | 只保存带 source event/digest 的只读投影 |
| Occurrence 与跨 Change Problem 身份 | Issue | 只保存 ID 引用 |
| Problem 状态、人工决定、resolution、regression | Issue | 只保存 event ID 和派生信号 |
| 失败分布、重复率、纠正趋势、resolution 趋势 | Retro Analysis | 是，作为派生 signal |
| skill drift、healing 效率、gate pushback、eval 趋势 | Retro Analysis | 是，作为派生 signal |
| Prompt、fixture、test、workflow、knowledge 改进建议 | Improvement | Candidate 在 run 内；接受后进入 Improvement Ledger |
| Improvement review/eval/apply/export/rollback 状态 | Improvement | project-scoped Ledger 权威持久化 |
| 可复用数据构造和执行知识 | Data Knowledge | Retro 只持久化待提升的 delta |
| Markdown summary/review queue | 无权威性 | 可重建 projection |

### 3.3 禁止的平行事实

Improvement Candidate/Proposal 不得声明以下字段：

- Problem `classification`、`severity`、`status`、`version`；
- Problem root-cause assessment 的独立副本；
- `resolved`、`not_an_issue`、`accepted_risk` 等 Problem disposition；
- 用自然语言重新构造的产品问题 identity。

需要显示这些信息的 projection 必须携带 `problem_id`、`problem_version`、`as_of_event_id` 和 source digest，并明确为不可写的 snapshot。

---

## 4. 总体架构与时序

### 4.1 Full workflow 保持不变

```text
execution
  -> inspect
  -> collect-observations
  -> analyze-issues
  -> reconcile-issues
  -> healing
       -> rerun
       -> inspect
       -> collect/analyze/reconcile issues
  -> report
  -> archive
```

规则：

- `operation:inspect` 仍先产生现有 failure analysis 和 quality gate artifacts。
- Issue 子图对初次执行和每次 healing rerun 都运行。
- Issue Analyzer 只做单批次的问题识别，不做跨 Change Retro。
- Reconciler 在 report 前更新 Change Occurrence 和 Project Problem。
- Issue 分析失败继续 fail-open，但 Observation 不丢失；Issue risk 与 execution `final_status` 分离。
- full workflow 没有到 Retro 或 Improvement review 的 edge。

### 4.2 Retro workflow 独立运行

```text
select-window
  -> collect-retro-evidence
  -> propose-improvements
  -> reconcile-improvements
  -> END
```

详细行为：

1. `select-window` 解析显式 Change 列表、`since/until` 或确定性的 `last N` terminal Changes；不读取 consumed-change state。
2. `collect-retro-evidence` 通过只读 Interface 查询 Issue、Workflow 和 Eval 历史，固定 source heads/digests，计算派生 signals，写当前 run 的 `context.json`。
3. `propose-improvements` 调用 LLM，只读取当前 `context.json`，写非权威 `proposal-candidates.json` 和 summary。
4. `reconcile-improvements` 完整批校验 Candidate、证据引用和 delivery 资格；随后在 project lock 下去重并更新 Improvement Ledger，同时写当前 run receipt。
5. 零信号是成功 NOOP；不调用 LLM，不写 Candidate。

### 4.3 独立人工流程

Problem review 和 Improvement review 是两个独立 entrypoint：

```text
issue-review:
  load Problem -> advise -> human interrupt -> validate -> append Problem event

improvement-review:
  load Improvement -> human interrupt -> validate -> append Improvement event
```

批准 Improvement 不修改 Problem。应用某个 Improvement 后，Problem 是否解决仍由 Issue 的 linked fix/disposition 和 scope-aware verification 决定。

---

## 5. Module 与 Interface

### 5.1 Issue History Module

Seam 位于 `IssueHistoryReader` Interface，而不是 Ledger 文件路径。

```python
class IssueHistoryReader(Protocol):
    def read_window(self, selection: IssueWindowSelection) -> IssueEvidenceSlice:
        ...
```

`IssueWindowSelection`：

```python
@dataclass(frozen=True)
class IssueWindowSelection:
    change_ids: tuple[str, ...]
    project_event_through: str | None = None
```

`IssueEvidenceSlice` 至少包含：

```yaml
schema_version: "1"
window:
  change_ids: [RET-...]
  project_event_through: PEVT-...
sources:
  - kind: change_issue_ledger
    change_id: RET-...
    head_event_id: CEVT-...
    sha256: ...
  - kind: project_problem_ledger
    head_event_id: PEVT-...
    sha256: ...
integrity:
  status: complete | incomplete
  reasons: []
observations: []
occurrences: []
problem_snapshots: []
problem_events: []
```

Interface invariants：

1. 所有 event、Observation、Occurrence、Problem 引用都通过严格模型校验。
2. Change 选择、Occurrence→Problem 关联、merge alias 解析和 window filtering 隐藏在 Implementation 内。
3. 返回值固定 source head 与 digest；同一组 source bytes 产生 byte-identical slice。
4. 读取期间若 Project Ledger head 改变，生产 Adapter 重试稳定读取，不返回撕裂 snapshot。
5. `analysis_failed` 或 `project_sync_pending` 是可见的不完整状态；损坏 Ledger 是 hard error，不能静默跳行。
6. 任何 caller 都不能通过此 Interface 写 Ledger。

Adapters：

- `LedgerIssueHistoryReader`：生产 Adapter，读取严格 Change/Project Ledgers 和 projections。
- `InMemoryIssueHistoryReader`：测试 Adapter，直接提供 typed events。

两个 Adapter 使该 Seam 真实存在；Retro 测试不需要构造文件树或复制 Issue replay 逻辑。

### 5.2 Retro Evidence Module

Interface：

```python
def build_retro_context(
    selection: RetroWindowSelection,
    *,
    issue_history: IssueHistoryReader,
    workflow_history: WorkflowHistoryReader,
    eval_history: EvalHistoryReader,
) -> RetroContext:
    ...
```

该 Module 的 Depth 来自：一个 Interface 隐藏多来源读取、稳定性检查、source pinning、关联、聚合和 signal 计数。它返回值，不直接调用 LLM。

`RetroContext` 结构：

```yaml
schema_version: "2"
retro_id: retro-...
generated_at: ...
window:
  selection: ...
  change_ids: [RET-...]
source_manifest:
  issue_slice_sha256: ...
  workflow_sources: []
  eval_sources: []
integrity:
  status: complete | incomplete
  reasons: []
signals:
  issue:
    observation_distribution: []
    occurrence_trends: []
    assessment_corrections: []
    regressions: []
    review_decision_patterns: []
    resolution_outcomes: []
    repeated_not_an_issue: []
  workflow:
    gate_pushback: []
    healing_efficiency: {}
    skill_execution_drift: []
  eval:
    trends: []
signal_count: 0
```

每个 signal 必须引用 immutable source IDs。LLM 不得引用只有数组下标、Markdown 行号或自由文本名称的 evidence。

### 5.3 Retro Analysis Module

Retro Analysis 的 Interface 是一个完整 Candidate document，不是散落的自由文本文件。

```yaml
schema_version: "2"
context_sha256: ...
candidates:
  - candidate_id: IMP-CAND-...
    kind: workflow_improvement
    delivery: change_draft
    source_refs:
      problem_ids: [PROB-...]
      occurrence_ids: [OCC-...]
      issue_event_ids: [PEVT-...]
      workflow_evidence_ids: []
      eval_run_ids: []
    target: assurance_agent/workflow/inspect
    rationale: 多个 Change 的 inspect 都截断了 fuzz 根因行
    proposed_change: 优先提取 pytest E 行并在缺失时回读 raw log
    verification:
      suites: [workflow-full]
      required_cases: []
      success_criteria: 不再产生同签名的截断 Observation
    risk: low
    confidence: high
```

`ImprovementKind` 与 `DeliveryKind` 正交：

```text
ImprovementKind:
  prompt_improvement | fixture_improvement | test_improvement |
  workflow_improvement | domain_knowledge

DeliveryKind:
  memory_patch | change_draft | knowledge_delta
```

允许组合通过显式矩阵校验，而不是当前 `finding_kind -> apply_kind` 的一一映射。示例：

| ImprovementKind | 允许的 DeliveryKind |
|---|---|
| `prompt_improvement` | `memory_patch` |
| `fixture_improvement` | `memory_patch`, `change_draft` |
| `test_improvement` | `memory_patch`, `change_draft` |
| `workflow_improvement` | `change_draft` |
| `domain_knowledge` | `knowledge_delta` |

Candidate ID 只用于单次 LLM 输出；canonical Improvement ID 由 deterministic reconciler 根据 versioned fingerprint 生成。

每个 Candidate 必须至少引用一个可在当前 `source_manifest` 中解析的 source ID。`rationale` 可以解释这些事实为何构成改进机会，但不能把 Problem assessment 重新声明为 Candidate 字段。

### 5.4 Improvement Module

Improvement Module 拥有跨 Retro run 的改进行动身份和生命周期。

```python
def reconcile_improvement_candidates(
    candidates: ImprovementCandidateDocument,
    context: RetroContext,
    current: ImprovementProjection,
) -> ImprovementReconciliationPlan:
    ...
```

Fingerprint 输入：

- Improvement kind；
- delivery kind；
- normalized target；
- normalized proposed-change intent；
- fingerprint version。

Fingerprint 不包含：

- Retro ID、时间戳；
- Problem title、severity、status；
- confidence、risk、自然语言 rationale；
- source event 的顺序。

Exact fingerprint 命中已有 Improvement 时，只追加新的 supporting-evidence link；不创建重复 Improvement。Proposal 语义发生实质变化时创建新 Improvement，并可通过 `supersedes` 显式关联旧项。

Improvement events：

```text
improvement_proposed
improvement_evidence_linked
improvement_review_approved
improvement_review_rejected
improvement_rework_requested
improvement_eval_requested
improvement_eval_completed
improvement_exported
improvement_applied
improvement_rolled_back
improvement_superseded
```

Projection 状态：

```text
proposed -> approved | rejected | needs_rework
approved -> evaluating | exported
evaluating -> applied | rolled_back | awaiting_baseline | eval_error
exported -> applied | needs_rework
applied -> rolled_back
rolled_back -> needs_rework
any nonterminal state -> superseded
```

`rejected` 和 `superseded` 是 terminal；`applied` 仍可在发现回归后进入 `rolled_back`。`exported` 表示已进入工程变更流程，不表示来源 Problem 已解决。`applied` 也不直接解决 Problem；Issue verification 独立完成该判断。

---

## 6. Artifact 布局

### 6.1 单次 Retro run

```text
qa/retro/<retro-id>/
├── context.json                  # immutable, schema v2
├── proposal-candidates.json      # noncanonical LLM output
├── retro-summary.md              # rebuildable human view
├── accept-status.json            # batch digest + accepted/failed result
└── review-queue.md               # rebuildable view of reconciled Improvements
```

规则：

- run 目录只记录这次分析；不承担跨 run proposal 状态。
- `context.json` 写入后不可改；重新分析产生新的 `retro-id`。
- `proposal-candidates.json` 必须 pin `context_sha256`。
- `accept-status.json` 必须 pin Candidate batch digest 和写入的 Improvement event IDs。
- summary/review queue 不得被其他 Module 当作事实来源。

### 6.2 Project Improvement Ledger

```text
qa/improvements/
├── events.jsonl                  # canonical append-only source
├── improvements.json             # byte-stable rebuildable projection
└── review-queue.json             # rebuildable projection
```

所有 project writes 使用：

```text
synchronized: [project:qa/improvements/**]
exclusive: [project:improvement-registry]
```

### 6.3 Delivery artifacts

```text
qa/improvements/drafts/<improvement-id>.yaml
.aa/memory/<skill>.md
qa/improvements/knowledge-delta/<improvement-id>.proposal.yaml
.aa/data-knowledge.yaml
```

- `change_draft` 保存 Improvement ID、source refs、target、proposed change 和 verification scope，不复制 Problem assessment。
- `memory_patch` 必须先在 staging memory 上评测，再原子应用。
- `knowledge_delta` 保持 L2 proposal；只有现有 semantic validation 和人工 promotion 可以更新 L1 data knowledge。

---

## 7. Clean-cut 与窗口选择

### 7.1 不读取历史 `qa/retro/**`

新流程明确禁止：

- 扫描旧 run 的 `context.json`、`proposals.json`、`promotions.json`、summary 或 review queue；
- 从 `_state.json` 读取 consumed Changes；
- 从 `cross-run-report.json` 推断趋势；
- 使用旧 Retro proposal、promotion 或 rejection 作为新 LLM evidence；
- 为旧 schema 增加兼容 Adapter。

允许当前 run 内部按拓扑读取前置节点刚写入的文件。这不构成历史读取。

### 7.2 Window selection

Retro 支持三种互斥选择：

1. 显式 `change_ids`；
2. 闭区间 `since/until`；
3. 确定性的最近 `last N` 个 terminal Changes。

选择结果必须在 collect 开始时冻结为排序后的 Change ID 列表，并固定 Project Ledger event head。Retro 不标记 Change 为 consumed。相同窗口允许重跑；Improvement fingerprint/reconciliation 负责跨 run 去重。

如果 `until` 省略，collect 将当前 source head 固定为本 run 的 `until`，写入 context；后续新事件不进入该 run。

`since/until` 按 source event 的权威时间戳过滤，不使用目录 mtime。resolver 必须补齐所选事件的引用闭包：例如窗口内发生的一次 late Problem review，即使引用较早 Change 的 Occurrence，也要把解释该决定所需的 Problem、Occurrence 和 Observation refs 纳入 slice。显式 `change_ids`/`last N` 则纳入这些 Change 所连接 Problem 截至固定 project head 的 lifecycle events。

### 7.3 旧命令和文件

以下旧状态文件在新路径中不再生成或读取：

```text
qa/retro/_state.json
qa/retro/cross-run-report.json
qa/retro/<id>/promotions.json
qa/retro/<id>/eval-results.json
qa/retro/<id>/issue-drafts/**
```

旧目录保留在磁盘作为人工审计记录，但生产代码没有读取路径。删除旧目录不是本设计的要求。

---

## 8. Domain Knowledge 资格

Issue-derived `domain_knowledge` Candidate 必须满足全部条件：

1. 引用至少一个 Problem；
2. Problem assessment 为 `human_confirmed`；
3. Problem 状态为 `resolved`；
4. resolution 已通过 scope-aware verification；
5. proposed knowledge 是稳定、可复用的数据构造或执行事实；
6. payload 不包含 Problem status、severity、known-issue 标记或临时 workaround；
7. L2 semantic validation 通过。

以下状态不能直接产生 Issue-derived knowledge delta：

```text
detected | triaged | in_progress | verification_pending |
accepted_risk | not_an_issue
```

`not_an_issue` 可以支持 Retro 的 negative-classification signal，但不能变成领域事实。`accepted_risk` 仍是活跃 Problem，不得被包装为“已知正常行为”。

---

## 9. 失败、并发与重试

### 9.1 Evidence collection

- Issue Ledger 损坏：Retro collect hard fail，且不调用 LLM。
- Issue analysis failed/project sync pending：context 标记 `integrity: incomplete`；默认只允许 process-only Improvements，不允许 domain knowledge。
- Project Ledger 在读取期间变化：`LedgerIssueHistoryReader` 重试直到 head 稳定或返回 typed conflict。
- Workflow/Eval source 缺失：写入具体 degraded reason；不得伪造空历史。

### 9.2 Candidate generation

- LLM timeout/transport/rate-limit/invalid output：使用现有 agent retry policy。
- context digest 不匹配、未知 evidence ref、禁止字段、非法 kind/delivery：完整 Candidate batch 拒绝。
- 零 Candidate：成功 NOOP，不修改 Improvement Ledger。

### 9.3 Improvement reconciliation

- 完整批次先验证、再计算 event plan、最后写入；任何 Candidate 无效时零 project writes。
- Concurrent exact fingerprints 在 `project:improvement-registry` 下只创建一个 Improvement，其余追加 evidence link。
- Retry 使用 `retro_id + candidate_batch_digest` 作为 idempotency key。
- Projection 从 event bytes 纯 replay；相同 bytes 必须产生 byte-identical JSON。

### 9.4 Apply/export

- `memory_patch` 在 baseline 缺失时保持 `awaiting_baseline`；不得直接应用。
- eval regression 写 `rolled_back`，真实 memory 保持原内容或恢复为原 digest。
- `change_draft` 重复导出必须 hash-idempotent；内容冲突需要显式 overwrite/rework 决定。
- `knowledge_delta` 冲突不覆盖 L1，沿用 knowledge promotion 的显式冲突处理。

---

## 10. Execution Contracts

### 10.1 Retro collect

```yaml
operation:retro-collect:
  reads:
    - selected change Issue/execution/workflow evidence
    - project:qa/issues/**
    - project eval history
  writes:
    - project:qa/retro/${params.retro_id}/context.json
```

只有 deterministic operation 通过 typed Interface 读取 source history。写 allowlist 未声明的 `qa/issues/**` 和 `qa/improvements/**` 必须由现有 contract enforcement 拒绝；不新增平行的 `forbidden_writes` schema。

### 10.2 Retro agent

```yaml
skill:aa-retro:
  reads:
    - project:qa/retro/${params.retro_id}/context.json
  writes:
    - project:qa/retro/${params.retro_id}/proposal-candidates.json
    - project:qa/retro/${params.retro_id}/retro-summary.md
```

它不得读取 `qa/issues/**`、其他 Retro run、raw archive 或 Improvement Ledger。

### 10.3 Improvement reconcile

```yaml
operation:reconcile-improvements:
  reads:
    - current retro context/candidates
    - project:qa/improvements/**
  writes:
    - current retro accept-status/review-queue
    - project:qa/improvements/**
  synchronized:
    - project:qa/improvements/**
  exclusive:
    - project:improvement-registry
```

它不得写 `qa/issues/**`、memory、data knowledge 或产品代码。

### 10.4 Improvement review/eval/apply

review 只写 Improvement Ledger；eval 只写 eval artifacts 和 Improvement events；apply Adapter 只写已批准 delivery 的声明目标，并追加 apply receipt。每种 delivery 使用独立 Adapter，不共享宽泛的 filesystem 写权限。

---

## 11. 对现有 Implementation Plan 的影响

### 11.1 保留

现有 Issue plan 的 Tasks 1–14 保持职责和顺序：

- typed recovery、durable graph、human interrupt；
- synchronized project resources；
- Issue models、identity、transitions、Ledgers、projections；
- Observation collection、Issue Analyzer、reconciliation；
- `inspect-with-issues` full-workflow integration；
- independent Problem review；
- report/archive/risk consumption。

Task 11 的关键 invariant 保留：

```text
execution -> inspect-with-issues -> healing -> report
healing.rerun -> inspect-with-issues -> decide
```

### 11.2 替换 Task 15

现有 Task 15 整体替换，不执行以下步骤：

- 不给 `retro/archive_reader.py` 增加 Issue Ledger parsing；
- 不把 `issues/**` 复制进 `qa/retro/<id>/evidence/**`；
- 不允许 `skill:aa-retro` 直接读取 `qa/issues/**`；
- 不把 Problem decision/status 建模成 Retro 自有事实；
- 不继续使用 `workflow_bug -> issue_export`。

新的 Task 15 应拆为：

1. 实现并测试 `IssueHistoryReader` 和稳定读取 Adapter；
2. 实现 `RetroEvidenceModule` 与 schema-v2 context；
3. 重写 `aa-retro` Candidate schema 与 prompt；
4. 实现 Improvement models、identity、Ledger、projection；
5. 将 retro accept 改为 deterministic Improvement reconciliation；
6. 增加 independent Improvement review/eval/apply Adapter；
7. 删除所有历史 `qa/retro/**` 输入路径和旧三轨 runtime reader。

### 11.3 保留 Task 16，扩展 Task 17

Task 16 移除 known-product Issue 文件依赖的方向不变。

Task 17 增加以下 acceptance invariants：

1. Retro 不属于 full workflow graph。
2. Issue 仍在初次执行和每次 healing rerun 后、report 前 reconcile。
3. `skill:aa-retro` 无法读取或写入 `qa/issues/**`。
4. 新 Retro 不读取任何其他 `qa/retro/<id>/**`。
5. RetroContext 中每个 Issue-derived signal 都引用有效 immutable source ID 和 digest。
6. Improvement Candidate 不包含 Problem lifecycle 字段。
7. 相同 Improvement fingerprint 跨两个 Retro run 只产生一个 canonical Improvement。
8. Improvement review/apply 不修改 Problem；Problem resolution 仍需要 Issue verification。
9. 未 resolved/human-confirmed 的 Problem 不能产生 knowledge delta。
10. 旧 `_state.json`、cross-run report、promotions、issue drafts 对新流程没有读路径。

---

## 12. 测试策略

### 12.1 Interface 测试

- 同一 Ledger bytes 通过 production 和 in-memory Adapter 产生等价 `IssueEvidenceSlice`。
- merge alias、Problem version、late review event、resolution/regression 正确落入 window。
- Project head 在读取中变化时不返回撕裂 slice。
- malformed JSONL、未知 event schema、projection mismatch 可见失败。

### 12.2 Retro signal 测试

- 多个 Occurrence 聚合为一个 trend，不复制 Problem identity。
- human assessment correction 形成 correction signal。
- repeated not-an-issue 只形成 negative-classification signal。
- healing、skill、gate、eval 信号不被写入 Issue Ledger。
- signal evidence refs 全部可在 source manifest 中解析。

### 12.3 Candidate/reconcile 测试

- `workflow_issue` Problem 可以支持 `workflow_improvement`，但二者 ID/状态完全独立。
- `product_bug` 可以支持 prompt/test/workflow Improvement。
- Candidate 携带 severity/status/root cause 副本时完整批拒绝。
- 未知 Problem/event ref、context digest mismatch、非法 delivery matrix 完整批拒绝。
- concurrent identical Candidate 只创建一个 Improvement。

### 12.4 Clean-cut guard

增加 repository guard，扫描生产代码和 packaged skill：

- 禁止读取 `qa/retro/_state.json`、`cross-run-report.json`、旧 `promotions.json`；
- 禁止枚举其他 Retro run 作为 evidence；
- 禁止 `skill:aa-retro` contract 使用 `project:qa/retro/**` 宽泛 read；
- 禁止旧 `workflow_bug`、`issue_export` runtime enum；
- 历史 specs/plans 和 benchmark fixtures 可排除，但不能被 production path 加载。

### 12.5 End-to-end 场景

1. Full workflow 发现 HTTP 500，创建 `product_bug` Problem；本次 full 不运行 Retro。
2. 多个 Change 出现同类误分类；独立 Retro 生成 `prompt_improvement`，未创建新 Problem。
3. inspect 截断日志先形成 `workflow_issue`；Retro 后续形成引用该 Problem 的 `workflow_improvement`。
4. 相同窗口重跑，run artifacts 不同但 canonical Improvement 不重复。
5. Improvement 应用后，Problem 保持原状态；后续 authoritative execution 通过 Issue verification 才解决。
6. resolved/human-confirmed Problem 产生 knowledge delta；L1 在 promotion 前 byte-identical。

---

## 13. 验收标准

1. Issue full-workflow 时序与现有 approved design 一致。
2. Retro 与 full workflow 无 edge 或隐式调用。
3. 每个 Problem lifecycle 字段只有 Issue Ledger 一个权威 writer。
4. 每个 Improvement lifecycle 字段只有 Improvement Ledger 一个权威 writer。
5. Retro Agent 的全部 Issue 证据来自当前 digest-pinned context。
6. RetroContext 不可被用来 rebuild 或修改 Problem Ledger。
7. 历史 `qa/retro/**` 读取次数为零。
8. Exact Improvement fingerprint duplicate 数为零。
9. Unauthorized Retro writes to `qa/issues/**` 为零。
10. 未验证 Issue-derived knowledge promotion 数为零。
11. Ledger replay 和 projection byte stability 测试通过。
12. focused、integration、full pytest、lint 和 type-check 全部通过。

---

## 14. 明确删除的旧语义

新路径不保留以下语义：

- Retro 通过 archive failure analysis 重新建立一套产品问题事实；
- `RetroProposal.problem` 作为产品 Problem 的自然语言副本；
- `finding_kind=workflow_bug`；
- `apply_kind=issue_export`；
- per-run `promotions.json` 作为跨 run Proposal 状态；
- `_state.json` consumed-change cursor；
- `cross-run-report.json` 作为趋势输入；
- `known_product_issue_candidate` 写入 data knowledge；
- Agent 直接浏览 Project Problem Ledger。

替代关系：

| 旧语义 | 新语义 |
|---|---|
| `workflow_bug` | Issue `workflow_issue` + 可选 Improvement `workflow_improvement` |
| `issue_export` | `change_draft` delivery，引用 Improvement/Problem IDs |
| `RetroProposal.problem` | `rationale`，只解释改进机会 |
| per-run promotion state | Project Improvement Ledger |
| historical Retro trend | Issue/Workflow/Eval 权威历史重新派生 |
| consumed Change | 显式、冻结 window |

该 clean cut 使删除测试成立：删除 `qa/retro/**` 历史目录不会改变任何新 Retro 的输入、Issue/Problem 状态或 Improvement projection。
