# Trace 分层汇总及 Recovery 补齐设计

- Status: Design approved in conversation; awaiting written-spec review
- Date: 2026-07-31
- Baseline: `codex/capability-traceability-integration` at `6fabcec`
- Scope: Trace 四层事实汇总、reporting 层 sufficiency join、当前批次 reconciliation authority、`inspect-with-issues` / `issue-analyze` / `issue-reconcile` 的 Trace materialization、benchmark specialty report v3
- Depends on:
  - `docs/superpowers/specs/2026-07-30-layer-assurance-profile-design.md`
  - `docs/superpowers/specs/2026-07-30-e2e-mechanical-checks-policy-replay-design.md`
  - `docs/superpowers/specs/2026-07-31-fuzz-performance-assurance-wiring-design.md`
- Trace baseline: `feature/traceability-evidence-projection` at `e07786b`（原设计文档位于该分支的
  `docs/superpowers/specs/2026-07-29-traceability-evidence-projection-design.md`）

## 1. 问题

现有 Trace 已经能把 case、当前测试树、execution result、failure analysis 和 issue/problem
事实 fold 成 `TraceProjection`，也已经区分 `execution` 与 `reconciled` 两个 phase。但它还缺少
两个关键闭环。

第一，消费者只能看到 projection 级总数。`TraceRow.case_type` 已经覆盖 API、E2E、Fuzz、
Performance，benchmark 却仍只报告全局 rows、gaps、failure links、problem links 和 sufficiency
总数。因此无法回答“哪一层没执行、哪一层证据不足、哪一层只有全局 gap”，也无法与四层
capability/policy replay 矩阵逐层对齐。

第二，reconciled projection 的发布和 authority 校验不完整。正常路径会经过
`materialize-trace-projection`，但 analyzer failure 与 project sync pending 两条 recovery 直接跳到
`inspect-complete`。独立的 repeatable `issue-analyze`、`issue-reconcile` entrypoint 在成功或再次
失败后也直接结束。结果是当前 batch 可能没有新的权威 projection，而上一个 batch 的
`inspect/trace-projection.json` 继续留在磁盘上。

代码审计还验证了更深一层的问题：`fold_trace(..., phase="reconciled")` 目前只做文档模型校验，
不校验 failure analysis 与 issue snapshot 的 `change_id` / `batch_id` authority。实测把当前
manifest 保持在 batch B1，同时放入 batch B0 的 issue snapshot，projection 仍会输出 B0 的
`open_problem_ids`，且没有 identity gap；跨 change/旧 batch 的 failure analysis 也会被挂到当前
row。只改 graph continuation 无法解决这个问题。

本设计要建立的闭环是：

```text
current execution facts
  -> fact-only TraceProjection
  -> deterministic four-layer fact summary
  -> reporting-only sufficiency join

current issue state (success or typed recovery)
  -> current-batch authority validation
  -> valid complete/incomplete reconciled TraceProjection
  -> materialize before graph completion
```

## 2. 目标

1. 从 `TraceProjection.rows` 纯计算固定四层事实汇总，不重新扫描 case、result 或 tests 文件。
2. API、E2E、Fuzz、Performance 始终按稳定顺序出现，即使某层为零。
3. reporting 层按 `case_id` 严格关联 `SufficiencyReport`，给出逐层 sufficient / insufficient
   计数，同时保持 `TraceProjection` 无 policy、无时钟。
4. reconciled fold 只接受绑定当前 change、当前 authoritative batch 的 failure/issue authority。
5. 当前 issue reconciliation 不可用时仍发布绑定当前 batch 的合法 incomplete projection，保留
   可证明的 execution/failure 事实，但绝不沿用旧 authority 的 problem links。
6. 主 inspection graph 与两个独立 issue recovery entrypoint 的所有成功和 typed recovery 路径，
   在结束前都经过同一个 deterministic materializer。
7. benchmark 使用 typed、可校验的四层 trace payload；旧报告可读，但不得把缺失的分层证据
   补造成 complete。
8. 新 graph definition 只影响新 invocation；已有 invocation 不注入新节点。若其 graph/contract digest
   与当前 binary 漂移，当前 runtime 按既有能力明确拒绝 resume，而不是假装执行历史 handler 语义。

## 3. 非目标

- 不把 summary、sufficiency、policy action、wall clock 或 benchmark verdict 写回
  `TraceProjection`。
- 不新增 Trace ledger、Issue event、Graph event 或其他权威存储；projection 与 summary 仍是可重建
  的 derived data。
- 不改变 evidence sufficiency policy、quality-gate verdict、capability checks 或 policy replay
  语义。
- 不改变 LLM analyzer/reviewer 的职责，也不让 LLM 参与 summary、authority 校验或 recovery
  判定。
- 不重写旧 pinned graph，也不在旧 invocation resume 时注入新节点。
- 不在本设计内重构 Graph Engine 的通用 publication protocol、checkpoint authority、object-store
  fsync 或任意 SIGKILL window。审计发现的 prepared-publication/orphan-marker 通用缺口应单独作为
  Graph Runtime P0 立项；本设计只验证既有 engine 已承诺支持的 task/superstep resume 边界。
- 不新增 waived/豁免语义，也不把“不适用”从缺失输入推导出来。

## 4. 设计决策

### D1. 分层汇总是纯 view，不是 TraceProjection 新权威

新增 `summarize_projection_by_layer(projection)`。它只读取已经构造好的 `TraceProjection`，不读
文件、不读 policy、不读时钟，也不回写 projection。`TraceRow.case_type` 是 row→layer 的唯一
真源；layer 名称和顺序复用 `artifacts.models.assurance` 中的 `LAYER_NAMES` / `CASE_TYPES`。

`evidence/` 可以依赖 artifacts 的共享词汇，但不得反向 import `verification` profile 或 benchmark
实现。

### D2. phase 与 layer 是两个正交维度

本设计不新增 `recovery` phase。仍只有：

- `execution`：case/test/run 的执行期事实；
- `reconciled`：在 execution 事实之上增加当前有效的 failure/problem enrichment。

每个 phase 分别产生四行 layer summary。Recovery 是 reconciled authority 的状态来源，通过 typed
gap 和 `integrity=incomplete` 表达，不与 API/E2E/Fuzz/Performance 并列，也不伪装成第三个 phase。

新增纯 phase-pair validator：execution/reconciled 必须具有相同 change、authoritative batch、case
集合，并且每个同 case row 的 execution 事实字段完全相等；reconciled row 只允许增加
`failures` / `open_problem_ids`。Phase-level 关系同样是单调 enrichment，而不是任意不同：

- `unmapped_tests` 精确相等；
- execution 的每个 `TraceSource(path, exists, sha256)` 必须在 reconciled 中原样存在，reconciled 只可
  增加 §7 声明的 issue/failure source，source path 全局唯一；
- execution gaps 作为完整 tuple/multiset 必须包含在 reconciled gaps 中，reconciled 只可增加
  reconciled-authority/problem gap，不可删除或改写 execution gap；
- integrity 按 `complete < degraded < incomplete` 只能保持或变差，并且必须由 reconciled 的完整
  rows/gaps 重新派生，不能手工声明。

Persisted 两相位之间出现其他漂移时，reporting 必须拒绝，不能分别汇总后掩盖矛盾。

### D3. manifest batch 是 reconciliation authority 的根

`TraceProjection.authoritative_batch_id` 由当前 execution manifest/current fold input 决定。任何
reconciled source 只有在身份与该 projection 精确一致时，才有资格 enrich rows。这里的 manifest
authority 不是相信 `issue_evidence_manifest.digest` 的自声明值，而是从当前文件系统事实重新建立：

1. manifest entry path 必须唯一、规范化且位于 collector 的 change-relative allowlist；拒绝绝对路径、
   `..`、symlink escape 和重复 path；
2. entries 必须恰有一个 `execution/execution-manifest.yaml` anchor；
3. 每个 entry 的声明 digest 必须等于安全打开后的当前内容按
   `evidence_entry_digest/v1` 重算的值：UTF-8 内容先使用 collector 现有、固定顺序的 secret-redaction
   规则替换后 hash，无法 UTF-8 解码的 binary 才 hash raw bytes；anchor 也使用同一 entry digest
   语义校验当前 execution manifest 内容；
4. 对按 path 排序的 canonical entries 使用 collector 的同一算法重算 bundle digest，重算值必须等于
   manifest 的 `digest`；
5. 只有这个重算且已锚定 execution bytes 的值才记为 `M`，再参与下游 candidate/snapshot/status 链。

collector 的 entry-digest、redaction、bundle-digest、path normalization 和 safe-read helper 下沉到
foundational evidence 模块，collector、reconciler 和 Trace 共用；禁止三处各自实现。当前 manifest
schema `"1.0"` 固定映射到 `evidence_entry_digest/v1`；未来改变 pattern、替换顺序、UTF-8/binary 判定
或 canonicalization 都必须升级 manifest schema/semantics，不能原地改变 v1 digest。

Manifest entry digest 与 `TraceSource.sha256` 是两个不同 primitive：前者为了不把 secret 进入
authority hash 而使用上述 redacted/binary 语义；后者继续记录 raw bytes SHA-256，对任意磁盘 byte
变化敏感。manifest-referenced 文件在 `TraceSource` 中按 path 去重记录 raw digest，使 point-in-time
projection 覆盖这条 authority root；实现和字段命名不得混用两种 digest。

需要校验：

- `FailureAnalysis.change_id == projection.change_id`；
- `FailureAnalysis.batch_id == authoritative_batch_id`；
- `FailureAnalysis.source_batch_id == authoritative_batch_id`；
- `IssueEvidenceManifest.change_id/batch_id` 与当前 authority 一致；
- `IssueCandidateDocument.change_id/batch_id` 与当前 authority 一致；
- `IssueReconcileStatus.change_id/batch_id` 与当前 authority 一致；
- completed/analysis-failed authority 下，`ChangeIssueSnapshot.change_id/authoritative_batch_id` 与
  当前 authority 一致；
- candidate、snapshot 内 canonical analysis status、reconcile status 的
  `evidence_bundle_digest` 都绑定 evidence manifest；
