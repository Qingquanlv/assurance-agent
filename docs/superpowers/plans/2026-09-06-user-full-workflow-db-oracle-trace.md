# User Full Workflow DB Oracle and Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 User 创建样例从真实 full workflow 的业务规格生成测试，经确定性执行、独立 SQLite oracle 和可选必需 Trace 校验，只有证据充分且业务正确时才 achieved/export。

**Architecture:** 基于当前 Python StateGraph 与 semantic attempt kernel，复用 intake/generation/execution/quality/healing/product 主流程。保留 `execution.execute` / `execution.run` 两个业务节点，改接 execution 所有的稳定 TaskAttemptContract；product 仅装配闭集 profile executor，legacy 委托既有 raw Agent executor，新 profile 委托可恢复的权威执行后端。业务预期归 intake，机器计划归 generation，事实归 execution，业务判定归 quality；沿既有 application 终态认证与 export 扩展门禁。不新增 Agent、产品顶层节点或第二套图引擎。

**Tech Stack:** Python 3.11、uv workspace、Pydantic v2、pytest、SQLite、HTTPX；benchmark 的 FastAPI 0.111.0 / Tortoise ORM 0.23.0 / aiosqlite 0.20.0；OTel Python/FastAPI/Tortoise instrumentation、OTLP HTTP、上游 Collector file exporter。受控 pytest 使用当前已安装 Python 环境中的固定 subprocess 命令与标准输入/输出桥接，SUT/DB 观察仍由宿主执行器管理。Docker/OCI 仅保留为显式运行的可选安全增强实验。

**Spec:** [User 全流程验证规格](../specs/2026-09-06-business-spec-api-db-oracle-trace-design.md)

**2026-09-09 经用户确认的规格边界修订：** 本计划取代 spec §7.1 中“无法提供进程/写集隔离时新 profile 为 NOT_READY”的强沙箱要求。当前信任范围限定为受控本地 benchmark：固定 subprocess 不接收凭据、数据库/证据路径或父级写集，且其任何自报事实都不具备权威性；但不阻止同一 OS 用户下的恶意代码主动探测文件。OCI 强隔离仅作可选实验。spec 的其他业务真实性、证据认证和 A01–A26 要求保持不变。

**实施基线与本次 Review：** 最初实施基线为 `1b4187660e273183a9aab71f63fd8ee70d4dddcc`，源 Plan 来自主工作区 `d7e6591a`。2026-09-09 已执行 `git fetch origin main`，固定审查远程 main 为 `99316670ef008d4551926ec76765f0182e5e806b`，当前 HEAD 为 `a025831990fb776187fe03670e2118c9b50e385e`；merge-base 等于该 main。审查命令为 `git diff 99316670...a0258319`，同时审查尚未完成的 T10–T13。本文只更新 `codex/user-full-workflow-db-oracle-trace` worktree 内的实施指引；不合并代码、不修改原工作区文档。

## Global Constraints

- 本文件包含已提交实现及待修正工作。原有空任务框保留为历史实施清单，不表示当前全部未实现；实际进度以本次 Review 状态表为准。标记“补修”的项目均尚未实施，局部测试通过不代表真实 full 验收通过。
- 只新增一个 `execution_id`，关联既有 invocation/task/attempt/完整 pytest nodeid。case/assertion/action/checkpoint key 是静态引用，不新增平行的四层运行 ID。
- `validation_profile` 为一个闭集参数：`api_db.v1` 或 `api_db_trace.v1`。在编译前显式选择并冻结，执行失败后不得自动降级。旧配置未选择新模式时维持 legacy 读取语义，不授予新验证保证。
- 首条业务流程：`POST /api/v1/user/create`；模型表为 `user`，实际数据库是 SQLite。输入明确 `is_active=true`、`is_superuser=false`、`dept_id=null`、`role_ids=[]`；username ≤20 字符，email ≤255 字符。
- 业务预期来自已认可需求/规则；代码与 OTel 属性只帮助定位实际执行，不能作为 expected 的唯一来源。
- 固定配置属于 `.aa/`；每次运行清单由执行器自动生成并冻结，执行证据另存。项目/SUT 不得加载自定义 Python operation、validator、oracle 或 gate。
- 第一版不自动重试业务 POST。开始记录已持久化但终态未知时为 INCOMPLETE；恢复不重复 POST，显式新尝试重新生成 execution_id 和业务键。
- 独立 DB oracle 必须读取同一受控 SUT 的已提交状态；查询错误不能当作零行。仅 SELECT、参数绑定、明确列集；不导出密码、认证头和 SQL 参数。
- HTTP 动作上限 10 秒、独立 DB 读取上限 2 秒、动作与 DB 观察结束后的 telemetry 完成预算 10 秒。计划冻结后不可通过 healing 放宽。
- “完整”仅表示本计划必需的正向证据齐全且采集成功；不宣称观测到所有真实函数/SQL，也不提供恶意 SUT 的密码学证明。
- User 正常创建没有覆盖创建与角色修改的统一事务；rollback 验收使用提前冻结的 harness 事务变体，不顺带改正常业务语义。
- `.importlinter` 的依赖方向保持：generation → intake contracts；execution → intake/generation contracts；quality → execution 等上游 contracts。Feature 不 import product 或具体 Agent adapter。
- 隔离 worktree 已创建并完成依赖安装。后续工作仅在该 worktree 进行；不再次创建、不切回原脏工作区、不 cherry-pick 整个旧分支。新 worktree 没有被忽略的 vendored SUT app，T3 必须先实现可重建源快照，禁止 benchmark 回退到原工作区。
- wheel 中的 Python 工厂、semantic contracts、raw-file seal/promote 与 ProductLock v3 是本基线唯一接线机制；`.aa/` 仅为闭合组织数据。不得恢复 YAML graph/execute-slot 配置或引入 project-loadable handlers。
- 既有 `plan_ref/plan_digest` 始终表示 intake 的 `ResolvedAssurancePlan`；新增机器计划使用 `case_execution_plan_ref/case_execution_plan_digest`。两个身份都必须贯通，不覆盖或混用。
- 完整交付保留真实 `aa compile/start/run/status/export` 路径；`entrypoint=full` 与 `run_mode=case` 是不同字段，不能把 run_mode 改成 full。
- 本次补修保持既有 LangGraph 节点、边、重试路由与 semantic Attempt 提交机制。`prepare/finalize` 是节点内部阶段；SUT/Collector 启停属于 execution host。只调整必要的 selector/publish/Send 数据映射，不增加前置编排节点、Agent、第二套调度器或全图 `seed + context` 状态。
- 冻结根计划决定是否启用验证；缺少调用方可选字段不能切回 legacy。业务规格/来源由现有认证入口读取，完整 `CasePlanContextV1` 在候选 bindings 产生后由 host 构造。Agent 不填写信任摘要，正式机器计划和下游接纳校验不放宽。
- `api_db.v1` 与 `api_db_trace.v1` 的业务执行不依赖 Docker、Colima、镜像或 OCI qualification 文件。pytest 子进程只负责触发窄桥接；父级 host 独占 API 凭据、SUT/SQLite/Collector 句柄、业务动作、oracle 与证据写入。当前阶段不宣称能够隔离同一 OS 用户下的恶意测试代码。

### 2026-09-09 Review 状态与修正索引

| Task | 当前状态 | 本次必须补齐的内容 |
| --- | --- | --- |
| T1 | 已有提交实现 | 本轮未确认新增缺陷；随 T10 验证真实规格派生 |
| T2 | 已有提交，重新打开补修 | R1 生成时机；R2 legacy API 文件要求 |
| T3 | 已有提交，重新打开补修 | R7 已评审源码与实际运行制品关联 |
| T4 | 已有提交，重新打开补修 | R3 已完成；R10 以固定 subprocess bridge 替代业务路径中的 Docker 硬依赖 |
| T5 | 已有提交，重新打开补修 | R4 已完成；R10 从 profile preflight、配置和 Attempt binding 移除 OCI 资格门禁 |
| T6 | 已有提交实现 | 随 T2/T7 回归机器计划引用、两类 verified 输出接线 |
| T7 | 已有提交，重新打开补修 | R3 事实重放；R5 产品层 incomplete 接纳 |
| T8 | 已有提交，需产品路径再验收 | R5 经安装 executor 的修复闭环；R8 收敛私有 graph 依赖 |
| T9 | 已有提交实现 | T10/T12/T13 验证新事实确实进入原终态/导出门禁 |
| T10 | 仅生命周期前置部分已提交 | User manifest、空 change 派生、生产 host 启停与真实 full 均未完成 |
| T11–T13 | 尚未实现 | T11 真实驱动/Collector；T12 增补 R6 原始 Trace 接纳；T13 全矩阵 |

架构与兼容性审查：

| ID / 优先级 | 代码证据及影响 | 对应 Task |
| --- | --- | --- |
| R2 / P1 | generation `operations/planning.py:72–104,745` 无条件要求 API bindings，API skill 同样列为必需；legacy Dept 等 API 也被迫生成 User 专用 binding | T2、T10 |
| R4 / P1 | product `verification_execution.py:96–118` 在 root preflight 要求已运行实例；execution `operations/verified_attempt.py:48–65` 要求预填 selection，`user_attempt.start_user_attempt` 无生产调用者 | T5、T10 |
| R5 / P1 | product `verification_quality.py:77–81` 拒绝合法 `VerifiedIncompleteExecutionV1`；现有修复测试直接调用 materializer，绕过真实安装 executor | T7、T8、T10 |
| R8 / P2，封装判断 | product `repair_authorization.py:12,52–62` 调用 execution 私有 graph selector/activation 重算 AttemptKey；内部图输入变化会扩散到产品授权逻辑。保留独立 journal/receipt 认证，仅收敛接口 | T8 |
| R10 / P1，过度设计 | execution `operations/verified_execution.py` 只构造 `DockerVerificationHost`；product `verification_execution.py` 和 `verified_attempt.py` 要求 qualification 文件并执行 Docker preflight。Docker/Colima 因而成为两个 verified profile 和 full workflow 的硬依赖，但当前目标只需要固定子进程触发父级权威动作 | T4、T5、T10、T13 |

规格与执行正确性审查：

| ID / 优先级 | 代码证据及影响 | 对应 Task |
| --- | --- | --- |
| R1 / P1 | generation `contracts/{agent,workflow}.py`、`graphs/routes.py` 提前要求完整 context/sources/profile；context 又依赖当前 plan 尚未产出的 bindings 摘要，全新 change 无法自然推进 | T2、T10 |
| R3 / P1 | execution `operations/verified_execution.py:257–266,422–426` 在 HTTP 终态未知时把零行当作终态后置条件；quality 会判 FAILED，违反 spec §8/A17 要求的 INCOMPLETE | T4、T7、T10 |
| R6 / P1，剩余设计缺口 | execution `_journal_refs` 仅索引四类记录；quality `operations/assessment.py:787–794,864–871` 仅允许现有 journal 并重放 API/DB。原 T12 仅扩展 evaluator，无法接纳真实 Trace 事实 | T12、T13 |
| R7 / P1 | generation 的 `sut_digest` 来自已评审源引用，execution 复制到 manifest；harness `prepare:457–460` 另行复制固定快照，缺少两者文件内容关联，可能评审新代码却执行旧制品 | T3、T10、T13 |
| R9 / P1，验收设计缺口 | generation `operations/codegen.py:469–475` / `contracts/admission.py:290–302,589` 会拒绝无 bridge 候选；原 T10 的 no-bridge 注入点无法证明 A03 的“pytest 通过但实际动作未执行” | T10、T13 |

文档基线同步：远程 main 的 Dept benchmark 已是 API-only；不恢复四 family，也不恢复已移除的 codegen-fix 流程。当前安装目录为 32 条 Agent contracts、16 条 Task contracts；测试比较精确集合，不依赖计划中的旧数量。保留现有 kernel、独立 DB oracle、窄 bridge 协议、冻结义务和交付认证；这些与规格一致，不因文件多而统一重写。

补修顺序：T2 → T3/T4 → T5 → T7 → T8 → T10 阶段一真实验收 → T11 → T12 → T13。R10 在继续 T7 前完成 T4/T5 的最小补修；任务编号保持不变。每次只提交对应补修及必要 schema/declaration 更新，不能把这些问题合并成跨 capability 通用框架重构。

---

## 1. 初始基线审查与沿用的接线选择

下表记录 2026-09-06 的初始迁移判断；执行当前补修时，以前述 2026-09-09 状态及各 Task 补修项为准。

