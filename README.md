# assurance-agent

**assurance-agent** 是一套面向 AI 驱动 QA 工作流的 **确定性 CLI（`aa`）+ Skill 套件**，宿主为 OpenCode。它把测试基础设施脚手架、Explore 研判、Case 设计、Fact Baseline、测试规划、代码生成、执行、质量门禁、失败归因、Healing 与质量报告串成一条**可审计、可追踪、可重放**的流水线。

核心信条：**CLI 只做确定性调度（graph 状态机、gate 裁决、Quality Score），推理由宿主 Agent（OpenCode）里的 skill 完成。** 编排由 GraphRuntime（schema v2）驱动：CLI 按声明式 graph schema 做 Plan → Execute → Update 调度，`aa status --next --json` 输出 ledger 投影的待办（pending tasks / pending interrupts），gate 裁决与 `events.jsonl` 写入由 CLI 独占。

> 这是 TypeScript 版 `assurance-workflow-skills` 的 Python 净室重写：命令名由 `aws` 改为 `aa`，项目配置目录由 `.aws` 改为 `.aa`，技能前缀由 `aws-*` 改为 `aa-*`，构建链从 npm 换成 uv。

---

## 安装

需要 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)。

### 方式一：从源码用 uv 安装为工具（推荐）

```bash
git clone https://github.com/Qingquanlv/assurance-agent.git
cd assurance-agent
uv tool install .
aa --version
```

### 方式二：从源码用 uv pip 安装到当前环境

```bash
cd assurance-agent
uv pip install .
aa --version
```

### 方式三：仓库内开发运行（不安装）

```bash
uv sync
uv run aa --version
```

---

## 快速开始

```bash
cd your-project        # 被测项目（SUT）根目录
aa init --yes          # 生成 .aa/ 配置 + qa/ 目录 + tests/ 脚手架 + OpenCode 集成
aa doctor              # 环境自检：ok / warning / error
aa doctor --json       # 机器可读自检
```

`aa init` 生成：

```
.aa/config.yaml                # 项目配置
.aa/execution-policy.json      # 执行策略
.aa/module-map.yaml            # 变更文件 → QA 模块映射
.aa/data-knowledge.yaml        # L1 静态领域知识（人工维护）
qa/cases/  qa/changes/         # Case 库与变更工作区
tests/api/ tests/e2e/ tests/fuzz/ tests/perf/   # 测试栈脚手架
opencode.json                  # OpenCode 插件注册（./.opencode/plugins/aa.mjs）
.opencode/agents/ .opencode/tools/ .opencode/plugins/   # OpenCode 资产
skills/                        # 同步的 aa-* skill 套件
```

新装环境的资源解析不依赖源码仓库：`aa init` 从 wheel 内 `_resources/` 加载默认 schema 与技能资产（见「资源分发」）。

### 工作流总览

打包 schema 为 **`schema_version: "2"`**（GraphRuntime）：`entrypoints` + `graphs` 拓扑；同超步内真并行与资源序列化；task retry 与业务 budget 分计；interrupt/resume；严格 `events.jsonl` 为权威；eval mid-graph 仅经 validated import-manifest。

一个变更（change）从 `explore` 走到 `report`，每阶段落结构化产物到 `qa/changes/<change-id>/`；CLI 用确定性状态机推进：

```bash
aa status  --change <id> --next --json              # ledger 投影的待办（pending tasks / interrupts）
aa gate    check --change <id> --node-path <path>   # 读取该节点冻结的 gate 报告（不重裁）
aa run     --change <id>                            # 执行测试（写 execution/runs/<batch-id>/）
aa report  inspect  --change <id>                   # 失败分类 + quality-gate-result.json
aa report  generate --change <id>                   # Quality Score + 报告三件套
aa workflow run --change <id> --entrypoint full --adapter opencode --server http://127.0.0.1:4096
```

`aa workflow run` 是完整的确定性 driver（主循环 + gate + 状态推进），不是 `status --next` 的别名。

编排 schema（v2）的节点级词汇——`retry`/`timeout` 命名策略、`budget` 业务预算、`fan_out` 动态展开、`routes` 多路分派、`interrupt` 人工中断（语义见 `docs/schemas.md`「Workflow schema v2 编排词汇」）。恢复语义：ledger（`events.jsonl`）是唯一权威，重跑 `aa workflow run` 即从 ledger 重投影续跑（`aa status` 可见 checkpoint 与 event_seq）。