- snapshot analysis status 与 reconcile status 的 `candidate_digest` 都等于当前 candidate
  document 的 canonical digest；
- completed authority 下，snapshot analysis status=completed、reconcile status=completed、project
  sync status=completed。

Completed 还必须证明 reconciliation 的集合完整性，而不只是顶层状态一致。令 `N` 为当前
candidate 数量：

- `snapshot.analysis_status.candidate_count == N`；
- v2 completed reconcile status 的 `occurrence_count` 必填且等于 `N`；failed/pending 下必须为空；
- 使用 reconciler 下沉的 per-candidate digest / occurrence ID / problem ID helper，从每个 candidate
  重算当前批 expected occurrence；snapshot 中 `change_id/batch_id` 为当前值的 occurrence ID 集必须与
  expected set 精确相等、无重复；
- 每个 expected occurrence 的 `change_id`、`batch_id`、`observation_ids`、`problem_id`、
  `analysis.evidence_bundle_digest` 和 per-candidate `analysis.candidate_digest` 必须与 candidate/M
  精确一致；
- candidate 引用的每个 observation 必须在 snapshot 中存在且其 observation change/batch 身份为当前
  authority；
- 每个 expected occurrence 的 source `problem_id` 必须存在于 current project problem projection，且
  其 `occurrences` 恰好一次包含该 occurrence ID；若 source problem 已 merge，既有 canonical alias
  chain 仍必须合法。缺失、悬空、跨 change/batch 或 change/project projection 不一致的 nested fact
  全部使 problem authority 不成立。

上述 `snapshot` / `project problem projection` 不能只是“模型能 parse 的缓存文件”。为了继续支持
累计历史 problem links，本设计把现有 ledger 确立为历史 authority：

- strict-read `change:issues/events.jsonl`，重放得到 canonical `ChangeIssueSnapshot`；
- strict-read `project:qa/issues/events.jsonl`，重放得到 canonical `ProblemProjection`；
- 将 replayed model 的 canonical JSON 与磁盘 `issues/snapshot.json` / `qa/issues/problems.json` 比较，
  不相等即 projection replay mismatch；后续只使用 replayed object 建 links；
- strict-read 当前 `inspect/observations.json`，要求其 change/batch 与 current authority 一致，且其
  observation ID→canonical payload map 与 change ledger replay 中当前批 observation 集精确相等；
- change ledger 的 observation/occurrence ID 全局唯一。每个 event envelope 的 change/batch 必须与
  nested observation/analysis/occurrence 一致；analysis/occurrence 另校验 nested evidence digest，
  pending event 校验 candidate/evidence identity。使用共享 helper 重算 observation ID、occurrence ID
  及可重算的 event/idempotency identity；
- 当前 analysis-failed / project-sync-pending recovery 还必须分别存在 identity/M/C 一致的
  `IssueAnalysisFailedEvent` / `ProjectSyncPendingEvent`，不能只凭旁路 status 文件宣称 recovery；
- project ledger 必须通过 seq、idempotency、expected-version 和纯 projection replay。对当前 change
  的 expected occurrences，再与 project event/projection 的 problem membership 交叉验证。

当前 candidate 的 exact-set 校验不能替代历史跨账本一致性。定义 `linkable_occurrences` 为 replayed
current-change snapshot 中所有可能参与 case→open-problem link 的 occurrence（包括当前批和历史批）。
对这个集合中的**每一个** occurrence，authority validator 还必须证明：

- occurrence 的每个 `observation_id` 在 replayed change observations 中恰好解析一次；observation 与
  occurrence 的 `change_id`、`batch_id` 分别相等，禁止 dangling、cross-change 和 cross-batch 引用；
- occurrence 的 source `problem_id` 必须对应 replayed project projection 中真实存在的 source Problem，
  且 occurrence ID 在该 source Problem 的 membership 中恰好出现一次、不得出现在其他 source
  Problem；再从该 source 沿唯一、无环的 canonical alias chain 解析到 terminal problem。既有 merge
  projector 保留 source history、不会把 occurrences 搬到 target，因此 terminal 只决定 canonical
  problem ID 与 open/closed 状态，不能被误当成 membership owner；
- 对 project projection 中属于该 current change、并可能产生 link 的 occurrence membership 做反向
  校验：先按 membership owner/source Problem 匹配 replayed change snapshot 中同 ID、同
  `occurrence.problem_id` 的 occurrence，再解析相同 terminal alias；不允许 project-only、target-only、
  wrong-source 或 duplicate membership。其他 change 的 membership 不由本 change ledger 证明，也不得
  进入本 change 的 links；
- 只有以上 cross-ledger join 全部成立后，才按 terminal problem 的 open/closed 状态生成历史 link。

因此两份 ledger “各自可重放”只是必要条件，不是充分条件。当前批 expected set 通过后，任何历史批
的 dangling observation、cross-batch observation 或 change/project problem membership 分歧仍会使整组
problem authority unavailable；不得只跳过坏的历史 occurrence 后继续声称 complete。

Project store 的 genesis 是显式例外：仅当 `qa/issues/events.jsonl` 与 `problems.json` **同时缺失**，且
replayed current-change snapshot 的累计 occurrence 数为零时，把两者解释为 canonical empty project
state，不产生 missing gap。只缺一个文件、任一文件非空损坏、或 change history 已有 occurrence 时，
不得使用 empty fallback。该规则与 reconciler 现有“first ever reconcile missing means empty”一致。

历史 observation/occurrence 只有通过上述 ledger replay 才可参与 case→problem link；任何 raw snapshot
里额外的 cross-change、伪历史或 duplicate nested item 都因 canonical replay mismatch 被拒绝。若不做
ledger replay，唯一安全降级是完全不使用历史 links；本设计不允许继续遍历未经证明的累计 snapshot。

为保持层级，event envelope schema 固定下沉到 `artifacts/models/issue_events.py`，strict JSONL reader
与纯 projector 固定下沉到 `evidence/issue_replay.py`，identity helper 固定下沉到
`evidence/issue_identity.py`。workflow store 反向复用并保留兼容 re-export；`evidence.trace` 不得向上
import `workflow.issues`。这只是移动纯 domain/replay 能力，不新增 ledger 或改变事件语义。

`inspect/issue-analysis-status.json` 是 analyzer/recovery 的过程产物；Issue lifecycle 明确规定 analyzer
output never canonical，deterministic reconciler 也不读取它。成功分支的 canonical analysis status
来自 change issue snapshot 中的 `issue_analysis_completed` 投影，analysis-failed 分支同样来自
deterministic recovery event 的 snapshot 投影。Trace 不得把 agent-authored status 提升为 completed
authority。

只有完整证明当前 batch 已完成 analysis + reconcile + project sync，且两份 projection 与各自 strict
ledger replay 相等后，replayed snapshot 才能作为 problem association 的权威。它可使用已验证的历史
observations/occurrences 计算当前仍开放的问题。若当前 authority 证明失败，则整组 problem links
不可用，禁止退回上一个 batch 或直接使用未验证 snapshot。

### D4. Recovery 产出 incomplete 证据，不以缺文件表示

Issue analysis/reconciliation 的业务失败不是 materializer task failure。它们已被持久化为 status
或 snapshot 状态，fold 必须把这些事实投影为 blocking gap，并写出合法 reconciled projection：

- `phase = reconciled`；
- `authoritative_batch_id = 当前 execution batch`；
- `integrity = incomplete`；
- 有效的 execution rows 与当前 failure analysis 继续保留；
- `open_problem_ids = ()`，因为当前 problem authority 未成立；
- `aa verify` 必须 fail，并明确列出 blocking gap。

只有 fold 抛出未建模错误、projection 模型不合法、workspace freeze/commit 失败或原子写失败，才是
materializer task failure；这种失败不得进入 completion node。

### D5. 三个 graph 共用“状态收敛 → materialize → 完成”末端

新 definition 的拓扑不变量是：任何能够把 issue state 视为 settled 的路径，都必须先执行
`operation:materialize-trace-projection`。

```text
inspect-with-issues
  reconcile success --------------------------+
  analyzer recovery -> record-analysis-failure +--> materialize -> inspect-complete -> END
  sync recovery ----> record-project-sync-pending +

issue-analyze
  analyze -> reconcile success ----------------+
  analyzer recovery -> record-analysis-failure +--> materialize -> END
  sync recovery ----> record-project-sync-pending +

issue-reconcile
  reconcile success ---------------------------+
  sync recovery -> record-project-sync-pending +--> materialize -> END
```

这只是 graph schema 接线，不要求修改 Graph Engine 的 recovery 语义。Recovery continuation 指向
普通 materializer node；dedicated recovery node 仍不得拥有 ordinary incoming/outgoing edge。

### D6. Benchmark trace payload 升级为 typed v3

现有 `SpecialtyReportV2.traceability_evidence` 是 `dict[str, Any]`，无法表达或校验四层守恒、严格
case join 和 legacy 状态。把必填 typed 四层结构继续塞进 v2 会改变 v2 的语义，因此新 collector
写 `SpecialtyReportV3`：

- v3 使用 typed `TraceabilityEvidenceV3`；
- loader 保留 v1、v2、v3 reader；
- v1/v2 保持原始全局 trace 展示，并明确标记 `legacy_unlayered`；
- renderer 不为旧报告生成四个零值 complete row；
- 新 collector 只写 v3，不再生成新的 v2；
- capability/policy replay payload 的既有语义不因外层版本升级而改变。

Specialty v3 与 TraceProjection v2 是两个独立版本边界：前者类型化 reporting payload，后者承载
新增 recovery/identity gap。两者都不改变 capability/policy replay 的既有语义。

### D7. 旧 pinned graph 不回填新事实，也不承诺历史 handler resume

当前 GraphRuntime 会用当前编译结果解析 invocation，并在 graph/contract digest 漂移时抛
`GraphDefinitionChanged`；磁盘 pinned schema/contract 供 replay/reporting 验证，不是生产 resume 的
历史 handler 实现。由于本设计会修改原 operation target 的 contract 和 writer 行为，不能声称旧
invocation 在新 binary 上继续执行历史语义。

因此边界明确为：

