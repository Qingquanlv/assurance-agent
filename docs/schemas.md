# Schema 契约

本页是打包机器契约的权威人类说明。`assurance_agent/_resources/schemas/` 下的文件是机器可读的事实源，本页只解释它们的角色与用户可见行为，不复制字段约束。当本页与机器契约冲突时，以机器契约为准。

## 打包文件

| 机器契约 | 适用对象 | 用途 |
|---|---|---|
| `schemas/workflow-schema.yaml` | 工作流 phases / gates / loops / params | 随 CLI 分发的运行期编排契约，可被项目 schema 覆盖 |
| `schemas/explore-advisory.schema.json` | `qa/changes/<id>/explore/advisory.json` | Explore advisory 产物的 JSON Schema 参考 |
| `schemas/explore-context.schema.json` | `qa/changes/<id>/explore/context.json` | 聚合 explore context 产物的 JSON Schema 参考 |

JSON Schema 文件是给非 Python 消费者的参考。运行期产物校验由 `assurance_agent/artifacts/` 下的 pydantic 模型 + 路径注册表实现，`aa validate` 与 risk 语义检查都消费同一注册表（唯一契约来源；任何模块禁止私开字典解析同一产物）。

## Workflow schema 解析顺序

无显式覆盖时，CLI 按此顺序解析 workflow schema：

1. 项目 `.aa/workflow-schema.yaml`；
2. 项目 `schemas/workflow-schema.yaml`；
3. 包内默认 `schemas/workflow-schema.yaml`。

显式 `--schema` 覆盖是**排他**的：路径缺失即报错，不回退到隐式候选。

## 编排扩展词汇（retry / fan_out / loop kind）

以下词汇为可选增强，默认打包 schema 未使用；语义由 `assurance_agent/workflow/orchestration/` 实现，此处只描述用户可见行为。

**phase `retry`**——声明式重试策略（`max_attempts` / `backoff_seconds`）。只对**派发调用**（adapter / CLI 执行）的瞬时失败重试，每次重试用全新签名 attempt id；结果提交（state apply）失败永不重试。schema 的 per-phase 策略优先，driver 全局 `max_phase_attempts` 仅作 skill 相位的下限。编排器内置相位禁止声明。

**phase `fan_out`**——动态 fan-out（map/join 语义）。`each` 是对全局 evidence 作用域求值的 DSL 表达式，必须产出 `list[str]`（元素须为文件系统安全的路径段，数量受 `max_items` 约束，默认 32 / 上限 128）。引擎在每次 `compute_status` 投影时把 base 相位展开为 `<base>[<item>]` 子相位（`produces` 中的 `{item}` 模板逐项替换）；下游相位以 base id `requires` 即自动获得对全部子相位的 join（`requires_mode` 语义不变）。契约失败均 fail closed：上游完成后 `each` 仍非 `list[str]`（或元素不安全）→ 相位 `stopped`；`each` 为空列表 → base 视为 done（无工作）。v1 限制：fan-out 相位不得挂 gate / loop / repair_of，不得 require 另一个 fan-out 相位（map 由子相位承担，裁决留给下游汇聚相位）。driver 对子相位串行派发，prompt 中绑定 item。

**loop kind registry**——`loops:` 的 `kind` 不再由引擎特判：每种 kind 是注册到 `loop_registry` 的投影器（内置 `healing` / `review_fix`），引擎只消费统一的 `LoopSnapshot` 协议（dispatch / block_members / control_actions / terminal）。新增 loop kind = 注册一个投影器，无需改引擎。

## Checkpoint 与恢复语义

主循环每个**迭代边界即 checkpoint**：每提交一个相位结果或控制动作，`driver.json` 的 `iteration` 递增并落盘（`last_checkpoint_at` 记录时间）。checkpoint 实体是 `workflow-state.yaml` + `events.jsonl`（状态与审计流），`driver.json` 只是 checkpoint 指针。恢复不需要快照回读——`compute_status` 是对（schema、产物、state、events）的纯投影，重跑 `aa workflow run --change <id>` 即从最近迭代边界重投影继续；`iteration` 计数跨 run 累积（属于 change，而非单次运行）。`aa workflow status` 展示 checkpoint 段与恢复提示。

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

## Retro 三轨与 memory

Retro proposal 用 `finding_kind` / `apply_kind` 分流：

| finding_kind | apply_kind | 落点 |
|---|---|---|
| `prompt_rule` | `memory_append` | `.aa/memory/<skill>.md`（经 promote → eval → apply） |
| `workflow_bug` | `issue_export` | `qa/retro/<id>/issue-drafts/<proposal-id>.yaml` |
| `domain_knowledge` | `knowledge_delta` | `qa/retro/<id>/knowledge-delta/<proposal-id>.proposal.yaml` |

```text
aa retro export-issues --retro-id <id> [--overwrite]
aa retro export-knowledge --retro-id <id> [--overwrite]
```

导出物化后，`knowledge_delta` 经 `aa knowledge promote --from <path> --yes` 合入 L1。运行时 memory 由 `load_skill_memory(project_root, skill)` 注入 graph v2 / legacy prompt（8 KiB 上限，过滤 `deprecated:` 行）。

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