| 原 Plan 假设 | `1b418766` 实际状态 | 本修订 |
| --- | --- | --- |
| prepare ID 控制 execute alias，可切换到 host result | graph 使用 semantic Agent contract ID，raw runtime 强制 AgentRunRequest/AgentRunResult | T5 增加稳定 Task facade 和受控 executor；不修改 Agent target 伪装 host |
| 修改 workflow/main.yaml、module.yaml guard | 文件已移除，Python StateGraph 与 typed cycle 控制路由 | T5/T7 修改 graphs factory/nodes/state/routes；保持产品拓扑和业务节点 |
| build_execution_view 未使用且需下移 | execution/execution_view.py 与 generated_merge.py 已存在；ExecutePrepare 已调用 | T3 直接扩展现有能力，不再次移动或复制 |
| finalize_achieved 未接通，gate 只看首次 execute | application 已认证 full terminal 并调用；status 已按当前 execution gate 选择 execute/run | T9 增加 verification 认证，保留终态、重跑与幂等机制 |
| InspectFinalize 是唯一质量入口 | 先执行 quality.materialize-assessment-inputs；现有 disposition 是闭集 | T7 在 assessment 装配证据、inspect 确定性裁决、报告与路由贯通 |
| safety/workflow_state 是全部修复接纳边界 | full 走 apply-test-repair，实际接纳在 operations/application.py::_verify_application | T8 在真实接纳路径保护冻结义务 |
| manifest/计划中的 selected 可直接作为产品输入 | benchmark 转为 candidate_test_families；intake resolve-plan 冻结 selected 和根计划 | T1/T2/T5/T10 保留候选→策略/Explore→根计划链；新机器计划另命名 |
| 原 SUT 源码能在 worktree 复制 | app/依赖被忽略；run_item 的 parent fallback 会找到原工作区 | T3 固定可重建源快照；T10 显式项目位置并替换新模式的 runtime owner |

权威执行只输出 `collected` / `incomplete` 的事实收集状态；quality 计算 PASSED/FAILED/INCOMPLETE。现有 `ExecutionCycleResultV1.final_status=PASS/FAIL` 保留给 legacy，新模式采用独立的 `VerifiedExecutionCycleResultV1`，从 publish 到 assessment 的所有读者按冻结 profile 判别。

两条稳定 Task facade 使用新增 contract IDs `assurance.execution.task.execute.v1` / `assurance.execution.task.run.v1`；图上业务 semantic IDs 保持 `execution.execute` / `execution.run`。闭合的 product 装配选择原 raw Agent executor 或新 host executor，执行期间不动态换配置。远程 main 的 Agent binding 精确集合保持原身份与目标，新 Task contracts 纳入安装目录与锁。无需给 CapabilityBuildContext 增加任意配置/handler API。

当前业务后端是在已安装 Python 环境中启动的薄 pytest subprocess，以固定 argv 和有界 JSONL 管道请求父级执行冻结计划。父级持有 API 凭据、SUT/SQLite 绑定及事实；子进程环境使用显式 allowlist，不接收凭据、数据库路径、证据写集或动态 host handle。该边界防止测试通过协议伪造业务事实，但不构成同一 OS 用户下的恶意代码沙箱。已有 OCI runner、构建脚本和真实资格测试保留为显式 opt-in 的安全实验，其结果独立记录，不进入 profile 选择、root preflight、业务 verdict、achieved/export 或普通 CI 门禁。

恢复必须覆盖本基线的 kernel activity/reconcile：当前裸 `DeterministicTaskExecutor` 无 reconcile，且构造的 TaskContext 不提供真实取消/activity/secret ports，不能直接当作一次性 HTTP 副作用执行后端。T4/T5 复用 production task host 与恢复端口，并保持 kernel 的 seal/validate/promote/receipt 顺序。

## 2. 交付顺序与任务边界

阶段一：Task 1–10，交付 `api_db.v1` 的真实 User full workflow。阶段二：Task 11–13，交付同一业务规格的 `api_db_trace.v1`。

```text
1 规格与来源 → 2 机器计划 → 3 运行环境/清单 → 4 权威执行 → 5 semantic attempt 装配
2 + 4 + 5 → 6 codegen/执行接线 → 7 质量判定 → 8 healing → 9 最终交付 → 10 阶段一 full 验收
10 → 11 OTel 接入与锁定 → 12 Trace 判定/完成 → 13 阶段二 full 验收和 CI
```

主链按编号依赖推进；同一任务内互不修改相同文件的测试素材与实现可并行研究。每个任务有自己的行为测试和独立提交。不把密切依赖的前后链拆成无法独立验证的独立项目。

## 3. 文件与产物布局

以下路径均相对仓库根目录；标为“新”的文件由本计划创建，其余是现有文件。

| 所有者 | 新增文件 | 修改的主要现有入口 |
| --- | --- | --- |
| intake | `assurance_intake/contracts/verification.py`、`resources/schemas/assertion-sources.v1.schema.json` | `contracts/{cases,plan,workflow,attempts}.py`、`operations/{finalize,agent_skills,resolve_plan}.py`、`graphs/{nodes,state}.py`、`validators/cases.py`、注册、case design/review skills |
| generation | `assurance_generation/contracts/execution_plan.py`、`operations/execution_plan.py`、`resources/schemas/case-execution-plan.v1.schema.json` | `contracts/{plans,agent,codegen,workflow,attempts}.py`、`operations/{planning,review,codegen,cycle}.py`、`graphs/{nodes,state}.py`、`validators/plans.py`、注册及 API skills |
| execution | `contracts/verification.py`、`operations/verification_manifest.py`、`operations/sqlite_oracle.py`、`operations/verified_execution.py`、`operations/verified_process.py`、`bridge.py`、`bridge_runner.py`、`operations/telemetry.py` | `execution_view.py`、`generated_merge.py`、`operations/{runner,agent_skills,__init__}.py`、`contracts/{agent,evidence,workflow,attempts}.py`、`graphs/{factory,nodes,state}.py`、schemas/注册/依赖；`DockerVerificationHost` 仅供可选实验 |
| quality | `contracts/verification.py`、`operations/verification.py` | `operations/{assessment,agent_skills,inspect,metrics,report}.py`、`contracts/{assessment,attempts}.py`、`graphs/{assessment,nodes,state,routes}.py`、schema/注册 |
| healing | `operations/verification_guard.py` | `operations/{application,safety,workflow_state}.py`、`contracts/application.py`、`graphs/{nodes,state}.py`、aa-apply-test-repair 技能及结果契约 |
| product | `verification.py`、`verification_execution.py` | `models.py`、`binding_builder.py`、`product.py`、`agent_contracts.py`、`runtime_bindings.py`、`runtime_ports.py`、`application.py`、`status.py`、`export.py`、`graphs/{execute,routes,state,revisions}.py`、配置 schemas |
| benchmark | `user_oracle_harness.py`、`fixtures/user-oracle/sut-source/`、`fixtures/user-oracle/{bootstrap.py,collector.yaml,runtime-lock.json,requirements.in,requirements.lock}` | `run_item.py`、`manifest.json`、`tests/validate_live_run.py`；`runner.Dockerfile` / `runner-lock.json` 仅属于可选 OCI 实验 |
| 构建/CI | 普通 CI 不新增容器构建步骤；`scripts/build_verification_runner.py` 仅为可选实验入口 | `.github/workflows/ci.yml`、三个 wheel/engine smoke 脚本 |
| 测试素材 | `tests/verification_support.py`、`tests/fixtures/verification/{user-case.json,user-sources.json,user-plan.json}` | 各包现有 fixtures 与产品 full/manifest/交付测试 |

`assurance_*` 的包目录分别在 `packages/capabilities/assurance-*/`；product 文件在 `packages/products/assurance-product/assurance_product/`；benchmark 文件在 `benchmark/assurance-product/`。每个新 schema 同步对应插件 `_SCHEMA_FILES`、静态 declaration 与 wheel 资源测试；不用新建跨 Feature 的万能 contracts wheel。

按一次 change 保存：

```text
qa/changes/<change_id>/
  cases/system/user/case.yaml                  # 人可读规格
  cases/system/user/assertion-sources.json     # 正式来源 sidecar
  plan/<plan_digest>/resolved-assurance-plan.json  # 既有 intake 根计划
  plans/api-case-execution-plan.json           # 新机器计划包，独立 ref/digest
  codegen/api-generated-files.json             # 既有生成映射，引用计划摘要
  execution/<execution_id>/manifest.json       # 父执行器自动生成
  execution/<execution_id>/evidence.json        # 逐项实际观察及完成记录
  execution/<execution_id>/otlp.jsonl           # 第二阶段受控导出原始数据
  execution/execute-result.json                # 首轮批次索引，引用各 execution_id
  execution/run-result.json                    # 当前重跑索引，权威历史按 attempt/receipt 保留
  inspect/verification.json                    # 确定性业务/证据判定
```

实际写入采用当前 raw-file + typed result 路径：Agent 仅在 phase_write_claims.runtime 允许位置写候选；确定性 finalizer 在其独占写集产出规范化机器材料；host 在本 attempt 授权 write_root 写事实。Kernel 封存实际 bytes、执行已声明验证、promote 并生成 receipt，graph publish 只投影已提交结果。动态 execution_id 子目录由 execution 父目录的受控 ResourceClaimTemplate 覆盖，并通过路径/身份验证收窄；不把此写集、凭据或数据库绑定传入 pytest subprocess。拒绝绕过 seal/promote 直接写最终 change 目录，也不引入旧文档的 typed artifact materialization API。

## 4. 冻结的最小接口

| 类型/函数 | 所属任务与契约 |
| --- | --- |
| `BusinessAssertionV1` | T1 intake：`assertion_id, statement, source_id, subject, comparator, expected`；comparator 仅 eq / row_count_eq；expected 为 literal 或冻结 input key 引用 |
| `AssertionSourcesV1` | T1 intake：`case_id, revision, sources`；每个 source 有 ID、需求/已认可规则 origin、引用、摘要与 decision；implementation references 不在此表 |
| `CaseExecutionPlanV1` | T2 generation：单 case、既有 plan_ref/plan_digest、ReviewedCase ref、spec/source/config/SUT digest、profile、一个 HTTP action、一个 SQLite observer、逐项 binding、required IDs、completion、可选 TraceRequirements |
| `CaseExecutionPlanSetV1` | T2 generation：`schema_version='1', change_id, cases: tuple[CaseExecutionPlanV1, ...]`；统一保存到一个已声明 JSON artifact |
| `CasePlanContextV1` | T2 generation：已认证的根计划 ref/digest、ReviewedCase ref、verification policy digest、技术 config digest、SUT digest；生产调用方从已接纳材料构造，Agent 不能自填 |
| `compile_case_plan(case: dict, sources: AssertionSourcesV1, bindings: dict, validation_profile: str, *, context: CasePlanContextV1) -> CaseExecutionPlanV1` | T2，校验失败抛 `PlanNotReady(reason: str)`；expected 从 case 读取，bindings 中出现 expected/required 覆盖即拒绝 |
| `VerificationManifestV1` | T3 execution：execution_id、静态 case key、既有 workflow identity、完整 nodeid、根计划与case_execution_plan/spec/mapping/SUT/config digest、SUT 地址/实例、SQLite file identity、冻结输入、证据目录引用 |
| `ObservationV1` | T3 execution：execution_id、obligation_id、state（observed/missing/error/timeout/skipped）、actual（JSON value）、evidence ref/digest、reason；不接受 expected 或 passed 自报字段 |
| `VerificationEvidenceV1` | T3 execution：manifest digest、receipt、observations、collector/host completion、执行状态；拒绝重复 obligation 记录和身份冲突 |
| `ExecutionViewInputV1` | T3 execution：change_id、batch_id、execution_id、selected nodeids、已认证 generated-file descriptors；不引用 product 的 Python 类型 |
| `VerifiedExecutionRequestV1 / VerifiedExecutionResultV1` | T3 定义、T4 实现：请求带根计划与case_execution_plan/manifest/mapping/view引用；结果带execution_id、collected/incomplete、receipt/evidence引用。T5/T6 经 Task facade 接入，不继承 AgentRunResult |
| `VerifiedProcessReceiptV1` | T3 execution：argv、进程退出/超时/取消、raw pytest report、桥接协议结果；不得包含可覆盖 oracle 的 passed 标记 |
| `observe_user(db_file: Path, username: str, email: str, timeout_s: float=2.0) -> dict` | T3 SQLite observer：返回 `state, rows, reason`；只观察，不从 DB 生成 expected |
| `execute_case(case_id: str) -> None` | T4 bridge 的唯一测试侧入口；从父级会话获得分配，不接受 SQL、expected、URL、路径或状态参数 |
| `VerifiedExecutionHandler.execute(request: TaskRequest, context: TaskContext) -> TaskOutcome` | T4/T6 安装式可恢复 handler；首轮/重跑共用。TaskContext 必须经 T5 的 production host 提供；不能用裸适配器的假 attempt/空 activity。输出 typed host result |
| `VerifiedExecutionCycleResultV1` | T6：显式 profile、既有 plan_ref/digest、case_execution_plan_ref/digest、generation/mapping/source refs、epoch/repair_round、batch/executed_at、collected/incomplete、执行索引 ref、kernel receipt；不挤入 legacy PASS/FAIL |
| `ExecutionDispatchResultV1` | T5/T6：显式 profile 判别的 facade 输出，legacy 保留原 ExecutionEvidenceV1；新模式持有 VerifiedExecutionResultV1，由 publish 形成对应 cycle DTO |
| `VerificationVerdictV1` | T7 quality：execution_id、status、business、evidence、逐义务判定、required/executed/evaluated/satisfied 数量与原因 |
| `evaluate_verification(plan: CaseExecutionPlanV1, assertions: tuple[BusinessAssertionV1, ...], manifest: VerificationManifestV1, evidence: VerificationEvidenceV1) -> VerificationVerdictV1` | T7 纯函数，无网络/DB 访问；只消费上游 contracts |
| `assert_same_obligations(before: CaseExecutionPlanV1, after: CaseExecutionPlanV1) -> None` | T8 healing：规范、比较器、required、初态、profile、完成预算改变时抛 `ValueError`；允许受控技术绑定更新 |
| `authenticate_verified_delivery(project: Path, change_id: str, invocation_id: str) -> dict` | T9 product：从当前 ledger/接纳记录解析有效计划、执行及评价引用，返回认证过的交付摘要；不以文件自报 passed 为准 |