- 已完成的旧 invocation 仍可由 pinned snapshots 做 reporting/legacy 分类；不改写其 ledger/artifact；
- 尚未完成的旧 invocation 若 definition digest 漂移，resume 在既有 recovery barrier 后必须 fail
  closed：不得做新 planning、不得注入新 materializer、不得运行扩大 write-set 后的 handler、不得
  产生 V2/new-schema 输出；
- recovery barrier 仍可先完成已被旧 strict ledger 证明成功的 pending commit、synchronized
  publication apply/ack 或 ordinary materialization。由此恢复出的旧 Trace artifact 只能按 V1/legacy
  读取，authoritative loader 不得把它提升为 current；
- 若旧 run 没有 materialize 当前 projection，consumer 只能报告 missing/legacy/incomplete，不得根据
  新 schema 补造四层 complete 证据；
- 新 invocation 和新启动的 repeatable issue entrypoint 使用新 definition，并满足本设计拓扑。

让旧 invocation 真正 resume 需要另立 runtime schema + contract + handler implementation pinning
设计；不暗含在本 spec 中。

### D8. 磁盘 projection 是 point-in-time artifact

`inspect/trace-projection.json` 只证明 materialize 时刻的 source view。后续 `issue-review` 或其他合法
操作可以修改 project problems，而不启动 inspection graph；此时磁盘文件仍可通过模型校验，却不再
代表“当前”source state。

新增共享 authoritative loader
`load_current_reconciled_projection(project_root, change_id)`：读取持久化 projection，同时执行当前
reconciled live fold，并比较 change/batch 与 canonical projection digest。完全相等才返回；否则抛出
typed missing/stale error。所有声称消费“当前持久化 projection”的新代码必须经过该 loader。

`aa verify` 已直接 live-fold，不需要先读持久化文件。Benchmark 可以通过共享 loader 完成同一校验，
而不是仅依赖顶层 batch；registry 的 shape validation 不能替代 freshness validation。Issue review 后
主动 rematerialize 不在本轮范围，stale consumer 必须 fail closed 或显式重新 fold。

### D9. TraceProjection、reconcile status 与 quality evidence 显式版本化

`inspect/trace-projection.json` 当前在 registry 中是 `must_compat`；该等级禁止修改枚举取值。因此
不能在 schema v1 的 `TraceGapCode` 中直接加入 recovery code。每个 versioned artifact path 只有一个
registry-facing `RootModel`，内部按 `schema_version` 做 discriminated union；不能把 Python union type
直接塞给只接受 `type[BaseModel]` 的 `ArtifactSpec.model`。

版本边界固定为：

| Artifact | v1 literal | v2 literal | registry-facing model |
|---|---|---|---|
| `inspect/trace-projection.json`（execution batch 同模型） | `"1"` | `"2"` | `TraceProjectionDocument` |
| `inspect/issue-reconcile-status.json` | `"1.0"` | `"2.0"` | `IssueReconcileStatusDocument` |
| quality-gate result | `"1.0"` | `"2.0"` | `QualityGateResultDocument` |

三个 document wrapper 分别包
`Annotated[V1 | V2, Field(discriminator="schema_version")]`。Registry 注册 wrapper；共享 loader unwrap
成 concrete variant。新 writer 只构造 V2。旧 V1 原样保留，不用默认值“normalize 成 V2”；consumer
必须显式分支，只有字段语义确实共享的 pure summary 才接受 `V1 | V2` protocol。

现有 `TraceProjectionV1.schema_version` 有默认值，历史上缺少该 key 的 payload 仍可读取。为保持这条
真实兼容边界，`TraceProjectionDocument` 的 only-before normalization 对“顶层是 mapping 且 key
完全缺失”的输入注入 `schema_version="1"` 后再走 discriminator；显式 null、空字符串、未知版本或
非 mapping 一律拒绝。IssueReconcileStatus/QualityGate 的 V1 原本要求显式版本，不做同类注入。

Trace 具体改为：

- 保留现有 `TraceProjectionV1` reader；
- 新 writer 生成 `TraceProjectionV2`，字段仍 fact-only，但使用包含本设计新 code 的 v2 gap enum；
- 将现有 literal 明名为 `TraceGapCodeV1`；`TraceGapV1.code` 只接受它。新增
  `TraceGapCodeV2 = TraceGapCodeV1 | Literal[本设计新增 codes]` 与 `TraceGapV2`；不得原地 widen V1；
- layer summary 使用独立 `TraceSummaryGapCode = TraceGapCodeV1 | TraceGapCodeV2`（实际等于 V2
  superset），不把 summary 的消费类型反向写进任一 projection version；
- registry compat 从 `must_compat` 显式迁移为 `versioned`；
- shared loader、summary、verify 与 benchmark 接受 concrete v1/v2 variant 并显式分支；只共享真正
  相同的 fact 字段，绝不以默认值把 v1 “升级”为 completed recovery；
- v2 validator 强制 case ID 唯一、TraceSource path 唯一、canonical gap tuple 唯一、row
  automation/coverage 一致、execution phase enrichment 为空、execution target 与 case type 一致、
  gap target 为空或属于四层闭集；
- `fold_trace` 在返回任何新 projection 前调用 shared layer-summary validation。Runner、materializer、
  live verify 因而共用同一发布前边界；summary 不可构造时不得写 projection，verify 返回 typed
  failure；
- execution 与 reconciled 新 projection 都写 v2，旧文件不重写。

现有 `IssueReconcileStatus` registry 已是 `versioned`。新增 v2 status：

- schema version 升级；新 writer 全部写 v2；
- status enum 为 `completed | failed | pending`；
- `candidate_digest` 在三种状态都必填；
- `occurrence_count` 仅 completed 必填且 `>= 0`，failed/pending 必须为 null；
- `record-project-sync-pending` 除 ledger/snapshot 外，原子写 batch-bound、digest-bound 的
  `inspect/issue-reconcile-status.json`，`status=pending`；
- `record-project-sync-pending` 必须读取并严格解析 candidate document 后计算 canonical digest；
  candidates missing/malformed 时 operation fail，删除现有“以 evidence digest 代替 candidate digest”
  fallback；
- v1 completed/failed status 继续可读，但只作为 legacy fact；即使碰巧带有可选 digest/count，也不能
  建立本设计的 current completed/failed/pending authority。

这解决现有 snapshot 的限制：`ProjectSyncPendingEvent` 只设置 `project_sync_status=pending`，不会把
`authoritative_batch_id` 推进到 pending batch。Trace 以 current v2 pending reconcile status 证明 B1
recovery，snapshot 在该分支只提供累计状态且不贡献 problem links；不得假设其 authority 已是 B1。

Sufficiency 的版本化 wire DTO 固定属于 `artifacts/models/sufficiency.py`，不能让
`artifacts.models.inspect` 反向 import `evidence`。固定新增：

- `SufficiencyRowVerdictV2` 与 `ExecutionState` artifacts-owned DTO；
- `SufficiencyReportV2.schema_version = "2.0"`；
- `source_projection_digest`；
- `source_policy_digest`；
- `semantics = "evidence_sufficiency/v2"`；
- `require_current_batch: bool`，quality-gate success payload 必须为 `true`；
- 原有 `as_of`、`recency_hours`、typed verdicts。

Row wire contract 同样是 closed，而不是仅把旧对象塞入新外壳：

```python
SufficiencyReasonCode = Literal[
    "not_in_current_batch",
    "uncovered",
    "never_run",
    "execution_stale",
    "fuzz_run_missing",
    "perf_run_missing",
    "no_pass",
    "pass_stale",
]


class SufficiencyRowVerdictV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    case_id: NonEmptyStr
    sufficient: StrictBool
    missing_kinds: tuple[EvidenceKind, ...]
    reason_codes: tuple[SufficiencyReasonCode, ...]
    execution_state: ExecutionState


class SufficiencyReportV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["2.0"]
    source_projection_digest: NonEmptyStr
    source_policy_digest: NonEmptyStr
    semantics: Literal["evidence_sufficiency/v2"]
    require_current_batch: StrictBool
    as_of: AwareDatetime
    recency_hours: Annotated[int, Field(strict=True, gt=0)]
    verdicts: tuple[SufficiencyRowVerdictV2, ...]
```

DTO validators 强制 case ID 在 report 内唯一、每行 `missing_kinds` 唯一、missing/reason 长度相等，并满足：

- `sufficient=true` 当且仅当两 tuple 都为空；false 时两者非空；
- zipped pair 只能是
  `covered→uncovered`、
  `execution_recent→{not_in_current_batch,never_run,execution_stale}`、
  `fuzz_run→{not_in_current_batch,fuzz_run_missing}`、
  `perf_run→{not_in_current_batch,perf_run_missing}`、
  `pass_status→{not_in_current_batch,no_pass,pass_stale}`；
- `execution_recent→never_run` 要求 `execution_state=never_run`，
  `execution_recent→execution_stale` 要求 `execution_state=stale`。

Policy 的 `required_kinds[case_type]` 同步增加无重复 validator，使 evaluator 不会合法地产生重复
missing kind；这不改变任何现有合法 policy 的 verdict。

`evidence/sufficiency.py` 负责计算并返回这个 artifacts-owned DTO。V2 quality 的嵌套 wire contract
固定为：

```python
EvidenceCoverageErrorCodeV2 = Literal[
    "evidence_projection_missing",
    "policy_error",
]


class EvidenceCoverageSuccessV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["sufficiency"]
    report: SufficiencyReportV2


class EvidenceCoverageErrorV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["error"]
    error_code: EvidenceCoverageErrorCodeV2


EvidenceCoveragePayloadV2 = Annotated[
    EvidenceCoverageSuccessV2 | EvidenceCoverageErrorV2,
    Field(discriminator="kind"),
]


class CoverageDimensionV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # 与 legacy CoverageDimension 同名字段，但 evidence 必填且为 closed payload
    status: GateStatus
    available: bool
    line_coverage: float
    branch_coverage: float
    threshold: CoverageThreshold
    scope: Any = None
    evidence: EvidenceCoveragePayloadV2


class QualityGateDimensionsV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    functional: FunctionalDimension
    coverage: CoverageDimensionV2
    non_functional: NonFunctionalDimension | None = None


class QualityGateResultV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["2.0"]
    change_id: str
    batch_id: str
    dimensions: QualityGateDimensionsV2
    final_status: GateStatus
    warnings: list[str] | None = None
    diagnostics: dict | None = None  # legacy non-authoritative diagnostics only
```

