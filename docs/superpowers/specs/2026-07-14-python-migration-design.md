# assurance-agent：assurance-workflow-skills 的 Python 净室重写设计

日期：2026-07-14
状态：已获用户批准；已纳入首轮评审 5 个 P1 修订（产物契约、DSL 语义、driver 归属、events 双模式、资源分发）

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
| 实施方法 | **方案 B：净室重写**。以 `schemas/workflow-schema.yaml`、33 个 SKILL.md、README、docs/ 为规格书；TS 代码仅在规格沉默处作为规则参考查阅（Quality Score 权重、失败分类规则表、DSL 求值语义、events 双模式、driver 行为、`src/schema/` 产物验证器字段——本 spec 第 3、4、4a、5a 节已把这些规则显式收编为规格），不照抄实现 |

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
│   ├── artifacts/              # 产物数据契约：全部结构化产物的 pydantic 模型 + 路径注册表（见第 4a 节）
│   ├── workflow/
│   │   ├── core/               # workflow-state 读写、events.jsonl、case ID 规范、audit
│   │   ├── orchestration/      # schema 加载、DAG 引擎、DSL 解释器、gate 路由、healing loop
│   │   ├── driver/             # 工作流 driver：dispatch 主循环、adapter、detached 启动（见第 5a 节）
│   │   ├── execution/          # pytest / playwright / schemathesis / locust runner 与结果解析
│   │   └── report/             # 失败分类、Quality Score、质量报告生成
│   ├── risk/                   # Explore context 聚合 + advisory 校验
│   ├── eval/                   # AI Eval 框架（executor / scorer / judge / runner）
│   ├── retro/                  # 回顾聚合 + nightly driver
│   └── resources.py            # 运行时资源定位的唯一入口（importlib.resources，见第 15 节）
├── schemas/                    # workflow-schema.yaml + JSON schemas（唯一源，构建时映射进包）
├── skills/                     # 33 个 skill（SKILL.md，见第 8 节；唯一源，构建时映射进包）
├── .opencode/                  # OpenCode 集成：agents/*.md + JS 插件 + tools（保留 JS；同上映射）
├── tests/                      # pytest（unit / integration / eval）
├── examples/                   # 最小可运行示例
├── docker/ bin/ scripts/ benchmark/
└── docs/
```

分层约束（原 dependency-cruiser 职责）用 import-linter 表达：commands → workflow/risk/eval/retro → core，禁止反向依赖。

## 3. 编排引擎（workflow/orchestration/）

以 `schemas/workflow-schema.yaml` 为可执行规格：

- **Schema 加载**：pydantic 模型解析 phases / loops / gates / params；支持项目级 `.aa/workflow-schema.yaml` 覆盖，全新项目必须能用包内默认 schema 工作。
- **DSL 解释器**：schema 中 `when:` / `pass_when:` 等表达式是一门**独立的小语言**，不是 Python 表达式的安全子集，禁止任何形式的 `eval`。实现上可以借 `ast.parse` 做语法分析（其语法恰好是 Python 表达式语法的子集），但必须自行解释 AST，语义以下述规则为准（与源版 `dsl/evaluator.ts` 行为对齐）：
  - **字面量映射**：`true` / `false` / `null` 是 DSL 关键字，解析为布尔值与空值，不是普通名称。
  - **三值逻辑**：值缺失（文件不存在、字段不存在、路径中断）求值为 `missing`，而非 Python falsy。`missing` 参与比较结果为 `missing`；`and` / `or` / `not` 按三值真值表传播；顶层条件结果为 `missing` 时视为不满足（gate 按 `missing_field_is` 声明处理）。
  - **集合函数**：`any(collection, predicate)` / `all(collection, predicate)` / `count(collection, predicate)` 是二参数形式，第二个参数是隐式 lambda——predicate 中的裸标识符先在**集合元素的字段**中解析（子作用域），再回退到外层作用域。另支持 `len(x)`、`defined(x)`。
  - **作用域与 evidence 别名**：根作用域装载 `params`、`state`，以及 gate `reads` 声明的 evidence 文件——列表首个（或唯一）文件的顶层字段直接进入根作用域，`{ path: ..., as: alias }` 形式按别名装载（如 `qa`、`failure_analysis`）。phase 级 `when:` 条件可引用 `fix_proposal` 等约定别名，装载规则在 schema 文档中逐一列出。
  - **内置函数**：`gate(name)` 递归触发目标 gate 裁决，结果缓存（同一次求值内每个 gate 至多裁决一次），并做环检测（gate 相互引用即报错）；`file_exists(path)` 支持 `repo:` / `qa/` 前缀。
  - **安全与复杂度**：AST 节点白名单（比较、布尔运算、`in`、属性/下标访问、上述内置函数调用），白名单之外的节点在 schema 加载期即报错；表达式长度与嵌套深度设上限。
  - **对拍测试**：打包 schema 中每一条表达式都必须有单测覆盖（含 missing 路径），作为与源版语义对齐的验收标准。
- **DAG 引擎**：纯函数——输入 schema + workflow-state + params，输出各阶段状态（`ready` / `done` / `blocked` / `skipped` 等）与下一批可调度阶段（Scheme E dispatch 条目）。`requires_mode: any_active`、`repair_of`、`ready_when` 语义照规格实现。
- **Gate 裁决**：读取 gate 声明的 evidence 文件，按 `needs_fix_when` → `needs_human_review_when` → `reject_when` → `pass_when` 顺序求值；`invalid_json: stop`、`missing_field_is: stop`、`missing_file_is: stop` 容错语义照规格。四态裁决：pass / needs_fix / needs_human_review / reject（+ stop）。
- **Healing loop**：按 `loops.healing` 声明实现——成员阶段、`counter`、`max_param`、`allocate_on`、entry gate（enter/skip/stop）与 exit gate（exit/continue/stop）。attempts 记在 workflow-state；`max_healing_attempts` 耗尽即 STOP 并报告原因。

## 4. 状态与审计（workflow/core/）

- `workflow-state.yaml`：pydantic 模型 + 原子写（临时文件 + rename）+ state hash 校验（防 Subagent 篡改，沿用源版机制语义）。
- `events.jsonl`：append-only，但**区分两种写入模式**（对齐源版 events.ts 的双模式）：
  - `append_event_best_effort()`——遥测型事件（status 查询、gate 查询、普通运行日志）：写入失败静默降级，不改变命令退出码；
  - `append_event_strict()`——审计型事件（人工 decision、override、dispatch 记录、状态推进证据、healing attempt 分配）：写入失败即命令失败，且**必须回滚同一操作中已做的关联状态修改**（如 decision 写入失败回滚 state 变更、healing attempt 分配采用事务式恢复）。
  - 写入顺序约定：先落 strict 事件、后推进 workflow-state；两步之间失败时以事件为准回滚状态，保证「状态已推进但审计证据不存在」不可能出现。
- Case ID 规范：`TC_MODULE_001`（下划线大写）校验与规范化。
- Skill Load Gate：阶段进入前记录 `skill_loaded` / `skill_md_path` / `skill_loaded_at`，未通过不得置 done。

`qa/changes/<change-id>/` 产物目录结构与源版同构（proposal.md、explore/、cases/、facts/、plans/、review/、codegen/、execution/runs/<batch-id>/、inspect/、report/、healing/、archive/）。多级知识库（L1 `.aa/data-knowledge.yaml` → L4 qa/archive/）概念保留；codegen 阶段硬性要求 L1 存在。字段级的重设计自由度不是无边界的，由第 4a 节的产物契约注册表逐产物裁定。

## 4a. 产物数据契约（artifacts/）

源版在三个公开 schema 之外，还有 `src/schema/` 下约 14 个 zod 运行时验证器（workflow_state、case_yaml、qa_yaml、review、advisory、fact_baseline、execution_manifest、failure_analysis、fix_proposal、quality_gate_result、quality_report、apply_summary 等），`aws validate` 与 risk 语义检查都依赖这个注册表。Python 版必须有对等物，否则 gate、report、eval、skills 会各自重复定义浅层接口。

`assurance_agent/artifacts/` 是**全项目唯一的产物契约来源**：

- 每种结构化产物一个 pydantic 模型（对应源版 14 个验证器逐一迁移）。
- **路径 → 模型注册表**：change 相对路径 glob 映射到模型，`aa validate` 据此发现并校验产物；行为对齐源版 `aws validate`（`--phase` 按 produces 过滤、`--artifact` 单文件、`--json` 输出 `{ ok, results }`、退出码 0/1/2）。
- **逐产物兼容性分级**，在注册表中显式标注：
  - `must_compat`——skills 的 SKILL.md 直接指导 Agent 读写的字段、gate 表达式引用的字段（如 review JSON 的 `decision` / `auto_fix_allowed` / `codegen_readiness`、failure-analysis 的 `fix_proposal_eligible`）：字段名与取值枚举必须与 SKILL.md / workflow-schema.yaml 中的引用一致；
  - `versioned`——结构可改但需带 `schema_version` 字段并提供显式升级说明（如 workflow-state.yaml、execution manifest）；
  - `free`——纯 CLI 内部产物，可自由重构（如报告 markdown 的排版、events 的扩展字段）。
- **单一消费约束**：gate 求值、report 生成、eval scorer、`aa validate` 全部 import 同一模型，禁止在任何模块内私开字典结构解析同一产物。skills 改写时字段引用必须与模型对拍（第 8 节的交叉引用检查扩展为「skill 引用字段 ⊂ 模型字段」检查）。

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

`aa status` / `aa gate check` / `aa run` 按第 4 节双模式追加 events.jsonl（查询类遥测事件 best-effort；decision / dispatch / 状态推进等审计事件 strict）。统一退出码约定（0 成功 / 非 0 分级，具体码表实现时定义并写入 docs）。

## 5a. 工作流 Driver（workflow/driver/）

源版 `aws workflow run` 不是 `status --next` 的别名，而是完整的确定性 driver。Python 版**全量迁移**该模块，职责归属明确为 `assurance_agent/workflow/driver/`：

- **dispatch 主循环**（`loop.py`）：循环调用 DAG 引擎取下一批阶段 → 通过 adapter 委派执行 → gate 裁决 → 推进状态，直至完成 / STOP / needs_human_review。driver 专属退出码：`EXIT_COMPLETED` / `EXIT_STOPPED` / `EXIT_HUMAN_REVIEW` / `EXIT_ERROR`。
- **Adapter 协议**（`adapter.py`）：抽象「把一个阶段交给某个 Agent 执行」。两个实现：
  - `headless_adapter`——subprocess 驱动任意 headless agent CLI（如 `cursor-agent --print`），与 OpenCode 无关；
  - `opencode_adapter`——经 OpenCode server API 派发（`--server`、可选 `--model provider/model`、parent session 复用、鉴权头从环境读取）。
- **Detached 启动**（`workflow_start.py`）：供 OpenCode 插件 `workflow_start` tool 调用的后台启动路径；插件只负责收集参数并调用 `aa workflow run`，循环逻辑不驻留在 JS 侧。
- **Driver 状态与锁**（`driver_state.py`）：driver lock 防同一 change 重复启动；driver 状态文件记录进度供 `aa workflow status` 读取；breakpoint/resume——可在指定阶段暂停，人工介入后续跑。
- **Phase session 生命周期**（`phase_prompt.py` / `process_runner.py`）：每阶段的 prompt 组装、子进程管理与超时。

eval 的 workflow executor 复用本模块驱动真实工作流，不另行实现循环。

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
- JS 插件（原 aws.mjs，负责 skill 注册与 `workflow_start` tool）保留 JS 形态，调用目标改为 `aa` CLI；`workflow_start` 仅收集参数并调用第 5a 节的 detached 启动路径，循环逻辑不驻留 JS 侧。OpenCode 插件必须是 JS，属宿主约束。
- `aa skill refresh --sync-agents` 同步 agents + tools 资产到目标项目。

## 10. Eval 与 Retro

- **eval/**：净室重写——dataset loader、executor（复用第 5a 节 driver 驱动真实工作流，不另行实现循环）、scorer（workflow_case / codegen / full 等）、LLM judge（httpx 直调 API）、报告（JSON + HTML）。`docs/eval.md` 为权威规格，随迁移更新命令名。
- **retro/**：归档读取、聚合、eval 趋势、nightly driver（phase A/D/F），按源版 README/docs 描述的行为重写。

## 11. 测试策略

净室重写下，测试即行为规格的执行版：

- `tests/unit/`：DSL 解释器（与 schema 中每一条表达式对拍，含三值逻辑与 missing 传播）、gate 四态裁决、DAG 引擎推进、artifacts 模型与注册表、失败分类规则表、Quality Score、strict/best-effort 事件写入与回滚。
- `tests/integration/`：每个 CLI 命令在真实临时目录跑（`aa init` → 造 fixture → `aa status --next --json`、`aa validate` 等）；driver 主循环用 fake adapter 走通完成 / STOP / needs_human_review / resume 路径；场景清单参考源版 tests/integration 覆盖面，断言重写。
- golden 用例：以 `qa/changes/<id>/` fixture 驱动完整状态机推演，覆盖 healing loop 进入 / 退出 / 耗尽三条路径。
- 打包冒烟测试：见第 15 节。

## 12. 错误处理

- CLI 统一退出码约定。
- Gate evidence 缺失 / JSON 无效按 schema 声明的 `missing_file_is` / `invalid_json` 语义处理。
- events.jsonl 写入按第 4 节双模式：遥测事件 best-effort；审计事件 strict（失败即命令失败并回滚关联状态修改）。
- 有界重试：max_case_fix_attempts / max_plan_fix_attempts / max_healing_attempts，超限 STOP 并报告精确原因；`force_continue` 不得绕过 codegen 硬门禁。

## 13. 实施里程碑

一次性全迁，实现按依赖序推进：

1. 脚手架：pyproject（含资源打包配置，第 15 节）+ 包骨架 + `aa init/doctor/config`
2. 产物契约：`artifacts/` 全部 pydantic 模型 + 注册表 + `aa validate`
3. 编排核心：schema 加载 + DSL 解释器 + DAG + gate + state/events（引擎可独立测试）
4. `aa status/gate/state/decide` 命令 + risk（Explore）
5. 执行层 + `aa run` + report/inspect + `aa heal`
6. 工作流 driver（`aa workflow run`，headless/opencode 双 adapter、detached、lock、resume）
7. skills 全量改写 + `.opencode/` 集成 + `aa skill refresh`
8. eval + retro
9. 文档（README 重写为 Python 版）+ examples + 打包冒烟测试（第 15 节）

## 14. 明确不做（Out of Scope）

- npm 发布链路、jest 配置、dist/、`.history/`、`.scratch/` 等 TS 构建遗产。
- 与 TS 版产物的字节级兼容（概念与目录结构同构即可）。
- 除 OpenCode JS 插件与必要的 dashboard 前端资源外，不新增任何 Node/TS 代码。

## 15. 资源分发与打包

`schemas/`、`skills/`、`.opencode/` 是运行时资源：全新安装后 `aa init` 要能加载默认 schema、复制 OpenCode assets，`aa skill refresh` 要能发现 33 个 skills。设计约束：

- **唯一源在仓库顶层**（便于编辑与 review）；构建配置（hatchling `force-include`）在打 wheel/sdist 时把三个目录映射进 `assurance_agent/` 包内资源路径，editable 安装下同样生效——不存在需要人工同步的第二份副本。
- **统一访问入口**：`assurance_agent/resources.py` 是包内读取运行时资源的唯一模块，基于 `importlib.resources` 实现；任何模块**禁止**用源码仓库相对路径（`__file__/../..`）定位资源。
- **workflow schema 解析顺序**（对齐源版 docs/schemas.md）：项目 `.aa/workflow-schema.yaml` → 项目 `schemas/workflow-schema.yaml` → 包内默认。显式 `--schema` 覆盖是排他的：路径缺失即报错，不回退。
- **打包冒烟测试**（CI 必跑）：构建 wheel → 在全新虚拟环境安装 → 切到源码目录之外的临时目录运行 `aa init` / `aa doctor` / `aa status`，验证资源解析不依赖源码仓库。