测试 helper `read_fixture(name: str) -> dict` 在 Task 1 的 `tests/verification_support.py` 定义；它只读取上述 fixtures 目录里的 JSON。后续任务的测试都通过真实模型验证这些 payload，不返回固定 verdict。

```python
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "verification"

def read_fixture(name: str) -> dict:
    if name not in {"user-case.json", "user-sources.json", "user-plan.json"}:
        raise ValueError("unknown verification fixture")
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
```

## Task 1: 冻结 User 业务断言与来源

**Files:** 新建 intake `contracts/verification.py`、来源 schema、`tests/verification_support.py` 与 user-case/user-sources fixtures；修改第 3 节 intake 入口。测试新增 `packages/capabilities/assurance-intake/tests/test_verification_contracts.py`。

**Interfaces:** 产出 `BusinessAssertionV1`、`AssertionSourcesV1`，通过 intake contracts 公共入口导出；不生成 execution_id。

- [ ] 写业务 fixture。case key 使用 `TC_USER_CREATE_001`；冻结以下断言和输入来源，不从当前 SUT 查询结果填预期：

| assertion_id | subject | expected |
| --- | --- | --- |
| api.http_status | create.response.http_status | literal 200 |
| api.code | create.response.business_code | literal 200 |
| user.row_count | created_user.count | literal 1 |
| user.username | created_user.username | input username |
| user.email | created_user.email | input email |
| user.is_active | created_user.is_active | input is_active（固定 true） |
| user.is_superuser | created_user.is_superuser | input is_superuser（固定 false） |
| user.dept_id | created_user.dept_id | input dept_id（固定 null） |

- [ ] 先写并运行拒绝空断言/仅源码来源的测试，确认红灯来自缺能力，而不是无关 fixture 失败：

```python
import pytest
from pydantic import ValidationError
from assurance_intake.contracts.verification import BusinessAssertionV1

@pytest.mark.parametrize("payload", [None, "", {}, {"assertion_id": "x", "statement": ""}])
def test_empty_assertion_is_rejected(payload):
    with pytest.raises(ValidationError):
        BusinessAssertionV1.model_validate(payload)
```

Run: `uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q`。

- [ ] 实现闭合 Pydantic 模型及规范来源验证；`expected` 的 literal/input 两种形式用判别 union，拒绝未知字段。已认可来源内容由冻结需求输入核验，Agent 填 `decision=accepted` 本身不构成认可。

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict
from graph_engine.canonical import JSONValue

class LiteralExpectedV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["literal"]
    value: JSONValue

class InputExpectedV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["input"]
    key: Literal["username", "email", "is_active", "is_superuser", "dept_id"]
```

- [ ] `candidate_test_families` 仍由 ProductInput 输入；profile 与业务验证策略摘要随已认证资源进入 intake resolve-plan，selected 由既有策略/Explore/quality goal 冻结。Root plan 的版本化扩展兼容旧档案，不能改旧摘要规则后默默重算历史数据。将业务验证策略与可变技术绑定分开：SUT/插桩/技术配置摘要由机器计划与运行lock绑定，正常重构只更新这些技术身份，不因此改变业务策略和根计划。
- [ ] 新模式在 case design/finalize/review 路径按 typed assertions 解析；旧 authoring 文件继续读取。来源 sidecar 加入资源声明、允许输出及评审输入；更新技能对 advisory metadata 的禁止条款，只允许正式来源 sidecar。
- [ ] 增加“规范要求 true、源码当前 false，仍保留 true”测试；版本摘要不一致、重复 ID、source_code-only、缺来源均不 ready；schema 与模型一致、plugin declaration 与 wheel 一致。
- [ ] 运行 `uv run pytest packages/capabilities/assurance-intake/tests -q`，通过后只提交本任务路径：`feat(intake): add reviewed business assertion provenance`。

## Task 2: 机器计划、完整义务与确定性评审

**Files:** 新建 generation `contracts/execution_plan.py`、`operations/execution_plan.py`、机器计划 schema、`tests/fixtures/verification/user-plan.json`；修改 `operations/planning.py`、`operations/review.py`、`contracts/{plans,agent,workflow,attempts}.py`、`validators/plans.py`、插件注册及 planner/reviewer skills。新增 `packages/capabilities/assurance-generation/tests/test_execution_plan.py`。

**Interfaces:** 消费 T1 assertions/sources，产出第 4 节两个计划类型及 `compile_case_plan`；纯契约检查可放 contracts，由 execution/quality 复用，不能让它们 import generation operations。

**2026-09-09 补修 R1/R2（先于 T10）：** 继续修改 generation `contracts/{agent,workflow,attempts}.py`、`graphs/{nodes,state,routes}.py`、`operations/{planning,execution_plan,review}.py`、API planner/reviewer skills；product 只改 `graphs/execute.py::adapt_generation` 的字段映射。补测 `tests/test_execution_plan.py`、`test_plan_review.py`、`test_graph_routes.py`、`test_reviewed_plan_handoff.py`、`tests/product/test_user_oracle_full_workflow.py`。涉及递归 schema 的安装声明按既有生成方式同步，不手工放宽校验。

- [ ] **R1 输入与生成时机。** Product 显式传递冻结 profile、根计划 ref/digest、ReviewedCase/来源引用；`ResolveGenerationInputV1`/`PlanInputV1` 不再要求当前 plan 尚未生成的完整 context。resolve-inputs 保持认证 ReviewedCase 的既有职责和输出，plan prepare 通过已认证引用读取业务规格/来源。`AgentFinalizeInputV1` 的 codegen 专属“必须已有机器计划”约束移到对应阶段校验，不能把 plan finalize 误当成 codegen。缺业务规格/来源仍拒绝；禁止用假摘要填齐 context。
- [ ] **R1 finalize 内完成编译。** 在 `operations/execution_plan.py` 内复用已有规格认证与纯编译能力，读取本次 write root 的已认证 bindings 字节；同一份字节用于摘要、解析与编译，构造完整 `CasePlanContextV1` 后写正式计划。profile 按冻结根计划核验；缺候选、缺必需 binding 或输入不一致返回 not-ready/failed output。不得因 `case_plan_context is None` 跳过新模式编译或 review 校验。下游 plan-review 从已提交引用重新认证并比对确定性编译结果，codegen 继续消费机器计划 ref/digest。
- [ ] **R1 图数据收敛。** 删除 `_verified_plan_inputs` 对未生成 context 的前置齐套要求；`route_families` 只携带阶段可用字段，保留现有四 family destinations、skip/join 和重试条件。`select_plan/publish_plan` 等只做输入输出投影；完整 context 不在整个 graph state 中传播，不新建 generation seed 协议，也不将文件读取/编译放进 selector 或 route。
- [ ] **R2 legacy 兼容。** `plan_outputs`、prepare 的 allowed outputs、finalize 必需文件检查、review 输入和 skill 输出说明均按已冻结 profile 选择：legacy 保留远程 main 的原 API 文件集合；仅新 profile 增加候选 bindings/正式机器计划。静态 phase claims 可以声明安装能力的允许上限，但不能把允许写入等同于各模式必须产出；同名 semantic contract 的 canonical projection 不随运行 profile 改变。
- [ ] **补修验收。** 新 profile 的首轮输入只含已评审规格与来源即可进入 plan；首次 finalize 后才出现真实 bindings 摘要及机器计划。缺来源、漏 binding、review 自报 pass、隐藏 profile 均不能绕过。legacy 使用 main 原样五文件 API 候选可以完成 plan/review，不制造占位 User bindings。测试新旧图边与 retry/receipt 语义一致；重新编译后的新 revision 才可启动，不换装旧 invocation。

```python
# 放在 test_execution_plan.py：阶段输入可解析，正式编译仍需认证真实文件。
def test_plan_input_does_not_require_future_bindings_digest():
    from assurance_generation.contracts.agent import PlanInputV1
    context = read_fixture("user-plan.json")["context"]
    payload = {
        key: context[key]
        for key in ("change_id", "coverage_epoch", "plan_digest", "plan_ref", "reviewed_case")
    }
    result = PlanInputV1.model_validate({
        **payload, "capability_leafs": ["entities.item.create"],
        "artifact_paths": ["qa"], "validation_profile": "api_db.v1",
    })
    assert result.validation_profile == "api_db.v1"
```

- [ ] 在 fixture 明确 HTTP POST、User 字段输入、SQLite 固定查询 binding、初态不存在、8 条业务断言及 completion。required 集合由规范＋profile 推导，不采信 planner 自报数量。

```python
BASE_RUNTIME_OBLIGATIONS = frozenset({"initial.user_absent", "action.finished", "oracle.executed"})
TRACE_OBLIGATIONS = frozenset({"trace.http", "trace.user_write", "trace.user_completed", "trace.drained"})

def required_obligations(assertion_ids: frozenset[str], profile: str) -> frozenset[str]:
    if profile not in {"api_db.v1", "api_db_trace.v1"}:
        raise ValueError("unknown validation profile")
    trace_ids = TRACE_OBLIGATIONS if profile == "api_db_trace.v1" else frozenset()
    return assertion_ids | BASE_RUNTIME_OBLIGATIONS | trace_ids
```

- [ ] 红灯测试缺少 DB binding，即使 Agent review 返回 pass 也 NOT_READY：

```python
import pytest
from tests.verification_support import read_fixture
from assurance_intake.contracts.verification import AssertionSourcesV1
from assurance_generation.contracts.execution_plan import CasePlanContextV1
from assurance_generation.operations.execution_plan import compile_case_plan, PlanNotReady

def test_missing_oracle_is_not_ready():
    bindings = read_fixture("user-plan.json")["bindings"]
    bindings.pop("user.row_count")
    with pytest.raises(PlanNotReady, match="user.row_count"):
        compile_case_plan(
            read_fixture("user-case.json"),
            AssertionSourcesV1.model_validate(read_fixture("user-sources.json")),
            bindings,
            "api_db.v1",
            context=CasePlanContextV1.model_validate(read_fixture("user-plan.json")["context"]),
        )
```

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_execution_plan.py -q`。

- [ ] 实现单 case 模型、计划包、合法引用/比较器/摘要校验；bindings 只能描述 actual 的定位，不能携带 expected/required 覆盖。提供固定 User SQLite binding ID 和版本，而非允许任意 SQL DSL。
- [ ] 保持 `plan_ref/plan_digest` 为 intake 根计划；machine plan 使用 `case_execution_plan_ref/digest`，同时绑定根计划、ReviewedCase、来源及配置。增加只漂移其中一类计划、epoch 或 ReviewedCase 的拒绝测试。
- [ ] 同步 planning.py 的输出集合与 contracts/attempts.py 的 `_PLAN_FILES` 及 OUTPUT_ROUTE_TEMPLATES；新 profile 的 Agent 写 `plans/api-execution-bindings.json` 候选定位（闭合bindings模型），finalizer 独占写 `plans/api-case-execution-plan.json` 正式计划，两者进入各自 phase_write_claims。按 R2 区分静态允许写入与 profile 必需产物，legacy 不要求这两个新增文件。Agent 不直接决定正式计划的 expected、required 和 ready。PlanResult 引用独立机器计划摘要。`validators/plans.py` 按已声明精确文件类型分派，保留现有文件类型的严格检查。
- [ ] PlanFinalize 与 PlanReviewFinalize 共用确定性闭包校验；后者不能继续仅校验 Agent 的 pass 字段。覆盖缺项、重复/未知义务、悬空 expected、错误 source/spec digest、profile 与 Trace 冲突、空 required。
- [ ] 运行 `uv run pytest packages/capabilities/assurance-generation/tests/test_execution_plan.py packages/capabilities/assurance-generation/tests/test_planning.py packages/capabilities/assurance-generation/tests/test_plan_review.py packages/capabilities/assurance-generation/tests/test_plan_validator.py packages/capabilities/assurance-generation/tests/test_resources.py -q` 和 `uv run lint-imports`。提交 `feat(generation): compile closed case execution plans`。