`QualityGateResultV2` 只使用 `QualityGateDimensionsV2`。现有 common.py 的 legacy
`CoverageDimension`、`QualityGateResultV1` 和 `QualityReport` 保持不变，禁止为复用而原地收紧公共
字段。这样 registry 可以完整校验 V2，同时保持 import-linter 的 `evidence -> artifacts` 单向依赖。
V1 quality evidence 保持原 dict 形状，仅用于 legacy reader；新 writer 的 success/error 两条路径都
必须构造上述 typed variant，不得退回任意 dict。

新 quality writer 用实际参与计算的 canonical policy 写 `source_policy_digest`。Specialty v3 complete
validator 还必须要求该 digest 与 capability replay definition binding 的
`baseline_policy_digest` 相等；verify diagnostics 的 `policy_digest` 也必须等于同一值。若运行期间
读取了不同于 invocation pin 的 policy，报告只能 typed incomplete，不能把 provenance 不同的 verdict
并入 complete matrix。

所有真实 consumer 必须经过共享 loader，不得直接把 registry wrapper 当 concrete model：

- `load_trace_projection_document(raw) -> TraceProjectionV1 | TraceProjectionV2`；
- `load_issue_reconcile_status_document(raw) -> IssueReconcileStatusV1 | IssueReconcileStatusV2`；
- `load_quality_gate_result_document(raw) -> QualityGateResultV1 | QualityGateResultV2`。

| Path/model | Consumer | Loader result / behavior |
|---|---|---|
| TraceProjectionDocument | registry、batch trace reader、materializer current loader、benchmark | unwrap V1/V2；new authority writer V2 |
| IssueReconcileStatusDocument | trace fold、report builder、issue recovery/status reader | unwrap V1/V2；只有 V2 可建 current authority |
| QualityGateResultDocument | execution evidence loader、inspector/report builder、benchmark | unwrap V1/V2；只有 V2 typed success 可建 complete specialty join |

`report_builder.py`、`workflow/execution/evidence.py`、inspector 与 benchmark 的现有
`Model.model_validate*` 直读点全部迁移到 loader。Wrapper 只负责 wire dispatch，业务代码只接收 concrete
variant；不得依赖 `.root` 透传属性或把旧类名偷偷改指 V2。

## 5. 四层事实模型

在 `assurance_agent/artifacts/models/trace.py` 增加只读模型，在
`assurance_agent/evidence/layer_summary.py` 增加纯计算函数。
本节模型全部使用 `ConfigDict(frozen=True, extra="forbid")`。

```python
class TraceGapAggregate(BaseModel):
    total: int
    by_code: dict[TraceSummaryGapCode, int]

    @model_validator(mode="after")
    def _total_matches_breakdown(self) -> Self:
        if self.total != sum(self.by_code.values()):
            raise ValueError("gap total must equal by_code sum")
        return self


class TraceLayerFacts(BaseModel):
    layer: LayerName
    case_type: CaseType

    total: int
    automated: int
    covered: int
    uncovered: int
    not_required: int

    current_executed: int
    current_not_present: int
    target_not_selected: int

    latest_passed: int
    latest_failed: int
    latest_skipped: int
    never_run: int

    failure_rows: int
    failure_links: int
    open_problem_rows: int
    open_problem_links: int
    unique_open_problems: int

    gaps: TraceGapAggregate


class TraceLayerFactSummary(BaseModel):
    schema_version: Literal["1"]
    change_id: str
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: str
    source_projection_digest: str
    projection_integrity: TraceIntegrity
    layers: tuple[TraceLayerFacts, ...]  # exact LAYER_NAMES order
    global_gaps: TraceGapAggregate
```

上述字段名、层顺序和计数语义构成 summary wire contract；实现不得无偏差记录地改名或省略：

- 所有 count 都是 `int`（拒绝 bool）且 `>= 0`；
- `automated` 等于 `automation_required=true` 的 rows；
- `covered` / `uncovered` 只对应 automated rows；非自动化 row 进入 `not_required`；
- current 三态直接来自 `presence_in_current_batch`；
- latest 四态中，无 `latest_execution` 的 row 进入 `never_run`；
- failure/problem row count 是至少有一个 link 的 row 数，link count 是链接总数；
- unique problem count 在 layer 内按 canonical problem ID 去重；
- execution phase 的 failure/problem counts 必须为零；
- `gap.target` 为已知 layer 时只进入该 layer；`target is None` 只进入 global bucket；未知非空
  target 是 summary error，不能静默降级为 global；
- `by_code` 只保留非零项并按 code lexical order 构造，canonical bytes 不依赖输入 gap 顺序。
- `TraceGapAggregate.total == sum(by_code.values())`；`by_code` value 必须为 strict positive int，空 map
  只能与 `total=0` 同时出现。该不变量由模型自身校验，不能只依赖 summary builder 恰好算对。

### 5.1 算术不变量

对每个 layer：

```text
automated = covered + uncovered
total = covered + uncovered + not_required
total = current_executed + current_not_present + target_not_selected
total = latest_passed + latest_failed + latest_skipped + never_run
```

对整个 summary：

```text
sum(layer.total) = len(projection.rows)
sum(layer.gaps.total) + global_gaps.total = len(projection.gaps)
for every aggregate: gaps.total = sum(gaps.by_code.values())
layers.layer = LAYER_NAMES（精确顺序、无缺失、无重复）
layers.case_type = CASE_TYPES（精确顺序、无缺失、无重复）
```

输入 projection 出现重复 `case_id`、未知 gap target 或任何分区无法守恒时，summary 构造失败；
不得输出貌似 complete 的部分统计。

## 6. Sufficiency 的 reporting-only join

新增以下纯函数：

```python
join_layer_sufficiency(
    projection: TraceProjection,
    facts: TraceLayerFactSummary,
    report: SufficiencyReportV2,
    *,
    expected_policy_digest: str,
) -> TraceLayerEvidenceSummary
```

它不重新评估 policy，只聚合已经给出的 `SufficiencyReport`。join 前必须验证：

1. projection row 的 `case_id` 唯一；
2. sufficiency verdict 的 `case_id` 唯一；
3. 两边 case-id set 精确相等；
4. `report.source_projection_digest == projection_digest(projection)`；
5. `facts.source_projection_digest == projection_digest(projection)`；
6. facts 的 change/phase/batch 与 projection 一致；
7. `report.source_policy_digest == expected_policy_digest`；
8. `report.semantics == "evidence_sufficiency/v2"` 且 `require_current_batch is True`；
9. 每个 case 只能进入其 `TraceRow.case_type` 对应的 layer。

每层输出至少包含：

- `sufficient`；
- `insufficient`；
- `reason_counts`；
- 必填的 `execution_state_counts`。

`reason_counts` 只保留非零项并按 reason lexical order 构造；`execution_state_counts` 固定按
`never_run, stale, fresh` 三个 key 全量输出（包括零值），避免 renderer 自行补默认值。

并满足：

```text
sufficient + insufficient = layer.total
sum(execution_state_counts.values()) = layer.total
sum(layer.sufficient) = count(report.verdicts where sufficient)
sum(layer.insufficient) = count(report.verdicts where not sufficient)
```

每层 `execution_state_counts` 的 key set 必须精确等于
`{never_run, stale, fresh}`；`reason_counts` 必须精确等于该层 row verdict `reason_codes` 的 multiset
计数（无多项、无漏项），而不只是数值非负。

当前 quality gate 内嵌的 coverage evidence 是 execution projection 对应的
`SufficiencyReport`，因此 benchmark 把它 join 到 execution layer summary。Reconciled layer summary
仍是纯事实；`aa verify` 的 projection digest、blocking gaps、open problems 和 reported insufficient
继续作为独立 diagnostics，不从不完整的 VerifyResult 反推完整 SufficiencyReport。

当前 `SufficiencyReport` 没有 source binding，同 batch、同 case set 的旧 report 可能被 join 到内容
已变化的新 projection；相同 projection 在不同 policy 或 `require_current_batch` mode 下也会产生不同
verdict。新 writer 因此生成 D9 定义的 `SufficiencyReportV2`，同时绑定 projection digest、policy
digest、evaluator semantics 和 mode；digest primitive 从 verify 私有实现下沉为 evidence 共享函数，
summary、sufficiency、verify 和 benchmark 只使用这一份算法。

嵌入该 report 的新 `QualityGateResult` 同步升 v2（其 registry 已是 `versioned`），v1 reader 保留。
Specialty v3 只接受带 digest 的 v2 sufficiency evidence；旧 quality evidence 或 policy/error payload
不能参与 complete join，必须生成 typed incomplete trace section。Complete join 还要求
`semantics="evidence_sufficiency/v2"`、`require_current_batch=true`，并且 source policy digest 等于
当前 benchmark root invocation 的 pinned baseline policy digest。仅凭 batch/case set 相等永远不够。

## 7. Reconciled authority 与 gap 契约

### 7.1 新增读取源

`materialize-trace-projection` 在现有读取面上增加：

- `change:inspect/issue-evidence-manifest.json`；
- `change:inspect/observations.json`；
- `change:inspect/issue-candidates.json`；
- `change:inspect/issue-reconcile-status.json`；
- `change:issues/events.jsonl` 与 `change:issues/snapshot.json`；
- `project:qa/issues/events.jsonl` 与 `project:qa/issues/problems.json`。

上述 sources 都加入 `TraceSource`，存在时记录 digest。为了安全重验 manifest entries，execution contract
还必须声明 collector 可能引用的同一组 change-relative allowlist prefixes：`execution/**`、
`cases/**`、`facts/**`、`review/**`、`healing/**`、`codegen/**`；不得把 manifest path 当成任意动态
filesystem capability。每个 entry 先通过 allowlist 与 no-symlink-escape safe-open，再一次读取 bytes；
同一份 bytes 同时馈入 `evidence_entry_digest/v1` 与 raw `TraceSource.sha256`，避免二次打开的竞态。
写入与 authorization 仍只有 `change:inspect/trace-projection.json`。

