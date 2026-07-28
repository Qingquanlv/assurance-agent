# Improvement 自动审查与受限批准设计

- Status: Design approved
- Date: 2026-07-27
- Scope: Improvement `proposed` 阶段的自动审查、确定性批准 Gate、人工接管、审计与 Graph 串联
- Related:
  - `docs/superpowers/specs/2026-07-27-retro-explicit-batch-scope-design.md`
  - `docs/specs/2026-07-26-retro-v3-signal-analysis.md`
  - `docs/superpowers/specs/2026-07-25-issue-lifecycle-design.md`

## 1. 问题

当前 `improvement-review-workflow` 是完全人工的：

```text
load-improvement-review-context
  -> human interrupt
  -> apply-improvement-review
```

Retro reconcile 会把 Candidate 写成 `proposed` Improvement 并放入全局 review queue，但没有 reviewer skill、机器可读 assessment 或自动 Gate。即使提案风险低、证据完整且验证方式明确，也必须逐条等待人工 action，导致 Improvement lifecycle 在 `proposed` 阶段堆积。

API Plan Review 已证明“只读 reviewer -> 结构化输出 -> 确定性 Gate -> 必要时人工接管”可行，但不能原样复制：Plan Review 的 `pass` 只释放当前 Graph 的 codegen gate；Improvement 的 `approve` 会写入全局 canonical Ledger，并授予后续 evaluate/export/apply 的资格，权限更强。

## 2. 决策摘要

1. 新增只读 `aa-improvement-reviewer` skill，复用 Plan Review 的结构化审查协议，不复用 API Plan 的领域标准。
2. Skill 只生成 assessment；只有确定性 Gate 和 synchronized apply operation 能写 Improvement Ledger。
3. 满足严格条件的低风险 process Improvement 允许自动执行 `proposed -> approved`。
4. 系统永不自动 `reject`、`request_rework`、`supersede`，这些 canonical 决定继续属于人工。
5. Auto Review 不直接读取历史 `qa/retro/**`。Reconciler 在当前 Retro 内生成内容寻址、不可变的 review subject，Reviewer 只读这个 subject。
6. 自动审查由 Graph 原生编排，不在 `retro_cmd.py`、Benchmark shell 或 Agent prompt 中硬编码串联。
7. Retro pipeline 先写最终 `retro-status.json`；Auto Review 是其后的独立 Improvement lifecycle stage。Reviewer 失败不能反向令 Retro 失败。
8. 自动批准不会自动触发 evaluate、export、apply 或 rollback。
9. 人工可在交付开始前撤销错误的自动批准，将其转为 `needs_rework` 或 `rejected`；人工批准不开放该恢复边。
10. 本设计不引入 Improvement Fixer。提案修订需要新的 canonical revision event，另行设计。

## 3. 权威边界

| 组件 | 职责 | 禁止行为 |
|---|---|---|
| Retro proposer | 从已校验 Signal 生成 Candidate | 批准、拒绝或交付 Improvement |
| Improvement reconciler | fingerprint 去重、写 proposal/evidence event、生成 review subject | 根据 LLM 意见决定批准 |
| `aa-improvement-reviewer` | 读取一个冻结 subject，输出审查意见 | 写 Ledger、修改 subject、执行 delivery |
| Auto-review Gate | 校验 assessment，并计算 `auto_eligible` | 相信 Skill 自报的自动批准资格 |
| Auto-review apply | 持锁重读状态，写一个 typed event | 忽略 stale version/subject |
| Human review | 作出 reject/rework/supersede、处理所有升级项 | 绕过 stale/version 校验 |
| Delivery entrypoints | evaluate/export/apply/rollback | 因 auto approval 自动启动 |

Improvement Ledger 继续是生命周期的唯一 canonical authority。Review subject 和 assessment 是 digest-bound audit artifacts；它们不能单独改变状态。

## 4. Graph 拓扑

### 4.1 自动 Retro 串联

`retro` entrypoint 使用外层 orchestration graph：

