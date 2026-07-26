# Schema 契约

本页是打包机器契约的权威人类说明。`assurance_agent/_resources/schemas/` 下的文件是机器可读的事实源，本页只解释它们的角色与用户可见行为，不复制字段约束。当本页与机器契约冲突时，以机器契约为准。

## 打包文件

| 机器契约 | 适用对象 | 用途 |
|---|---|---|
| `schemas/workflow-schema.yaml` | 工作流 entrypoints / graphs / gates / params / policies（schema v2） | 随 CLI 分发的运行期编排契约，可被项目 schema 覆盖 |
| `schemas/execution-contracts.yaml` | 节点 target（`skill:*` / `operation:*` / `builtin:*`） | 资源 claim、写授权与可重试错误的执行契约目录，可被项目 `.aa/execution-contracts.yaml` 整体覆盖 |
| `schemas/explore-advisory.schema.json` | `qa/changes/<id>/explore/advisory.json` | Explore advisory 产物的 JSON Schema 参考 |
| `schemas/explore-context.schema.json` | `qa/changes/<id>/explore/context.json` | 聚合 explore context 产物的 JSON Schema 参考 |

JSON Schema 文件是给非 Python 消费者的参考。运行期产物校验由 `assurance_agent/artifacts/` 下的 pydantic 模型 + 路径注册表实现，`aa validate` 与 risk 语义检查都消费同一注册表（唯一契约来源；任何模块禁止私开字典解析同一产物）。

## Workflow schema 解析顺序

无显式覆盖时，CLI 按此顺序解析 workflow schema：

1. 项目 `.aa/workflow-schema.yaml`；
2. 项目 `schemas/workflow-schema.yaml`；
3. 包内默认 `schemas/workflow-schema.yaml`。

显式 `--schema` 覆盖是**排他**的：路径缺失即报错，不回退到隐式候选。

## Workflow schema v2 编排词汇

打包 schema 为 `schema_version: "2"`。根级键仅允许 `schema_version` / `name` / `params` / `entrypoints` / `policies` / `graphs` / `gates`；v1 的 `phases:` / `loops:` 在加载期即被拒绝。语义由 `assurance_agent/workflow/graph/`（GraphRuntime）实现，此处只描述用户可见行为；字段约束以 `workflow/graph/schema_v2.py` 的 pydantic 模型为准。

- **params**——类型化运行参数（`enum` / `list` / `bool` / `int` / `str` + `default`），invocation 级可覆盖，未声明的 param 直接拒绝。
- **entrypoints**——图的入口：`graph` 指定目标图；`allow` DSL 在启动前裁决 params；`with` 固化 param 覆盖；`restart: once|repeatable` 决定完成后能否再跑。
- **policies**——命名 `retry`（`max_attempts` / `retry_on` / `backoff`）与 `timeout`（`run_seconds` / `heartbeat_seconds`）策略，node 按名引用；`scheduler.max_parallel_tasks` 控制同一 superstep 的并行上限。
- **graphs**——拓扑主体：`nodes` + `edges`（可带 `when`）+ `routes`（DSL `select` 多路分派 + `cases`/`default`）+ `state`（带 reducer）/ `budgets` / `exports`；图可嵌套（node `uses: graph:<id>`）。
- **gates**——声明式裁决定义；node 用 `gate:` 在成功提交后裁决，或用 `builtin:gate` 节点做前置裁决。

节点（NodeDef）词汇速查：