Project read claim 从单个 `problems.json` 扩为上面两个精确文件，不使用 `project:qa/issues/**` 泛化
claim。Change ledger/snapshot 已落在既有 `change:issues/**` read 内；仍在 contract tests 中钉住精确
实际读取集合。所有 ledger/projection/observations source 都进入去重后的 `TraceSource`。

当前 `candidate_document_digest` 位于 `workflow/issues/identity.py`，collector 的 evidence bundle
digest 位于 `workflow/issues/collector.py`，而 `evidence/` 不得反向 import workflow。实现时把
entry-redaction/bundle/candidate/per-candidate/occurrence identity primitives 下沉到
evidence/artifacts 可依赖的基础模块，再让 workflow 原调用点反向复用；禁止在 trace fold 中复制
第二份 JSON canonicalization 或 identity 算法。

### 7.2 Authority 状态判定

fold 加载所有 source 并记录 `TraceSource` 后，按下面的唯一映射产生 gap。表中的 `current` 表示
`change_id` 与当前 change 相等，且所有 batch identity 都等于 authoritative batch。

| Source / condition | 唯一 gap code | gap source | gap batch | Enrichment |
|---|---|---|---|---|
| failure analysis missing 或 malformed | `failure_analysis_missing` | `inspect/failure-analysis.json` | current batch | failure links 禁止；issue authority 独立判定 |
| failure analysis wrong change/batch/source_batch | `failure_analysis_identity_mismatch` | `inspect/failure-analysis.json` | current batch | failure links 禁止；issue authority 独立判定 |
| failure analysis current + valid | 无 | — | — | failure links 允许 |
| evidence manifest missing/malformed/wrong change/wrong batch/entry-root 校验失败 | `issue_reconciliation_unavailable` | `inspect/issue-evidence-manifest.json` | current batch | problem links 禁止 |
| candidates missing/malformed/wrong change/wrong batch/wrong evidence digest | `issue_reconciliation_unavailable` | `inspect/issue-candidates.json` | current batch | problem links 禁止 |
| observations missing/malformed/wrong change/wrong batch/current replay set 不等 | `issue_reconciliation_unavailable` | `inspect/observations.json` | current batch | problem links 禁止 |
| change issue ledger missing/malformed/event identity 错误 | `issue_reconciliation_unavailable` | `issues/events.jsonl` | current batch | problem links 禁止 |
| issue snapshot missing 或 malformed | `issues_snapshot_missing` | `issues/snapshot.json` | current batch | problem links 禁止 |
| issue snapshot wrong change/authoritative batch | `issues_snapshot_identity_mismatch` | `issues/snapshot.json` | current batch | problem links 禁止 |
| issue snapshot 与 change ledger canonical replay 不等 | `issue_reconciliation_unavailable` | `issues/snapshot.json` | current batch | problem links 禁止 |
| current snapshot analysis status missing/wrong identity/wrong evidence digest/wrong candidate digest | `issue_reconciliation_unavailable` | `issues/snapshot.json` | current batch | problem links 禁止 |
| current snapshot analysis status `pending` | `issue_reconciliation_unavailable` | `issues/snapshot.json` | current batch | problem links 禁止；detail=`reason=analysis_pending` |
| current snapshot analysis status `failed` 且 digest 链正确 | `issue_analysis_failed` | `issues/snapshot.json` | current batch | problem links 禁止 |
| current reconcile status `failed` 且 v2 identity/digest 链正确 | `issue_reconcile_failed` | `inspect/issue-reconcile-status.json` | current batch | problem links 禁止 |
| current reconcile status `pending` 且 v2 identity/digest 链正确 | `project_sync_pending` | `inspect/issue-reconcile-status.json` | current batch | problem links 禁止；不要求 snapshot authoritative batch=current |
| reconcile status missing/malformed/wrong identity/wrong evidence digest/wrong candidate digest | `issue_reconciliation_unavailable` | `inspect/issue-reconcile-status.json` | current batch | problem links 禁止 |
| current snapshot analysis completed + reconcile completed + snapshot sync completed，但 candidate/occurrence/observation 集不完整或 nested identity 错误 | `issue_reconciliation_unavailable` | `issues/snapshot.json` | current batch | problem links 禁止 |
| project ledger + problems 同时缺失，且 replayed change history 无 occurrence | 无 | 两个 missing `TraceSource` | — | canonical empty；problem links 为空 |
| completed branch 的 project problem ledger 非 genesis missing/malformed/version/event identity 错误 | `issue_reconciliation_unavailable` | `qa/issues/events.jsonl` | current batch | problem links 禁止 |
| project problems 非 genesis missing 或 malformed | `problems_snapshot_missing` | `qa/issues/problems.json` | current batch | problem links 禁止 |
| project problems 与 project ledger canonical replay 不等 | `issue_reconciliation_unavailable` | `qa/issues/problems.json` | current batch | problem links 禁止 |
| completed change snapshot 与 project problem occurrence membership 不一致 | `issue_reconciliation_unavailable` | `qa/issues/problems.json` | current batch | problem links 禁止 |
| current snapshot analysis completed + reconcile completed + snapshot sync completed，且 M/C/occurrence/observation/project membership 全链完整 | 无 | — | — | problem links 允许 |

同一 source condition 只能映射到表中的一个 code；不得在 missing/corrupt 与 unavailable 之间任选。
`detail` 使用稳定枚举文本
`reason=missing|malformed|change_id_mismatch|batch_id_mismatch|source_batch_id_mismatch|entry_path_invalid|entry_duplicate|execution_anchor_missing|entry_digest_mismatch|bundle_digest_mismatch|evidence_digest_mismatch|candidate_digest_mismatch|observations_replay_mismatch|ledger_missing|ledger_malformed|event_identity_mismatch|projection_replay_mismatch|recovery_event_missing|analysis_pending|candidate_count_mismatch|occurrence_count_mismatch|occurrence_set_mismatch|occurrence_identity_mismatch|observation_reference_invalid|problem_occurrence_mismatch|status_inconsistent`，不得直接写入 validator exception 文本。

同一 source 同时多错时也必须产生唯一 canonical 结果。Failure source 的 reason 顺序固定为
`missing > malformed > change_id > batch_id > source_batch_id`。Issue authority source 的局部顺序固定为：

```text
manifest:
  missing > malformed > change_id > batch_id > entry_path_invalid > entry_duplicate
  > execution_anchor_missing > entry_digest_mismatch > bundle_digest_mismatch
candidates:
  missing > malformed > change_id > batch_id > evidence_digest_mismatch
observations:
  missing > malformed > change_id > batch_id > observations_replay_mismatch
change issue ledger:
  ledger_missing > ledger_malformed > event_identity_mismatch > recovery_event_missing
snapshot document:
  missing > malformed > change_id > authoritative_batch_id > projection_replay_mismatch
snapshot analysis status:
  missing > change_id > batch_id > evidence_digest_mismatch
  > candidate_digest_mismatch > analysis_pending > candidate_count_mismatch
reconcile status:
  missing/malformed > change_id > batch_id > evidence_digest_mismatch
  > candidate_digest_mismatch > status_inconsistent > occurrence_count_mismatch
completed snapshot contents:
  occurrence_set_mismatch > occurrence_identity_mismatch > observation_reference_invalid
project problem membership:
  ledger_missing > ledger_malformed > event_identity_mismatch > projection_replay_mismatch
  > problem_occurrence_mismatch
```

Failure analysis 至多发一个 failure-authority gap；整组 issue authority evaluation 至多发一个
issue-authority gap，先按下面的跨 source 优先级选择 source，再按上面的 source-local 顺序选择 reason。
Project problems 的既有 gap 独立计算。所有 gap 最终仍按 `_gap_sort_key` canonical 排序，因此同一输入
只能得到一种 projection bytes/digest。

Completed authority 的 digest 等式链为：

```text
M = recompute_bundle_digest(validated_unique_entries)
C = candidate_document_digest(issue_candidates authored JSON)

issue_evidence_manifest.digest == M
evidence_entry_digest_v1(current execution manifest bytes) == anchor_entry.digest
issue_candidates.evidence_bundle_digest == M
snapshot.analysis_status.evidence_bundle_digest == M
snapshot.analysis_status.candidate_digest == C
issue_reconcile_status.evidence_bundle_digest == M
issue_reconcile_status.candidate_digest == C
```

Completed authority 还要求：

```text
N = len(issue_candidates.candidates)
snapshot.analysis_status.candidate_count == N
issue_reconcile_status.occurrence_count == N
current_batch_occurrence_ids(snapshot) == expected_occurrence_ids(candidates)
```

最后一条是 set equality 加双方无重复，并逐 occurrence 验证 D3 的 nested identity/observation 关系；
不能只比较 count。

Analysis failed recovery 必须先证明 manifest/candidate 与 snapshot canonical analysis status 的
identity/M/C 链，且 `candidate_count == len(candidates)`，再认定 `issue_analysis_failed`；否则只能认定
`issue_reconciliation_unavailable`。新 v2 semantic reconcile failed/pending status 也必须携带 C；任何
v1 status 只能读取为历史事实，不能证明当前 recovery authority。

Issue authority 使用以下优先级，避免 recovery 的预期缺文件产生额外噪声：

1. 先验证 manifest → candidates → current observations 的 identity/digest 前缀，并 strict-read change
   ledger、校验 event identity 与 current observation replay set；任一不成立时直接
   `issue_reconciliation_unavailable`；
2. strict change-ledger replay 已证明 current `IssueAnalysisFailedEvent` 且 replayed snapshot
   `analysis_status=failed` 时，遮蔽 reconcile/project 后续 authority 错误；
3. 已验证的 current v2 `reconcile_status=failed` 遮蔽 missing/stale change snapshot 与 project
   ledger/projection；该 semantic-failure 分支不声称历史 links；
4. strict change-ledger replay 已证明 current `ProjectSyncPendingEvent`，且 current v2
   `reconcile_status=pending` identity/M/C 正确时，遮蔽 snapshot authoritative-batch mismatch 以及
   project ledger/projection missing/stale；
