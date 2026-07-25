# Archive / Retro Workflow Entrypoints 设计

- 日期：2026-07-25
- 状态：**已实现**（Tasks 1–7，`e24c4fc`–`1495c35`）
- 范围：`workflow-schema.yaml` 增加独立 `archive` / `retro` entrypoint 与子图；retro 厚拆为 operation + agent；顶层 `workflow` 改挂 `graph:archive-workflow`
- 非范围：promote / nightly resume / eval / apply；proposals.json ingest catalog contract；删除现有 `aa retro nightly *` CLI；单 change retro

---

## 0. 决议一览

| 议题 | 冻结选择 |
|---|---|
| 打包形状 | **A**：独立 entrypoint（对齐 `intake-workflow` / `execute-workflow`） |
| Retro 作用域 | **跨 change**（保持现有 nightly collect 语义） |
| Retro 深度 | **厚拆**：`collect → propose → accept` |
| 落地拆法 | **方案 1**：子图 + 薄 operation 包现有函数，不重写业务 |
| Archive 在 full 中 | 顶层 `workflow.archive` 改为 `uses: graph:archive-workflow`（`when: auto_archive`） |
| CLI | 第一刀保留 `aa retro nightly collect`；新增 `aa workflow run --entrypoint archive\|retro` |
| proposals contract | **不绑** ingest catalog；写边界继续用 `accept_proposals` |

---

## 1. 问题与目标

**问题：**

1. `archive` 已嵌在顶层 `workflow`，但没有像 `intake-workflow` 一样的独立入口，无法单独 `workflow run --entrypoint archive`。
2. `retro` 完全在 GraphRuntime 外（nightly CLI），刚补的 `accept_proposals` 写门也无法进入 graph 的 retry / 事件账本。
3. retro 是跨 change 的，和 change-scoped 的 assurance 主链不该硬串在一起。

**目标：**

1. 新增 `archive-workflow` + `entrypoint.archive`，与 `intake-workflow` 对称。
2. 新增 `retro-workflow` + `entrypoint.retro`，把 nightly collect 拆成可观测的三步图。
3. 顶层 `full` 继续可选跑 archive，但实现上复用同一子图。
4. operation 只包现有 Python（`enumerate_candidates` / `build_retro_context` / `accept_proposals` 等），业务语义不变。

---

## 2. Entrypoints 与 params

### 2.1 Entrypoints

```yaml
entrypoints:
  full:
    graph: workflow
    allow: "params.run_mode == 'full'"
  intake:
    graph: intake-workflow
    allow: "params.run_mode in ['full', 'case-only', 'review-case']"
  execute:
    graph: execute-workflow
    allow: >
      params.run_mode in
      ['full','api-only','e2e-only','plan-only','codegen-only','review-plan']
  case:
    graph: intake-workflow
    with:
      run_mode: case-only
  archive:
    graph: archive-workflow
    with:
      auto_archive: true
  retro:
    graph: retro-workflow
```

说明：

- `archive` entrypoint 通过 `with.auto_archive: true` 满足现有 `archive-gate` 对 `params.auto_archive == true` 的要求。
- `retro` 不依赖 `run_mode`；始终可调用。
- Runtime 仍可能携带某个 `change_id`（现有 CLI 惯例），但 retro 节点只读写 `project:qa/retro/...`，不依赖当前 change 的 assurance 产物。

### 2.2 新增 params

```yaml
params:
  # 已有
  auto_archive: {type: bool, default: false}
  # 新增
  retro_id: {type: string, default: ""}          # 空 = collect 时 generate_retro_id()
  retro_last: {type: int, default: 10}
  retro_min_evidence: {type: int, default: 2}
  retro_dry_run: {type: bool, default: false}    # true → collect 后 END，跳过 propose/accept
```

若 schema_v2 尚无 `string` param 类型，实现时扩展 params 类型表，或用约定空串的 enum/兼容写法——以能表达「可空 retro_id」为准。