---

## 命令参考

所有命令的统一退出码约定：`0` 成功；其余按语义分级（见各命令说明与 `docs/schemas.md`）。

| 命令 | 说明 |
|---|---|
| `aa init [--repair] [--yes]` | 初始化 QA 项目结构；`--repair` 仅补齐缺失文件；`--yes` 取默认值非交互 |
| `aa doctor [--json]` | 环境与配置自检；有 error 退出 1 |
| `aa config print` | 原样打印 `.aa/config.yaml` |
| `aa validate --change <id> [--phase <p>] [--artifact <rel>] [--json]` | 确定性校验 change 产物；退出码 0 通过 / 1 失败、缺失或零注册产物 / 2 用法错误 |
| `aa status --change <id> [--next] [--json]` | GraphStatus 投影（pending tasks / interrupts / next retry）；`--next` 只打印待办；退出码 0 running/completed / 20 stopped / 30 interrupted / 40 failed（命令或数据错误为 40） |
| `aa trace --change <id> [--json] [--type API\|E2E\|Fuzz\|Performance] [--only-gaps]` | 现场 fold execution 相位 trace 投影（只读；与 status 的 ledger 投影分离） |
| `aa verify --change <id> [--json]` | 现场 fold reconciled 相位投影并裁决证据充分性；退出码 0 pass / 30 needs_human / 40 fail |
| `aa gate check --change <id> --node-path <p> [--json]` | 返回该节点 ledger 冻结的 gate 报告（拒绝重裁可变文件）；退出码 0 pass/enter/exit/skip / 30 needs_fix/needs_human_review/continue / 40 reject/stop |
| `aa state ...` | 非图进度辅助（如 configure）；禁止用 apply/heal 伪造进度 |
| `aa decide --change <id> ...` | 非图策略决定（如 `allow_test_changes`）；图内人工裁决走 `workflow resume --interrupt` |
| `aa risk context --change <id> [--project-dir <root>]` | 聚合 diff / cases / archive → `explore/context.json` |
| `aa risk validate-advisory --change <id>` | 校验 `explore/advisory.json` 与 context |
| `aa run --change <id>` | 按 `selected_targets` 执行测试，写 batch 结果 + 顶层指针 |
| `aa report inspect --change <id>` | 失败分类 → `inspect/failure-analysis.json` + `quality-gate-result.json` |
| `aa report generate --change <id>` | Quality Score → `report/` 三件套 |
| `aa heal ...` | Healing 支持命令（fix-proposal 校验等） |
| `aa workflow run --change <id> --entrypoint full\|intake\|execute\|case\|archive\|retro --adapter opencode\|headless [...]` | GraphRuntime 主循环；退出码 0 completed / 20 stopped / 30 interrupted / 40 error |
| `aa workflow run --detach ...` | detached 后台启动（OpenCode `workflow_start`；立刻返回启动成败） |
| `aa workflow status --change <id>` | deprecated 别名（隐藏，一个版本后移除）；用 `aa status` |
| `aa workflow resume --change <id> [--interrupt <id> --action <a> --reason <text>]` | 续跑或解决 interrupt；unbound legacy commit-safety work may return `legacy_commit_safety_semantics_unbound` |
| `aa workflow import-checkpoint --change <id> --manifest <path>` | 校验后导入 fixture/benchmark checkpoint |
| `aa workflow supersede --change <id> --action rerun-v6\|stop --who <who> --reason <text>` | Sole audited legacy exit: `rerun-v6` starts one replacement root; `stop` is terminal only |
| `aa skill refresh [--sync-agents] [--dry-run]` | 同步 skills 到 `skills/`（始终）；`--sync-agents` 追加 `.opencode/{agents,tools,plugins}` |
| `aa eval run\|plan\|report ...` | AI Eval 框架（权威文档 `docs/eval.md`） |
| `aa retro [--change <id>... \| --since/--until \| --last N] [--retro-id <id>] [--dry-run] [--json]` | 触发独立 Retro 图；写当前 run 的 `qa/retro/<id>/context.json`（及 propose/reconcile 产物） |
| `aa retro show --retro-id <id> [--json]` | 只读展示一个显式当前 run（不扫描其他 Retro） |
| `aa improvement list [--state\|--kind\|--delivery] [--json]` | 从 Project Improvement Ledger 列出 Improvements |
| `aa improvement show --id <id> [--json]` | 展示单个 Improvement 投影 + 事件时间线 |
| `aa knowledge validate [--project-dir] [--change <id>] [--proposal <path>]` | 校验 L1/L2 data-knowledge 产物 |
| `aa knowledge promote [--project-dir] (--change <id> \| --from <path>) [--yes] [--force]` | 将 L2 proposal merge 进 L1 |