5. completed 分支依次执行 change ledger/event identity → snapshot canonical replay equality → current
   candidate/occurrence/observation completeness → reconcile identity/digest/count → project ledger/version
   replay → project projection equality/membership；
6. 只有全部 completed/current/consistent 时，replayed historical facts 才可 enrich problem links。

被高优先级状态遮蔽的文件仍进入 `TraceSource`，但不额外产生 issue-authority gap。Project problems
projection 的既有 missing/malformed gap 独立保留，不受上述遮蔽规则影响。

稳定新增 gap code：

- `failure_analysis_identity_mismatch`；
- `issues_snapshot_identity_mismatch`；
- `issue_analysis_failed`；
- `project_sync_pending`；
- `issue_reconcile_failed`；
- `issue_reconciliation_unavailable`。

这些 code 全部加入 `VERIFY_BLOCKING_GAP_CODES`。`on_insufficient=warn` 只影响 sufficiency，不能把
authority gap 降为 pass。

每个 recovery/authority gap 必须使用表中的稳定 source path 并填写当前 `batch_id`。消费者只按
code/source/batch 判定，`detail` 只提供稳定原因标签，不承担额外 machine branching。

### 7.3 Enrichment 保留与清空规则

| Source 状态 | execution row facts | failure links | problem links | integrity |
|---|---|---|---|---|
| 全部 current + completed | 保留 | 保留 | 从 current authoritative snapshot 计算 | 按全部 gaps 派生 |
| failure identity 错误 | 保留 | 清空 | 由 issue authority 独立决定 | incomplete |
| analysis failed | 保留 | 若 failure current 则保留 | 全部清空 | incomplete |
| project sync pending | 保留 | 若 failure current 则保留 | 全部清空 | incomplete |
| reconcile failed/unavailable | 保留 | 若 failure current 则保留 | 全部清空 | incomplete |
| snapshot identity 错误 | 保留 | 若 failure current 则保留 | 全部清空 | incomplete |

该表禁止“当前 authority 不成立，但碰巧从旧 snapshot 得到 problem links”的降级路径。

## 8. Workflow 拓扑

### 8.1 `inspect-with-issues`

保留正常 ordinary edges：

```text
reconcile-issues -> materialize-trace-projection -> inspect-complete -> END
```

修改两条 recovery continuation：

```yaml
analyze-issues.recover.continue_to: materialize-trace-projection
reconcile-issues.recover.continue_to: materialize-trace-projection
```

Recovery via nodes 继续只记录持久化 recovery fact；它们没有 ordinary edge。

### 8.2 `issue-analyze-workflow`

新增 `materialize-trace-projection` node，并改为：

```text
analyze success -> reconcile success -> materialize -> END
analyzer recovery -> record-analysis-failure -> materialize -> END
sync recovery -> record-project-sync-pending -> materialize -> END
```

两条 recovery 的 `continue_to` 都指向 materializer，不再指向 `END`。

### 8.3 `issue-reconcile-workflow`

新增同一 operation node，并改为：

```text
reconcile success -> materialize -> END
sync recovery -> record-project-sync-pending -> materialize -> END
```

### 8.4 Topology 守卫

canonical schema 测试和 mutation guard 必须证明：

- 三个 graph 中，每条完成路径都经过 materializer；
- 任一 recovery continuation 改回 `END` / `inspect-complete`，测试失败；
- materializer 的唯一 ordinary successor 是该 graph 的 completion/END；
- dedicated recovery via node 仍无 ordinary incoming/outgoing edge；
- graph compiler 无需增加 target-specific 特例。

## 9. Benchmark Specialty Report v3

报告的**采集完整性**与 projection 的**证据完整性**必须分开。Wire contract 使用 frozen、
`extra="forbid"` 的 discriminated union；字段不是任意 `dict[str, Any]`：

```python
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]


TraceCollectionFailureReason = Literal[
    "execution_projection_missing",
    "execution_projection_invalid",
    "reconciled_projection_missing",
    "reconciled_projection_invalid",
    "reconciled_projection_stale",
    "projection_identity_mismatch",
    "projection_phase_pair_mismatch",
    "quality_gate_missing",
    "quality_gate_invalid",
    "quality_gate_binding_mismatch",
    "sufficiency_binding_mismatch",
    "verify_result_missing",
    "verify_result_invalid",
    "verify_binding_mismatch",
    "layer_summary_invalid",
]


class TraceCommandStatus(BaseModel):
    trace_exit: StrictNonNegativeInt
    verify_exit: StrictNonNegativeInt


class ProjectionOverview(BaseModel):
    phase: Literal["execution", "reconciled"]
    batch_id: str
    projection_digest: str
    integrity: TraceIntegrity
    row_count: StrictNonNegativeInt
    source_count: StrictNonNegativeInt
    gap_count: StrictNonNegativeInt
    unmapped_test_count: StrictNonNegativeInt


class LayerSufficiencyCounts(BaseModel):
    layer: LayerName
    case_type: CaseType
    sufficient: StrictNonNegativeInt
    insufficient: StrictNonNegativeInt
    reason_counts: dict[SufficiencyReasonCode, StrictNonNegativeInt]
    execution_state_counts: dict[ExecutionState, StrictNonNegativeInt]


class TraceLayerSufficiencySummary(BaseModel):
    schema_version: Literal["1"]
    source_projection_digest: str
    source_policy_digest: str
    semantics: Literal["evidence_sufficiency/v2"]
    require_current_batch: Literal[True]
    as_of: AwareDatetime
    recency_hours: StrictPositiveInt
    layers: tuple[LayerSufficiencyCounts, ...]  # exact four/order


class TracePhaseEvidence(BaseModel):
    overview: ProjectionOverview
    facts: TraceLayerFactSummary


class CoverageSummary(BaseModel):
    status: GateStatus
    line: float
    branch: float
    final_status: GateStatus


class VerifyDiagnostics(BaseModel):
    phase: Literal["reconciled"]
    verdict: VerifyVerdict
    policy_digest: str
    projection_digest: str
    blocking_gap_count: StrictNonNegativeInt
    open_problem_count: StrictNonNegativeInt
    reported_insufficient_count: StrictNonNegativeInt


class CompleteTraceabilityEvidenceV3(BaseModel):
    status: Literal["complete"]
    command_status: TraceCommandStatus
    execution: TracePhaseEvidence
    reconciled: TracePhaseEvidence
    sufficiency: TraceLayerSufficiencySummary
    coverage: CoverageSummary
    verify: VerifyDiagnostics


class IncompleteTraceabilityEvidenceV3(BaseModel):
    status: Literal["incomplete"]
    reason_code: TraceCollectionFailureReason
    detail: str = ""
    command_status: TraceCommandStatus | None = None


TraceabilityEvidenceV3 = Annotated[
    CompleteTraceabilityEvidenceV3 | IncompleteTraceabilityEvidenceV3,
    Field(discriminator="status"),
]


class SpecialtyReportV3(BaseModel):
    schema_version: Literal["3"]
    change_id: str
    capability_contract_policy: CapabilityPolicyReplayV2
    traceability_evidence: TraceabilityEvidenceV3
```

这里 `status=complete` 只表示 collector 已成功验证并汇总全部输入；其中 reconciled projection
仍可因为业务 recovery gap 而具有 `projection_integrity=incomplete`。反之，projection 缺失、
batch/digest 绑定错误或 strict case join 失败属于 collection incomplete，不能生成 layer rows。

Complete payload validator 必须强制：两 phase overview/facts identity 和 digest 一致、四层顺序精确、
`overview.row_count == sum(facts.layers[*].total)`、
`overview.gap_count == sum(facts.layers[*].gaps.total) + facts.global_gaps.total`、每个 gap aggregate 的
total/breakdown 相等、fact/sufficiency 算术守恒、sufficiency digest 绑定 execution、verify digest
绑定 reconciled；
sufficiency/verify 的 policy digest 都必须等于
`capability_contract_policy.definition_binding.baseline_policy_digest`，且 sufficiency semantics/mode
必须为固定值。Capability replay 没有有效 definition binding 时不得构造 complete trace section。
Incomplete payload 不允许携带 execution/reconciled/layer/coverage/verify complete 字段，避免
“status incomplete 但夹带半张 complete matrix”。

Expected collection error 到 reason code 的映射是闭集：文件不存在用对应 `*_missing`；JSON/model
错误用对应 `*_invalid`；change/phase/batch 顶层身份错误用
`projection_identity_mismatch`；shared row 漂移用 `projection_phase_pair_mismatch`；live fold 与持久化
reconciled digest 不同用 `reconciled_projection_stale`；quality/verify 的 identity 或 digest 错误用各自
`*_binding_mismatch`；合法 V1 quality 或 V2 error payload 因不能证明 typed sufficiency，固定使用
`quality_gate_binding_mismatch`；V2 SufficiencyReport/facts projection digest、policy digest、
semantics/mode 或 case
bijection 错误用 `sufficiency_binding_mismatch`；纯 summary validator 错误用
`layer_summary_invalid`。实现不得发明新字符串或把预期错误退化成 exception message。

Collector 必须继续校验：

- change identity；
- execution/reconciled phase；
- execution/reconciled authoritative batch 相等；
- quality gate batch 等于 execution batch；
- verify projection digest 等于 reconciled projection digest；
- quality sufficiency 与 verify 的 policy digest 都等于 capability replay pinned baseline policy digest。

此外必须调用共享 phase-pair validator，证明两相位 row-level execution facts 相同；只比较顶层
batch ID 不足以建立 enrichment-only 不变量。

然后调用共享 summary/join 函数，不在 benchmark 复制聚合算法。预期的 projection missing、
identity/batch/digest binding、case join 或计数守恒失败应写出 typed incomplete payload，再让 collect
非零退出；未建模的程序错误可以直接失败且不发布报告。两者都不得输出 partial complete matrix。

Renderer 新增两个逐层表：

1. `Trace Layer Facts`：每个 change × phase × layer 一行；
2. `Trace Layer Sufficiency`：每个 change × execution layer 一行。

global gaps 独立展示，不能重复塞入每个 layer。已有全局 projection、coverage、verify diagnostics
可以保留，作为 layer matrix 的总览和交叉校验。

