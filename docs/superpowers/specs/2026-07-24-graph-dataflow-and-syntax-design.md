# Graph 数据面摄入设计（v7.2）

- 日期：2026-07-24
- 状态：**S0a–S5 已实现（引擎层）**（v7.1 规格 + 实施回填）
- 范围：ledger 事件、checkpoint fold、planner/scheduler、ingest catalog、gate evidence、import/resume
- 说明：本文件为 **normative** 规格。v7.2 把实施阶段的落地决策回填进规格：**新 invocation 写 `event_schema_version=3`**，resume/interrupt 已切换 `ResumeAnchor` wire。语法糖（`review_cycle` 等）**不在范围**。

---

## 0. 决议一览（v7.1 冻结 + v7.2 实施回填）

| 议题 | 冻结选择 |
|---|---|
| Fan-out 与 NodeHistory | Child 只落 `tasks`；generation 槽位跟踪 shell + aggregate；**失败/中断由 child 终局或 interrupt 事件驱动，不经 aggregate** |
| Ordinal | **双计数器**：`generation_ordinal`（skip 消耗）与 `task_ordinal`（`_task_id` 现网语义） |
| `activation_id` | **继续用 `task_ordinal`**（与现网 planner L1371 一致）；**不**改用 `generation_ordinal` |
| Activate/skip 幂等 | 同 `(NodeKey, generation_ordinal)` + 相同**决策身份**（§5.4）→ fold no-op |
| Wire 字段 | §6.5 事件增量表为权威 |
| Fan-out DSL outputs | 首版 **fan-out 父节点 `node().outputs` 不暴露 child ingest**；读 child 用 `EvidenceBinding.producer_task_id` |
| Legacy fan-out | Migration **合成** aggregate 生命周期，或标记代 `succeeded + frozen_outputs={} + outputs_committed=true` |
| Resume wire | **v7.2：已切换 `ResumeAnchor`**（每层独立 event，禁止复制）；见 §11 |
| Checkpoint 文件 | v2 哈希 **不**向前兼容；resume 靠 replay ledger 重建 |
| **`event_schema_version`（v7.2）** | **新 invocation 写 `3`**；migration 接受 1/2/3，`> 3` 拒绝 |
| 阶段 | **S0a–S5 已实现（引擎层）** |

---

## 1. 问题与目标

**问题：** 路由/gate 反复读盘；业务证据未进入可重放投影；commit 与 generation 语义未持久化。

**目标：**

1. 从 content-addressed **concrete file blob** 摄入 `FrozenOutput`。
2. DSL `node(id).outputs` 只暴露 **已 commit 的最新 generation** 的解包 value map。
3. Durable **`NodeHistory`** 为世代权威；task 字典为 attempt/lease 附属。
4. **唯一** outputs 提升入口：`apply_outputs_commit`（仅由 `SuperstepCommittedEvent` 触发）。
5. Import / resume / fan-out 闭合到同一套 wire 与不变量。

**非目标：** many/directory ingest、fan-out reduce、A4 可见性、`review_cycle` 语法糖、contracts 内 `artifact:` 解析、**fan-out 父节点聚合 child ingest 到 `node().outputs`**（见 §5.1）。

---

## 2. Digest 契约（normative）

| 项 | 规范 |
|---|---|
| 算法 | SHA-256，小写 hex |
| 换行 | digest 输入字节仅 **LF** |
| JSON canonical | UTF-8；`sort_keys=True`；separators `(",", ":")`；`ensure_ascii=False`；无 float |
| IR / catalog digest 输入 | 结构 → JSON canonical 字节后哈希（不直接哈希 YAML 原文） |
| Contract digests | **保持现有** per-contract 原文 sha256（不强制 JSON canonical，以免无谓迁移） |

**三个独立 pin（禁止合成总哈希）：**

| Pin | 含义 | 落点 |
|---|---|---|
| `ir_digest` | Canonical IR | `CompiledWorkflow`、`GraphInvocationStartedEvent`、`GraphProjection` |
| `ingest_catalog_digest` | IngestArtifactCatalog | 同上 + `.graph-runtime/pinned/ingest-artifact-catalog.<digest>.json` |
| `contract_digests` | 现有 map | 同上 |

**Legacy 豁免（ingest catalog）：**

