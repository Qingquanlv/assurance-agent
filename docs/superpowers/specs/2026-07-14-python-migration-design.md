# assurance-agent：assurance-workflow-skills 的 Python 净室重写设计

日期：2026-07-14
状态：已获用户批准

## 1. 背景与目标

将 `/Users/lvqingquan/skills/assurance-workflow-skills`（TypeScript CLI + Skill 套件，约 181 个 TS 文件 / 3.2 万行）迁移到本仓库 `assurance-agent`，采用 Python 技术栈，项目结构参照 `browser-use`。

已确认的关键决策：

| 决策项 | 结论 |
|---|---|
| 迁移范围 | 一次性全部迁移：CLI + 工作流引擎 + eval + retro + 33 个 skills + OpenCode 集成 |
| CLI 命令名 | `aa`（原 `aws`） |
| Skill / Subagent 前缀 | `aa-*`（原 `aws-*`） |
| 项目配置目录 | `.aa/`（原 `.aws/`） |
| Python 包名 | `assurance_agent` |
| 保真度 | 允许重构：保留核心概念（阶段状态机、gate、产物落盘、healing loop），结构与格式按 Python 习惯重设计；不承诺与 TS 版字节级兼容 |
| 运行形态 | 宿主制（与现状一致）：Python CLI 只做确定性调度 + gate，推理由宿主 Agent 中的 skill 完成 |
| 宿主 | OpenCode 为主：保留 `.opencode/` 集成（agents + JS 插件） |
| 工具链 | uv + pyproject.toml + pydantic v2 + click + ruff + pyright + pytest |
| 实施方法 | **方案 B：净室重写**。以 `schemas/workflow-schema.yaml`、33 个 SKILL.md、README、docs/ 为规格书；TS 代码仅在规格沉默处（Quality Score 权重、失败分类规则表等）作为规则参考查阅，不照抄实现 |

## 2. 总体架构

核心信条不变：**CLI 只做确定性调度（状态机、gate 裁决、Quality Score），推理由宿主 Agent（OpenCode）里的 skill 完成。** 编排采用 Scheme E（subagent-dispatch）语义：`aa status --next --json` 输出下一批可调度阶段（含 agent / skill / kind），gate 与 workflow-state 写入由 CLI 独占。

### 顶层目录

```
assurance-agent/
├── pyproject.toml              # uv 管理; [project.scripts] aa = "assurance_agent.cli:main"
├── .python-version             # 3.11+
├── .pre-commit-config.yaml     # ruff + pyright
├── README.md
├── assurance_agent/            # 唯一 Python 包（对应 TS 的 src/）
│   ├── cli.py                  # click 入口，只做参数解析和分发
│   ├── config.py               # 项目配置加载（.aa/config.yaml）
│   ├── exceptions.py
│   ├── commands/               # 每个子命令一个模块
│   ├── workflow/
│   │   ├── core/               # workflow-state 读写、events.jsonl、case ID 规范、audit
│   │   ├── orchestration/      # schema 加载、DAG 引擎、DSL 求值器、gate 路由、healing loop
│   │   ├── execution/          # pytest / playwright / schemathesis / locust runner 与结果解析
│   │   └── report/             # 失败分类、Quality Score、质量报告生成
│   ├── risk/                   # Explore context 聚合 + advisory 校验
│   ├── eval/                   # AI Eval 框架（executor / scorer / judge / runner）
│   └── retro/                  # 回顾聚合 + nightly driver
├── schemas/                    # workflow-schema.yaml + JSON schemas（规格源，随包分发）
├── skills/                     # 33 个 skill（SKILL.md，见第 8 节）
├── .opencode/                  # OpenCode 集成：agents/*.md + JS 插件 + tools（保留 JS）
├── tests/                      # pytest（unit / integration / eval）
├── examples/                   # 最小可运行示例
├── docker/ bin/ scripts/ benchmark/
└── docs/
```

分层约束（原 dependency-cruiser 职责）用 import-linter 表达：commands → workflow/risk/eval/retro → core，禁止反向依赖。

## 3. 编排引擎（workflow/orchestration/）

以 `schemas/workflow-schema.yaml` 为可执行规格：