`status=incomplete` 时 renderer 不输出该 item 的 layer rows，只输出 change、closed reason 和稳定
detail diagnostic。`evidence-row` 子命令同步支持 v3 complete/incomplete：incomplete row 使用显式
status/reason，不以 `0` 代替未知 counts，cursor helper 也不得因占位列把它误判为 complete。

v1/v2 reader 的兼容规则：

- 继续渲染其原有全局字段；
- 分层区显示 `legacy_unlayered`；
- 不生成四个零值 layer row；
- 不参与 v3 的跨报告 layer completeness 断言。

新 collector 针对旧 pinned invocation 运行且缺少当前 reconciled projection 时，写
`status=incomplete, reason_code=reconciled_projection_missing` 并非零退出；它不等同于已有 v1/v2
报告的 `legacy_unlayered`。

CLI collect 的退出码同时考虑 capability replay integrity 与 trace collection status：任一为
incomplete 都返回非零，但必须先原子写出通过 `SpecialtyReportV3` 校验的可诊断报告。Benchmark
caller 在看到非零后仍把已成功验证的 v3 文件加入 render set，并单独标记 item failure；不能因为
trace section 失败而丢掉同一 item 已采集的 capability replay 诊断。只有未建模异常或连 incomplete
model 都无法构造时才不发布文件。

## 10. Healing、Repeatable Recovery 与 Resume

### Healing rerun

healing 每次 rerun 都会重新进入 `inspect-with-issues`。若 B0 已有 projection，B1 无论正常成功、
analysis recovery 或 sync recovery，都必须在既有 ordinary workspace commit/recovery 语义下替换为
`authoritative_batch_id=B1` 的 projection。B0 的 problem/failure links 不得因文件残留继续冒充
B1 authority；本条不扩张为对 §3 已排除的所有未修复 SIGKILL window 作新保证。

### Repeatable recovery

- `issue-analyze` 成功后必须把先前 `issue_analysis_failed` projection 刷新为当前 completed
  projection；再次失败则刷新为当前 incomplete projection。
- `issue-reconcile` 成功后必须把先前 `project_sync_pending` projection 刷新为 completed；再次
  conflict/transport 则刷新 pending incomplete projection。
- idempotent 重跑在 source bytes 不变时产生字节一致的 projection/summary。

### Resume

Resume 测试必须区分 synchronized predecessor 与普通 materializer：

1. `reconcile-issues` 的 synchronized commit 已持久化，但 project problems 尚未 apply 或 apply 后尚未
   ack：resume 先完成/确认 project projection，再计划 materializer；fold 必须看到新 problems bytes。
2. materializer task success 已持久化，但 superstep/ordinary canonical apply 尚未完成：resume 修复
   ordinary output，且不得重新调用已经被 strict ledger 证明成功的 handler。

Trace materializer 本身只写 change-local ordinary artifact，没有 project publication ack；不得把前一
个 synchronized node 的 ack 语义归到 materializer 上。两类 resume 都不得把 B0 projection 当作 B1
成功。

通用的 prepared publication、checkpoint-file/event 间隙和 object-store kill safety 不作为本 spec
的 completion 条件；它们需要独立的 runtime durability spec 与 fault harness。

## 11. 错误处理

- 未知 layer/case type：模型或 summary fail closed。
- 未知非空 gap target：summary fail closed。
- projection 重复 case ID：summary fail closed。
- SufficiencyReport 缺失、额外或重复 case ID：join fail closed。
- status 文件 malformed：严格按 §7.2 truth table 产生 `issue_reconciliation_unavailable`，不得
  复用其他 code 或视为 completed。
- source identity mismatch：拒绝该 source enrichment，不因文件“能解析”而接受。
- 合法业务 recovery：写 valid incomplete projection，materializer task succeeded。
- fold/model/atomic write/workspace publication 错误：materializer task failed，graph 不得完成。
- authority blocking gap 优先于 sufficiency policy；verify 必须 fail。

## 12. 兼容与迁移

1. `TraceProjection` 保持 fact-only，但新 writer 显式升级到 v2；registry 改为 `versioned`，并通过
   `TraceProjectionDocument` 的 schema-version discriminated union 保留 v1/v2 reader；V1 历史缺省
   version key 仅由 wrapper before-normalizer 恢复为 `"1"`。
2. 新 gap code 只属于 v2；不得让 schema v1 接受新枚举并继续声称 `must_compat`。
3. `IssueReconcileStatus` 新 writer 升 `"2.0"` 并支持 batch/digest-bound pending；registry-facing
   document 保留 `"1.0"` completed/failed reader。
4. artifacts-owned `SufficiencyReportV2` 绑定 execution projection、policy、semantics 与 evaluator
   mode；`QualityGateResultDocument` 保留 `"1.0"` reader并由 `"2.0"` writer 嵌入 typed V2
   sufficiency。旧 quality artifact 继续可读，但不能生成 complete v3 layer sufficiency。
5. execution/reconciled projection 路径不变。
6. `operation:materialize-trace-projection` 写路径不变，只扩展精确 reads；
   `record-project-sync-pending` 新增精确 reconcile-status v2 写入。
7. 旧 pinned graph/contract snapshot 不修改；当前 runtime 遇 digest drift 的未完成旧 invocation
   fail closed。只有 replay/reporting 读取历史 pin，不承诺历史 handler resume。
8. Specialty report 新写 v3，reader 保留 v1/v2；旧报告不重写。
9. authoritative loader 对 source 已变化的 point-in-time artifact 返回 typed stale，不自动重写。
10. Fuzz/Performance wiring 会修改相同的 specialty models/renderer/collector。实施顺序应先完成其
   四层 replay 接线，再落本设计的 v3 trace payload；core summary/recovery 可以先开发，但最终
   reporting commit 必须基于已合并的四层 replay 模型。

## 13. 验证矩阵

### 13.1 Pure summary

- 混合四层 rows 固定输出四行，顺序稳定；
- 缺失 layer 输出全零 row，不省略；
- coverage/current/latest 四组分区分别守恒；
- execution phase failure/problem 计数为零；
- reconciled failure/problem row/link/unique 计数正确；
- target gap 只归一个 layer，global gap 只归 global；
- layer/global `TraceGapAggregate` 的 total 与 `by_code` sum 不等、zero/positive 规范不成立时，模型
  与 persisted summary loader 都 fail closed；
- V1 projection 明确拒绝 V2-only recovery code；V2 projection 与 summary aggregate 接受并计数；
- 未知 target、重复 case、手工构造的矛盾 projection fail closed；
- 相同 projection 两次 summary canonical bytes 一致；
- execution/reconciled phase pair 的 shared row 漂移被拒绝，只有 enrichment 字段允许不同；
- phase pair 删除/改写 execution source 或 gap、改变 unmapped tests、伪造更好 integrity 均被拒绝；
- 重复 case/未知 target/row 语义矛盾使 `fold_trace` 在发布前失败；materializer 不写 artifact，
  runner 不发布 execution projection，`aa verify` 返回 typed failure 而不是 traceback/pass。

### 13.2 Sufficiency join

- exact case set 时，每层 sufficient + insufficient = total；
- reason counts 与 row verdicts 对齐；
- missing、extra、duplicate verdict 分别失败；
- sufficient 与 missing/reason 空性矛盾、非法 kind→reason pair、naive `as_of`、非正 recency、重复
  missing kind 分别被 V2 model 拒绝；
- 一个 layer 的 verdict 不会进入另一个 layer；
- 同 batch、同 case set 但 execution row/source bytes 改变时，旧 sufficiency digest 被拒绝；
- facts digest 与 report digest 任一不等于 execution projection digest 时 join 失败；
- projection 相同但 policy digest 不同、semantics 未知或 `require_current_batch=false` 时 join 失败；
- Specialty complete 要求 sufficiency/verify policy digest 都等于 root invocation pinned baseline；
- execution-state key 缺失/额外或 sum 不等 layer total、reason_counts 与 row reasons 不一致时 join 失败；
- policy/as_of 变化只改变 sufficiency view，不改变 fact summary。

### 13.3 Reconciled authority

- current failure/snapshot/status 正常 enrich；
- cross-change failure analysis 被拒绝且 failure links 清空；
- prior-batch failure analysis 被拒绝；
- prior-batch issue snapshot 被拒绝且旧 problem links 清空；
- 同 change、同 batch 但 manifest/candidate/snapshot-analysis/reconcile digest 链任一不相等时被拒绝；
- manifest entry duplicate/unsafe/missing anchor、entry bytes digest 不匹配、bundle digest 自声明篡改分别
  被拒绝；同步篡改 manifest.digest 与全部下游 M 仍因重算 bundle 不等而失败；
- 含 token/password 的 UTF-8 entry、普通 UTF-8 entry、binary entry 都与 collector round-trip；修改
  脱敏后仍可见的文本、普通文本或 binary bytes 会按 v1 entry 语义触发 mismatch；只替换 secret 值时
  entry digest 按设计保持稳定，但 raw `TraceSource.sha256` 必须变化并使 persisted projection stale；
- 同 batch execution manifest 内容改变但 issue manifest 未重建时，anchor entry digest 校验失败；
- redaction pattern/order 的 mutation 必须由 digest-semantics compatibility test 拒绝，不能静默改变 v1；
- candidate canonical digest 使用与 analyzer/reconciler 相同的下沉 helper，raw key order/format 变化
  不改变结果，语义 bytes 变化必须改变结果；
- completed status 但 snapshot 少一个 current occurrence、重复 occurrence、cross-change/batch nested
  occurrence、wrong per-candidate digest/problem ID、dangling observation ref 分别拒绝并清空 links；
- candidate_count、occurrence_count 与 candidate 集不一致时 authority unavailable；count 相等但
  occurrence set 不等同样失败；
- project Problem 缺失 expected occurrence membership、重复 membership 或 alias chain 非法时拒绝；
- 两份 ledger 各自 strict replay 有效、但历史 occurrence 引用 dangling/cross-batch observation 时拒绝；
- 两份 ledger 各自 strict replay 有效、但历史 occurrence 的 source problem 与 project membership
  不一致、membership 重复或 project-only 时拒绝；