- **Legacy invocation**（`event_schema_version` 缺省或 `1`）：`ingest_catalog_digest` 允许 `""`。
- **新 invocation**（`GraphInvocationStartedEvent.event_schema_version: 3`）：`ingest_catalog_digest` **禁止**为空；启动时 fail closed。

读路径：legacy `graph_digest` → 映射为 `ir_digest` 后丢弃 `graph_digest`。写路径只写 v2+ 字段。`CompiledWorkflow.digest` 若保留则 **必须** `== ir_digest`（deprecated alias）。

---

## 3. IngestArtifactCatalog（normative）

文件：`assurance_agent/_resources/schemas/ingest-artifact-catalog.yaml`。

```yaml
schema_version: 1
artifacts:
  api_plan_review:
    kind: file_ingest              # file_ingest | path_only
    model: review@1
    model_schema_digest: "<sha256 of exported JSON Schema canonical bytes>"
    path: change:review/api-plan-review.json
    codec: json                    # json | yaml
    cardinality: one               # 唯一合法值
    compat: must_compat
    ingest:
      mode: full                   # 首版唯一合法值（file_ingest）
  api_plan:
    kind: path_only
    path: change:plans/api-plan.md
    cardinality: one
```

- `file_ingest`：blob → validate → `FrozenOutput`。
- `path_only`：仅供 workflow `outputs:` 符号展开；不产生 frozen/DSL outputs。
- 不写 producer。符号引用首版仅 workflow outputs；contracts 继续写 path，CI 交叉校验。
- `ResourcePath.parse` 首版不扩展 `artifact:`。

---

## 4. FrozenOutput 与三层语义（normative）

```python
class FrozenOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: object                 # canonical JSON object/array/scalar
    source_path: str
    source_sha256: str
    model_id: str                 # e.g. review@1
    model_schema_digest: str
    catalog_symbol: str
```

| 名称 | 形态 | 用途 |
|---|---|---|
| `frozen_outputs` | `dict[str, FrozenOutput]` | Ledger envelope / 审计 |
| `candidate_outputs` | `dict[str, object]` **仅 value map** | Attached gate（finalize 内） |
| DSL `outputs` | `dict[str, object]` | 解包自 **已 commit** generation |

**Canonical value：** `model_dump(mode="json", exclude_unset=True)`。模型默认值若需成为证据，必须在实例上显式出现。首版无 `project.fields`。

**上限：** 单符号 ≤ 64KiB canonical JSON；每 task aggregate ≤ 256KiB。

**不变量：**

1. `outputs_committed == true` ⇒ `frozen_outputs` present（map，至少 `{}`）。
2. 一切 **新** success（含 synthetic / import）必须写 `frozen_outputs: map`（可 `{}`）。
3. `outputs_committed == false` ⇒ DSL 无 `outputs` 键。
4. Attached gate 只读 `candidate_outputs` value map。

---

## 5. NodeHistory 与 generation（normative）

### 5.1 容器

```text
NodeKey = (checkpoint_ns, graph_id, node_id)

NodeHistory:
  latest_generation_ordinal: int
  generations_by_ordinal: dict[int, NodeGeneration]

NodeGeneration:
  generation_ordinal: int
  status: activated | skipped | running | succeeded | failed
          | abandoned | interrupted | stopped
  reached: bool | null
  activation_id: str | null
  aggregate_task_id: str | null
  frozen_outputs: dict[str, FrozenOutput] | absent
  outputs_committed: bool
  gate_report: object | null
  fan_out_expansion_id: str | null
  # …

GraphProjection.node_histories: dict[NodeKey, NodeHistory]
```

**Fan-out 代语义（冻结）：**

- 每个 `generation_ordinal` 槽位描述 **节点壳层 +（可选）aggregate synthetic task**。
- **Fan-out child** 的 start/success/fail 只更新 `tasks[task_id]`，**不**直接写 `NodeGeneration.frozen_outputs`。
- Child `TaskProjection` 必须携带 `generation_ordinal`、`task_key`、`fan_out_child: true`。
- Aggregate `TaskProjection` 携带 `fan_out_aggregate: true`；其 success 写入 shell 的 `frozen_outputs`（通常 `{}`）。

**Fan-out 父节点 DSL outputs（首版限制）：**