- **Schema 加载**：pydantic 模型解析 phases / loops / gates / params；支持项目级 `.aa/workflow-schema.yaml` 覆盖，全新项目必须能用包内默认 schema 工作。
- **DSL 求值器**：schema 中 `when:` / `pass_when:` 等表达式语法接近 Python。不移植 TS 的 tokenizer/parser/AST（5 个文件），改用 Python `ast.parse` + 白名单节点校验的安全求值器：只允许比较、布尔运算、`in`、属性/下标访问，以及内置函数 `gate()`、`file_exists()`、`any()`。表达式中的裸标识符（`decision`、`passed` 等）解析为当前 gate 主 evidence 文件的顶层字段。
- **DAG 引擎**：纯函数——输入 schema + workflow-state + params，输出各阶段状态（`ready` / `done` / `blocked` / `skipped` 等）与下一批可调度阶段（Scheme E dispatch 条目）。`requires_mode: any_active`、`repair_of`、`ready_when` 语义照规格实现。
- **Gate 裁决**：读取 gate 声明的 evidence 文件，按 `needs_fix_when` → `needs_human_review_when` → `reject_when` → `pass_when` 顺序求值；`invalid_json: stop`、`missing_field_is: stop`、`missing_file_is: stop` 容错语义照规格。四态裁决：pass / needs_fix / needs_human_review / reject（+ stop）。
- **Healing loop**：按 `loops.healing` 声明实现——成员阶段、`counter`、`max_param`、`allocate_on`、entry gate（enter/skip/stop）与 exit gate（exit/continue/stop）。attempts 记在 workflow-state；`max_healing_attempts` 耗尽即 STOP 并报告原因。

## 4. 状态与审计（workflow/core/）

- `workflow-state.yaml`：pydantic 模型 + 原子写（临时文件 + rename）+ state hash 校验（防 Subagent 篡改，沿用源版机制语义）。
- `events.jsonl`：append-only、best-effort——写入失败不改变命令退出码。
- Case ID 规范：`TC_MODULE_001`（下划线大写）校验与规范化。
- Skill Load Gate：阶段进入前记录 `skill_loaded` / `skill_md_path` / `skill_loaded_at`，未通过不得置 done。

`qa/changes/<change-id>/` 产物目录结构与源版同构（proposal.md、explore/、cases/、facts/、plans/、review/、codegen/、execution/runs/<batch-id>/、inspect/、report/、healing/、archive/）；具体字段允许 Pythonic 重设计。多级知识库（L1 `.aa/data-knowledge.yaml` → L4 qa/archive/）概念保留；codegen 阶段硬性要求 L1 存在。

## 5. CLI 命令面（commands/）

click group，与源版命令一一对应，仅改名：

```
aa init [--repair]          aa doctor [--json]         aa config print
aa status --change <id> [--next] [--json]
aa gate check --change <id> --phase <phase>
aa state apply --change <id> --phase <phase>           aa state heal ...
aa run --change <id>        aa report inspect|generate --change <id>
aa risk context|validate-advisory --change <id>
aa skill refresh [--sync-agents]
aa eval ...                 aa retro ...
aa decide ...               aa heal ...                aa validate ...
aa workflow ...
```

命令清单以源版 `src/commands/` 的 16 个命令模块为准，一一对应迁移（config、decide、doctor、eval、gate、heal、init、report、retro、risk、run、skill、state、status、validate、workflow），仅命令名前缀由 `aws` 改为 `aa`。

`aa status` / `aa gate check` / `aa run` best-effort 追加 events.jsonl。统一退出码约定（0 成功 / 非 0 分级，具体码表实现时定义并写入 docs）。

## 6. 执行层（workflow/execution/）

四种 runner，`subprocess` 调用 + 结果解析：

| 层 | 框架 | 目录 |
|---|---|---|
| API | pytest | tests/api/ |
| E2E | pytest-playwright | tests/e2e/ |
| Fuzz | schemathesis (via pytest) | tests/fuzz/ |
| Performance | Locust | tests/perf/ |

结果落 `execution/runs/<batch-id>/`（append-only 主证据源）+ 顶层最新指针文件。被测项目测试栈本身是 Python，可直接采用 pytest `--json-report` 插件协议等原生机制。

## 7. 报告层（workflow/report/）