- 合法 `source owns occurrence -> terminal` merge history 被接受，并以 terminal canonical ID/open status
  建 link；把 membership 只放到 target、同时放 source/target 或放错 source 的 mutation 分别拒绝；
- raw snapshot 注入 cross-change historical occurrence、same-change forged historical occurrence、重复
  observation ID 时，均因 strict ledger replay/canonical projection mismatch 被拒绝；
- change ledger seq/idempotency/event-envelope↔nested identity mutation、current observations document 与
  replay set 漂移分别拒绝；
- project ledger version/event identity mutation或 problems projection 与 replay 不等时拒绝；
- project events/problems 双缺且 change history 无 occurrence 时接受 canonical empty；单缺或有历史
  occurrence 时拒绝 empty fallback；
- 合法多批 ledger replay 仍保留已验证历史 open problem link，证明实现没有退化为 current-only；
- analysis failed、project sync pending、semantic reconcile failed 各产生对应 blocking gap；
- B0 authoritative snapshot + B1 project-sync-pending event 的现有投影形状不能冒充 B1 snapshot；
  B1 必须由 v2 pending reconcile status 独立证明；
- authority unavailable 时 execution/failure facts 按规则保留，problem links 为零；
- completed current snapshot 可以使用其累计历史关联；
- issue-review 改写 project problems 后，authoritative loader 拒绝旧 persisted projection；live fold
  或重新 materialize 后才可再次声称 current；
- 所有新 gap 在 `aa verify` 下 fail，即使 sufficiency action 为 warn。

### 13.4 GraphRuntime integration

- 主 graph 正常 analyzer/reconcile 后 materialize current batch；
- 主 graph analyzer 的 timeout/transport/rate_limit/invalid_output recovery 后完成并 materialize
  incomplete projection；
- 主 graph reconcile 的 conflict/transport recovery 后完成并 materialize incomplete projection；
- semantic reconcile failure 虽然 operation task succeeded，projection 仍为 incomplete；
- `issue-analyze` 成功、analysis recovery、sync recovery 三条路径均 materialize；
- `issue-reconcile` 成功、sync recovery 两条路径均 materialize；
- 上述主 graph 两条 recovery、`issue-analyze` 三条终止路径、`issue-reconcile` 两条终止路径都先
  预置 B0 projection，并分别断言最终 artifact 已刷新为 B1；不得只用 healing happy path 代替；
- materializer 失败时三个 graph 都不能完成；
- synchronized reconcile 的 apply/ack recovery 与 ordinary materializer 的 superstep/apply recovery
  分别覆盖，并保持单次 handler effect；
- topology mutation 绕过 materializer 时测试失败。

### 13.5 Healing 与 compatibility

- 预置 B0 projection，healing rerun B1 后所有 phase/layer/binding 均指向 B1；
- B1 recovery 不保留 B0 problem links；
- 已完成旧 pinned invocation 只用于 replay/legacy 分类；未完成旧 invocation 在 schema/contract drift
  后 resume 于 recovery barrier 之后明确抛 `GraphDefinitionChanged`，不做新 planning、不注入节点、
  不调用新 handler、不写 V2 artifact；
- old materializer task-success + ordinary apply pending，以及 old synchronized apply/ack pending 两个
  fixture 证明 barrier 可先完成 ledger-proven repair；修复出的 V1 artifact 仍被 current loader
  判为 legacy/stale，不能成为 complete v3 evidence；
- 缺少当前 projection 的旧 run 报告为 legacy/incomplete；
- Specialty v3 round-trip、extra-forbid、四层顺序和计数 validator 全绿；
- Specialty v3 mutation 中，未知 sufficiency reason、naive `as_of`、overview row/gap count 与 facts
  aggregate 不一致、gap total/breakdown 不一致分别被 loader 拒绝；
- v1/v2 fixture 仍可读取并渲染 `legacy_unlayered`；
- TraceProjection v1/v2、IssueReconcileStatus v1/v2、QualityGateResult v1/v2 兼容 fixture 全绿；
- 三个 registry-facing document wrapper 对各自 v1/v2 round-trip，未知 schema version fail closed；
- TraceProjection V1 缺失 schema_version 仅按 legacy default 读取；显式 null/unknown 仍拒绝；
- `aa artifacts validate`、`aa report generate` 分别用 V1-only 与 V2-only fixture 通过对应兼容路径；
- execution evidence reload 与 benchmark collector 对 QualityGate V1/V2 各有命令/集成级覆盖，证明
  consumer 已 unwrap concrete variant，而不只是 registry model round-trip；
- v3 malformed case join、batch mismatch、digest mismatch 均先原子写对应 closed reason 的
  incomplete report，再 collect 非零退出；
- 每个 `TraceCollectionFailureReason` 至少一个 collector test，未知 reason/extra complete field 被
  model 拒绝；
- benchmark caller 保留并渲染非零 collect 已发布的 valid incomplete report。

## 14. 预期改动面

生产代码：

- `assurance_agent/artifacts/models/trace.py`
- `assurance_agent/artifacts/models/issues.py`
- `assurance_agent/artifacts/models/issue_events.py`（新增或从 workflow 下沉纯 event schema）
- `assurance_agent/artifacts/models/sufficiency.py`（新增，拥有 V2 wire DTO）
- `assurance_agent/artifacts/models/common.py`
- `assurance_agent/artifacts/models/inspect.py`
- `assurance_agent/artifacts/models/__init__.py`
- `assurance_agent/artifacts/registry.py`
- `assurance_agent/evidence/digests.py`（新增）
- `assurance_agent/evidence/issue_identity.py`（新增，承载 observation/candidate/occurrence identity）
- `assurance_agent/evidence/issue_replay.py`（新增，strict ledger read + pure projection）
- `assurance_agent/evidence/layer_summary.py`（新增）
- `assurance_agent/evidence/current_projection.py`（新增或并入 trace loader）
- `assurance_agent/evidence/trace.py`
- `assurance_agent/evidence/sufficiency.py`
- `assurance_agent/evidence/verify.py`
- `assurance_agent/workflow/issues/identity.py`
- `assurance_agent/workflow/issues/events.py`（兼容 re-export）
- `assurance_agent/workflow/issues/projection.py`
- `assurance_agent/workflow/issues/ledger.py`
- `assurance_agent/workflow/issues/history.py`
- `assurance_agent/workflow/issues/collector.py`
- `assurance_agent/workflow/issues/reconciler.py`
- `assurance_agent/workflow/issues/operations.py`
- `assurance_agent/workflow/report/quality_gate.py`
- `assurance_agent/workflow/report/inspector.py`
- `assurance_agent/workflow/report/report_builder.py`
- `assurance_agent/workflow/execution/evidence.py`
- `assurance_agent/workflow/execution/runner.py`
- `assurance_agent/_resources/schemas/execution-contracts.yaml`
- `assurance_agent/_resources/schemas/workflow-schema.yaml`
- `assurance_agent/eval/specialty_models.py`
- `assurance_agent/eval/specialty_render.py`
- `benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py`
- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `docs/schemas.md`

主要测试：

- `tests/unit/evidence/test_layer_summary.py`（新增）
- manifest source-root / occurrence-set authority unit tests
- `tests/unit/evidence/test_issue_replay_authority.py`（新增）
- `tests/unit/evidence/test_fold_trace_reconciled.py`
- `tests/integration/test_verify_cli.py`
- `tests/unit/workflow/graph/test_contracts.py`
- `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- `tests/integration/test_issue_lifecycle_workflow.py`
- healing rerun / graph resume 对应 integration tests
- `tests/unit/benchmark/test_specialty_report.py`
- specialty model/render round-trip 与 legacy fixtures
- registry version-dispatch 与 import-linter ownership tests

## 15. 验收标准

1. 所有新 TraceProjection v2 在发布前都能由同一共享函数生成严格四层 summary，benchmark 不
   复制 fold/聚合逻辑；v1 只经兼容 reader 读取。
2. 四层、current presence、latest status、gap bucket 和 sufficiency join 全部满足算术不变量。
3. 已复现的跨 change/batch failure/problem 污染被测试钉死并修复；completed issue authority 还证明
   manifest entry root、strict ledger↔projection replay equality 与
   candidate→occurrence→observation→project membership 集合完整性。
4. 主 inspection 与两个独立 issue entrypoint 的所有 success/recovery 路径都先 materialize 再完成。
5. Recovery 输出当前 batch 的 valid incomplete projection；旧 batch projection 不再残留冒充当前
   authority。
6. Project sync pending 由 batch/digest-bound IssueReconcileStatus v2 证明，不依赖 snapshot 错误的
   authoritative batch 假设。
7. 新 authority gaps 在 `aa verify` 中始终 blocking。
8. SufficiencyReport/QualityGateResult v2 的 projection digest 与 facts、execution projection 严格一致，
   并绑定 pinned policy digest、固定 evaluator semantics 与 `require_current_batch=true`。
9. Healing B0→B1 后 projection、summary、report 全部绑定 B1。
10. Specialty report v3 是 closed typed 四层结构；v1/v2 可读但明确 `legacy_unlayered`。
11. point-in-time projection source 变化后，authoritative loader 返回 typed stale，不继续声称 current。
12. 新运行使用新 graph；旧 pinned graph 与历史报告不被改写或补造。未完成旧 invocation 的
    definition drift resume 在允许 ledger-proven recovery-barrier repair 后 fail closed，不宣称历史
    handler 可执行，也不把修复出的 V1 artifact 提升为 current。
13. focused tests 与完整 CI gate 全绿：Ruff、format、Pyright、import-linter、pytest、packaging smoke。

## 16. 后续独立工作

本次审计同时发现 Graph Runtime 通用 durability 风险：prepared publication 在原 invocation resume
时可能无法清理、checkpoint cache 可能被误当成 commit authority，以及 object-store/atomic install 的
SIGKILL 窗口。这些问题影响所有 synchronized/ordinary publication，而不只 Trace。应单独生成
“Graph publication durability and crash recovery” P0 spec，使用真实 kill-window fault harness 设计和
验证；不要在本设计的实现中顺手修改通用 commit protocol。