- `node(fan-out-parent).outputs.<symbol>` **不**聚合 child ingest 结果。
- 已 commit 的 fan-out 父代 DSL `outputs` 仅来自 aggregate 的 `frozen_outputs`（首版通常为 `{}` 或 absent→无 outputs 键）。
- 下游若需 child 产物，必须使用 **`EvidenceBinding.producer_task_id`** 指向具体 child task，或 `fan_out.reduce`（另开设计）。

**`aggregate_task_id` 赋值时机：**

| 节点类型 | 何时设置 | 值 |
|---|---|---|
| 普通节点 | `NodeActivatedEvent` 后、首个 `TaskAttemptStarted` 前 | `_build_task` 的 `task_id` |
| Fan-out 节点 | **`FanOutExpandedEvent` 后立刻**（确定性预计算，不必等 aggregate start） | `_task_id(..., ordinal=len(task_ids), task_key="__aggregate__")` |
| Fan-out 代 child 未全部终局 | 已预置 `aggregate_task_id`；aggregate **尚未** start | |

DSL：

```text
history = node_histories[key]
gen = history.generations_by_ordinal[history.latest_generation_ordinal]
# latest_generation_ordinal < 0 → MISSING node
# outputs 仅当 gen.status == succeeded and gen.outputs_committed
#   → {symbol: fo.value for symbol, fo in gen.frozen_outputs.items()}
# 否则无 outputs 键
```

### 5.2 双计数器：`generation_ordinal` vs `task_ordinal`

| 字段 | 所有者 | 语义 | 用于 |
|---|---|---|---|
| **`generation_ordinal`** | activate/skip 时分配 | skip 消耗；连续 `0..latest` | `NodeHistory`、DSL latest 代 |
| **`task_ordinal`** | 现有 `_task_id` 规则 | **现网不变** | `_task_id`、`activation_id` |

**`task_ordinal` 分配（禁止变更）：**

1. **普通节点：** `count(tasks where node_id == nid)`（skip 不占位）。
2. **Fan-out child：** `fan_out_item_index`（0..N-1）。
3. **Fan-out aggregate：** `len(expansion.task_ids)`，`task_key = "__aggregate__"`。

**`activation_id`（与现网一致，禁止改用 generation_ordinal）：**

```text
# 普通节点 activate
activation_id = canonical_digest({
  invocation_id, checkpoint_ns, graph_id, node_id,
  ordinal: task_ordinal    # 非 generation_ordinal
})

# skip 事件无 activation_id；以 generation_ordinal 定位槽位
```

**兼容性：** v1 in-flight invocation resume 时 `task_id` 与 `activation_id` 公式均不变；仅 **新增** wire 字段 `generation_ordinal`。

### 5.3 Allocator（generation_ordinal）

| 项 | 规则 |
|---|---|
| 所有者 | Planner 在 `NodeActivatedEvent` / `NodeSkippedEvent` 分配 |
| 公式 | `next = latest_generation_ordinal + 1`（无历史则 0） |
| Fan-out | 展开前必须有 `NodeActivatedEvent`；`FanOutExpandedEvent` 携带同一 `generation_ordinal` |

### 5.4 Activate/skip 幂等（fold）

**决策身份（normative）：** 幂等比对只取决定「这一代要不要跑、跑什么」的字段，`source_reads_sha256` **不参与**。

| 事件 | 决策身份字段 |
|---|---|
| `NodeActivatedEvent` | `type` + `node_id` + `generation_ordinal` + `activation_id` + `input_sha256` |
| `NodeSkippedEvent` | `type` + `node_id` + `generation_ordinal` + `expression` + `input_sha256` |

`source_reads_sha256` 是**观测性**来源快照：planner 在后续 superstep 重发同一决策时，上游刚落盘的文件会让它增长（例：healing 的 `complete-not-needed` 在 `proposal` 写出 `fix-proposal.json` 前后各被跳过一次），那不是决策变化，不得判为冲突。

| 条件 | fold 行为 |
|---|---|
| `(NodeKey, generation_ordinal)` 不存在 | 正常写入 |
| 已存在且决策身份相同 | **no-op**（`source_reads_sha256` 可不同） |
| 已存在且决策身份不同 | `LedgerIntegrityError` |

### 5.5 Fan-out 代 `NodeGeneration.status` 矩阵（normative）