## Task 3: 运行清单、执行视图与独立 SQLite observer

**Files:** 新建 execution `contracts/verification.py`、`operations/{verification_manifest,sqlite_oracle}.py`；扩展现有 execution `execution_view.py`、`generated_merge.py` 与 `operations/agent_skills.py`，product `execution_view.py` 保留既有 re-export；新增 benchmark `user_oracle_harness.py`、`fixtures/user-oracle/bootstrap.py`、受版本控制的 `fixtures/user-oracle/sut-source/` 及阶段一基础 `requirements.in/requirements.lock/runtime-lock.json`。新增 execution `tests/test_verification_manifest.py`、`tests/test_sqlite_oracle.py` 和 `tests/unit/benchmark/test_user_oracle_harness.py`。

**Interfaces:** 产出 Manifest/Observation/Evidence 三种契约及 `observe_user`；harness 为固定入口 CLI，由声明式环境配置启动，不能作为 SUT 插件 import。`user_oracle_harness.py prepare/start/stop` 的 JSON 回执含 SUT 地址、进程实例、SQLite 绝对路径、源文件摘要与结束原因。

**2026-09-09 补修 R7：** 同时修改 `operations/{managed_sut,agent_skills,user_attempt,verification_manifest}.py`、harness 和对应 manifest/harness 测试；补足已评审源与执行副本的关联，不增加新的源码认证服务。

- [ ] 用现有 source file manifest 建立 ReviewedCase 源引用到 SUT 运行副本文件的明确路径映射，并逐文件比对摘要。`sut_digest` 是评审源引用集合摘要，runtime/source digest 是完整制品摘要；两者含义不同，不能直接比较两个不同投影的 hash，也不能仅把计划摘要复制到 manifest 当作核验成功。
- [ ] benchmark 在 intake 前物化并冻结本次明确的 SUT 制品。attempt prepare 从这份已冻结制品构建独占运行副本，不能再无条件换回 `fixtures/user-oracle/sut-source`。基础快照和闭集故障/重构变体仍由仓库固定 harness 产生；所有覆盖文件/启动配置纳入原清单，不允许任意项目插件。运行副本、源映射、prepare/start receipt 和已接纳机器计划必须关联到同一制品。
- [ ] 红灯测试让已评审源码 A 对应实际运行 B：即使两边各自摘要自洽、B 能正常创建 User，也须在业务 POST 前 NOT_READY。正向测试基于冻结 A 的副本执行成功；T13 再覆盖真实重构制品。保留原 source drift、symlink、错误 SQLite 和已提交状态测试。

- [ ] 红灯测试使用真实临时 SQLite，确认另一连接看不到未提交写入，回滚后仍为零行：

```python
import sqlite3
from assurance_execution.operations.sqlite_oracle import observe_user

def test_oracle_reads_only_committed_state(tmp_path):
    db = tmp_path / "db.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE "user" (username TEXT, email TEXT, is_active INTEGER, is_superuser INTEGER, dept_id INTEGER)')
    writer = sqlite3.connect(db)
    try:
        writer.execute('INSERT INTO "user" VALUES (?, ?, ?, ?, ?)', ("qa_t1", "qa_t1@example.com", 1, 0, None))
        observation = observe_user(db, "qa_t1", "qa_t1@example.com")
        assert observation["state"] == "observed"
        assert observation["rows"] == []
        writer.rollback()
        assert observe_user(db, "qa_t1", "qa_t1@example.com")["rows"] == []
    finally:
        writer.close()
```

Run: `uv run pytest packages/capabilities/assurance-execution/tests/test_sqlite_oracle.py -q`。

- [ ] 实现固定查询：

```sql
SELECT username, email, is_active, is_superuser, dept_id
FROM "user"
WHERE username = ? OR email = ?
LIMIT 2
```

使用 `Path.as_uri()` 构造 SQLite `mode=ro` URI，开启 query_only，并通过 progress handler/锁等待限制落实总预算。结果大于一行时只需保留最多两行即可证明 cardinality 失败；0/1 布尔列严格归一，未知值不能转 truthy。数据库不存在、锁超时、文件身份变化分别返回 error/timeout，不返回空 rows 假装成功查询。

- [ ] 初始 absence 使用同一 observer；后态新建独立连接，不复用 SUT ORM。manifest 冻结 username/email 后不可再换值；分配前最多三次碰撞尝试。DB 绝对路径、文件身份和 managed SUT 配置三者核验一致。
- [ ] 直接扩展 execution 所有的 `build_execution_view` 与 `MergedGeneratedSet`，用 `ExecutionViewInputV1` 收窄输入；现有 ExecutePrepare 已调用它，不再迁移或复制实现。Feature 不 import product。新 view 以 execution_id 区分真实重跑，legacy 路径兼容保留。新模式的安全测试副本只包含 tests/support，目录 0555、文件 0444，确保非 root runner 能读取；不复制凭据或数据库。模式归一进入 view 摘要，原项目文件权限不变。
- [ ] 每次业务尝试创建 execution_id 并冻结 manifest；关联实际 `AttemptKey`、BusinessActivation、coverage_epoch/repair_round 与完整 nodeid。恢复只凭同一授权 scope/activity receipt 采用既有清单，匹配摘要才复用。`TaskRequest.attempt=1` 是现有兼容字段，不作真实性、去重或轮次依据；不要求host生成新的轮次计数。新增同版本重跑 ID 不同、旧证据拒绝、参数化 nodeid 不折叠、路径错绑拒绝测试。
- [ ] 在已跟踪 `fixtures/user-oracle/sut-source/` 提交经过核查的当前 benchmark 最小运行源快照（app、migrations、启动入口、必要配置/依赖、来源与许可证）；排除数据库、密码、token、web/node_modules。runtime-lock 校验真实文件清单与摘要。仅摘要不构成可重建来源；普通 CI 不依赖原 checkout 或网络上的浮动 main。
- [ ] harness 从上述快照在指定 worktree 的 run 根物化 project 副本；Agent/change/export 指向这一显式 project_dir，各 case attempt 的 managed SUT 再使用独占运行副本。新 profile 禁止 `_resolve_sut` 的 parent fallback；缺源直接 NOT_READY。添加原 checkout 不存在仍可准备、缺源不回退、change/output 不越出本 worktree 的测试。
- [ ] harness 只复制运行必需 app/migrations，使用 SQLite backup 或已停止种子库准备管理员；先绑定监听 socket 再启动服务，防止端口抢占；不复用未知 ready URL。启动/停止只控制本次拥有的 PID/实例，未知实例必须拒绝。阶段一即提交实际 SUT 源文件清单、基础依赖锁与摘要，不等到 OTel 阶段才锁环境；T11 在同一锁中增加经过兼容验证的 OTel 依赖。
- [ ] 验证 `uv run pytest packages/capabilities/assurance-execution/tests/test_verification_manifest.py packages/capabilities/assurance-execution/tests/test_sqlite_oracle.py tests/unit/benchmark/test_user_oracle_harness.py tests/product/test_execution_view.py tests/product/test_candidate_execution_isolation.py -q`。提交 `feat(execution): bind isolated runs and observe SQLite state`。

## Task 4: 固定 pytest subprocess、窄桥接与父级执行

**Files:** 新建 execution `bridge.py`、`bridge_runner.py`、`operations/verified_process.py`、`operations/verified_execution.py`；修改 execution `operations/runner.py`、`operations/__init__.py`、`pyproject.toml`、根 `uv.lock`。新增 `packages/capabilities/assurance-execution/tests/test_verified_process.py`、`test_verified_execution.py`。已有 `runner.Dockerfile`、`runner-lock.json`、`scripts/build_verification_runner.py` 与 `test_verified_runner_qualification.py` 只保留为显式 opt-in 的 OCI 实验，不由产品导入。

**Interfaces:** 测试只调用 `execute_case(case_id: str) -> None`。host 驱动 T3 固定 harness prepare/start/stop，通过结构化 receipt 认证实例；不得从配置加载任意命令或 Python 类。父级 `VerifiedExecutionHandler` 使用固定计划和 Manifest，接收 execute 请求并产生 Observation，不接收测试提供的 SQL/expected/status。`VerifiedProcessReceiptV1` 记录真正进程终态、raw pytest report、超时/取消原因和桥接请求数量。

**2026-09-09 补修 R3：** 修改 `operations/verified_execution.py::{execute_frozen_action,collect_facts}` 和 `tests/test_verified_execution.py`；T7 同步独立重放，避免两个实现对事实含义不同。

**2026-09-09 补修 R10：** 修改 `operations/verified_process.py`、`operations/verified_execution.py` 与对应测试，新增固定 `SubprocessVerificationHost`，作为两个 verified profile 的业务后端。已有 `DockerVerificationHost` 不删除，但只能由显式 OCI 实验调用；Docker/Colima 缺失不能使业务执行 `NOT_READY`。

- [ ] HTTP timeout/连接中断且动作是否完成未知时，可保存当时的 DB rowset 作为诊断材料，但不把它提升为同步业务后置条件。`action.finished` 和依赖该终态的 `user.*` 义务保持 missing/not_evaluated，原始观察标明 `http_terminal_unknown`；不得因零行自动 FAILED，也不得因恰好一行 PASSED。
- [ ] 写端到端事实比较测试：HTTP 终态未知＋零行→INCOMPLETE，未知＋一行→INCOMPLETE；已知成功 envelope＋零行→FAILED；已知终态真实回滚→FAILED。恢复同一 action_started 保留原诊断、不重发 POST；正常路径仍只做一次有界后态读取。
- [ ] **R10** 将 `SubprocessVerificationHost` 接入 `VerifiedExecutionHandler` 的默认构造。它只接受安装代码生成的固定 argv、测试视图、nodeid、case_id 和现有预算；外部 `.aa/` 配置不能提供命令、模块或 Python 类。Docker 不可用、qualification 文件缺失以及 Colima 未启动时，普通 verified 执行仍能完成。

- [ ] 先写真实子进程测试：空入口、assert True、薄合法桥接、伪造完成帧、重复 execute 请求、未知 case、取消/超时及零收集。以下生成入口必须合法，通过与否取决于父级证据：

```python
from assurance_execution.bridge import execute_case

def test_create_user():
    execute_case("TC_USER_CREATE_001")
```

- [ ] `SubprocessVerificationHost` 使用当前安装环境的 `sys.executable`，固定运行已安装的 `assurance_execution.bridge_runner`。pytest 与 pytest-json-report 是 execution wheel 的声明运行依赖；普通执行不在线安装依赖，也不从 PATH 或项目配置选择另一份 pytest。HTTPX 同样是宿主 execution wheel 的运行依赖，不能只加到 root dev。
- [ ] 子进程以测试视图为 cwd，使用显式环境 allowlist，至少固定 `PYTHONUNBUFFERED=1`、`PYTHONDONTWRITEBYTECODE=1`，并移除 API token、数据库路径、host authority/secret handles、Collector 地址和证据路径。允许传递的临时目录必须位于本 Attempt 的测试临时根。该控制只减少意外泄漏，不描述为 OS 沙箱。
- [ ] 新业务后端使用固定 argv 列表，不调用 shell；外部配置不能改变命令：

```python
def subprocess_argv() -> list[str]:
    return [
        sys.executable,
        "-m",
        "assurance_execution.bridge_runner",
    ]
```