| 字段 | 作用 |
|---|---|
| `uses` | 执行体：`skill:<name>`（agent 推理）、`operation:<name>`（进程内确定性操作）、`builtin:join\|gate\|interrupt`、`graph:<id>`（子图） |
| `agent` | skill 节点使用的 subagent 角色 |
| `when` | DSL 条件，不满足则跳过该节点 |
| `outputs` | 声明产物路径（`change:` / `project:` / `repo:` 前缀） |
| `gate` | 节点成功提交后求值的 gate |
| `retry` / `timeout` | 引用 policies 中的命名策略 |
| `with` | 静态参数，原样进 task input（operation 唯一的参数化通道） |
| `resources` | `reads` / `writes` / `exclusive` 资源声明，供并行冲突判定与写授权收窄 |
| `evidence` | 引用上游节点已冻结输出作为本节点输入证据 |
| `state_writes` | 把 task value 写入图 state（按声明的 reducer 合并） |
| `join` | `builtin:join` 的汇聚声明（`all` / `all_active` / `any`） |
| `fan_out` | 运行期按 DSL 求值 `items` 动态展开 child task（map），可配 `reduce` 汇聚；展开即冻结，源漂移 fail closed |
| `budget` | 业务预算消耗（如 fix 次数上限），耗尽路由到 `exhausted_to` 节点 |
| `interrupt` | `builtin:interrupt` 的人工中断声明：`actions` 列表，`resume` 时由人选择并续跑 |
| `recover` | retry 耗尽后的降级路径：经 `via` operation 记录后 `continue_to` 指定节点 |

**DSL**——`allow` / `when` / `select` / gate 表达式共用一套白名单 AST 解释器（非 `eval`，有长度/深度上限）：`len` / `defined` / `file_exists` / `gate(id)` / `node(id)` / `capabilities_present(review, dk)` / `plan_review_route(node)` / `any` / `all` / `count`；比较运算仅 `==` `!=` `<` `<=` `>` `>=` `in` `not in`。

**执行体目录与自定义**——`schemas/execution-contracts.yaml` 是全部 `skill:*` / `operation:*` / `builtin:*` target 的唯一目录：每条声明 handler、资源 claim、写授权（`authorization_writes`）与可重试错误；未登记的 target 编译期报错，写范围不可推导的保守为 `global:exclusive`。自定义路径：

- **换图/改图（零引擎代码）**：项目放 `.aa/workflow-schema.yaml`（或 `schemas/workflow-schema.yaml`）即可整体替换拓扑；运行中改图会产生新 digest，旧 invocation 拒绝普通 resume（fail closed）。
- **新增 skill 节点（零引擎代码）**：skill 文件 + contracts 加条目，node 声明 `uses: skill:<name>` 与 `agent:`。
- **新增 operation（需改引擎，三步）**：`workflow/graph/handlers/operation.py` 的 `default_operations()` 注册 callable → contracts 加条目 → schema 中 `uses: operation:<name>`。operation 的参数经 node `with:` 传入，由函数自行校验。

## Checkpoint 与恢复语义（schema v2）

权威状态只有 `events.jsonl`（append-only ledger）；`workflow-state.yaml` 是从 ledger 投影的人工/报告视图，运行时决策从不读它；`driver.json` 只是非权威进程指针（锁 + 最近已知的 `invocation_id` / `checkpoint_id` / `event_seq`）。主循环按 superstep（Plan → Execute → Update）推进：每次提交的 `checkpoint_id` 是 canonical digest，恢复不读快照——重跑 `aa workflow run --change <id>` 即从 ledger 重投影、跳过已成功 task 继续。运行中途修改 schema YAML 会触发 digest 漂移并在下一 superstep 中止（`GraphDefinitionChanged`，exit 40），已提交的 ledger 不受影响。`aa status` 从 ledger 投影 GraphStatus。

## 校验 change 产物

`aa validate` 不调用 LLM，确定性校验结构化 change 产物：

```text
aa validate --change <id> [--phase <phase>] [--artifact <relpath>] [--json]
```

- 默认校验 `qa/changes/<id>/` 下每个被识别的产物，用 `assurance_agent/artifacts/registry.py` 的路径注册表匹配。
- `--phase <phase>` 限定到该阶段 `produces` 声明的产物。
- `--artifact <relpath>` 校验单个 change 相对文件。
- `--json` 输出机器可读的 `{ ok, results }`；每个 result 为 `{ path, artifact_type, ok, errors[] }`。

退出码：全部通过 `0`；校验失败（含 change、所请求 artifact 缺失、显式请求未注册 artifact，或扫描不到任何注册产物）`1`；用法错误（如未知 phase）`2`。人类可读输出逐产物报告及其错误；“零个注册产物”必须 fail closed，不能形成 CI 假绿。

产物兼容性分级（见注册表 `compat` 字段）：