| 场景 | `NodeGeneration.status` | 驱动源 |
|---|---|---|
| activate 后、尚无 child start | `activated` | `NodeActivatedEvent` |
| 至少一个 child running/retry；无终局 failed | `running` | generation reducer 扫描 child `TaskProjection` |
| 全部 child succeeded → aggregate succeeded | `succeeded` | aggregate outcome（§10） |
| 任一 child **终局** failed/abandoned（不可重试） | `failed` | generation reducer 扫描 child；**无需** aggregate |
| interrupt 落在 fan-out 代 | `interrupted` | `GraphInterruptedEvent`（匹配 node / generation） |
| skip | `skipped` | `NodeSkippedEvent` |

**修订 G8：** aggregate outcome **仅驱动成功路径**的 `succeeded` 与 `frozen_outputs`；失败/中断 **不**等待 aggregate。

### 5.6 Generation 完整性矩阵

| 规则 | 说明 |
|---|---|
| G1 | 同 `(NodeKey, generation_ordinal)` 不能存在不同**决策身份**（§5.4）的 activated 与 skipped |
| G2 | `generation_ordinal` 集合连续 `0..latest` |
| G3 | 普通/aggregate：`TaskAttemptStarted.task_id == NodeGeneration.aggregate_task_id` |
| G3f | Fan-out child：豁免 G3；`task_id ∈ FanOutExpandedEvent.task_ids` 且 child.`generation_ordinal` 匹配 |
| G4 | 普通/aggregate：outcome 所属代 = `TaskProjection.generation_ordinal`（**由 task_id 反查，不要求 wire 携带**） |
| G4f | Fan-out child：豁免对 `NodeGeneration` 的 outcome 写入 |
| G5 | `committed_task_ids` ⊆ succeeded 且 `frozen_outputs` present |
| G6 | `FanOutExpandedEvent` 前必须有同代 `NodeActivatedEvent` |
| G7 | Commit 只改 `outputs_committed` |
| G8 | Fan-out 成功路径仅 aggregate 写 shell `frozen_outputs`；失败/中断见 §5.5 矩阵 |

### 5.7 双写消解

| 状态面 | 权威 |
|---|---|
| `NodeHistory` / `NodeGeneration` | generation reducer |
| `tasks` | attempt reducer |

**状态转移（普通 / fan-out 成功路径）：**

```text
(absent) --activate--> activated --start--> running --success--> succeeded
                 \--skip--> skipped
running --child终局fail--> failed          # fan-out：不经 aggregate
running --interrupt--> interrupted
running --aggregate success--> succeeded     # fan-out
succeeded --commit--> succeeded + outputs_committed=true
```

---

## 6. 事件流迁移（normative）

### 6.1 `migrate_graph_event_stream`

有状态深模块；输出 typed events；`event_schema_version > 3` 拒绝。migration 覆盖 1→2（generation/ingest 字段）与 2→3（resume/interrupt payload 默认）。

### 6.2 读路径

```text
read_events_migrated(change_dir):
  raw = read_events_raw(change_dir)
  migrated_graph = migrate_graph_event_stream([e for e in raw if source==graph])
  return merge_preserving_seq(raw, migrated_graph)  # non-graph 逐字节透传
```

### 6.3 Wire 版本

| 项 | 规范 |
|---|---|
| 权威 pin | **`GraphInvocationStartedEvent.event_schema_version` 为唯一权威** |
| 其他 graph 事件 | 可选冗余携带；fold **不读** |
| 新 invocation | 写入 `3` |
| 缺省 | 视为 `1` |
| `> 3` | 拒绝 |

### 6.4 v1→v2 migration 要点

| 类 | 行为 |
|---|---|
| `graph_digest` | → `ir_digest` |
| `graph_invocation_started.ingest_catalog_digest` | 缺省 → `""` |
| 缺 `generation_ordinal` | 按 seq 为 activate/skip 回填；outcome 经 `task_id` + expansion 表反查 |
| `activation_id` | **保留 ledger 内旧字节**；migration **不重算** |
| 缺 `frozen_outputs` | legacy success：absent；`outputs_committed=false` |
| 缺 `committed_task_ids` | `[]` |
| Fan-out 无 activation | 合成父代 activated 语义 |
| **Legacy fan-out 无 aggregate** | 若全部 child 已 succeeded 且无 aggregate 事件：**二选一**——(A) 合成 aggregate planned→started→succeeded→committed 最小链；或 (B) 直接标记该代 `status=succeeded`、`frozen_outputs={}`、`outputs_committed=true`（S0a 夹具选 B 为默认） |
| activate/skip 重复行 | 同 canonical 字节 → 去重 |
| **Checkpoint 文件** | 旧 `.graph-runtime` checkpoint id **不**保证匹配 v2 哈希；resume **必须** replay ledger 重建 projection，不得仅信旧 checkpoint 文件 |

