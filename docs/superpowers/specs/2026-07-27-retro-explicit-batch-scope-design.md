# Retro 显式批次窗口与证据隔离设计

- Status: Proposed
- Date: 2026-07-27
- Scope: 自动 Retro 的批次选择、部分批次语义、Graph evidence snapshot、Benchmark 串联与 Improvement reconcile
- Related: `docs/specs/2026-07-26-retro-v3-signal-analysis.md`

## 1. 问题

自动 Retro 当前以 `retro_last=10` 选择最近的 terminal Change。这个选择不是调用方的真实批次：本批少于十个 Change 时会混入旧批次，本批超过十个 Change 时会截断当前批次。它还迫使 Retro 从宽泛的 `qa/changes/**` 与 `eval/**` 快照中重新推断范围。

宽泛的 Eval 同步已暴露一个确定性 Graph workspace bug：同步基线会捕获 `eval/out/runs/**/samples/**/sut/**` 中的嵌套 SUT，而 write-set freeze 又排除顶层 `eval/`；Issue ledger 的后缀特例随后把嵌套 `issues/events.jsonl` 误判为未授权删除，令 `collect-retro-evidence` 以 `forbidden_write` 失败。失败发生在 proposer 之前，因此不会生成 Candidate，也不会进入 Improvement lifecycle。

## 2. 决策摘要

1. 自动 Retro 的分析单位是当前 Orchestrator invocation 的显式 Change 集合。
2. 批次由 Orchestrator 定义，Retro 不通过目录、时间戳、Change ID 命名或 `last N` 推断成员。
3. `completed`、`failed`、`stopped` 等 Graph terminal Change 均进入批次窗口；`final_status=FAIL` 和 archive gate 拒绝不排除成员。
4. 批次可以以 `incomplete` 关闭。已 terminal 的 Change 继续进入 Retro；未 terminal 成员以结构化 `excluded` 记录。
5. 自动 Batch Retro 只分析当前批次原始证据。跨批状态和重复提案由 Problem/Improvement Ledger 处理；跨批弱趋势由独立的 time-range Trend Retro 处理。
6. 原始 Eval sample/SUT 永不进入 Retro workspace。Retro 只读取小型 canonical Eval report projection。
7. `forbidden_write` 继续 fail-closed，但只能由实际 workspace 写入触发，不能由不对称快照产生。

## 3. 领域术语与所有权

- **Item**：Orchestrator 的一个执行输入。Benchmark 中一个 item 映射为一个 Change。
- **Change**：Full workflow、Issue 分析、healing、report 和 archive 的执行与证据单元。
- **Batch**：一次 Orchestrator invocation 调度的全部 Item/Change。Batch ID 由 Orchestrator 创建。
- **Included Change**：本批已达到 Graph terminal、进入 Retro evidence window 的 Change。
- **Excluded Change**：本批计划成员，但关闭批次时尚未 terminal 的 Change。
- **Shell Change**：承载 project-level Retro GraphRuntime 文件的技术目录；它不是 Batch 成员，也不是 Retro 证据。

Orchestrator 拥有 Batch membership；Retro 拥有已解析的 immutable evidence window；全局 Ledger 拥有跨批生命周期。

## 4. Batch 输入契约

自动调用向 Retro 传入：

```json
{
  "retro_id": "retro-20260727-batch",
  "change_ids": [
    "RET-user-management-20260727-183740-cursor"
  ],
  "batch_scope": {
    "schema_version": "1",
    "batch_id": "20260727-183740-cursor",
    "status": "incomplete",
    "planned_change_ids": [
      "RET-user-management-20260727-183740-cursor",
      "RET-dept-management-20260727-183740-cursor"
    ],
    "excluded": [
      {
        "change_id": "RET-dept-management-20260727-183740-cursor",
        "reason": "non_terminal"
      }
    ]
  }
}
```

约束：

- `change_ids`、`planned_change_ids` 和 `excluded[].change_id` 各自唯一并使用 canonical 排序。
- `change_ids` 与 excluded IDs 不相交。
- 两者并集必须等于 `planned_change_ids`。
- `status=complete` 要求 `excluded=[]`；`status=incomplete` 要求至少一个 excluded member。
- 每个 included Change 必须存在并达到 Graph terminal。违反时 collect 返回 `invalid_input`，不得把 Orchestrator 契约错误降级成完整窗口。
- 支持的初始 exclusion reason 为 `non_terminal`、`hard_timeout`、`cancelled` 和 `not_started`。
- `batch_scope` 进入 `window.json`，并由 `context.json` 原样冻结，确保 Candidate 与 receipt 可追溯到批次成员。