```text
retro-orchestration-workflow
  -> graph:retro-workflow
       -> analyze
       -> propose
       -> reconcile
       -> write retro-status
       -> END
  -> select-current-retro-auto-review-items
  -> fan-out graph:improvement-auto-review-cycle
  -> summarize-auto-review-batch
  -> END
```

约束：

- `retro-workflow` 自身仍以 reconcile/outbox 和 `retro-status.json` 为闭环终点；
- selector 只消费本轮 accept/drain receipt 指向、当前仍为 `proposed` 且 subject 尚未被当前 policy 审过的 Improvement；
- fan-out item 是明确的 `{improvement_id, subject_sha256}`，不得扫描目录推断；
- `retro_dry_run=true`、零 Candidate 或 `pending_reconcile` 且尚未 drain 时，selector 返回空列表；
- Auto Review 的 timeout、invalid output、conflict 等都在 child cycle 内收口，不传播成 Retro failure；
- 只有项目存储完全不可写、连审查状态也无法记录时，外层命令才可报告技术错误，但已写出的 Retro status 不得被覆盖。

### 4.2 单条 Auto Review cycle

```text
load-review-subject
  -> skill:aa-improvement-reviewer
  -> validate-improvement-review-assessment
  -> improvement-auto-review-gate
       pass + auto_eligible -> apply-auto-approval -> END
       otherwise            -> record-auto-review -> END
       reviewer failure     -> record-review-error -> END
```

这个 graph 没有 human interrupt。所有非自动批准结果都保持 `proposed` 并留在全局人工 review queue。现有 `improvement-review` repeatable entrypoint 继续承载人工 interrupt，并展示最新 Auto Review assessment。

另提供显式、repeatable 的 `improvement-auto-review` entrypoint，用于对指定 Improvement 重试。自动重试必须显式指定新 attempt；正常 selector 不会对同一 subject/policy 无限重放。

## 5. 冻结 Review Subject

### 5.1 生成时机

Reconciler 已经持有当前 Candidate、RetroContext v3、所引用 Signals 和已校验 source refs，因此由它生成 Reviewer 输入，而不是让 Reviewer 重新遍历历史 Retro。

每个 subject 写入：

```text
qa/improvements/review-subjects/<subject-sha256>.json
```

subject 至少包含：

```json
{
  "schema_version": "1",
  "improvement_id": "IMP-...",
  "kind": "workflow_improvement",
  "delivery": "change_draft",
  "target": "assurance-agent:retro:collect",
  "rationale": "...",
  "proposed_change": "...",
  "verification": {
    "suites": ["retro-workflow"],
    "required_cases": [],
    "success_criteria": "..."
  },
  "risk": "low",
  "confidence": "high",
  "source_refs": {},
  "signal_evidence": [],
  "source_manifest": {},
  "provenance": {
    "retro_id": "retro-...",
    "candidate_id": "...",
    "context_sha256": "sha256:...",
    "candidate_batch_digest": "sha256:..."
  }
}
```

`signal_evidence` 只包含 Candidate `signal_ids` 指向的 canonical Signal；deterministic pipeline-failure/evidence-gap Candidate 则包含对应的结构化 failure envelope。它不复制无关 Signal、历史 Retro、原始日志、Eval SUT 或密钥。

### 5.2 Digest 与版本

- `subject_sha256` 对 canonical subject bytes 计算，不包含 Ledger version、review attempt、assessment 或时间戳；
- proposal/evidence-link event 增加可选 `review_subject_sha256`，旧事件缺失该字段仍可 replay；
- Improvement projection 暴露当前 `review_subject_sha256: str | null`；
- 新 evidence 改变 source refs/Signal 后生成新 subject digest；
- 仅仅记录 Auto Review event 不改变 subject digest，避免 version 增长触发无限复审；
- 老的 `proposed` Improvement 若没有 subject，不回读历史 Retro 补建，直接保留给人工 review。