### 6.5 wire 字段增量表（权威）

**v1→v2（generation + ingest）：**

| 事件 | 新增/变更字段 | 必填 | 说明 |
|---|---|---|---|
| `GraphInvocationStartedEvent` | `event_schema_version` | 新 run：是 | 权威 version pin |
| | `ir_digest` | 是 | 替代 `graph_digest` |
| | `ingest_catalog_digest` | 新 run：非空 | legacy 可 `""` |
| `NodeActivatedEvent` | `generation_ordinal` | 是 | |
| | `activation_id` | 是 | **仍基于 task_ordinal**（§5.2） |
| `NodeSkippedEvent` | `generation_ordinal` | 是 | v1 无此字段 |
| `FanOutExpandedEvent` | `generation_ordinal` | 是 | |
| `TaskAttemptStartedEvent` | — | | `generation_ordinal` **不**要求 wire 携带；fold 经 `task_id` 反查 |
| `TaskAttemptSucceededEvent` | `frozen_outputs` | 新 success：是 | map，可 `{}` |
| `SuperstepCommittedEvent` | `committed_task_ids` | 是 | sorted task_id |

**v2→v3（resume/interrupt anchor）：**

| 事件 | 新增/变更字段 | 必填 | 说明 |
|---|---|---|---|
| `GraphInterruptedEvent` | `anchor` | v3：是 | `ResumeAnchor{invocation_id, checkpoint_ns, node_id, interrupt_id}` |
| | `parent_anchor_ref` | 否 | bubbled 链上父 anchor 的 canonical digest |
| `GraphResumedEvent` | `anchor` | v3：是 | 每层独立 anchor |
| | `parent_anchor_ref` | 否 | 首层为 `null`，后续层指向前一层 anchor digest |
| | `payload` | 否 | 结构化 resume 载荷（默认 `{}`；migration 回填） |

**Migration 默认：** v1/v2 缺 `anchor`/`parent_anchor_ref` → `null`；缺 `payload` → `{}`。fold 对 v1/v2 ledger 仍按 `interrupt_id` 定位（anchor 可选）。

---

## 7. Commit 协议

### 7.1 `apply_outputs_commit`

对每个 task_id 定位 **NodeGeneration**（普通/aggregate；非 fan-out child），校验 succeeded + `frozen_outputs` present，置 `outputs_committed=true`。

### 7.2 Checkpoint 哈希（v2 破坏性扩展）

```text
committed_task_ids = sorted(C)
write_set_ids = [ws.write_set_id for ws in sorted(write_sets, key=lambda ws: ws.task_id)]

checkpoint_id = sha256(canonical_json({
  "superstep_id", "parent_checkpoint_id",
  "write_set_ids", "target_tree_id",
  "committed_task_ids"
}))
```

旧 checkpoint 文件 id 与新公式 **不兼容**（§6.4）。

### 7.3 Import commit

合成 `SuperstepPlannedEvent` + `SuperstepCommittedEvent`；**计入** `supersteps`；output-only 时 `parent_checkpoint_id = bootstrap-{invocation_id}`，`target_tree_id = root_tree_id`，`write_set_ids = []`。

### 7.4–7.5

Import 重摄入比对；`ImportTaskCoord` 含 `ordinal_semantics_version: 2`、`generation_ordinal`、`task_ordinal`、`fan_out_role`。

---

## 8. 摄入顺序

`freeze_write_set` → blob 读 → validate → `frozen_outputs` → success（`outputs_committed=false`）→ `SuperstepCommitted` → `apply_outputs_commit`。

---

## 9. EvidenceBinding

Planner 冻结 `producer_task_id` + symbol + `source_sha256`（`resolve_evidence_bindings` 解析节点 `evidence:` / `with.evidence` 声明，写入 `ExecutableTask.evidence_bindings`）；执行期读 **已 commit** 的 `frozen_outputs`（`resolve_committed_frozen_output`/`resolve_committed_value`）；禁止按 node latest 重解析。Fan-out child 产物 **必须**走此路径（§5.1）。

