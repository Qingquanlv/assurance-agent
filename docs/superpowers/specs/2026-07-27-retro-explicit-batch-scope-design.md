# Retro 显式批次窗口与证据隔离设计

- Status: Design approved
- Date: 2026-07-27
- Scope: 自动 Retro 的批次选择、部分批次语义、Graph evidence snapshot、Benchmark 串联与 Improvement reconcile
- Related:
  - `docs/specs/2026-07-26-retro-v3-signal-analysis.md`
  - `docs/superpowers/specs/2026-07-27-improvement-automatic-review-design.md`

## 1. 问题

自动 Retro 当前以 `retro_last=10` 选择最近的 terminal Change。这个选择不是调用方的真实批次：本批少于十个 Change 时会混入旧批次，本批超过十个 Change 时会截断当前批次。它还迫使 Retro 从宽泛的 `qa/changes/**` 与 `eval/**` 快照中重新推断范围。

宽泛的 Eval 同步已暴露一个确定性 Graph workspace bug：同步基线会捕获 `eval/out/runs/**/samples/**/sut/**` 中的嵌套 SUT，而 write-set freeze 又排除顶层 `eval/`；Issue ledger 的后缀特例随后把嵌套 `issues/events.jsonl` 误判为未授权删除，令 `collect-retro-evidence` 以 `forbidden_write` 失败。失败发生在 proposer 之前，因此不会生成 Candidate，也不会进入 Improvement lifecycle。

## 2. 决策摘要

1. 自动 Retro 的分析单位是当前 Orchestrator invocation 的显式 Change 集合。
2. 批次由 Orchestrator 定义，Retro 不通过目录、时间戳、Change ID 命名或 `last N` 推断成员。
3. 每个实际启动的 Batch 都无条件进入 Retro；Full、Issue、archive 或 Eval 的 verdict 不能成为 Retro 入口 gate。
4. `completed`、`failed`、`stopped`、`hard_timeout`、`cancelled` 和仍非 terminal 的 Change 都进入批次窗口；失败、缺失与部分证据本身就是 workflow/process evidence。
5. 批次可以以 `incomplete` 关闭。Retro 对可用证据做正常分析，并把不可用证据转换成结构化信号，而不是在 collect 阶段停止。
6. Retro 自己的 collect、分析、assemble、propose 或 reconcile 失败也必须成为确定性的 workflow improvement evidence；Retro 采用 always-enter、always-finalize 语义。
7. 自动 Batch Retro 只分析当前批次原始证据。跨批状态和重复提案由 Problem/Improvement Ledger 处理；跨批弱趋势由独立的 time-range Trend Retro 处理。
8. 原始 Eval sample/SUT 永不进入 Retro workspace。Retro 只读取小型 canonical Eval report projection。
9. `forbidden_write` 继续 fail-closed 地保护 canonical workspace，但触发后转入 Retro pipeline failure fallback，不能让本次改进记录消失。

## 3. 领域术语与所有权

- **Item**：Orchestrator 的一个执行输入。Benchmark 中一个 item 映射为一个 Change。
- **Change**：Full workflow、Issue 分析、healing、report 和 archive 的执行与证据单元。
- **Batch**：一次 Orchestrator invocation 调度的全部 Item/Change。Batch ID 由 Orchestrator 创建。
- **Batch Member**：本批计划或实际执行的 Change，无论成功、失败、超时或是否 terminal 都进入 Retro window。
- **Evidence Availability**：成员证据的 `complete | partial | absent` 状态；它控制可用分析范围，不控制 Retro 是否执行。
- **Pipeline Failure**：Retro 自己任一 stage 的结构化失败记录；由 deterministic fallback 转换为 process Improvement evidence。
- **Shell Change**：承载 project-level Retro GraphRuntime 文件的技术目录；它不是 Batch 成员，也不是 Retro 证据。

Orchestrator 拥有 Batch membership；Retro 拥有已解析的 immutable evidence window；全局 Ledger 拥有跨批生命周期。

## 4. Batch 输入契约

自动调用向 Retro 传入：