- [ ] bridge_runner 在 pytest 捕获前保存原始管道，将 pytest 普通 stdout 转 stderr；stdout 仅传长度受限 JSONL 帧。协议仅 `execute(case_id)`、`runner_report(raw)`，父级返回 ack/error；所有子进程帧都不构成 oracle 完成证据。禁止 TTY、shell、任意 frame 路径和额外参数；分片、超长、非法 JSON、未知 frame 均记录协议错误。
- [ ] 通过 production activity/dispatch receipt 绑定当前授权，在外部动作前持久化 `action_started`；父进程先确认初态，实际 HTTP POST 一次、读取后态并保存观察。HTTPX 设置 `follow_redirects=False`、10 秒总边界，不自动重试；凭据只在父级解析。函数输出只含真实实际值，expected 仍引用 T1/T2。
- [ ] 使用 Popen 和有限等待/取消，默认 runner 总预算 60 秒，单帧最大 256 KiB、stderr 最多 8 MiB；这些是执行配置并进入摘要，不能由测试改变。终止并 wait/reap 本次拥有的进程组后再封存，不能留下 pytest child。动作未知时保留 INCOMPLETE。恢复发现 action_started 且无终态时不执行第二次；再评估已有完整 evidence 时不重复请求。
- [ ] 新 host executor 实现 recover/reconcile，符合 production task host 和 RecoverableTaskHandler/activity 端口。恢复界限：有终态但尚未 seal/promote 时采用相同材料；dispatch 已开始但动作终态未知时形成可提交的 INCOMPLETE；没有可认证历史时拒绝，不重发 POST。真实 cut 测试覆盖 dispatch 前、HTTP 返回但 terminal 未保存、seal 前及 promotion 后；同一 attempt 的实际 POST 总数最多 1。TaskAttemptContract 重试预算不代替此验证。
- [ ] 产品测试必须用真实 subprocess bridge 完成父级 HTTP→SQLite，不仅使用 FakePytestHost；同时证明子进程帧、pytest exit code、伪造 runner report 和子进程自报结果都不能生成或替代父级 oracle 事实。显式 OCI 实验仍可证明更强的文件/网络/canary 隔离，但其缺失只记录“OCI 实验未运行”，不影响本 Task、full workflow 或交付结论。
- [ ] 将 `test_verified_runner_qualification.py` 改为仅在 `AA_RUN_OCI_QUALIFICATION=1` 时收集真实资格断言；默认 `uv run pytest` 明确显示为可选实验未启用，不因缺少 Docker/记录而失败。显式实验命令固定为先运行 `uv run python scripts/build_verification_runner.py`，再运行 `AA_RUN_OCI_QUALIFICATION=1 uv run pytest packages/capabilities/assurance-execution/tests/test_verified_runner_qualification.py -q`；实验失败只使该实验失败，不能改写业务 verdict。
- [ ] Run: `uv run pytest packages/capabilities/assurance-execution/tests/test_verified_process.py packages/capabilities/assurance-execution/tests/test_verified_execution.py packages/capabilities/assurance-execution/tests/test_runner.py -q`。提交 `fix(execution): run verified bridge without OCI dependency`。

## Task 5: 冻结 profile 与 semantic attempt 装配

**Files:** 新建 product `verification_execution.py`；修改 product `models.py`、`binding_builder.py`、`agent_contracts.py`、`runtime_bindings.py`、`runtime_ports.py`、`product.py`、`graphs/revisions.py` 和配置 schemas；execution `contracts/{attempts,verification}.py`、`graphs/{factory,nodes,state}.py`、注册/declaration。新增 `tests/product/test_verified_execution_profile.py`、`test_verified_attempt_recovery.py`。

**Interfaces:** `validation_profile: Literal['api_db.v1','api_db_trace.v1'] | None` 是唯一模式，源于已认证部署资源并进入 ProductLock 与根计划。新增 `assurance.execution.task.execute.v1` / `assurance.execution.task.run.v1` 两个 TaskAttemptContract，显式 input/output model、resources、retry(max_attempts=1)、timeout、validators。`ProfiledExecutionExecutor.execute(validated_input, scope)` 和 `reconcile(...)` 遵循现有 attempt/production host 协议；下游 host 使用 T4 handler。

**2026-09-09 补修 R4：** 继续修改 product `verification_execution.py`、`runtime_ports.py`；execution `operations/{verified_attempt,user_attempt,readiness}.py`、必要的 `contracts/{agent,readiness}.py`；补测 `tests/product/test_verified_readiness.py`、`test_verified_attempt_recovery.py`。SUT 生命周期只在现有 execution host 内装配。

**2026-09-09 补修 R10：** 继续修改 product `models.py`、`verification_execution.py` 与 execution `operations/verified_attempt.py`，从 verified profile 的配置、preflight、binding data 和恢复检查中移除 runner qualification。现有 `runner.source_root` 承担了另一个必要职责，改名并上移为 `VerificationHostConfigV1.sut_source_root`，仅用于认证和物化受控 User SUT；它不选择 pytest runner。固定 subprocess 后端由已安装 execution wheel 决定，不增加第二个 profile/mode 参数。

- [ ] root preflight 只验证已冻结 profile、SUT 源制品/依赖锁及必要 secret handles 的可用性；Trace 模式还验证已锁定 Collector/OTel 制品资格。它不读取 runner qualification、不执行 Docker 命令，也不要求未来 case/Attempt 的 execution_id、运行中 SUT、动态地址或 DB selection。保留原 full 其他 Agent 的预检。
- [ ] 进入已授权的 `execution.execute/run` host 后，先认证 generation closure，再调用已有 `start_user_attempt`，从该 Attempt 身份派生 execution_id、独占 SUT/SQLite、凭据覆盖和动态 readiness；在实际 POST 前完成实例/DB/源码核验和 manifest 冻结。输入只携带冻结配置与已接纳引用，动态资料留在 host activity 和既有证据产物中。不能把起停逻辑塞进 `adapt_execution` 或 graph route。
- [ ] 为既有 execute/reconcile/cancel 接上同一生命周期：启动失败也清理已拥有进程；正常完成在证据封存后清理；未知动作形成 INCOMPLETE；恢复读取原 activity/authority，不创建第二个实例或重新登录/POST。过期 liveness receipt 或已经正常停止的 SUT/Collector，不得阻止已封存事实重放及 cleanup。不同的新 Attempt 则必须获得新实例和业务键。
- [ ] 单独验证 root 无实例也能进入 intake、进入执行后才生成实例，以及同一 invocation 两次 Attempt 使用不同 SQLite。对源码/依赖锁/handle 不合法的静态输入仍立即 NOT_READY；Docker/Colima/qualification 缺失不能阻止执行。对实例不一致的动态输入在 POST 前 NOT_READY。保留 `test_verified_attempt_recovery.py` 原 crash-cut 保证，并在 T10 用真实产品绑定验证，不靠手填完整 verification/readiness。

- [ ] 先写 composition 红灯测试：两种 profile 均能 boot；未知值、输入/lock/根计划不一致均拒绝；profile=None 的 legacy 执行行为保持；新模式不会调用 raw Agent executor；仍有同一组产品业务 semantic node IDs。
- [ ] graph factory 将现有业务节点的 contract ID 换成两个稳定 Task facade；profile 选择不放到 Feature graph context。product 的封闭装配只支持：None→现有 ResolvedRawAgentExecutor，api_db.v1/api_db_trace.v1→T4 使用固定 subprocess bridge 的可恢复 host executor。执行与 reconcile 必须选择同一已冻结 delegate，不嵌套第二个 kernel attempt。

```python
def execution_backend(profile: str | None) -> str:
    if profile is None:
        return "legacy_raw_agent"
    if profile in {"api_db.v1", "api_db_trace.v1"}:
        return "verified_host"
    raise ValueError("unknown validation profile")
```

- [ ] 将 execution Task catalog 加入 product `FEATURE_TASK_ATTEMPT_CONTRACTS`；更新 `_resolve_task_contract`/boot 特定闭集装配，使两条 facade 得到对应 executor。仍认证当前安装目录全部 Agent binding 的目标/secret/resource closure，不将 raw runtime target 替换为 oracle handler。新增 task 及 delegate contract/schema/source/config 摘要全部纳入现有安装声明与 ProductLock/GraphBuildManifest 认证。
- [ ] facade contract 的 I/O schema、resources、retry、timeout 固定发布；不按profile动态改同名contract的canonical projection。profile/config摘要进入已认证资源、ProductLock与validated input，GraphRevision/AttemptKey沿既有机制绑定它们；execute和reconcile都验证输入与冻结配置相等。
- [ ] **R10 配置收敛：** 从当前 `DeploymentBindingsV1.verification_host`、binding builder、配置 schema 与 Task binding data 删除 `runner` / `qualification_path` / `qualification_digest`；新增唯一必要字段 `sut_source_root`，继承原 `runner.source_root` 的源码定位、project confinement、运行锁和 managed-SUT authority 认证。host binding 只传 `user_host.sut_source_root`，不再传 `verification_runner`。OCI 实验直接读取仓库内 `runner-lock.json` 和显式构建输出，不进入 `.aa/`、ProductLock 的业务配置或 AttemptKey。不要新增 `runner_mode`、`use_docker`、第三个 validation profile 或自动 fallback。
- [ ] 新contract/schema导致GraphRevision变化时重新compile/start；不把旧运行中的invocation换装新contract继续执行。旧档案按版本读取，新invocation的resume只允许同一lock/revision/profile；增加漂移拒绝测试。
- [ ] facade 的 resources、输出契约及 timeout 覆盖实际选定 delegate；legacy phase claims 继续有效，verified 新事实只能由 host 写。检查全仓 semantic contract 数量硬编码与相关快照，改成对安装目录精确集合的比较；不可简单删掉闭集检查。Agent binding 的集合保持远程 main 的现有定义，不把新 task 塞入 Agent routes。
- [ ] 在 `runtime_ports._bind_executor_host` 的已安装装配路径穿透facade，为实际delegate接上production host/activity/reconcile；验证invocation、AttemptKey、授权fence和资源路径。当前裸 `_task_context` 的空activity/secrets、恒false cancel及以workspace identity代替lock的metadata不可当作新后端授权；由host绑定真实锁/授权/activity。attempt兼容字段仍不作运行身份依据。凭据只由受控secret handle或固定harness临时凭据交给父级 host，不能进入候选文件、pytest 子进程环境或report。
- [ ] 更新 `_preflight_selected_root` 当前仅凭 `.agent.` 名称检测的逻辑，用安装代码中的闭集facade→实际backend依赖展开；legacy facade仍预检OpenCode，verified分支只预检本后端，full的其他Agent节点仍照常预检。不得因隐藏在facade后而漏掉依赖/授权。
- [ ] 按 R4/R10 区分静态输入与动态实例：`api_db.v1` 的 root 预检源锁和 host handles，实际 SUT/DB 在 execution Attempt 内启动核验；只有 `api_db_trace.v1` 额外要求 Collector/OTel。两个 profile 共用同一固定 subprocess bridge，不读取 OCI 配置。缺少所选业务能力输出 NOT_READY，阶段一不依赖 Collector。prepared/dispatch_started/terminal/sealed/promoted 各 cut 的恢复调用同一 delegate，未知 POST 终态不得因 retry 或 resume 再次触发动作。
- [ ] Run: `uv run pytest tests/product/test_verified_execution_profile.py tests/product/test_verified_attempt_recovery.py tests/product/test_semantic_attempt_bindings.py tests/product/test_agent_execution_contracts.py tests/product/test_composition_authority.py tests/product/test_graph_revision_contracts.py tests/product/test_full_graph_audit.py -q`。提交 `feat(product): compose verified semantic execution attempts`。

## Task 6: 将生成物接到权威执行及重跑

**Files:** 修改 generation `contracts/{agent,codegen}.py`、`operations/codegen.py`、API codegen skill、generated-files validator；execution `contracts/{agent,evidence,workflow,attempts}.py`、`graphs/{nodes,state}.py`、`operations/agent_skills.py`、`operations/__init__.py`、plugin/schema/声明；相关 fixtures。新增 `packages/capabilities/assurance-generation/tests/test_verified_codegen.py`。

**Interfaces:** codegen/mapping 同时携带既有 Assurance Plan 与独立 case execution plan ref/digest、ReviewedCase/epoch 身份。Task facade 输出 `ExecutionDispatchResultV1`；新模式 host 返回 `VerifiedExecutionResultV1`，kernel 接纳后 graph publish 形成 `VerifiedExecutionCycleResultV1`；legacy 保留原 Evidence/CycleV1。

- [ ] 红灯测试覆盖生成映射丢 case、绑定旧 plan、修改规范 digest，以及“Agent 全通过但没有 host evidence”。复用现有 mapping_document/codegen_input fixtures，通过普通生成物接纳路径放入真实候选测试，不预填 runtime verdict。
- [ ] 新 profile 下 codegen 生成 Task 4 的 bridge 入口，不再复制 HTTP/DB/Trace 判定实现；不得生成 execution_id，expected 或数据库路径不写入测试代码。额外 assert 的个数不用于通过判据。
- [ ] 共用现有 selection/view 校验核心，分别为 legacy prepare 与 verified host 准备输入。new profile 不穿过 AgentRunRequest/AgentRunResult 解析器；按冻结 profile 明确选择 typed result：

```python
def execution_result_branch(profile: str | None) -> str:
    if profile in {"api_db.v1", "api_db_trace.v1"}:
        return "verified_host"
    if profile is None:
        return "legacy_agent"
    raise ValueError("unknown validation profile")
```