- `must_compat`——skill 直接读写、gate 表达式直接引用的字段（如 review 的 `decision`/`auto_fix_allowed`/`codegen_readiness`，failure-analysis 的 `fix_proposal_eligible`）：字段名与枚举取值不得改。
- `versioned`——带 `schema_version`、允许结构演进的 CLI 产物（如 `workflow-state.yaml`、execution manifest）。
- `free`——纯 CLI 内部产物（报告 markdown 排版、events 扩展字段），不进注册表、不校验。

## Data knowledge（L1 / L2）

**L1（repo 级）**：`.aa/data-knowledge.yaml` — 正式领域知识库，注册于 `assurance_agent/artifacts/repo_registry.py`，由 `aa knowledge validate`（无 `--change`）与 `aa knowledge promote` 消费。

**L2（change 级）**：`plans/data-knowledge.proposal.<layer>.yaml`（`layer ∈ {api,e2e}`）— 规划/评审阶段的增量提案，注册于 change-relative `artifacts/registry.py`。`mode: bootstrap` 表示 L1 缺失首生；`mode: delta` 表示 L1 存在但缺 leaf。

```text
aa knowledge validate [--project-dir] [--change <id>] [--proposal <path>]
aa knowledge promote [--project-dir] (--change <id> | --from <proposal-path>) [--yes] [--force]
```

- `validate` 从 registry 解析 pydantic 模型，不维护平行映射。
- `promote` 将 L2 leaf merge 进 L1（剥离 proposal-only metadata）；冲突写入 `promote-conflicts.json`，需 `--force` 才覆盖。
- API/E2E plan-review gate 通过 `required_capabilities[]`（review JSON 中的 leaf dotted keys）与 L1 做 pre-codegen 能力校验；缺 leaf → `needs_human_review` + **knowledge-remediation** checkpoint（人工 promote 后 `fix_and_proceed` 重跑 review）。
- Fuzz/Performance plan-review gate 读 review JSON 的 `layer_applicable`：被 proposal 选中但无对应 `type:Fuzz`/`type:Performance` case（空 scope）时 reviewer 置 `layer_applicable: false` → gate 走 `skip`（分支结束、codegen 跳过），而非硬 `reject` 拖垮整条并行链。缺失该字段时按原 `pass`/`reject` 语义处理。

## Retro（schema-v2）与 Improvement lifecycle

Retro 是**独立入口**（`aa workflow run --entrypoint retro` / `aa retro`），不挂在 full workflow 上。当前 run 只读写 `qa/retro/<retro-id>/`；生产路径不扫描、不迁移、不消费历史 Retro 目录。

### 当前 run 产物

| 文件 | 写入方 | 说明 |
|---|---|---|
| `qa/retro/<id>/context.json` | `operation:retro-collect` | schema_version `"2"`：冻结 window、`source_manifest`、signals、integrity |
| `qa/retro/<id>/proposal-candidates.json` | `skill:aa-retro` | Candidate 批（非权威）；须 pin `context_sha256` |
| `qa/retro/<id>/retro-summary.md` | `skill:aa-retro` | 人类摘要 |
| `qa/retro/<id>/accept-status.json` | `operation:reconcile-improvements` | 批级 receipt（accepted/failed + digests/event ids） |
| `qa/retro/<id>/review-queue.md` | reconcile | 指向本批 canonical Improvement IDs |

### Window 选择

互斥：`--change`（显式 Change 集） / `--since`+`--until`（时间窗） / `--last N`（默认 10）。`--dry-run` 只 collect，跳过 propose/reconcile。

### Improvement kinds 与 deliveries

| kind | 允许的 delivery |
|---|---|
| `prompt_improvement` | `memory_patch` |
| `fixture_improvement` / `test_improvement` | `memory_patch` 或 `change_draft` |
| `workflow_improvement` | `change_draft` |
| `domain_knowledge` | `knowledge_delta` |

Project Improvement Ledger（唯一权威）：

| 路径 | 角色 |
|---|---|
| `qa/improvements/events.jsonl` | 追加事件 |
| `qa/improvements/improvements.json` | 确定性投影 |
| `qa/improvements/review-queue.json` | 待审队列 |