```json
{
  "retro_id": "retro-20260727-batch",
  "change_ids": [
    "RET-dept-management-20260727-183740-cursor",
    "RET-user-management-20260727-183740-cursor"
  ],
  "batch_scope": {
    "schema_version": "1",
    "batch_id": "20260727-183740-cursor",
    "status": "incomplete",
    "members": [
      {
        "change_id": "RET-dept-management-20260727-183740-cursor",
        "execution_status": "hard_timeout",
        "evidence_availability": "partial"
      },
      {
        "change_id": "RET-user-management-20260727-183740-cursor",
        "execution_status": "completed",
        "evidence_availability": "complete"
      }
    ]
  }
}
```

约束：

- `change_ids` 与 `members[].change_id` 各自唯一、canonical 排序且集合必须相等。
- `status=complete` 要求所有 member 的 evidence availability 为 `complete`；否则必须是 `incomplete`。
- `execution_status` 支持 `completed`、`failed`、`stopped`、`hard_timeout`、`cancelled`、`running` 和 `not_started`。
- `evidence_availability` 由 Orchestrator 根据是否存在可读 Change workspace 和 authoritative batch artifacts 初步填写；Retro collect 会验证并可将 `complete` 降级成 `partial`/`absent`，不能反向升级。
- Change 缺失、非 terminal、ledger 缺失或损坏不再是 Retro 入口错误；它们生成 `batch_member_evidence_gap`，并使对应 domain integrity incomplete。
- `batch_scope` 进入 `window.json`，并由 `context.json` 原样冻结，确保 Candidate 与 receipt 可追溯到批次成员。

人工 `aa retro --change ...`、`--last` 和 `--since/--until` 继续支持。只有自动 Batch Retro 必须提供 `batch_scope`；`--last` 不再用于自动闭环。

## 5. 部分批次语义

Orchestrator 在等待上限到达后可以关闭 incomplete Batch，但仍必须调用 Retro：

1. `complete` member 走完整 Issue、Workflow 与关联 Eval 分析；
2. `partial` member 只读取已经闭合、可校验的 allowlisted artifacts，禁止把正在写入的文件当作 immutable evidence；
3. `absent` member 不伪造领域事实，而是生成 `batch_member_evidence_gap` workflow signal；
4. `RetroContext.integrity.status` 为 `incomplete`，reason 包含 `batch_member_evidence_gap:<change-id>:<execution-status>`；
5. 有完整来源支撑的 process Candidate 仍可进入 reconcile；
6. incomplete context 禁止 `domain_knowledge` Candidate；
7. 所有 member 均无领域证据时，跳过三个领域分析 Agent，直接由 deterministic fallback 生成 evidence-gap process Candidate；
8. Batch completion 与 archive eligibility 无关，archive gate 拒绝本身必须成为 Workflow evidence。

## 6. 自动 Batch Retro 数据流

```text
Orchestrator BatchManifest
  -> validate-batch-scope
  -> resolve explicit change_ids window
  -> collect available typed Issue / Workflow / Eval slices
       unavailable/corrupt source -> typed evidence-gap signal
  -> analyze issue, workflow, eval in parallel
       stage failure -> typed pipeline-failure signal
  -> assemble RetroContext v3
  -> propose Improvement Candidates
       proposer failure -> deterministic process Candidate
  -> reconcile global Improvement Ledger
       new fingerprint      -> improvement_proposed
       existing fingerprint -> improvement_evidence_linked
       ledger unavailable   -> durable pending outbox
  -> accept-status + review queue
```

`reconcile-improvements` 或 durable pending outbox 是 Retro pipeline 自身的闭环终点。新 Improvement 必须先以 `proposed` 写入全局 review queue；相同 fingerprint 不创建第二条 Improvement，而是追加新批 evidence。`retro-status.json` 写定后，外层 Graph 可以按照独立的 Improvement Auto Review 设计，对本轮 receipt 指向的 `proposed` Improvement 执行失败隔离的自动审查。Retro proposer/reconciler 本身不批准 Improvement，Auto Review 也不自动触发 delivery 或 evaluation。

### 6.1 Retro 自失败 fallback

RetroSupervisor 在正常 Graph 外负责最终收口，避免 Graph 自身失败造成递归或证据丢失：