**执行期消费：** handler 在执行前把已解析的 evidence value 注入 task input 的 `evidence` 键（`task_runner` 解析 `task.evidence_bindings` → `{alias: value}`）。drift（`source_sha256` 不匹配已 commit generation）时 fail closed。

---

## 10. Fan-out aggregate（成功路径）

1. `NodeActivatedEvent`（`generation_ordinal`）
2. `FanOutExpandedEvent` → **预置** `aggregate_task_id`（§5.1）
3. Child lifecycle → 仅 `tasks`；generation reducer 按 §5.5 维护 shell status
4. 全部 child succeeded 且 committed → synthetic aggregate wave → `frozen_outputs: {}` → commit

Child 终局 failed → §5.5 直接 `failed`，**跳过**步骤 4。

---

## 11. Nested resume（v7.2 已实现）

> **实施状态：** resume/interrupt 已从 v1 `interrupt_id` 复制切换到 **`ResumeAnchor` wire**。root resume 沿 interrupt namespace（`inv0/node1/inv1/...`）为每层 invocation emit 一条独立 `GraphResumedEvent`，每条携带该层的 `checkpoint_ns` 与 `anchor`；**禁止**把同一 event 复制到所有层。落点：`resume_wire.py`、`runtime._commit_resume_command`（`event_schema_version >= 3` 分支）。

### 11.1 ResumeAnchor

```python
class ResumeAnchor(BaseModel):
    invocation_id: str
    checkpoint_ns: str
    node_id: str
    interrupt_id: str
```

### 11.2 分层传播

- root resume 解析 `pending.checkpoint_ns` → 沿 ns 每个 invocation id emit 一条 `GraphResumedEvent`。
- 第一层 `parent_anchor_ref = null`；后续层 `parent_anchor_ref = canonical_digest(前一层 anchor)`。
- 仅首层携带 `audited_reads_sha256`（audited resume 校验）。
- `payload` 为可选结构化载荷：`ResumeCommand.payload` 原样转发到 **首层** `GraphResumedEvent.payload`（非首层为 `{}`）；**payload model 校验（`payload_model_id` / `payload_model_schema_digest`）为后续增量，当前 payload 透传不校验**。

### 11.3 fold

fold 对每条 `GraphResumedEvent` 按 `interrupt_id` 定位并置 `resolved_action`；v3 anchor 为审计/可读性冗余，fold 不依赖 anchor 定位。

---

## 12. Subgraph 整包 export（S5）

```yaml
exports:
  api_plan_review:
    from: review-cycle
    output: api_plan_review
```

转发：原样引用子图 committed `FrozenOutput`，禁止 re-wrap。

**Finalize 钩子（已实现，normative 接口）：**

```text
apply_subgraph_exports(child_projection, child_graph_id, export_defs) -> dict[str, FrozenOutput]
```

- 调用点：父 subgraph task **`finalize` 成功路径**，在写 `TaskAttemptSucceededEvent.frozen_outputs` **之前**（`finalize._apply_subgraph_exports`）。
- 输入：§12 `exports` 编译结果（`CompiledNode.exports`）+ 子 invocation 的 `node_histories`（按 `from` 解析）。
- 输出：合并进父 task 的 `frozen_outputs`（copy-by-ref，同一 `source_sha256`）。
- 编译期：非 `graph:` 节点声明 `exports` 被拒绝；`from` 必须是子图已知 node。

---

## 13. 阶段拆分（实施状态）

| 阶段 | 范围 | 状态 |
|---|---|---|
| **S0a** | §5–§6；wire §6.5；fan-out 状态矩阵；legacy fan-out migration | ✅ 已实现 |
| **S0b** | §7 commit/checkpoint/import；fan-out activation+aggregate 成功路径 | ✅ 已实现 |
| **S1** | Catalog + 三 pin | ✅ 已实现 |
| **S2** | blob 摄入 + attached gate | ✅ 已实现 |
| **S3** | EvidenceBinding + §11 resume wire 切换 | ✅ 引擎已实现；**evidence handler 执行期消费见 §9 注记** |
| **S4** | gate 双跑 | ✅ 已实现 |
| **S5** | §12 + `apply_subgraph_exports` | ✅ 已实现 |
| **另开** | review_cycle；many；reduce；A4；fan-out 父 outputs 聚合；ImportTaskCoord 深化；payload model 校验 | — |

---

## 14. 风险