独立入口：`improvement-review` / `improvement-evaluate` / `improvement-export` / `improvement-apply` / `improvement-rollback`。CLI 读模型：`aa improvement list|show`（只读投影，不扫 `qa/retro/`）。

Delivery 落点：

- `memory_patch` → `.aa/memory/<skill>.md`（evaluate → apply/rollback；运行时 `load_skill_memory` 注入，8 KiB 上限，过滤 `deprecated:`）
- `change_draft` → `qa/improvements/drafts/<id>.md`（人工落地 Change 后 `record-*-applied`）
- `knowledge_delta` → `qa/improvements/knowledge-delta/<id>.proposal.yaml`（**不**写 L1；合入 L1 仍走 `aa knowledge promote`，且要求引用的 Problem 已 `resolved` + `human_confirmed` + 有 verification scope）

已删除的旧路径（无生产读/写）：`aa retro nightly`、`export-issues` / `export-knowledge`、`qa/retro/_state.json`、`cross-run-report.json`、per-run `promotions.json` / `issue-drafts/**`、枚举 `workflow_bug` / `issue_export`。

## Issue lifecycle (Change Issue Ledger + Project Problem Ledger)

Issue lifecycle replaces the retired Markdown/JSON known-product side channel. Canonical state lives in two append-only Ledgers and their deterministic projections:

| Scope | Ledger | Projection | Role |
|---|---|---|---|
| Change | `qa/changes/<id>/issues/events.jsonl` | `qa/changes/<id>/issues/snapshot.json` | Immutable Observations and Occurrences for one Change |
| Project | `qa/issues/events.jsonl` | `qa/issues/problems.json`, `qa/issues/review-queue.json` | Cross-Change Problem identity, lifecycle, and human review queue |

Only deterministic reconciler/review apply operations append Ledger events. Inspect/classifier artifacts (`inspect/failure-analysis.json`) and the legacy `known_product_issue` execution label are **classification hints only** — they do not read or write legacy known-product issue files and do not mutate Problems by themselves.

Risk context reads structured Problems from `qa/issues/problems.json` (not archived Markdown/JSON). Reports separate execution `final_status` from Issue risk (`report/quality-report.json` schema 1.1 `issues` section). Open or unknown Issues never block archive; they affect archive status wording only.

## Quality Score 与 Quality Gate

Quality Score 由 CLI **确定性**计算，LLM 不参与。Quality Gate 四态：`PASS` / `PASS_WITH_WARNINGS` / `FAIL` / `SKIPPED`，跨维度 worst-wins 合并。

维度与权重（从 TS 源提取，落地在 report 模块与打包规则数据；权重与阈值以代码/数据文件为准，本表为说明）：

| 维度 | 计入 | 说明 |
|---|---|---|
| functional | 权重最高 | API / E2E / Fuzz 用例通过率（含 unmapped_tests 惩罚） |
| coverage | 阈值门禁 | 行 / 分支 / 模块 / diff 覆盖率对 `.aa/config.yaml` 阈值 |
| non_functional | 独立维度 | Performance（Locust 绝对阈值：p95、error_rate） |

`score_breakdown` 各维度取值为浮点分数或 `"N/A"`（该维度未启用）。报告三件套：`quality-report.json` / `quality-report.md` / `executive-summary.md`。

## 失败分类

`aa report inspect` 把执行失败归入固定分类（规则表数据从 TS 源提取为打包 YAML 规则数据 `assurance_agent/_resources/rules/failure-classification.yaml`，随包分发并有单测对拍）。分类决定该失败是否 `fix_proposal_eligible`（进入 Healing Loop）。可自愈类（如 `locator_failure`、`wait_strategy_failure`、`test_code_error`、条件性 `test_data_failure`）与不可自愈类（如 `assertion_failure`、`business_logic_failure`、`known_product_issue`、`coverage_gap`、`fuzz_*`、`perf_*`）的完整清单见规则数据文件与 README「失败分类速查」。

## 维护规则

先改机器 schema / 运行期 pydantic 模型的字段约束，再更新本页——且只在契约的**用途、产物映射、解析顺序或用户可见校验行为**变化时更新。不要把完整字段清单复制到本页。