---

## 3. `archive-workflow`

```yaml
archive-workflow:
  max_supersteps: 10
  nodes:
    archive:
      uses: skill:aa-archive
      agent: aa-archiver
      outputs: ["project:qa/archive/${context.change_id}/"]
      gate: archive-gate
      retry: agent-transient
      timeout: agent
  edges:
    - {from: START, to: archive}
    - {from: archive, to: END}
```

顶层 `workflow` 变更：

```yaml
# was: skill:aa-archive inline
archive:
  uses: graph:archive-workflow
  when: "params.auto_archive == true"
```

`archive-gate` **不改语义**（仍读 execution / inspect / healing / reviews）。

---

## 4. `retro-workflow`

### 4.1 拓扑

```yaml
retro-workflow:
  max_supersteps: 20
  nodes:
    collect:
      uses: operation:retro-collect
      outputs:
        - project:qa/retro/${params.retro_id}/context.json
      retry: never
      timeout: local-operation

    propose:
      uses: skill:aa-retro
      agent: aa-doc-author
      when: "params.retro_dry_run == false"
      outputs:
        - project:qa/retro/${params.retro_id}/proposals.json
        - project:qa/retro/${params.retro_id}/retro-summary.md
      retry: agent-transient
      timeout: agent

    accept:
      uses: operation:retro-accept
      when: "params.retro_dry_run == false"
      outputs:
        - project:qa/retro/${params.retro_id}/proposals.json
        - project:qa/retro/${params.retro_id}/review-queue.md
      retry: never
      timeout: local-operation

  edges:
    - {from: START, to: collect}
    - {from: collect, to: END, when: "params.retro_dry_run == true"}
    - {from: collect, to: propose, when: "params.retro_dry_run == false"}
    - {from: propose, to: accept}
    - {from: accept, to: END}
```

零信号（对齐 `NIGHTLY_NOOP`）：`collect` **成功**结束并携带 `signal_count == 0`；图路由到 `END`，不跑 propose/accept。具体 route DSL 在实现计划里对照现有 `routes` / edge `when` 能力选定一种，语义必须是「成功 NOOP」，不是 `STOP` 失败。

### 4.2 Operations

| operation | 行为 | 成功 | 失败 |
|---|---|---|---|
| `operation:retro-collect` | 解析 `retro_id`；`enumerate_candidates`（`last=retro_last`）；`build_retro_context`；写 `qa/retro/<id>/context.json`；`mark_consumed_change`（与 nightly 一致） | 写出 context；value 含 `retro_id`、`signal_count` | 聚合/IO 基础设施错 → task fail |
| `operation:retro-accept` | `accept_proposals`；`validate_retro_proposals` 过滤；`partition_proposals_for_review`（`min_evidence=retro_min_evidence`）；写 `review-queue.md`；`complete_retro_stage` | 规范 proposals + review-queue | `AaError`（无法路由）→ task fail；优先映射为可触发 propose 重试的 `invalid_output`（若 engine 允许 operation 返回该 kind） |

注册位置：

- `assurance_agent/workflow/graph/handlers/operation.py`（或同级 retro 专用模块再 re-export）
- `assurance_agent/_resources/schemas/execution-contracts.yaml`

### 4.3 `retro_id` 与路径模板

问题：`outputs` 里的 `${params.retro_id}` 在 param 为空时不能指向最终目录。

冻结实现策略（按优先级尝试，计划阶段选定其一并写测试）：

1. **首选**：entrypoint / collect 前由 driver 或 operation 把生成的 id **写回 run params**（或等价的可模板展开槽位），使后续节点 `${params.retro_id}` 非空。
2. **备选**：collect 写固定 sidecar（如 `qa/retro/.active-retro-id`），后续节点用已知路径；outputs 声明与真实路径在 finalize 侧对齐。
3. **禁止**：propose/accept 各自重新 `generate_retro_id()`。