- S0a 读路径 + generation reducer 爆破半径最大；需全量事件夹具（含 §13 S0a 四条）。
- v2 checkpoint 哈希使旧 checkpoint 文件失效；依赖 ledger replay。
- Fan-out 强制 activation + aggregate 成功路径改变现网「child 全成功即 node 成功」——失败路径仍等价，成功路径多一次 synthetic commit。
- Import 计入 `supersteps` 可能触顶。

---

## 15. 后续增量（当前范围之外）

S0a–S5 引擎层已闭合。以下明确留作后续，不阻塞当前规格闭合：

- **§5.3 代号分配器接线**：`_next_generation_ordinal` 已实现但 **未接入** `_activate`/`_emit_skip`/`FanOutExpandedEvent`——现网所有 activate/skip 都写 `generation_ordinal=0`，同一 node 的跨代再激活折叠进同一槽位。直接接线会破坏 `node(id).outputs`：planner 会在后续 superstep 幂等重发同一决策（同 `activation_id`），逐次 `+1` 把已成功的代挤出最新槽位。接线前必须先按 §5.4 决策身份判定「本次决策是否已有槽位」并复用之。
- **§11.2 payload model 校验**：`payload_model_id` / `payload_model_schema_digest` 与 payload schema 校验（当前 payload 透传）。
- **另开**：review_cycle、many/directory ingest、fan-out reduce、A4 可见性、fan-out 父 outputs 聚合。

**已回填（v7.2 之后）：**

- **Attached gate verdict 接线**：attached gate 由 `finalize` 在 **节点成功之后** 求值并冻结 `gate_report`，它对控制流的唯一出口是 route；「声明了 `gate:` 但没有 route 读其 verdict」的节点等于 **不设防**（ledger 上留 `verdict: stop`，write-set 照常 commit）。生产 schema 里曾有 5 处：
  - `archive-workflow.archive`（FAIL 的 change 照样被归档）→ 前置 `precheck`（`builtin:gate`）+ `routes: {pass: archive, stop: STOP}`。
  - `{api,e2e}-branch.codegen` 的 `*-codegen-precondition-gate` → 前置 `codegen-precheck`（`builtin:gate`）+ `routes: {pass: codegen, stop: STOP}`，`codegen` 不再有任何直入 edge。
  - `{fuzz,performance}-branch.codegen` → 该分支已有的 `codegen-gate`（route `pass: codegen` / `skip: END`）就是同一前置条件，删除重复的 attached gate 即可；两个 `*-codegen-precondition-gate` 定义因此在图上不再被引用。
  - 不变量由 `test_attached_gate_verdicts_reach_control_flow`（attached gate 必须被 route 选中）与 `test_codegen_is_reachable_only_through_a_gate_route`（codegen 只能经 `builtin:gate` 的 route 抵达）锁死。
- **跨子图 `gate()` 解析**：view 版 `gate()` 原先只查冻结 node 结局，查不到即 fail-closed 成 `stop`——`*-codegen-precondition-gate` 读的 `gate('<plan-review-gate>')` 是在 `*-plan-cycle` **子图内** 裁决的，父图 view 里没有对应 node 结局，于是现网一律 `stop`。现改为：冻结结局优先，否则在同一 view 上就地重算（memo 去重、成环 fail closed），语义对齐 v1 `resolve_gate_verdict`。见 `tests/unit/workflow/graph/test_gate_reference_resolution.py`。
- **Catalog `model_schema_digest`**：`review@1` 从 `""` 回填为导出 JSON Schema canonical bytes 的 sha256（`9b67987…`）；`validate_catalog_runtime` 现对 pinned digest 与实时计算值做 **drift fail-closed** 校验。
- **生产 workflow 接入（api 分支参考集成）**：`api-plan-cycle` 声明 `exports: {api_plan_review: {from: review, output: api_plan_review}}`；`api-branch.codegen` 声明 `evidence: {api_plan_review: {node: review-cycle, symbol: api_plan_review}}`。行为可加（gate 仍读盘；evidence 仅注入 codegen prompt + gate scope），plan 期 evidence 依赖由拓扑保证。
  - **已知不一致**：catalog `e2e_plan_review` 的 `path` 为 `change:review/e2e-plan-review.json`，但 e2e review 节点实际产出 `change:review/plan-review.json` —— 故 e2e 分支暂未接入 evidence/exports（catalog 路径需先对齐）。