人工 `aa retro --change ...`、`--last` 和 `--since/--until` 继续支持。只有自动 Batch Retro 必须提供 `batch_scope`；`--last` 不再用于自动闭环。

## 5. 部分批次语义

Orchestrator 在等待上限到达后可以关闭 incomplete Batch：

1. included Change 继续收集 Issue、Workflow 与关联 Eval 证据；
2. excluded Change 不进入 evidence slice，不允许读取其半成品 ledger；
3. `RetroContext.integrity.status` 为 `incomplete`，reason 包含 `batch_member_excluded:<change-id>:<reason>`；
4. 有完整来源支撑的 process Candidate 仍可进入 reconcile；
5. incomplete context 禁止 `domain_knowledge` Candidate；
6. 如果 included 集合为空，不调用三个分析 Agent，不调用 proposer，写确定性 incomplete/NOOP receipt；
7. Batch completion 与 archive eligibility 无关，archive gate 拒绝的 terminal Change 必须包含。

## 6. 自动 Batch Retro 数据流

```text
Orchestrator BatchManifest
  -> validate-batch-scope
  -> resolve explicit change_ids window
  -> collect typed Issue / Workflow / Eval slices
  -> analyze issue, workflow, eval in parallel
  -> assemble RetroContext v3
  -> propose Improvement Candidates
  -> reconcile global Improvement Ledger
       new fingerprint      -> improvement_proposed
       existing fingerprint -> improvement_evidence_linked
  -> accept-status + review queue
```

`reconcile-improvements` 是自动 Retro 的闭环终点。新 Improvement 必须以 `proposed` 写入全局 review queue；相同 fingerprint 不创建第二条 Improvement，而是追加新批 evidence。后续人工 review、delivery、evaluation 仍由独立 Improvement lifecycle entrypoint 处理，Retro 不自动批准或落地修改。

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

Retro contract 改为读取并同步 `project:qa/eval/**`。显式 Change/Batch 模式只收 `source_change_ids` 与 included Change 相交的报告；空关联的 Benchmark Eval metrics 不进入 Batch Retro。

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

1. 从 manifest 构造 included `change_ids` 和 excluded members；
2. 使用显式批次参数调用 canonical Retro Graph；
3. `retro_shell_change_id` 仅承载 Graph，不参与 selection；
4. loop summary 展示 batch ID、complete/incomplete、included 与 excluded；
5. 删除自动链路中的 `retro_last=10` 和基于最新目录推断批次成员的行为。

## 11. 错误处理

- Batch contract 不一致、included Change 缺失或非 terminal：`invalid_input`，Graph failed。
- included ledger 损坏、source digest 漂移：collect hard fail，不启动分析 Agent。
- excluded non-terminal member：context incomplete，但流程继续。
- 单域 Agent timeout/invalid output：沿 Retro v3 typed recovery 进入 settled join。
- Candidate/schema/ref 失败：reconcile fail-visible，不写部分 Ledger event。
- 实际写出 authorization scope：`forbidden_write`，Graph failed。
- 无关 raw Eval artifact：不可见，不参与完整性，也不能产生 write diff。

## 12. 验收测试

1. 两个 included Change（PASS + `final_status=FAIL`）进入同一 window。
2. archive gate 拒绝的 terminal Change仍被包含。
3. 本批少于十个 Change 时不混入旧批；多于十个时不截断。
4. incomplete Batch 对 terminal 子集完成分析，并在 context 记录 excluded member。
5. included 为空时零 Agent、零 Candidate、确定性 incomplete/NOOP receipt。
6. included 中出现 missing/non-terminal Change 时 `invalid_input`。
7. Batch Retro 不读取历史 `qa/retro/**`。
8. `eval/out/**/samples/**/sut/qa/changes/**/issues/events.jsonl` 不进入任务树，也不触发 `forbidden_write`。
9. 只有 `qa/eval/**` 中与 batch `change_ids` 相交的报告进入 Eval slice。
10. 新 Candidate 产生一个 `proposed` Improvement 和 review queue entry。
11. 下一批相同 fingerprint 只产生 `improvement_evidence_linked`，Improvement 数量不增加。
12. incomplete context 拒绝 `domain_knowledge`，但允许有完整 source refs 的 process Candidate。
13. Benchmark resume 使用同一 batch manifest，成员不重复，Retro window 可重现。

## 13. 非目标

- 不改变 Problem fingerprint 或 Improvement fingerprint。
- 不自动批准、交付或评估 Improvement。
- 不迁移、不读取历史 `qa/retro/**`。
- 不将 Benchmark Eval metrics 变成产品 gate。
- 不通过扩大 authorization write scope 修复 workspace false positive。