在 Improvement registry lock 内先幂等发布内容寻址 subject，再 append/rebuild Ledger。Subject 已写而 Ledger append 失败只产生无引用 orphan，可安全重试；Ledger 不得引用不存在或 digest 不匹配的 subject。

## 6. Reviewer Skill 契约

新增 `aa-improvement-reviewer`：

- 只读当前 `<subject-sha256>.json`；
- 不依赖对话历史；
- 不读取 `qa/retro/**`、`qa/issues/**`、源代码工作树或其他 Improvement；
- 只写当前 review ID 的 assessment 与 summary；
- 输出后必须通过 registry schema 校验。

输出路径：

```text
qa/improvements/reviews/<review-id>/assessment.json
qa/improvements/reviews/<review-id>/summary.md
```

assessment 核心字段：

```json
{
  "schema_version": "1",
  "review_type": "improvement",
  "review_id": "AUTO-...",
  "improvement_id": "IMP-...",
  "expected_improvement_version": 3,
  "subject_sha256": "sha256:...",
  "decision": "pass",
  "findings": [],
  "evidence_traceability": "complete",
  "scope_readiness": "ready",
  "verification_readiness": "ready",
  "delivery_safety": "ready",
  "human_review_required": false
}
```

`decision` 允许：

- `pass`
- `changes_requested`
- `needs_human_review`
- `reject`

其中 `reject` 和 `changes_requested` 都只是 advice，不直接映射到 canonical state。

Reviewer 必须检查：

1. source refs 与 Signal 的可追溯性；
2. rationale、target 与 proposed change 是否一致；
3. Improvement kind 与 delivery 是否匹配；
4. 修改范围是否具体且有明确所有者；
5. verification suite/case/success criteria 是否可执行；
6. delivery 和 rollback 边界是否安全；
7. 是否存在重复、supersede 或意图歧义；
8. 风险是否被低估，是否仍有产品/领域判断需要人工确认。

Reviewer 不得输出 `auto_eligible`，也不得写“已批准”状态。

## 7. 确定性 Auto-review Gate

Gate fail-closed。只有以下条件全部为真时 verdict 才是 `auto_approve`：

1. projection state 是 `proposed`；
2. assessment 的 `expected_improvement_version` 等于持锁重读版本；
3. assessment、projection 和文件实际 bytes 的 `subject_sha256` 三者一致；
4. subject 对应当前 projection 的 semantic review subject；
5. `kind` 属于 `prompt_improvement | fixture_improvement | test_improvement | workflow_improvement`；
6. `kind != domain_knowledge` 且 `delivery != knowledge_delta`；
7. canonical `risk == low`；
8. canonical `confidence == high`；
9. Reviewer `decision == pass` 且 `human_review_required == false`；
10. 所有 source refs 可由 subject manifest 解析，digest 无漂移；
11. findings 中没有 blocking、high 或 critical 项；
12. verification 至少声明一个 suite，且 success criteria 非空；
13. 没有未解决的 duplicate、supersedes 或 target ownership 歧义；
14. 当前 review policy 未对同一 subject 产生过非错误 terminal assessment；已有 `review_error` 时，只有显式 retry 的新 attempt 才可重新进入 Gate。

任何字段缺失、JSON 非法、digest 不一致或未知枚举都走 `review_error`/人工队列，不能降级成 pass。Skill 自报的 risk、confidence 或 eligibility 不参与授权计算。

## 8. Ledger 事件与 Projection

新增两个 strict typed event：

### 8.1 `improvement_auto_review_approved`

```json
{
  "type": "improvement_auto_review_approved",
  "improvement_id": "IMP-...",
  "expected_improvement_version": 3,
  "review_id": "AUTO-...",
  "subject_sha256": "sha256:...",
  "assessment_sha256": "sha256:...",
  "policy_version": "1",
  "reviewer": "skill:aa-improvement-reviewer"
}
```

它是唯一自动执行 `proposed -> approved` 的事件。