- **失败分类器**：11 类分类（locator_failure、wait_strategy_failure、test_code_error、test_data_failure、fuzz_*、perf_*、known_product_issue、coverage_gap、assertion_failure、business_logic_failure）。分类规则表从 TS 源码提取为 YAML 规则数据并文档化，成为规格的一部分。
- **Quality Score**：CLI 确定性计算，LLM 不参与。权重公式从 TS 提取并写进 docs/schemas.md。
- **Quality Gate**：四态 PASS / PASS_WITH_WARNINGS / FAIL / SKIPPED，worst-wins 跨维度合并。
- 报告三件套：quality-report.json / quality-report.md / executive-summary.md。

## 8. Skills 迁移（33 个）

33 个 skill 目录中 32 个为 `aws-*`，另有 1 个 `writing-skills` 元技能（无前缀，原名迁移）。系统性改写而非照搬：

1. 前缀 `aws-*` → `aa-*`；CLI 调用 `aws ...` → `aa ...`；`.aws/` → `.aa/`。
2. 涉及 npm build/link 的说明改为 uv 说明。
3. `aa-dashboard` 的 server.cjs 改写为 Python 单文件静态服务器（`http.server`），消灭一处 Node 依赖。
4. 迁移完成后运行 skill 间交叉引用链接检查脚本。

## 9. OpenCode 集成（.opencode/）

- `agents/*.md`（6 个 subagent 角色：aa-doc-author、aa-reviewer、aa-test-author、aa-reporter、aa-archiver、aa-intake-host）改名迁移；权限约束保留（禁止 `aa gate` / `aa status`、禁止写 workflow-state.yaml）。
- JS 插件（原 aws.mjs，负责 skill 注册与 `workflow_start` tool）保留 JS 形态，调用目标改为 `aa` CLI。OpenCode 插件必须是 JS，属宿主约束。
- `aa skill refresh --sync-agents` 同步 agents + tools 资产到目标项目。

## 10. Eval 与 Retro

- **eval/**：净室重写——dataset loader、executor（subprocess 驱动 OpenCode 跑真实工作流）、scorer（workflow_case / codegen / full 等）、LLM judge（httpx 直调 API）、报告（JSON + HTML）。`docs/eval.md` 为权威规格，随迁移更新命令名。
- **retro/**：归档读取、聚合、eval 趋势、nightly driver（phase A/D/F），按源版 README/docs 描述的行为重写。

## 11. 测试策略

净室重写下，测试即行为规格的执行版：

- `tests/unit/`：DSL 求值器（与 schema 中每一条表达式对拍）、gate 四态裁决、DAG 引擎推进、失败分类规则表、Quality Score。
- `tests/integration/`：每个 CLI 命令在真实临时目录跑（`aa init` → 造 fixture → `aa status --next --json` 等）；场景清单参考源版 tests/integration 覆盖面，断言重写。
- golden 用例：以 `qa/changes/<id>/` fixture 驱动完整状态机推演，覆盖 healing loop 进入 / 退出 / 耗尽三条路径。

## 12. 错误处理

- CLI 统一退出码约定。
- Gate evidence 缺失 / JSON 无效按 schema 声明的 `missing_file_is` / `invalid_json` 语义处理。
- events.jsonl 写入永不抛出。
- 有界重试：max_case_fix_attempts / max_plan_fix_attempts / max_healing_attempts，超限 STOP 并报告精确原因；`force_continue` 不得绕过 codegen 硬门禁。

## 13. 实施里程碑

一次性全迁，实现按依赖序推进：

1. 脚手架：pyproject + 包骨架 + `aa init/doctor/config`
2. 编排核心：schema 加载 + DSL + DAG + gate + state（引擎可独立测试）
3. `aa status/gate/state` 命令 + risk（Explore）
4. 执行层 + `aa run` + report/inspect
5. skills 全量改写 + `.opencode/` 集成 + `aa skill refresh`
6. eval + retro
7. 文档（README 重写为 Python 版）+ examples

## 14. 明确不做（Out of Scope）

- npm 发布链路、jest 配置、dist/、`.history/`、`.scratch/` 等 TS 构建遗产。
- 与 TS 版产物的字节级兼容（概念与目录结构同构即可）。
- 除 OpenCode JS 插件与必要的 dashboard 前端资源外，不新增任何 Node/TS 代码。