- [ ] 逐项原始 evidence 的来源绑定由父级接纳记录与运行清单认证；不从 Agent structured_result 构造这些事实。所有测试未触发 bridge 时必须发布可保存的 incomplete，不因 pytest exit 0 补记录。
- [ ] 首轮和 healing 重跑共用 host；当前索引与按 attempt/execution_id 的不可覆盖事实分别保存。映射、ReviewedCase、epoch/repair_round、根计划与机器计划均绑定 kernel receipt；旧成功不得补新 attempt。生成到 execution 的 DTO/schema/state/claims 同步新字段。
- [ ] Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_verified_codegen.py packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-execution/tests/test_agent_skills.py packages/capabilities/assurance-execution/tests/test_verified_execution.py tests/product/test_change_local_output_routing.py -q`。提交 `feat(generation): connect generated cases to verified execution`。

## Task 7: 接通现有 assessment 的确定性判定与报告

**Files:** 新建 quality `contracts/verification.py`、`operations/verification.py`；修改 `operations/{assessment,agent_skills,inspect,metrics,report}.py`、`contracts/{assessment,attempts}.py`、`graphs/{assessment,nodes,state,routes}.py`；execution `graphs/{nodes,state}.py`、`contracts/workflow.py`；product `graphs/{execute,routes,state,tail_contracts}.py`。新增 quality `tests/test_verification.py`，扩展 `tests/test_assessment_materialization.py`。

**Interfaces:** `evaluate_verification(...) -> VerificationVerdictV1` 是唯一业务算法。`materialize_assessment_inputs` 先认证当前根计划、ReviewedCase、generation、执行 cycle 与证据来源，再向既有 fact-baseline/inspect 传递确定性材料。verification verdict、pytest status、coverage state 与 InspectionDisposition 分开。

**2026-09-09 补修 R3/R5：** 修改 quality `operations/assessment.py::_journal_observations`、必要的 `operations/verification.py`，product `verification_quality.py::ProfiledAssessmentExecutor.execute`；扩展 `tests/product/test_execution_quality_flow.py`、`test_repair_authorization.py` 及 quality `tests/test_verification.py`。保留现有 materialize-assessment task 和 handler。

- [ ] **R3** 认证原始 journal 后重放与 T4 相同的动作终态依赖：未知 HTTP 下的 rowset 仅作诊断，不评价同步 `user.*` 后置条件。测试完整 materializer 的 outcome 为 INCOMPLETE；已确认业务错误叠加遥测缺失仍为 FAILED 并保留 missing evidence，不把所有不完整一律覆盖成同一种结论。
- [ ] **R5** 产品 executor 显式接受现有 `VerifiedExecutionCycleResultV1` 和 `VerifiedIncompleteExecutionV1`。前者从 cycle、后者从 `defect.validation_profile` 核对冻结 profile，继续走原 host handler 与身份认证；legacy 拒绝两类 verified 输入。不能将 incomplete 强转为正常 cycle，也不能伪造尚未执行的 SUT/DB/Trace receipt。
- [ ] 新增通过 `runtime_bindings` 实际安装 executor 的测试：合法 missing-bridge defect 到达 materializer、inspect 后得到 `repairable_execution_failure`；普通环境/遥测不完整得到 blocked；错误 profile、旧 Attempt/伪造 defect 被拒绝。已有直接调用 `materialize_assessment_inputs` 的测试保留为单元测试，但不再作为产品装配闭环的证据。

- [ ] 红灯用例覆盖全部正确→PASSED；错误字段→FAILED；未运行 oracle/查询错误→INCOMPLETE；业务违反与 telemetry 缺失并存→FAILED 且保留缺失明细。实际 facts 来自 T3/T4 或在冻结故障边界制造。

```python
def reduce_verdict(*, violated: bool, complete: bool, runner_ok: bool) -> str:
    if violated:
        return "FAILED"
    if not complete or not runner_ok:
        return "INCOMPLETE"
    return "PASSED"
```

- [ ] 先验证身份/摘要/重复，再比较原始 actual 与规格 expected。required 从规范/profile 重建，分别计算 required/executed/evaluated/satisfied；失败断言算已评价，缺失/跳过不能缩小分母，空义务不能成为100%。
- [ ] `publish_execution → product adapt_execute/adapt_quality → route_execute/route_run → MaterializeAssessmentInput → materialize_assessment_inputs → InspectFinalize` 全链按显式 profile 处理新的 cycle DTO。不得把 collected/incomplete 转换成 legacy PASS/FAIL；封存了不完整事实的 kernel attempt 可 committed，但其业务仍不能通过。无法认证的材料必须拒绝。
- [ ] 在现有 assessment materializer 校验只读 evidence 引用并装配/保存 verification facts；InspectFinalize 用唯一 evaluator 计算或复核结论，Agent 自报 pass/100% 仅作不可信说明。report 引用相同 verdict/receipt，加入逐义务 actual/expected 安全差异和 business/evidence 状态。
- [ ] 映射到现有闭集 disposition：verification PASSED 且既有 coverage 门槛也满足→satisfied；PASSED 但 coverage 不足→coverage_insufficient；明确 SUT 业务错误→needs_human（保留 FAILED）；环境/遥测/动作未知→blocked（保留 INCOMPLETE）；确定的生成入口缺 bridge 且可修复→repairable_execution_failure。后者才进入既有修复。不得新增 exhausted/inconclusive/repair_required 字符串冒充现有 InspectionDisposition。
- [ ] 真实图测试验证 collected 与 incomplete 均到达 assessment，错误值和缺遥测均得到诊断，既有 coverage insufficiency 仍能走原 Case 回流；仅提高 assertion tally 或 Agent 文本无法变成 satisfied。诊断不经过成功 report→achieved 路由。
- [ ] Run: `uv run pytest packages/capabilities/assurance-quality/tests/test_verification.py packages/capabilities/assurance-quality/tests/test_assessment_materialization.py packages/capabilities/assurance-quality/tests/test_agent_skills.py tests/product/test_execution_quality_flow.py tests/product/test_report_flow.py tests/product/test_coverage_loop.py tests/product/test_full_graph_audit.py -q` 和 `uv run lint-imports`。提交 `feat(quality): judge verified evidence through assessment`。

## Task 8: healing 不得削弱业务义务

**Files:** 新建 healing `operations/verification_guard.py`；修改 `operations/{application,safety,workflow_state}.py`、`contracts/application.py`、`graphs/{nodes,state}.py`、product `graphs/execute.py` 的 repair 输入适配、aa-apply-test-repair skill 与契约。新增 `packages/capabilities/assurance-healing/tests/test_verification_guard.py`，扩展 `tests/verification_support.py` 与 `tests/product/test_repair_authorization.py`。

**Interfaces:** `assert_same_obligations(before, after) -> None`，只消费 generation contracts；候选绑定允许指向重构后的新 SUT 摘要，但要求重新编译并保留规范。

**2026-09-09 补修 R5/R8：** 继续修改 product `repair_authorization.py`、必要的 execution 公共 contracts，测试 `tests/product/test_repair_authorization.py`。R8 是封装收敛，不能以简化为由削弱已有 stale attempt、receipt 或输入摘要检查。

- [ ] **R8** `RepairAuthorizationIssuer` 不再依赖 `assurance_execution.graphs.nodes` 的私有 selector/activation。优先从已发布 defect/receipt 定位 journal，再以当前 invocation、graph revision、semantic node、根计划、generation、epoch/round 和输入摘要核对确为当前执行。如果仍需重算输入与 AttemptKey，将现有最小纯投影放到 execution 所有的公共 contract，由 graph 与 issuer 复用；不引入授权 registry、服务或新的 receipt 层。仅信任传入 attempt_key 自报值不可接受。
- [ ] **R5** 在 T7 安装 executor 修正后，补实际 `execution → installed assessment → inspect → repair authorization → apply-test-repair → execution.run` 测试。合法 bridge 修复保留冻结业务义务并获得新 execution_id；删 oracle、改 expected、换旧 receipt 均在真实接纳处拒绝，不通过 monkeypatch admission 或绕过 product wrapper 来证明 full 通过。

- [ ] 先写删除 oracle、required 改 optional、expected 改实际值、增加超时、Trace 降级、换旧执行证据均被拒绝的参数测试。
- [ ] 将受保护字段形成规范化投影，必须包括规范/source摘要、断言引用/比较器、初态、profile、完成预算、Trace 关系语义。技术定位单独比较：

```python
PROTECTED_PLAN_FIELDS = (
    "plan_digest", "spec_digest", "sources_digest", "validation_profile",
    "required_obligation_ids", "assertion_refs", "initial_state", "completion", "trace_requirements",
)

def assert_protected_fields_equal(before: dict, after: dict) -> None:
    changed = [key for key in PROTECTED_PLAN_FIELDS if before[key] != after[key]]
    if changed:
        raise ValueError("verification obligations changed: " + ", ".join(changed))