### 8.2 `improvement_auto_review_recorded`

```json
{
  "type": "improvement_auto_review_recorded",
  "improvement_id": "IMP-...",
  "expected_improvement_version": 3,
  "review_id": "AUTO-...",
  "subject_sha256": "sha256:...",
  "assessment_sha256": "sha256:...",
  "policy_version": "1",
  "verdict": "needs_human_review",
  "reason_code": "verification_not_ready"
}
```

`verdict` 允许 `changes_requested | needs_human_review | reject_advice | review_error`。该事件增加版本并更新审计 projection，但 state 保持 `proposed`，因此仍在 review queue。

Projection 增加向后兼容字段：

```text
review_subject_sha256: str | null
approval_source: none | human | automatic
last_auto_review: {
  review_id,
  subject_sha256,
  assessment_sha256,
  policy_version,
  verdict
} | null
```

Review ID 和 event idempotency key 由 `improvement_id + subject_sha256 + policy_version + attempt` 稳定派生。默认 automatic attempt 为 `1`；显式 retry 才能增加 attempt。

Projection fold 规则同时钉死：human approval 设置 `approval_source=human`，Auto Approval 设置 `approval_source=automatic`；离开 `approved` 后 active `approval_source` 重置为 `none`，历史来源继续由 Ledger event 保留。`improvement_auto_review_recorded` 只更新 `last_auto_review`，不得改变 Proposal 内容、subject digest 或 state。

## 9. 人工接管与恢复边

现有人工 action 保留：

```text
approve | reject | request_rework | supersede | stop
```

人工 review context 增加最新 subject digest、Auto Review verdict、findings summary 和 assessment digest，但 advice 仍不能直接写 canonical fields。

新增受限恢复边：

```text
approved(approval_source=automatic) -> needs_rework
approved(approval_source=automatic) -> rejected
```

约束：

- 只能由 audited human decision 触发；
- 当前 approval source 必须是 `automatic`；
- Improvement 必须仍处于 `approved`，即 evaluate/export/apply 尚未开始；
- stale version 必须拒绝；
- 人工批准的 `approved` 不开放这两条边；
- delivery 已开始后沿既有 rollback/supersede 路径处理，不能把已交付事实抹回 proposed/rejected。

## 10. 失败、重试与并发

- Reviewer timeout/transport/rate-limit：按 agent retry policy 重试；耗尽后由 deterministic recovery 写 schema-valid 的 `review_error` assessment/status，并记录 `improvement_auto_review_recorded`。
- Reviewer invalid output：重试后仍非法则同样升级人工，不允许使用自然语言 fallback 判 pass。
- Subject missing/digest drift：不调用 Reviewer，记录 `review_error`，state 保持 `proposed`。
- Apply stale version：本次结果记为 stale，不写决定事件；若新 projection 仍为 `proposed` 且 subject 已改变，可由 selector 产生新 review。
- Improvement registry conflict：使用 project-sync retry；耗尽后保持原状态并写 review status，Retro status 不受影响。
- 同一 subject/policy 并发 review：event idempotency 和 expected version 保证每个 attempt 最多一个 canonical terminal result；另一方得到 idempotent success 或 stale。
- Auto Review 自身失败不在同一 cycle 内生成新的 Improvement，避免“审查失败 -> 新提案 -> 再审查”的递归。它保留结构化审计记录，供人工或后续 Trend Retro 分析。
- Ledger/outbox 尚未 reconcile 的 Candidate 不进入 Auto Review；outbox 成功 drain 后由当轮 selector 接手。

## 11. Delivery 隔离

自动批准只改变生命周期状态，不建立到以下 entrypoint 的 Graph edge：

```text
improvement-evaluate
improvement-export
improvement-apply
improvement-rollback
```

这些流程继续显式启动，并继续执行各自的 state、baseline、safety、digest 和 rollback 校验。未来若要自动交付，必须单独设计授权 policy，不能复用本设计的 `auto_eligible`。

## 12. Storage 与 Execution Contract