---

## 工作流入口（entrypoints）一览

打包 schema（v2）声明以下入口（`aa workflow run --entrypoint <name>`）：

| Entrypoint | 图 | 说明 |
|---|---|---|
| `full` | workflow | 全流程：bootstrap → intake → assurance（`params.auto_archive=true` 时续 archive） |
| `intake` | intake-workflow | bootstrap + intake：explore → case-design → case-review-cycle |
| `case` | intake-workflow | intake 快捷入口（`with run_mode=case-only`） |
| `execute` | execute-workflow | bootstrap + assurance：fact-baseline → 四层分支 → 执行 → inspect → healing → report |
| `archive` | archive-workflow | 归档 precheck gate + `aa-archive` |
| `retro` | retro-workflow | 独立 Retro：collect → propose → reconcile |
| `issue-review` / `issue-analyze` / `issue-reconcile` | issue-* | Issue 生命周期（`restart: repeatable`） |
| `improvement-review` / `-evaluate` / `-export` / `-apply` / `-rollback` | improvement-* | Improvement 交付生命周期（`restart: repeatable`） |

`assurance` 图内部按 `test_types` / `run_mode` 裁剪：fact-baseline 之后 api / e2e / fuzz / performance 四个分支并行（各为 plan → review-cycle → codegen），join 后执行测试，再经 inspect-with-issues → healing → report。每个测试脚本必须与 `case.yaml` 的 Case ID 绑定（规范：`TC_MODULE_001`，下划线大写）。

正式 DAG 定义见打包的 `workflow-schema.yaml`（解析顺序见 `docs/schemas.md`）；自定义图与节点词汇见 `docs/schemas.md`「Workflow schema v2 编排词汇」。

---

## 每阶段结构化产物

```
qa/changes/<change-id>/
├── proposal.md
├── workflow-state.yaml        ← ledger 投影的人工/报告视图（运行时决策不读它）
├── events.jsonl               ← append-only ledger（strict / best-effort 双模式，唯一权威）
├── driver.json                ← 非权威进程指针（锁 + 最近已知图位置）
├── explore/{context.json, advisory.json}
├── cases/<module>/case.yaml
├── facts/fact-baseline.json
├── plans/…  review/…  codegen/…
├── execution/{execution-manifest.yaml, runs/<batch-id>/, *.json}
├── inspect/{failure-analysis.json, quality-gate-result.json}
├── report/{quality-report.json, quality-report.md, executive-summary.md}
├── healing/{fix-proposal.json, fixer-safety-check.json, *-apply-summary.json}
└── archive/
```

结构化产物的字段契约集中在 `assurance_agent/artifacts/`（唯一来源）；`aa validate` 据此校验。多级知识库：L1 `.aa/data-knowledge.yaml`（人工维护，codegen 硬性要求）→ L4 `qa/archive/<change-id>/`。

---

## OpenCode 使用方式

```text
skill load aa-workflow

Requirement:
测试菜单管理模块的增删改查功能

Run mode:
full

Test types:
api,e2e
```

`aa init` 会注册 OpenCode 插件（`opencode.json` 的 `plugin` 数组加入 `./.opencode/plugins/aa.mjs`）并复制 6 个 `aa-*` subagent、`workflow_start` tool 与全部 skills。Subagent 权限受 `.opencode/agents/*.md` 约束（禁 `aa gate` / `aa status`、禁写 `workflow-state.yaml`）。

后续刷新技能与资产：

```bash
aa skill refresh                 # 仅同步 skills/
aa skill refresh --sync-agents   # 同步 skills/ + .opencode/{agents,tools,plugins}
aa skill refresh --dry-run       # 只报告将变更的文件，不落盘
```

---

## 资源分发