1. Graph stage 的 recovery edge 优先写 `pipeline-failure.json` 并继续到 finalize；
2. workspace freeze、graph compile/dispatch 等无法进入 recovery edge 的失败，由 Supervisor 从 Retro shell ledger 或捕获的结构化 runtime exception 构建同一 failure envelope；
3. failure envelope 只包含 `retro_id`、batch ID、stage/node、structured `error_kind`、message fingerprint、可选 runtime event ID 与时间，不复制异常路径下的原始敏感内容；
4. deterministic fallback 使用稳定模板生成 `workflow_improvement` Candidate，target 为 `assurance-agent:retro:<stage>`，intent 由 `stage + error_kind` 决定，避免自由消息改变 fingerprint；
5. fallback Candidate 直接进入同一个 Improvement reconciler，不再调用 proposer，也不触发新的 Retro；
6. Improvement Ledger 暂时不可写时，将已校验 envelope/Candidate 原子写入 `qa/improvements/outbox/<retro-id>.json`；下次 Retro 在分析前先幂等 drain outbox；
7. 每轮写 `retro-status.json`，result 只允许 `completed`、`completed_with_gaps` 或 `pending_reconcile`。只有项目存储完全不可写、连 status/outbox 都无法持久化时才返回 `technical_failure`。

Supervisor 不是任意 workspace 写入旁路。它的 write interface 固定为当前 `qa/retro/<retro-id>/{pipeline-failure.json,retro-status.json}` 与 `qa/improvements/outbox/<retro-id>.json`，正常 Ledger 提交仍只经过现有 Improvement store/lock/reconciler。

## 7. 历史连续性

常规 Batch Retro 不读取所有旧 Change，也不读取历史 `qa/retro/**`：

- Issue slice 只包含本批 Occurrence 以及这些 Occurrence 引用的当前 Problem projection；
- Workflow 和 Eval raw evidence 只包含本批关联记录；
- Improvement reconciler 读取全局 Ledger 以做 fingerprint 去重和 evidence link；
- 跨批弱趋势由人工或定时 `--since/--until` Trend Retro 分析明确时间窗口；
- 同一个 Change 重新分析必须使用新 retro ID，既有 Improvement 通过 fingerprint 保持单一身份。

## 8. Eval evidence projection

移除 Retro contract 对 `project:eval/**` 的 broad read/synchronized claim。`aa eval run` 在写原始 `eval/out/runs/<run-id>/report.json` 的同时，写一个不含 sample、SUT、日志或密钥的小型 canonical projection：

```text
qa/eval/runs/<run-id>/report.json
```

projection 至少包含：`run_id`、`suite`、`verdict`、`started_at`、`completed_at`、`source_change_ids`、`failure_signature`、`sample_ids` 和原始 report digest。写入使用 immutable/idempotent 语义：同一 run ID 的相同 bytes 是重放，不同 bytes 是 conflict。

Retro contract 改为读取并同步 `project:qa/eval/**`。显式 Change/Batch 模式只收 `source_change_ids` 与 Batch member 相交的报告；空关联的 Benchmark Eval metrics 不进入 Batch Retro。projection 缺失或损坏只使 Eval domain incomplete，并生成 process evidence，不阻止 Issue/Workflow 分析和最终 reconcile。

## 9. Workspace 快照修复

GraphRuntime 的 Issue ledger capture 特例必须只匹配项目顶层：

```text
qa/changes/<change-id>/issues/events.jsonl
qa/archive/<change-id>/issues/events.jsonl
```

任何位于 `eval/**`、task workspace 副本或其他 excluded ancestor 下的同名后缀都保持 excluded。基线 overlay、task materialization 和 write-set freeze 必须对 excluded ancestor 使用同一语义。

不得通过向 Retro 增加 `project:eval/**` authorization write 权限绕过错误。

## 10. Benchmark 串联

Benchmark 在 run 开始时以 `RUNSTAMP` 创建 durable batch manifest，成员来自当次 `BENCHMARK_ITEMS`，不是磁盘目录扫描。每个 item 结束或超时时原子更新成员状态；resume 使用同一个 RUNSTAMP 和 manifest。

循环结束后：