```

- [ ] 在 full 实际 `ApplyTestRepairFinalizeHandler → _verify_application` 接纳边界调用 guard，并将根计划、machine plan、规范/source/profile 引用加入 AppliedTestRepair 输入链；复用当前 main 的 apply-test-repair 路径，不恢复已删除的 generation codegen-fix 节点。不能只在未被 full 使用的 safety helper 或 Agent 自报字段上校验。缺基线材料同样拒绝修复接纳。旧测试维持原安全规则，新模式追加冻结计划保护。
- [ ] 先贯通 product `adapt_repair_failure` 与 healing `_approved_sources` 的 profile-aware 来源认证。后者当前强制 legacy ExecutionEvidenceV1.status=failed 且存在failed test；新模式改为认证当前verified cycle/receipt、批准的修复提案和执行事实，仅允许已确定的生成缺陷（如missing bridge）进入修复，不能把任意INCOMPLETE都视为可修复。healing只消费允许的上游contracts与自身输入模型，不import quality实现。保留legacy failed门槛。
- [ ] 增加正向例：只改 Python 函数定位、等价 ORM/SQL 绑定和新增 helper span，更新 SUT/技术配置摘要后重新编译，通过同一业务预期的真实执行；重跑新 execution_id，旧结果保留。
- [ ] 在 `test_application.py` 和真实 apply-test-repair→rerun 产品路径测试删除/降级义务被拒绝，合法技术修复被接纳并获得新execution_id；不能只测guard纯函数。
- [ ] Run: `uv run pytest packages/capabilities/assurance-healing/tests/test_verification_guard.py packages/capabilities/assurance-healing/tests/test_application.py packages/capabilities/assurance-healing/tests/test_safety.py packages/capabilities/assurance-healing/tests/test_workflow_state.py tests/product/test_issue_healing_flow.py tests/product/test_repair_authorization.py -q`。提交 `feat(healing): preserve frozen verification obligations`。

## Task 9: 扩展既有终态认证、achieved 与 export

**Files:** 新建 product `verification.py`；修改 `application.py`、`status.py`、`models.py`、`export.py` 和状态/发布 schemas。新增 `tests/product/test_verified_delivery.py`，扩展 `test_terminal_full_closure.py`、`test_quality_achieved_gate.py`、`test_achieved_terminal.py`、`test_cli_export.py`、`test_publish_recovery.py`。

**Interfaces:** `authenticate_verified_delivery(project, change_id, invocation_id)` 从当前 checkpoint 的 execution/quality gate 与 kernel receipt 解析有效材料，核对两类 plan、epoch、mapping、generation、execution IDs、verdict/原始证据摘要。它接入现有 `_terminalize_full_if_achieved → finalize_achieved → publish_achieved`，不建立第二套发布流程。

- [ ] 经真实 application run/resume 的红灯测试：full terminal 自报 achieved，但 verification 缺 DB evidence、旧 execution_id 或非 PASSED，不能保存成功 status/apply manifest/export receipt。还覆盖已持久化 achieved 快速路径上的证据漂移。
- [ ] 保留 application 对完整 terminal envelope、selected families 和已持久化终态的校验；在 `finalize_achieved` 的现有 execution/quality gate 加入新模式分支，在写 apply manifest/achieved 前完成 verification 认证。普通 status 读取不新增有副作用的发布。

```text
当前 full checkpoint / kernel receipts
→ 既有 invocation、selected families、生成物门禁
→ 新模式的根计划/机器计划/证据/verification 认证
→ 既有 apply manifest 与 achieved 持久化
→ export 复核同一证据引用、幂等 publish receipt
```

- [ ] 保留 `_require_execution_gate` 按当前 `semantic_node_id` 选择 execute/run 的机制；扩展它对新 cycle/index 的认证。新模式按当前 epoch/repair_round/receipt 检查前驱关系，不照搬 legacy“首轮 pytest failed”作为重跑唯一前提，因为首轮可能 collected 但缺 bridge/oracle。首次成功/失败及历次证据永久保留，不取文件时间最新者。
- [ ] PASSED 还须满足原质量门槛；FAILED/INCOMPLETE/NOT_READY 不得借 legacy passed 补齐。export 与已发布快路径都核对 profile、plan 与证据摘要；输入漂移拒绝发布/复用 receipt，不能悄悄重算覆盖。
- [ ] Run: `uv run pytest tests/product/test_verified_delivery.py tests/product/test_terminal_full_closure.py tests/product/test_quality_achieved_gate.py tests/product/test_achieved_terminal.py tests/product/test_cli_export.py tests/product/test_publish_recovery.py tests/product/test_export_security.py -q`。提交 `feat(product): extend terminal gates with verification receipts`。

## Task 10: User API＋DB 的真实 full benchmark

**Files:** 修改 benchmark `run_item.py`、`manifest.json`、`tests/validate_live_run.py`、T3 harness；沿用已提交的 `benchmark/vue-fastapi-admin/benchmark/requirements/user-create-oracle.md`、`tests/product/test_user_oracle_full_workflow.py`，扩展为实际产品路径测试；更新既有 manifest/layout 测试。T2/T3/T4/T5/T7/T8 的生产补修在各自 Task 中提交，不藏进 benchmark fixture。

**当前状态：** 已有真实本地 HTTP/SQLite 生命周期、wrong-value/rollback-success 变体、独立 Attempt helper 与来源快照检查。User item 尚未进入 manifest，run_item 尚未接入新模式，`start_user_attempt` 尚无生产调用者。名为 full_workflow 的现有测试只证明前置能力；不能标记本 Task 已完成。

**Interfaces:** 新 manifest item ID 固定为 `opencode-user-api-db`；`entrypoint=full`、`run_mode=case`、`case_modules=['system/user']`、`selected_test_families=['api']`（manifest 字段）、`validation_profile='api_db.v1'`。现有 run_item 转成 ProductInput.candidate_test_families=[api]；policy.required/allowed 与本轮 quality goal 均限定 API，真实 intake.resolve-plan 冻结 selected=[api]。harness/run_item 的 `--fault` 闭集值为 `none,missing-binding,no-bridge,no-action,skip-oracle,wrong-value,rollback,rollback-success,db-unavailable,wrong-environment,unknown-http,forged-evidence,downgrade,missing-write`；其中 `no-action` 为本次 R9 补充，第二阶段在 T13 扩展 Trace 值。故障选择在本次 full 启动前冻结。

- [ ] 更新 manifest 校验红灯测试，添加独立 User item；保留最新远程 main 的 Dept API-only 选择、模型路由和现有要求，不恢复旧四 family 约束。两种新 profile 限制最终 selected 只能为 API；不能向 ProductInput 填已移除的 selected_test_families 或绕过 resolve-plan。
- [ ] 先完成 R1/R2/R3/R4/R5/R7/R10 的确定性补修测试和 T8 的 R8 封装补修。用现有 scripted Agent 驱动安装产品的完整 graph，从空 change 逐节点产生 case/review/bindings/机器计划/codegen；禁止使用 `accepted_verified_execution_input` 预填链路产物、测试侧预造完整 context/readiness，或直接调用底层 materializer 替代产品 executor。这一测试只证明装配，之后仍须真实 Agent full 验收。测试环境没有 Docker CLI、Colima、qualification 文件或 image 时仍必须通过产品装配并到达真实 subprocess 执行。
- [ ] 在 `run_item.main` 的 runtime 分派处，新 profile 跳过现有 `_managed_sut_runtime`（它固定9999/3100并要求web/node_modules），由T4 host管理每个case attempt的SUT/DB及第二阶段Collector。run_item只准备显式project副本和冻结配置，使用动态受控端口，API-only不启动frontend。添加同invocation两次attempt都能独立start/drain/stop、无旧runtime抢占的测试。必要节点必须真正 succeeded，不接受 `_status_steps` 当前把 stopped 算完成的做法。
- [ ] 需求文件收窄到 User 创建与本 spec 八条断言，不宣称整个 User 管理模块已覆盖。harness 的源文件摘要清单包含 app、migrations、配置及启动覆盖；原 SUT 源码漂移时预检失败，不能拿主仓库 Git HEAD 替代。
- [ ] 同一真实 full invocation 从空 change 开始，经 intake/explore/resolve-plan/case/review/机器plan/review/codegen，执行本次生成测试，再质量判定/报告/retro/apply/achieved/export。沿用现有真实 Agent adapter；scripted Agent 只用于快速产品路由测试，不能充当此 live 门槛。
- [ ] 将空 change 初态、root plan profile、来源制品清单、节点因果顺序和关键阶段产物 digest 写入现有 benchmark evidence；必经节点必须是 succeeded，review 还须通过，执行 kernel committed 不等于业务 PASSED。正常与故障使用独立 invocation，不能从历史成功目录选最新文件补齐。
- [ ] 扩展阶段一明确结果：unknown-http 的诊断零行仍为 INCOMPLETE，恢复 POST 次数不增加；错运行源码/错 DB/业务键已占用在动作前阻止；普通业务失败/证据缺失不能通过修复降级。通过现有 product terminal/export 验证这些结果，不只断言 helper 返回值。T7/T8 的合法 bridge defect 接纳/授权测试独立记录，不能冒充实际已运行 pytest 的 A03。
- [ ] 固定故障位置：missing-binding 在机器计划候选接纳前注入；no-bridge 在生成候选接纳前形成合法但未调用桥接的入口；skip-oracle 在父级已声明故障边界跳过观察；wrong-value 在 SUT 写入时更改 is_active；不事后改已冻结材料再补摘要。
- [ ] **R9 区分候选拒绝与运行遗漏。** no-bridge 候选应由现有严格 generation admission 拒绝，不能计作 A03。A03 使用新增 no-action：正常生成并实际运行 bridge 测试，复用固定 benchmark fault 绑定，在父级 `VerifiedExecutionHandler` 现有 action callback 中跳过实际动作、不补写 action/oracle 记录；pytest exit 0 仍产生普通 verified cycle 的 `completion_status=incomplete`，最终 INCOMPLETE 且拒绝交付。该分支只属于仓库闭集故障 harness，不开放任意 hook。`VerifiedIncompleteExecutionV1` 是执行前 bridge defect，与运行期 no-action 分开，后者不能自动获得 healing 授权。
- [ ] rollback 变体在原 UserController.create_user 外包 `in_transaction('sqlite')`，原函数真实写入后抛固定异常；独立 harness 记录事务连接/写入/回滚事实。rollback-success 在事务退出后返回成功 envelope。未包装事务就抛错不得当作 rollback 验收。
- [ ] 修改 benchmark 成功判据：正常项要求 achieved/export；故障项的 benchmark harness 成功表示正确得到预期 NOT_READY/FAILED/INCOMPLETE 且拒绝交付，业务用例自身保持失败状态。
- [ ] 运行以下新增 CLI 路径，保存 ledger、各版本摘要、manifest、DB原始观察与最终判定：

```bash
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode --fault no-bridge
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode --fault no-action
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode --fault wrong-value
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode --fault rollback-success
```

- [ ] 阶段一完成 spec A01–A09、A15–A19、A22–A25 的适用部分；运行 `uv run pytest tests/product/test_user_oracle_full_workflow.py tests/product/test_phase5_benchmark_manifest.py tests/product/test_phase5_benchmark_change_layout.py tests/phase6/test_final_benchmark_manifest.py -q`。提交 `test(benchmark): verify User API DB through full workflow`，保存阶段一验收结果再进入 T11。

## Task 11: 实际 Tortoise/SQLite OTel 接入与可重现依赖

**Files:** 完善 benchmark `fixtures/user-oracle/{bootstrap.py,collector.yaml,runtime-lock.json,requirements.in,requirements.lock}` 和 harness；修改 execution `pyproject.toml`、根 `uv.lock`；新增 `benchmark/assurance-product/tests/test_user_otel_compatibility.py`。

**Interfaces:** bootstrap 在创建 app/连接前安装 OTel；支持受控 flush/shutdown 回执。runtime-lock 保存实际源文件、bootstrap、完整 Python 依赖锁、Collector 平台/版本/制品摘要；所有 digest 来自实际文件，不填写占位值。

**2026-09-09 范围收敛：** 沿 T5 的同一个 execution host 生命周期，Trace profile 才按 Attempt 启动 Collector，再把动态端点传给受控 SUT。普通 `api_db.v1` 不启动或要求 OTel/Collector。`start_user_attempt` 当前仅接受 `api_db.v1`，本 Task 显式扩展同一闭集入口及 readiness，不能复制第二套 User runtime。

- [ ] Collector 配置使用锁定制品自带的 `health_check`，为本次 Attempt 生成固定 `response_body.healthy` JSON，满足现有 readiness 的 `probe_nonce/execution_id` 核对；保留进程 birth identity、配置/制品摘要与 host receipt。该能力须在选定版本实际验证，不使用默认健康响应假装包含这些字段，也不新建探测服务。健康 GET 只证明当前实例可用，不能充当 OTLP 接收或 drain 完成证明。参考 [上游 health_check 配置](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/extension/healthcheckextension/README.md)。
- [ ] 新增启动/故障/清理测试：Collector 启动失败不执行业务 POST；SUT 退出但 exporter drain 未完成时保留诊断材料；同一 Attempt 恢复不重建 Collector/SUT 来伪造完整性；新 Attempt 使用新输出文件与动态端点。采样设置、导出协议和健康配置均进入本次 runtime lock/receipt，不能由测试代码选择。

- [ ] 先用当前真实 User.save 写兼容测试：必须看到 FastAPI server 下、真实 SqliteClient 调用产生的 CLIENT span；普通路径和实际事务变体均验证，不能只测内存里手工创建 span。
- [ ] 添加官方 SDK、FastAPI/Tortoise instrumentation、OTLP HTTP exporter 的依赖输入，约束 benchmark 现有 FastAPI/Tortoise/aiosqlite 版本；解析并提交完整带 hash 的锁。版本选择作为本任务真实兼容实验输出，不在计划里捏造“已验证组合”。

```bash
uv lock
uv pip compile benchmark/assurance-product/fixtures/user-oracle/requirements.in --generate-hashes --output-file benchmark/assurance-product/fixtures/user-oracle/requirements.lock
```

- [ ] bootstrap 采用官方入口，所有 provider/采样/导出端点来自受控运行配置；以下是待兼容实验验证的调用形状，不是已验收版本组合：

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.tortoiseorm import TortoiseORMInstrumentor

def instrument_sut(app, provider):
    TortoiseORMInstrumentor().instrument(tracer_provider=provider, capture_parameters=False)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
```

- [ ] 在实际 create_user 和 update_roles 均正常返回之后、成功 envelope 之前产生 `user.create.completed`；取实际对象 username。不得命名 committed 或改变正常事务。写 span 与业务检查点可以是 server 的不同子孙，不能强求后发检查点成为之前写调用的父节点。
- [ ] 运行独占 Collector：OTLP HTTP receiver→受控脱敏处理→file JSONL exporter；锁定实际制品，不使用 latest。默认不开 SQL 参数收集；出口先从实际调用提取归一化表/操作，再移除 SQL 原文、参数、密码和认证字段；测试搜索真实凭据 canary，确保 OTLP 与 receipt 未泄漏。
- [ ] 官方 Tortoise span 原始属性可能处在旧/新 semconv 模式，固定正常化适配并保存原版本；未知模式预检失败。commit/rollback span 不作为现有 instrumentor 的保证；User INSERT 真实路径必须验证，其他事务方法旁路不能声称已覆盖。
- [ ] Run: `uv run python benchmark/assurance-product/user_oracle_harness.py verify-otel-compatibility`。该命令在本任务新增，使用锁定 SUT 环境运行 `test_user_otel_compatibility.py`，确认真实数据库与真实 OTLP 文件，而非根开发环境凑齐依赖。提交 `feat(benchmark): instrument the actual User SQLite path`。

## Task 12: Trace 关联、完成与缺失判定

**Files:** 新建 execution `operations/telemetry.py`（读取/收集）和 `contracts/telemetry.py`（由 producer/replay 共用的纯数据归一与关联规则）；扩展 `contracts/{verification,workflow,attempts}.py`、`operations/{verified_execution,verified_attempt,readiness}.py` 及相关 schema/declaration。修改 quality `operations/{assessment,verification}.py`；必要时同步 product `verification.py` 的原始证据闭合读取。新增 execution `tests/test_telemetry.py`、quality `tests/test_trace_verification.py`；扩展 quality `tests/test_verification.py`、`tests/product/test_verified_delivery.py` 和实际 installed assessment 路径测试。

**Interfaces:** `load_otlp_records(path: Path, execution_id: str) -> tuple[dict, ...]` 在 operations 读取有界原始数据；`check_trace_requirements(plan, manifest, spans, completion) -> tuple[ObservationV1, ...]` 在 execution contracts 中作纯事实归一/关联，producer 与 quality 已认证材料重放共用。quality 不 import execution operations，也不重复实现第二份 Trace 解析/匹配算法；业务 expected 比较仍由 quality evaluator 完成。重复 trace_id/span_id 去重，冲突内容拒绝。

**2026-09-09 补修 R6：** Trace 接入必须贯穿“原始材料 → 已提交引用 → assessment 认证重放 → 判定 → 交付”，不能只加 evaluator。沿用既有 journal、EvidenceArtifactRef、completion 和 kernel receipt，不另起独立证据存储系统。