`schemas/`、`skills/`、`.opencode/` 是运行时资源，唯一源在包内 `assurance_agent/_resources/`，随 wheel/sdist 分发。任何模块只经 `assurance_agent/resources.py`（`importlib.resources`）访问，禁止源码仓库相对路径。workflow schema 解析顺序：项目 `.aa/workflow-schema.yaml` → 项目 `schemas/workflow-schema.yaml` → 包内默认；显式 `--schema` 覆盖是排他的（路径缺失即报错，不回退）。项目级覆盖（自定义图、节点与 execution contracts）的编写指南见 `docs/schemas.md`「Workflow schema v2 编排词汇」。

---

## 开发与测试

```bash
uv sync                       # 安装依赖（含 dev 组）
uv run pytest -v              # 全量测试
uv run ruff check .           # lint
uv run ruff format --check .  # 格式检查
uv run pyright                # 类型检查
uv run lint-imports           # 分层依赖契约（.importlinter）
uv run pre-commit run -a      # 本地一键跑 ruff + pyright
bash scripts/packaging_smoke_test.sh   # 构建 wheel + 全新环境安装 + 源码目录外运行
```

分层契约（import-linter）：`cli → commands → eval → risk → workflow → verification → evidence → artifacts → config → resources`，禁止反向依赖。

核心模块：

| 目录 | 说明 |
|---|---|
| `assurance_agent/commands/` | 每个子命令一个模块（只做参数解析与输出） |
| `assurance_agent/eval/` | AI Eval 框架 |
| `assurance_agent/risk/` | Explore context 聚合与 advisory 校验 |
| `assurance_agent/workflow/graph/` | GraphRuntime：schema v2 编译、Plan/Execute/Update superstep 调度、checkpoint/ledger、handlers |
| `assurance_agent/workflow/orchestration/` | gate 裁决、DSL 解释器、共享 schema 模型（graph 包单向复用其语义） |
| `assurance_agent/workflow/core/` | workflow-state、events、case ID、技能同步 |
| `assurance_agent/workflow/driver/` | `aa workflow run` 主循环、headless/opencode adapter、detached、lock、resume |
| `assurance_agent/workflow/execution/` | pytest / playwright / schemathesis / locust runner |
| `assurance_agent/workflow/report/` | 失败分类、Quality Score、报告生成 |
| `assurance_agent/verification/` | 契约校验、assert_ideal、plan-check 渲染 |
| `assurance_agent/evidence/` | trace fold、证据充分性、verify 裁决 |
| `assurance_agent/artifacts/` | 产物 pydantic 契约 + 路径注册表 + `aa validate` |
| `assurance_agent/retro/` `assurance_agent/workflow/improvements/` | 独立 Retro 证据收集 + Project Improvement Ledger |
| `assurance_agent/_resources/` | 运行时资源唯一源（schemas / skills / opencode） |

---

## 失败分类速查

| 类型 | 可 Healing | 说明 |
|---|:---:|---|
| `locator_failure` | ✓ | 元素定位失效 |
| `wait_strategy_failure` | ✓ | 等待策略问题 |
| `test_code_error` | ✓ | 测试代码错误 |
| `test_data_failure` | 条件 | 测试数据问题 |
| `assertion_failure` | ✗ | 断言失败（可能是产品 bug） |
| `business_logic_failure` | ✗ | 业务逻辑问题 |
| `known_product_issue` | ✗ | 已知产品 bug |
| `coverage_gap` | ✗ | 覆盖缺口 |
| `fuzz_stateful_failure` | review | 服务端 5xx 等 |
| `perf_threshold_exceeded` | ✗ | 压测超阈值 |

完整分类规则表见 `docs/schemas.md` 与打包的失败分类规则数据文件。

---

## Four-layer assurance (v6) notes

- Docs: `docs/schemas.md` (v6 fields / validators / effects) and
  `docs/release-notes/2026-08-four-layer-assurance.md`.
- Eval hard metrics and evidence-export algorithm: `docs/eval.md`.
- Layer selection default is `api` + `e2e` via `selection_normalizer/v1`;
  layers are `api`, `e2e`, `fuzz`, `performance`.
- Declared-only assurance inputs; generated-files validators
  `generated_files_candidate/v1` / `codegen_fix_candidate/v1`; durable effects
  `healing_allocation/v2`, `fixer_proposal_approved/v1`, `heal_record_apply/v2`.
- OpenCode configuration/request binding is what CI proves; third-party sandbox
  enforcement beyond that surface is outside CI proof.