1. 从 manifest 构造全部 Batch member `change_ids`、execution status 与 evidence availability；
2. 无论本批 workflow/archive 结果如何，都使用显式批次参数调用 canonical Retro Graph；
3. `retro_shell_change_id` 仅承载 Graph，不参与 selection；
4. loop summary 展示 batch ID、complete/incomplete、每个 member 状态与 Retro `retro-status`；
5. 删除自动链路中的 `retro_last=10` 和基于最新目录推断批次成员的行为。

Retro 结果是改进观测结果，不是 Full/archive 的前置或后置 gate。`completed_with_gaps` 和 `pending_reconcile` 不改变原执行 verdict；Benchmark 仍可依据 Full workflow 自身结果失败，但不得因 Retro 记录了该失败而跳过 Retro 或覆盖原 verdict。

## 11. 错误处理

- Batch membership schema/集合不一致：正常 collect 不可信，转入 deterministic batch-contract failure fallback，并产生 process Candidate。
- Change 缺失、非 terminal、ledger 缺失/损坏或 source digest 漂移：拒绝消费不可信内容，写 typed evidence gap，context incomplete，流程继续。
- 单域 Agent timeout/invalid output：沿 Retro v3 typed recovery 进入 settled join。
- Candidate/schema/ref 失败：不写部分 Ledger event，转为 proposer/reconcile pipeline failure Candidate。
- 实际写出 authorization scope：当前 task fail-closed；Supervisor 将 `forbidden_write` 转成 pipeline failure envelope 后完成 fallback reconcile。
- Improvement Ledger lock/IO 暂时失败：写 durable outbox，结果为 `pending_reconcile`。
- 无关 raw Eval artifact：不可见，不参与完整性，也不能产生 write diff。
- 唯一不可补偿错误是项目存储完全不可写；此时无法持久化任何分析或 outbox，只能返回 `technical_failure`。

## 12. 验收测试

1. 两个 Batch member（PASS + `final_status=FAIL`）进入同一 window。
2. archive gate 拒绝的 terminal Change仍被包含并产生 Workflow evidence。
3. 本批少于十个 Change 时不混入旧批；多于十个时不截断。
4. incomplete Batch 同时携带 complete、partial、absent member；可用证据完成分析，缺口进入 context。
5. 全部 member 证据 absent 时零领域 Agent，但 deterministic fallback 产生 evidence-gap process Candidate。
6. missing/non-terminal Change 不阻止 Retro，且不得生成无来源的领域事实。
7. Batch Retro 不读取历史 `qa/retro/**`。
8. `eval/out/**/samples/**/sut/qa/changes/**/issues/events.jsonl` 不进入任务树，也不触发 `forbidden_write`。
9. 只有 `qa/eval/**` 中与 batch `change_ids` 相交的报告进入 Eval slice。
10. 新 Candidate 产生一个 `proposed` Improvement 和 review queue entry。
11. 下一批相同 fingerprint 只产生 `improvement_evidence_linked`，Improvement 数量不增加。
12. incomplete context 拒绝 `domain_knowledge`，但允许有完整 source refs 的 process Candidate。
13. Benchmark resume 使用同一 batch manifest，成员不重复，Retro window 可重现。
14. collect `forbidden_write`、Agent timeout、assemble failure 和 proposer invalid output 各自通过 fallback 形成稳定 workflow Improvement；不递归启动 Retro。
15. reconcile ledger 暂时不可写时写 outbox；下一次 Retro drain 后只产生一组幂等 Ledger events。
16. `retro-status.result` 对正常、部分证据和 pending reconcile 分别为 `completed`、`completed_with_gaps`、`pending_reconcile`。

## 13. 非目标

- 不改变 Problem fingerprint 或 Improvement fingerprint。
- Retro proposer/reconciler 不自动批准 Improvement；批准权只存在于独立的 Improvement Auto Review/Human Review lifecycle。
- 不自动交付或评估 Improvement。
- 不迁移、不读取历史 `qa/retro/**`。
- 不将 Benchmark Eval metrics 变成产品 gate。
- 不通过扩大 authorization write scope 修复 workspace false positive。
- 不声称在项目存储完全不可写时仍能闭环；这种物理故障保留为唯一 `technical_failure`。