- [ ] 将本次 Collector 原始文件固定为 evidence root 下的 `telemetry.otlp.jsonl`，完成材料为 `telemetry-completion.json`；后者逐项保留 driver flush、SUT flush、Collector drain/退出及封存状态/原因。两者经原 host journal/封存机制绑定 execution_id、manifest/实例、配置、大小与内容摘要，加入 `_journal_refs`、cycle.raw_evidence_refs 和 phase claims。原始 OTLP 不由测试或 Agent 写入，导出完成前不先写 complete。
- [ ] 扩展 `assessment._verified_materials` 的闭集记录/文件检查：先认证 OTLP 与完成材料引用，再通过公共纯函数重放 Trace 观察，最后与 execution 发布值作完整比较。处理缺文件、截断、摘要冲突、旧 execution/实例、重复冲突和额外未提交证据；不因扩展文件种类取消现有四类 action/process journal 的核验。
- [ ] execution 的 outcome/index 和 assessment 的 completion 重建均覆盖 Trace profile；api_db.v1 仍为 not_required。report、achieved/export 及幂等快速路径引用同一份封存 raw refs/完成记录，篡改原始 Trace 或 drain 结果不能靠旧 PASSED 复用发布。
- [ ] 通过真实 installed `materialize-assessment-inputs` 验证合法原始 Trace 可以进入 inspect/report/终态门禁；缺失或被改写的 Trace 不通过。单独 `test_trace_verification.py` 的 synthetic fixture 只证明解析规则，不能计为此接纳验收或 T13 live。

- [ ] 红灯测试真实 OTLP fixture，覆盖业务/DB span 单独缺失、旧 execution_id、相同业务键但错实例、断父链、仅 oracle SELECT、仅角色/审计表写入，以及新增中间 helper 的正常父链。
- [ ] 父级 HTTP driver 创建 client span、注入 W3C trace context 与 execution_id；SUT FastAPI 请求 hook 仅允许测试身份字段。observer 的 span scope/service/role 明确标为 oracle，不能满足 `trace.user_write`。
- [ ] 用户写入匹配来自真实 Tortoise instrumentation scope，SQLite 数据库绑定一致，并归一为 User 持久化调用；不能匹配任意 DB span。允许等价 SQL，不比较完整 SQL字符串、调用次数或固定直接父节点；成功调用接受正确语义下的 UNSET，不要求强制 OK。
- [ ] 完成流程明确且有总预算：

```text
HTTP 动作有终态 → 独立 DB 查询结束 → client span 结束
→ driver provider flush/shutdown → managed SUT provider flush/shutdown
→ Collector 有序关闭 → 原始 JSONL 封存及摘要 → 判定
```

- [ ] 在同一个已封存 `telemetry-completion.json` 中分别保存各阶段完成结果及受控回执引用；不为每个阶段增加新的身份层或存储系统。全量采样不是完成证明。导出错、file 截断、必需字段丢失、drain 超时→INCOMPLETE；晚到数据不能直接覆盖封存评价，显式新评价仍引用原始材料集合。
- [ ] 增加完整组合测试：正常落库但缺 write span→INCOMPLETE；真实回滚且提前发 completed→FAILED；既有业务错误与遥测缺失并存→FAILED＋missing evidence；同一规范在 helper/函数/等价SQL重构后重新绑定新制品仍 PASSED。
- [ ] Run: `uv run pytest packages/capabilities/assurance-execution/tests/test_telemetry.py packages/capabilities/assurance-quality/tests/test_trace_verification.py packages/capabilities/assurance-quality/tests/test_verification.py tests/product/test_execution_quality_flow.py tests/product/test_verified_delivery.py -q`，运行 `uv run lint-imports`，再跑 T11 真实兼容命令。提交 `feat(quality): require correlated and complete User traces`。

## Task 13: 第二阶段 full 故障矩阵与最终交付

**Files:** 新增 manifest item `opencode-user-api-db-trace`；扩展 benchmark fault 枚举与 `tests/validate_live_run.py`，更新 `tests/product/test_user_oracle_full_workflow.py`、`.github/workflows/ci.yml`、三个 wheel/engine smoke 脚本；更新产品 README 使用说明与阶段验收记录。

**Interfaces:** 新故障值 `drop-business-span,drop-write-span,broken-context,stale-trace,drain-timeout,early-completed,refactor`；固定在 full 启动前绑定。`refactor` 是保持业务语义的正向变体，其余 Trace 故障按 spec A11–A14/A21/A26 分类。

**2026-09-09 验收补强 R6/R7/R9：** 本 Task 消费所有前置修正；不得通过修改快照期望、放宽来源检查或跳过实际产品 executor 让矩阵变绿。

- [ ] `refactor` 在 intake/explore 前生成并冻结真实的新制品：函数改名/提取 helper、等价 ORM 写法、增加辅助 span；使实际 runtime 对应新源文件与锁，经过重新评审、bindings 编译和真实执行。业务断言/需求内容及检查点语义不变，技术来源摘要可以变化。不能只替换 trace JSON，或在运行时打补丁却继续使用旧源摘要。
- [ ] 矩阵明确记录阻止阶段与期望 verdict：no-bridge 候选拒绝不冒充运行期验证；no-action/skip-oracle/unknown-http/遥测缺失为 INCOMPLETE；真实错误字段/回滚（含提前 completed）为 FAILED；语义等价 refactor 为 PASSED。已确认业务违反叠加 telemetry 缺失保持 FAILED＋缺失明细，不缩减 required 集合。
- [ ] 每个 Trace 场景验证 raw OTLP/分阶段完成记录的封存及 assessment 重放，正常项须通过现有 achieved/export；缺文件、过期实例或摘要漂移在已发布快速路径也拒绝。首次执行、合法修复重跑和恢复的记录分别保留，不能用普通 incomplete 自动触发 bridge healing。
- [ ] 保存三类交付结果：确定性单元/安装产品图测试、真实 OTel 兼容实验、真实 Agent full 矩阵。任何一类未跑均明确记录未验收；不以另外两类替代，也不将测试文件名含 full 当作验收依据。OCI 隔离结果如显式运行则作为第四类可选安全实验单列，缺失不阻止交付，也不能被描述为已验证隔离。

- [ ] 明确第二项配置只选 `api_db_trace.v1`，同一 User 规范，不复制一份可自行漂移的 expected。Stage1 与 Stage2 共用 DB observer/比较器/业务 assertion IDs。
- [ ] 逐项跑完 full 矩阵，从无预填产物的新 change 开始；每个故障在应到达的真实产品边界被阻止，保存节点状态/门禁结论，禁止用单独 evaluator 的失败输出替代 full 失败。

```bash
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-business-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-write-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault early-completed
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault refactor
```

- [ ] 验证正常运行 publish receipt、apply manifest、测试/规格/计划/证据摘要全部一致；异常运行无成功发布。重复 export 幂等；重跑、恢复、缺证据、legacy/profile降级均不能误报 achieved。
- [ ] 打包验证 execution wheel 具备 HTTPX、pytest、pytest-json-report 与 OTel 等声明运行依赖，SUT 独立锁能重建；所有受控 harness 变体和 bootstrap 已跟踪，不依赖忽略目录中的未提交改动。wheel smoke 必须实际运行固定 subprocess bridge。
- [ ] 普通 CI 不构建镜像、不启动 Docker/Colima、不读取 qualification 文件；确定性测试和 wheel smoke 在仅有 uv workspace 的环境中完成。`scripts/build_verification_runner.py` 与真实 OCI 资格测试只通过独立显式命令运行，结果保存到实验记录且不参与普通 CI、业务 verdict 或 achieved/export。真实 Agent full benchmark 单独按本节命令验收，普通 CI 使用 scripted Agent 做路由测试，二者结果分开保存。
- [ ] 执行全部 CI（每项通过后才进入最终验收归档）：

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

- [ ] 记录每个 live 样例的 outcome、执行时长、环境启动成本、接入改动、误报与限制，不预设性能提升。正常样例达到 achieved 并 export；archive 仍为可选。提交 `test(product): certify full User trace verification and delivery`。

## 5. Spec 验收覆盖映射

| Spec 验收 | 实施/测试任务 |
| --- | --- |
| A01 正常 API＋DB | T1–T10，重点 T10 live full |
| A02 必需 binding/expected 遗漏 | T2 编译与评审、T10 full |
| A03/A04 空执行/未执行 oracle | T4/T6 权威执行、T7 判定、T10 no-action/skip-oracle full；no-bridge 候选拒绝单独记录 |
| A05 错误字段 | T3/T7、T10 full |
| A06/A07 真实回滚/伪成功响应 | T3 独立连接、T10 真事务变体；T13 带 Trace |
| A08/A09 DB错误/错环境/初态冲突 | T3/T4、T10 |
| A10 正常 API＋DB＋Trace | T11–T13 live full |
| A11/A12/A21 遥测缺失/旧证据/断关联/缺write | T12、T13 full |
| A13 回滚前错误 completed | T10 harness、T12 判定、T13 full |
| A14 正常重构 | T3/R7 源到运行制品绑定、T8 义务保护、T11兼容、T12匹配、T13 真实新制品 full |
| A15 弱化义务 | T2/T8/T9、两阶段 full |
| A16/A17 重跑/未知终态 | T3 身份、T4/R3 恢复及后置条件依赖、T7 重放、T9 有效执行索引、两阶段 full |
| A18 伪造 evidence | T4 父级忽略子进程自报事实、T6 接纳、T7/T9 认证、两阶段 full；不依赖 OS 沙箱声明 |
| A19 无显式assert的合法入口 | T4/T6、T10/T13正常 full |
| A20 legacy/profile降级 | T5/T8/T9/T13 |
| A22 其他DB操作冒充写入 | T3/T7/T10，T12/T13加Trace |
| A23/A24/A25 绕过生成/修复/最终门禁 | T6/T8/T9、T10/T13 full |
| A26 真驱动兼容及无凭据泄漏 | T4 固定 subprocess 协议及环境 allowlist、T11 真实锁定兼容、T12/T13；可选 OCI 实验另记 |

## 6. 计划执行与文档维护

每个任务按红灯测试→最小实现→针对性绿灯→精确路径提交执行；不要把一组新增测试全部标 skip 来完成阶段。执行时发现定位随其他已合入改动变化，先更新本任务文件列表和接口，再继续；不据旧行号覆盖用户的新实现。

本次 Review 只修改本 plan。已有提交与待补修区分见文首状态表；新增/调整接口仍须逐任务实现。保留单一 execution_id、同一个 profile 参数、独立 SQLite oracle、两类计划身份及既有 kernel/assessment/修复/终态接线，不扩大产品拓扑。

2026-09-09 审查验证：generation `test_execution_plan.py`、`test_graph_routes.py` 共 68 passed；`tests/product/test_verified_execution_profile.py` 15 passed。第一次产品测试因沙箱内 wheel 构建失败出现 15 个 setup errors，使用 `UV_OFFLINE=true uv run --no-sync pytest tests/product/test_verified_execution_profile.py -q --tb=short` 在本地离线重跑后全部通过。另用现有模型/fixture 确定性复现 R1 齐套输入拒绝、R3 未知 HTTP→FAILED、R5 installed executor 拒绝 incomplete；绿灯测试未覆盖这些缺口。本轮没有执行真实 Agent full、OCI 隔离资格或 OTel 兼容实验，不宣称完整 CI/阶段交付通过。

自检要求：每条 A01–A26 都有上表任务与真实 full 验收；新接口名称前后一致；新增路径和现有路径有明确区分；不把 schema/提示词、单独 helper 绿灯或 synthetic span 当作交付完成。

## 7. 本轮 Review 的定位依据

以下均是本 worktree 基线文件；Task 中标为新增的文件不在此表。

| 范围 | 实际代码入口 |
| --- | --- |
| Python topology/Agent边界 | `AGENTS.md`；execution `graphs/factory.py`；product `agent_contracts.py`、`runtime_bindings.py`、`product.py` |
| Frozen Assurance Plan | intake `contracts/plan.py`、`contracts/workflow.py`、`graphs/nodes.py`；product `models.py`；`tests/product/test_acg_plan_flow.py` |
| 执行视图已接线 | execution `execution_view.py`、`generated_merge.py`、`operations/agent_skills.py`；`tests/product/test_candidate_execution_isolation.py` |
| 恢复与提交 | kernel `attempts/kernel.py`、`attempts/production_host.py`；product `runtime_bindings.py`、`runtime_ports.py` |
| typed质量路由 | execution `contracts/workflow.py`、`graphs/nodes.py`；quality `operations/assessment.py`、`contracts/assessment.py`；product `graphs/execute.py`、`graphs/routes.py` |
| full修复 | healing `graphs/factory.py`、`operations/application.py` |
| 终态已认证 | product `application.py::_terminalize_full_if_achieved`、`status.py::_require_execution_gate` / `_require_quality_gate` |
| SUT缺失与旧runtime | `.gitignore`；benchmark `run_item.py::_resolve_sut` / `_managed_sut_runtime` / `main` |
| 完整CI | `.github/workflows/ci.yml` 的 lint/typecheck/import-contracts/tests/packaging-smoke |

实施前不需要重做数据库选型或新增 workflow 业务节点；需要按本修订完成边界适配。OTel 的版本兼容和真实 Agent full 矩阵仍是交付任务；Docker/OCI 资格仅为独立可选实验。文档 review 不证明这些验收已通过。