### 4.4 作用域约定

- Retro 是 **project-scoped**：产物前缀一律 `project:qa/retro/<retro_id>/`。
- 不读取、不修改当前 `change_id` 下的 explore/case/plan/execution 产物（除非 nightly 既有逻辑为建 context 而读 archive）。
- Graph 仍可有 `context.change_id`（CLI 启动要求），但对 retro-workflow 仅为会话壳，不是业务输入。

---

## 5. Skill / Prompt

- `skill:aa-retro` 继续使用现有 `aa-retro` SKILL.md（含 write-gate 说明）。
- Graph agent 节点的 prompt 应等价于（或复用）`build_retro_proposal_prompt(retro_id)`，确保要求 `finding_kind` / `payload`。
- Agent 名第一刀用 `aa-doc-author`（与 explore/case-design 一致）；若后续有专用 retro agent 再替换。

---

## 6. CLI 与兼容

| 入口 | 第一刀行为 |
|---|---|
| `aa workflow run --entrypoint archive --change <id>` | 跑 `archive-workflow` |
| `aa workflow run --entrypoint retro`（+ params） | 跑 `retro-workflow` |
| `aa retro nightly collect` | **保留**；可逐步改为调用同一 operation，本刀不强制删除 |
| `aa retro nightly resume` / promote / apply | **不动** |

`full` + `auto_archive=true` 行为与今日等价，仅实现从 inline skill 改为子图调用。

---

## 7. 测试要求

1. **Schema compile**：canonical `workflow-schema.yaml` 含 `archive` / `retro` entrypoints；`workflow.archive` 为 `graph:archive-workflow`。
2. **Archive**：独立 entrypoint 在 gate 满足时跑通（可用现有 fixture / 合同测试）。
3. **Retro collect 零信号**：图成功 END，无 proposals。
4. **Retro 旧形状 proposals**：propose 写出无 `finding_kind` 的 JSON → accept 重写为规范形状（或 fail 映射可重试）。
5. **Retro dry_run**：只 collect，不 invoke agent。
6. **回归**：现有 `intake` / `execute` / `full`（`auto_archive=false`）行为不变。

---

## 8. 非目标（再确认）

- 不把 promote / resume / eval / apply 收进图。
- 不绑 `proposals.json` 的 ingest-artifact-catalog contract。
- 不删除 nightly CLI。
- 不做单 change retro entrypoint。
- 不改 `archive-gate` 的 pass/stop 条件。

---

## 9. 实现顺序（供 writing-plans）

1. Params + entrypoints + `archive-workflow` 抽取（schema-only 可先绿 compile 测试）。
2. 注册 `operation:retro-collect` / `operation:retro-accept` + execution-contracts。
3. 落地 `retro-workflow` 拓扑与 `retro_id` 解析策略。
4. 接 agent prompt / outputs 授权。
5. 单测 +（可选）集成：entrypoint archive / retro。
6. 文档：README / skill 中补充 entrypoint 用法（若现有文档已列 entrypoints）。

---

## 10. 已闭合实现细节（Tasks 1–7 回填）

| # | 议题 | 闭合选择 |
|---|---|---|
| 1 | 零信号路由 | edge `when` 读 `node('collect').value.signal_count`（Task 2 暴露 `task.value`）；`signal_count == 0` → END，成功 NOOP |
| 2 | `accept` 失败 kind | 无法路由的 proposals → `invalid_output`（Task 5 `retro_accept`）；可触发 propose 的 `agent-transient` 重试 |
| 3 | `retro_id` param 类型 | schema 用 `type: str, default: ""`（Task 6）；空串由 `ensure_retro_params()` 在 runtime 注入 |
| 4 | `--change` 要求 | 仍强制（Task 6 CLI）；`change_id` 仅作会话壳，retro 读写 `project:qa/retro/...` |