自动 Reviewer 的最小 contract：

```text
reads:
  project:qa/improvements/review-subjects/${params.subject_sha256}.json
writes:
  project:qa/improvements/reviews/${params.review_id}/assessment.json
  project:qa/improvements/reviews/${params.review_id}/summary.md
```

Validator/Gate 只读当前 subject、assessment 和 Improvement projection。Apply additionally：

```text
reads/writes:
  project:qa/improvements/**
synchronized:
  project:qa/improvements/**
exclusive:
  project:improvement-registry
```

Agent contract 不得读取或写入 `qa/issues/**`、`qa/retro/**`、`.aa/memory/**`、源代码、测试或 delivery target。Project-level review 输出使用 review ID 唯一路径；不同 Improvement 可并行，Ledger append 仍串行持锁。

## 13. 状态与可观测性

`aa status`/Improvement status 应展示：

- Improvement state；
- approval source；
- current subject digest；
- last auto-review verdict/policy/review ID；
- 是否仍在 human review queue；
- assessment 和 summary 路径；
- Auto Review batch 的 approved/escalated/error/stale 计数。

Retro summary 可链接 Auto Review batch summary，但不得把 review verdict 混入 Retro signal/integrity，也不得覆盖 Full、archive 或 Retro 的原 verdict。

## 14. 验收测试

1. 低风险、高置信、非 knowledge、证据完整且 reviewer pass 的 process Improvement 自动变为 `approved`。
2. `domain_knowledge`/`knowledge_delta` 即使 reviewer pass 也保持 `proposed` 并进入人工队列。
3. medium/high risk、非 high confidence、空 verification suite、blocking finding 各自不能自动批准。
4. Reviewer `reject` 只写 `reject_advice`，不得写 `improvement_review_rejected`。
5. Reviewer `changes_requested` 只升级人工，不得自动进入 `needs_rework`。
6. Reviewer timeout、invalid JSON、未知枚举和 digest drift 均保持 `proposed`。
7. 相同 subject/policy 重放不产生第二个 review event；新 evidence 产生新 subject 后可重新审查。
8. Auto Review event 增加 Ledger version，但不会因 version 自增无限复审同一 subject。
9. 审查期间 evidence-link 导致 version/subject 漂移时，旧 apply stale fail-closed。
10. 两个并发 Auto Review 只有一个 canonical 结果。
11. 人工可在 delivery 前将 automatic-approved 转为 `needs_rework` 或 `rejected`。
12. 人工批准的 Improvement 不能使用自动批准恢复边。
13. automatic-approved Improvement 不会自动产生 eval/export/apply event。
14. Retro 在 Reviewer 失败前已经 final；Reviewer 失败不改变 `retro-status.result`。
15. Retro dry-run、零 Candidate 和 pending outbox 不启动 Reviewer。
16. outbox drain 后新 proposal 在同一 orchestration 中进入 Auto Review。
17. Selector 只审本轮 receipt 指向的 Improvement，不扫描旧 review queue。
18. 老 proposal 没有 `review_subject_sha256` 时不读取历史 Retro，保留给人工。
19. Skill write-set 外写入 fail-closed，且不能污染 Ledger/Issue/Retro artifacts。
20. Human review context 能展示最新 assessment，但 apply 仍以持锁重读 projection 为准。

## 15. 非目标

- 不自动交付、评估、应用或回滚 Improvement。
- 不自动 reject、request rework 或 supersede。
- 不新增 Improvement Fixer 或原地修改 proposal。
- 不修改 Improvement fingerprint 规则。
- 不迁移或读取历史 `qa/retro/**` 来补建 subject。
- 不让 Reviewer 读取源代码后自行扩大改动范围。
- 不把 Auto Review verdict 变成 Full、archive、Retro 或 Benchmark pass/fail gate。
- 不自动审查 `awaiting_baseline`、`eval_error` 等 delivery recovery 状态；它们继续使用既有 lifecycle recovery 流程。
