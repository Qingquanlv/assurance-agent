# User Full Workflow DB Oracle and Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 User 创建样例从真实 full workflow 的业务规格生成测试，经确定性执行、独立 SQLite oracle 和可选必需 Trace 校验，只有证据充分且业务正确时才 achieved/export。

**Architecture:** 复用现有 intake/generation/execution/quality/healing/product 的主流程。仅在两个新 profile 下，把已有 execution.execute / execution.run triplet 的 execute slot 绑定到安装的确定性 handler；不新增 Agent、顶层节点或第二套图引擎。业务预期归 intake，机器计划归 generation，运行清单与实际观察归 execution，最终业务判定归 quality，product 负责真实终态认证和交付。

**Tech Stack:** Python 3.11、uv workspace、Pydantic v2、pytest、SQLite、HTTPX；benchmark 的 FastAPI 0.111.0 / Tortoise ORM 0.23.0 / aiosqlite 0.20.0；OTel Python/FastAPI/Tortoise instrumentation、OTLP HTTP、上游 Collector file exporter。受控 pytest 后端使用固定镜像的 Docker CLI 容器与标准输入/输出桥接，SUT/DB 观察仍由宿主执行器管理。

**Spec:** [User 全流程验证规格](/Users/lvqingquan/agent/assurance-agent/docs/superpowers/specs/2026-09-06-business-spec-api-db-oracle-trace-design.md)

## Global Constraints

- 本文件是实施计划，当前没有实现或运行验收；所有任务框初始为空。
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
- 当前工作区有用户的迁移计划、OpenCode adapter、boundary 和测试改动。执行前用 using-git-worktrees 建立隔离工作区；不 stash、覆盖、合并或代为提交这些改动。以当时实际代码为基线复核本文锚点，不从其他未完成迁移计划假定接口已经实现。
- 完整交付保留真实 `aa compile/start/run/status/export` 路径；`entrypoint=full` 与 `run_mode=case` 是不同字段，不能把 run_mode 改成 full。

---

## 1. 已核实的接线缺口与本计划的选择

| 当前事实 | 本计划处理 |
| --- | --- |
| `binding_builder._binding_documents` 把所有 execute alias 指向 runtime adapter；`product.py` 再硬校验一次 | Task 5 仅为 execution 的两组 slot 增加闭集、按 profile 选择的确定性目标；其他 Agent 路由保持原校验 |
| execution 的 finalize 读取 Agent structured_result，再据 pytest 状态形成 passed | Task 4 的 host 实跑；Task 6 的 finalize 接纳显式 typed host result，不伪装 AgentRunResult |
| `ConfinedExecutionProcessHost` 仅限制 argv/报告路径，无 OS 写集隔离，且 subprocess 没有总超时 | Task 4 增加真实受限子进程后端和资格检查；不把现有类名当成隔离保证 |
| `build_execution_view` 有实现和测试，但正式路径没有调用 | Task 3 下移可复用的视图构建核心到 execution，通过 contracts DTO 接入，product 保留薄适配；不复制第二套 merge/view |
| `finalize_achieved` 有门禁，但 CLI `_publish_projections` 尚未调用；`_change_state` 直接将 full completed 映射 achieved | Task 9 接通终态认证及 apply manifest，区分图完成与验证通过；禁止只改孤立 helper |
| planner 两处产物白名单，validator 泛匹配 `*-plan.json` | Task 2 同步白名单，改为精确 artifact 类型分派，避免新机器计划被误判为旧 PlanResult |
| vendored SUT 的 app/依赖文件被忽略，且不是独立 Git 仓库 | Task 3/11 把 harness、覆盖内容、源文件摘要与依赖锁提交到已跟踪 benchmark 目录；不能拿主仓库 HEAD 当 SUT 版本 |

确定性执行不先生成“业务 PASSED”。新 profile 的 execution 输出 `collected` 或 `incomplete`，表示事实收集状态；quality 才输出 PASSED/FAILED/INCOMPLETE。Task 7 调整原有 execute→quality 和 run→quality 两条边的条件，让这两种状态进入既有质量节点；节点和边的数量保持不变。

强隔离后端选择是本计划的具体实施取舍：固定 OCI 镜像内运行薄 pytest，只有只读测试视图和私有临时目录，禁外部网络；通过 `docker run -i` 的父子管道请求父进程执行已分配计划。父进程持有 API 凭据、SQLite 路径和权威证据，容器不挂载宿主数据库、证据目录或 Docker socket。Docker 不可用或资格测试不通过时新 profile 为 NOT_READY；不静默回退到无限制宿主 pytest。无需为 SUT 改成容器部署。相关选项依据 [Docker run 官方文档](https://docs.docker.com/reference/cli/docker/container/run/)，实际隔离以 Task 4 的真实测试为准。

## 2. 交付顺序与任务边界

阶段一：Task 1–10，交付 `api_db.v1` 的真实 User full workflow。阶段二：Task 11–13，交付同一业务规格的 `api_db_trace.v1`。

```text
1 规格与来源 → 2 机器计划 → 3 运行环境/清单 → 4 权威执行 → 5 产品绑定
2 + 4 + 5 → 6 codegen/执行接线 → 7 质量判定 → 8 healing → 9 最终交付 → 10 阶段一 full 验收
10 → 11 OTel 接入与锁定 → 12 Trace 判定/完成 → 13 阶段二 full 验收和 CI
```

主链按编号依赖推进；同一任务内互不修改相同文件的测试素材与实现可并行研究。每个任务有自己的行为测试和独立提交。不把密切依赖的前后链拆成无法独立验证的独立项目。

## 3. 文件与产物布局

以下路径均相对仓库根目录；标为“新”的文件由本计划创建，其余是现有文件。

| 所有者 | 新增文件 | 修改的主要现有入口 |
| --- | --- | --- |
| intake | `assurance_intake/contracts/verification.py`、`resources/schemas/assertion-sources.v1.schema.json` | `contracts/cases.py`、`contracts/workflow.py`、`operations/finalize.py`、`operations/agent_skills.py`、`validators/cases.py`、`plugin.py`、`plugin-declaration.json`、case design/review skills |
| generation | `assurance_generation/contracts/execution_plan.py`、`operations/execution_plan.py`、`resources/schemas/case-execution-plan.v1.schema.json` | `contracts/plans.py`、`contracts/agent.py`、`contracts/codegen.py`、`contracts/workflow.py`、`operations/planning.py`、`operations/review.py`、`operations/codegen.py`、`validators/plans.py`、注册及 API plan/codegen/fixer skills |
| execution | `contracts/verification.py`、`operations/execution_view.py`、`operations/verification_manifest.py`、`operations/sqlite_oracle.py`、`operations/verified_execution.py`、`operations/verified_process.py`、`bridge.py`、`bridge_runner.py`、`operations/telemetry.py` | `operations/runner.py`、`operations/agent_skills.py`、`operations/__init__.py`、`contracts/agent.py`、`contracts/evidence.py`、`contracts/workflow.py`、schemas/注册/依赖 |
| quality | `contracts/verification.py`、`operations/verification.py` | `operations/agent_skills.py`、`operations/inspect.py`、`operations/metrics.py`、`operations/report.py`、相应契约/schema/注册 |
| healing | `operations/verification_guard.py` | `operations/safety.py`、`operations/workflow_state.py`、`validators/repair.py`、API fixer/coverage repair 技能及结果契约 |
| product | `verification.py` | `models.py`、`binding_builder.py`、`product.py`、`agent_contracts.py`、`execution_view.py`、`status.py`、`cli.py`、`export.py`、`resources/workflow/main.yaml`、对应配置 schemas |
| benchmark | `user_oracle_harness.py`、`fixtures/user-oracle/{bootstrap.py,collector.yaml,runtime-lock.json,requirements.in,requirements.lock,runner.Dockerfile,runner-lock.json}` | `run_item.py`、`manifest.json`、`tests/validate_live_run.py` |
| 构建/CI | `scripts/build_verification_runner.py` | `.github/workflows/ci.yml`、`scripts/assurance_product_wheel_smoke_test.sh` |
| 测试素材 | `tests/verification_support.py`、`tests/fixtures/verification/{user-case.json,user-sources.json,user-plan.json}` | 各包现有 fixtures 与产品 full/manifest/交付测试 |

`assurance_*` 的包目录分别在 `packages/capabilities/assurance-*/`；product 文件在 `packages/products/assurance-product/assurance_product/`；benchmark 文件在 `benchmark/assurance-product/`。每个新 schema 同步对应插件 `_SCHEMA_FILES`、静态 declaration 与 wheel 资源测试；不用新建跨 Feature 的万能 contracts wheel。

按一次 change 保存：

```text
qa/changes/<change_id>/
  cases/system/user/case.yaml                  # 人可读规格
  cases/system/user/assertion-sources.json     # 正式来源 sidecar
  plans/api-case-execution-plan.json           # 新机器计划包，内含 cases 列表
  codegen/api-generated-files.json             # 既有生成映射，引用计划摘要
  execution/<execution_id>/manifest.json       # 父执行器自动生成
  execution/<execution_id>/evidence.json        # 逐项实际观察及完成记录
  execution/<execution_id>/otlp.jsonl           # 第二阶段受控导出原始数据
  execution/execute-result.json                # 首轮批次索引，引用各 execution_id
  execution/run-result.json                    # 重跑批次索引，不覆盖首轮事实
  inspect/verification.json                    # 确定性业务/证据判定
```

实际写入使用现有 runtime 产物接纳/声明路径；父级私有工作目录中的原始证据先封存再发布引用。若实施基线已完成 typed artifact materialization 迁移，接入其已安装接口；否则在现有 Feature finalizer 使用闭合 schema、canonical JSON、运行时写入和摘要接纳，不能把未来文档中的未实现类名当成依赖，也不借本功能重写通用 artifact kernel。

## 4. 冻结的最小接口

| 类型/函数 | 所属任务与契约 |
| --- | --- |
| `BusinessAssertionV1` | T1 intake：`assertion_id, statement, source_id, subject, comparator, expected`；comparator 仅 eq / row_count_eq；expected 为 literal 或冻结 input key 引用 |
| `AssertionSourcesV1` | T1 intake：`case_id, revision, sources`；每个 source 有 ID、需求/已认可规则 origin、引用、摘要与 decision；implementation references 不在此表 |
| `CaseExecutionPlanV1` | T2 generation：单 case、spec/source/config/SUT digest、profile、一个 HTTP action、一个 SQLite observer、逐项 binding、required IDs、completion、可选 TraceRequirements |
| `CaseExecutionPlanSetV1` | T2 generation：`schema_version='1', change_id, cases: tuple[CaseExecutionPlanV1, ...]`；统一保存到一个已声明 JSON artifact |
| `compile_case_plan(case: dict, sources: AssertionSourcesV1, bindings: dict, validation_profile: str) -> CaseExecutionPlanV1` | T2，校验失败抛 `PlanNotReady(reason: str)`；expected 从 case 读取，bindings 中出现 expected/required 覆盖即拒绝 |
| `VerificationManifestV1` | T3 execution：execution_id、静态 case key、既有 workflow identity、完整 nodeid、plan/spec/mapping/SUT/config digest、SUT 地址/实例、SQLite file identity、冻结输入、证据目录引用 |
| `ObservationV1` | T3 execution：execution_id、obligation_id、state（observed/missing/error/timeout/skipped）、actual（JSON value）、evidence ref/digest、reason；不接受 expected 或 passed 自报字段 |
| `VerificationEvidenceV1` | T3 execution：manifest digest、receipt、observations、collector/host completion、执行状态；拒绝重复 obligation 记录和身份冲突 |
| `ExecutionViewInputV1` | T3 execution：change_id、batch_id、execution_id、selected nodeids、已认证 generated-file descriptors；不引用 product 的 Python 类型 |
| `VerifiedExecutionRequestV1 / VerifiedExecutionResultV1` | T3 定义、T4 实现执行：请求持有已冻结 plan/manifest/mapping/view 引用；结果持有 execution_id、collected/incomplete、receipt/evidence 引用及摘要。T6 将其接入现有 triplet，不能继承 AgentRunResult |
| `VerifiedProcessReceiptV1` | T3 execution：argv、进程退出/超时/取消、raw pytest report、桥接协议结果；不得包含可覆盖 oracle 的 passed 标记 |
| `observe_user(db_file: Path, username: str, email: str, timeout_s: float=2.0) -> dict` | T3 SQLite observer：返回 `state, rows, reason`；只观察，不从 DB 生成 expected |
| `execute_case(case_id: str) -> None` | T4 bridge 的唯一测试侧入口；从父级会话获得分配，不接受 SQL、expected、URL、路径或状态参数 |
| `VerifiedExecutionHandler.execute(request: TaskRequest, context: TaskContext) -> TaskOutcome` | T4/T6 安装式执行 operation；首轮/重跑共用；输出显式 host result，不是 AgentRunResult |
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

- [ ] 新模式在 case design/finalize/review 路径按 typed assertions 解析；旧 authoring 文件继续读取。来源 sidecar 加入资源声明、允许输出及评审输入；更新技能对 advisory metadata 的禁止条款，只允许正式来源 sidecar。
- [ ] 增加“规范要求 true、源码当前 false，仍保留 true”测试；版本摘要不一致、重复 ID、source_code-only、缺来源均不 ready；schema 与模型一致、plugin declaration 与 wheel 一致。
- [ ] 运行 `uv run pytest packages/capabilities/assurance-intake/tests -q`，通过后只提交本任务路径：`feat(intake): add reviewed business assertion provenance`。

## Task 2: 机器计划、完整义务与确定性评审

**Files:** 新建 generation `contracts/execution_plan.py`、`operations/execution_plan.py`、机器计划 schema、`tests/fixtures/verification/user-plan.json`；修改 `operations/planning.py`、`operations/review.py`、`contracts/{plans,agent,workflow}.py`、`validators/plans.py`、插件注册及 planner/reviewer skills。新增 `packages/capabilities/assurance-generation/tests/test_execution_plan.py`。

**Interfaces:** 消费 T1 assertions/sources，产出第 4 节两个计划类型及 `compile_case_plan`；纯契约检查可放 contracts，由 execution/quality 复用，不能让它们 import generation operations。

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
        )
```

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_execution_plan.py -q`。

- [ ] 实现单 case 模型、计划包、合法引用/比较器/摘要校验；bindings 只能描述 actual 的定位，不能携带 expected/required 覆盖。提供固定 User SQLite binding ID 和版本，而非允许任意 SQL DSL。
- [ ] 同时扩展 `_PLAN_OUTPUT_NAMES` 与 `_PLAN_FILES`，加入 `api-case-execution-plan.json`；PlanResult 仅引用计划摘要。`validators/plans.py` 改为已声明精确文件类型分派，保留旧 `{family}-plan.json` 的严格检查。
- [ ] PlanFinalize 与 PlanReviewFinalize 共用确定性闭包校验；后者不能继续仅校验 Agent 的 pass 字段。覆盖缺项、重复/未知义务、悬空 expected、错误 source/spec digest、profile 与 Trace 冲突、空 required。
- [ ] 运行 `uv run pytest packages/capabilities/assurance-generation/tests/test_execution_plan.py packages/capabilities/assurance-generation/tests/test_planning.py packages/capabilities/assurance-generation/tests/test_plan_review.py packages/capabilities/assurance-generation/tests/test_plan_validator.py packages/capabilities/assurance-generation/tests/test_resources.py -q` 和 `uv run lint-imports`。提交 `feat(generation): compile closed case execution plans`。

## Task 3: 运行清单、执行视图与独立 SQLite observer

**Files:** 新建 execution `contracts/verification.py`、`operations/{execution_view,verification_manifest,sqlite_oracle}.py`；修改 product `execution_view.py` 保留薄适配及现有 execution paths；新增 benchmark `user_oracle_harness.py`、`fixtures/user-oracle/bootstrap.py` 及阶段一基础 `requirements.in/requirements.lock/runtime-lock.json`。新增 execution `tests/test_verification_manifest.py`、`tests/test_sqlite_oracle.py` 和 `tests/unit/benchmark/test_user_oracle_harness.py`。

**Interfaces:** 产出 Manifest/Observation/Evidence 三种契约及 `observe_user`；harness 为固定入口 CLI，由声明式环境配置启动，不能作为 SUT 插件 import。`user_oracle_harness.py prepare/start/stop` 的 JSON 回执含 SUT 地址、进程实例、SQLite 绝对路径、源文件摘要与结束原因。

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
- [ ] 把现有 `build_execution_view` 的底层复制/摘要逻辑移至 execution，接收 `ExecutionViewInputV1` DTO（selected nodeids、经过验证的 generated-file descriptors、change/batch/execution keys）。product 仅将 MergedGeneratedSet 转 DTO；Feature 不 import product。新 view 以 execution_id 区分真实重跑，legacy 路径兼容保留。新模式的安全测试副本只包含 tests/support，目录 0555、文件 0444，确保非 root runner 能读取；不复制凭据或数据库。模式归一进入 view 摘要，原项目文件权限不变。
- [ ] 每次尝试创建 execution_id 并以原子写入冻结 manifest；恢复先按既有 invocation/task/attempt/nodeid 查现有清单，匹配摘要时复用，不新建身份。新增同版本重跑 ID 不同、旧证据拒绝、参数化 nodeid 不折叠、路径错绑拒绝测试。
- [ ] harness 只复制运行必需 app/migrations，使用 SQLite backup 或已停止种子库准备管理员；Agent/project_dir 与导出仍指原项目。先绑定监听 socket 再启动服务，防止端口抢占；不复用未知 ready URL。启动/停止只控制本次拥有的 PID/实例，未知实例必须拒绝。阶段一即提交实际 SUT 源文件清单、基础依赖锁与摘要，不等到 OTel 阶段才锁环境；T11 在同一锁中增加经过兼容验证的 OTel 依赖。
- [ ] 验证 `uv run pytest packages/capabilities/assurance-execution/tests/test_verification_manifest.py packages/capabilities/assurance-execution/tests/test_sqlite_oracle.py tests/unit/benchmark/test_user_oracle_harness.py tests/product/test_execution_view.py tests/product/test_candidate_execution_isolation.py -q`。提交 `feat(execution): bind isolated runs and observe SQLite state`。

## Task 4: 真实受限 pytest、窄桥接与父级执行

**Files:** 新建 execution `bridge.py`、`bridge_runner.py`、`operations/verified_process.py`、`operations/verified_execution.py`、benchmark `fixtures/user-oracle/runner.Dockerfile` 与 `runner-lock.json`、`scripts/build_verification_runner.py`；修改 execution `operations/runner.py`、`operations/__init__.py`、`pyproject.toml`、根 `uv.lock`。新增 `packages/capabilities/assurance-execution/tests/test_verified_process.py`、`test_verified_execution.py`。

**Interfaces:** 测试只调用 `execute_case(case_id: str) -> None`。父级 `VerifiedExecutionHandler` 使用固定计划和 Manifest，接收 execute 请求并产生 Observation，不接收测试提供的 SQL/expected/status。`VerifiedProcessReceiptV1` 记录真正进程终态、raw pytest report、超时/取消原因和桥接请求数量。

- [ ] 先写真实子进程测试：空入口、assert True、薄合法桥接、伪造完成帧、重复 execute 请求、未知 case、取消/超时及零收集。以下生成入口必须合法，通过与否取决于父级证据：

```python
from assurance_execution.bridge import execute_case

def test_create_user():
    execute_case("TC_USER_CREATE_001")
```

- [ ] 构建固定 Python 3.11 runner 镜像，安装本次 execution wheel、pytest、pytest-json-report 及锁定依赖。runner-lock 记录平台、基础镜像摘要、实际 wheel 名/摘要、依赖锁摘要和构建 image ID；预检逐一比较。HTTPX 是宿主 execution wheel 的运行依赖，不能只加到 root dev。普通执行不在线安装依赖，不使用宿主 PATH 上碰巧存在的 pytest。旧 runner 不全局替换。
- [ ] 构建入口为本任务新增的 `uv run python scripts/build_verification_runner.py`：复用现有 wheel smoke 的 wheel 构建清单，按 runner.Dockerfile 和锁定输入准备镜像，输出本次资格记录的位置。测试从该记录取得已构建镜像，验证记录绑定当前 wheel 摘要；源码变化后必须重新构建，不能套用已提交样本中的旧 image ID。镜像安装只发生在显式准备阶段。
- [ ] 新后端使用 argv 列表调用 Docker；固定命令由安装代码生成，外部配置只能提供已核验镜像和限额。关键参数如下，实际字符串值从已验证运行清单获得：

```python
def container_argv(image: str, view: str, container_name: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--pull=never", "-i",
        "--name", container_name, "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
        "--pids-limit=64", "--memory=512m", "--cpus=1",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,src={view},dst=/tests,readonly",
        "--workdir=/tests", image,
        "python", "-m", "assurance_execution.bridge_runner",
    ]
```

- [ ] bridge_runner 在 pytest 捕获前保存原始管道，将 pytest 普通 stdout 转 stderr；stdout 仅传长度受限 JSONL 帧。协议仅 `execute(case_id)`、`runner_report(raw)`，父级返回 ack/error；所有子进程帧都不构成 oracle 完成证据。禁止 TTY、shell、任意 frame 路径和额外参数；分片、超长、非法 JSON、未知 frame 均记录协议错误。
- [ ] 父进程先确认初态，再持久化 `action_started` receipt，实际 HTTP POST 一次、读取后态并保存观察。HTTPX 设置 `follow_redirects=False`、10 秒总边界，不自动重试；凭据只在父级解析。函数输出只含真实实际值，expected 仍引用 T1/T2。
- [ ] 使用 Popen 和有限等待/取消，默认 runner 总预算 60 秒，单帧最大 256 KiB、stderr 最多 8 MiB；这些是执行配置并进入摘要，不能由测试改变。终止拥有的 Docker 容器并确认退出，再封存；不能只 kill Docker CLI 留下容器继续运行。动作未知时保留 INCOMPLETE。恢复发现 action_started 且无终态时不执行第二次；再评估已有完整 evidence 时不重复请求。
- [ ] 真资格测试证明容器不能读/写宿主证据、不能修改只读测试文件、不能直接访问 SUT、不能拿到认证 canary；父级仍能收桥接请求并完成真实 HTTP→SQLite。本任务测试不能仅用 FakePytestHost；Docker 不可用时命令失败并记录未验证，不用 skip/xfail 作为任务完成。
- [ ] Run: `uv run pytest packages/capabilities/assurance-execution/tests/test_verified_process.py packages/capabilities/assurance-execution/tests/test_verified_execution.py packages/capabilities/assurance-execution/tests/test_runner.py -q`。提交 `feat(execution): execute frozen plans behind a confined pytest bridge`。

## Task 5: 一个 profile 参数与两个确定性 execute slot

**Files:** 修改 product `models.py`、`binding_builder.py`、`product.py`、`agent_contracts.py`、`resources/declarations/deployment.schema.json`、`resources/schemas/product-input-v1.json`；execution `contracts/workflow.py`、`contracts/agent.py`、注册声明。新增 `tests/product/test_verified_execution_profile.py`。

**Interfaces:** 部署绑定新增 `validation_profile: Literal['api_db.v1','api_db_trace.v1'] | None`，省略仅表示既有 legacy 配置。只有既定的两个 execution prepare ID 可切换目标，不能配置任意 operation。

- [ ] 红灯测试枚举两个 profile：API-only full 可编译；未知值拒绝；其余 Agent 路由未变；编译 lock 含 profile 与依赖配置摘要。复用 `tests/product/cli_support.py::write_product_input` 生成真实资源 SHA。
- [ ] 在 `models.py` 定义下列纯选择函数，builder 和 product 双重校验共用；不复制两套可能漂移的规则。测试须核对最终 composition，不只测此函数：

```python
VERIFIED_TARGETS = {
    "assurance.execution.execute.prepare": "assurance.execution.verified-execute",
    "assurance.execution.run.prepare": "assurance.execution.verified-execute",
}

def selected_execute_target(prepare_id: str, profile: str | None, runtime_execute: str) -> str:
    if profile is not None and profile not in {"api_db.v1", "api_db_trace.v1"}:
        raise ValueError("unknown validation profile")
    if profile is not None and prepare_id in VERIFIED_TARGETS:
        return VERIFIED_TARGETS[prepare_id]
    return runtime_execute
```

- [ ] 两组 prepare/finalize 采用 profile 判别的 typed 分支，不通过“哪种 JSON 解析成功”猜执行路径。唯一可配置模式属于部署配置；ProductInput 携带它的冻结值/配置引用用于一致性认证，不是第二个可独立选择的开关。与需求的必需 Trace 冲突或与编译 lock 不一致时拒绝；new profile 的 API 目标不得绑定到 Agent 执行后端。
- [ ] `bind_agent_execution_contracts` 对这两组安装执行资源声明与一次性业务动作恢复策略，不套用 Agent transient retry。新 raw evidence 的写入不授予 codegen Agent 或测试容器。调用 Task 4 已实现的 handler；缺少运行环境或受控进程后端时返回明确 NOT_READY，不实现 fake passed。
- [ ] Run: `uv run pytest tests/product/test_verified_execution_profile.py tests/product/test_binding_coverage.py tests/product/test_agent_execution_contracts.py tests/product/test_product_input.py tests/product/test_full_graph_audit.py -q`。确认节点/边 ID 集合不变、其他路由目标仍逐一验证。提交 `feat(product): bind verified execution profiles to existing slots`。

## Task 6: 将生成物接到权威执行及重跑

**Files:** 修改 generation `contracts/{agent,codegen}.py`、`operations/codegen.py`、API codegen/fixer skills、generated-files validator；execution `contracts/{agent,evidence,workflow}.py`、`operations/agent_skills.py`、`operations/__init__.py`、plugin/schema/声明；相关 fixtures。新增 `packages/capabilities/assurance-generation/tests/test_verified_codegen.py`。

**Interfaces:** codegen 输入带已审计划 ref/digest，映射保存 case→plan→test；execute/run 的 prepare 返回 `VerifiedExecutionRequestV1`，execute 返回 `VerifiedExecutionResultV1`，finalize 只接纳 host 结果和证据摘要。

- [ ] 红灯测试覆盖生成映射丢 case、绑定旧 plan、修改规范 digest，以及“Agent 全通过但没有 host evidence”。复用现有 mapping_document/codegen_input fixtures，通过普通生成物接纳路径放入真实候选测试，不预填 runtime verdict。
- [ ] 新 profile 下 codegen 生成 Task 4 的 bridge 入口，不再复制 HTTP/DB/Trace 判定实现；不得生成 execution_id，expected 或数据库路径不写入测试代码。额外 assert 的个数不用于通过判据。
- [ ] 现有 prepare 先读取/校验 plan 和 mapping，构建 Task 3 的执行 view，再形成显式请求。finalize 必须按冻结 profile 处理 typed host result：

```python
def execution_result_branch(profile: str | None) -> str:
    if profile in {"api_db.v1", "api_db_trace.v1"}:
        return "verified_host"
    if profile is None:
        return "legacy_agent"
    raise ValueError("unknown validation profile")
```

- [ ] 逐项原始 evidence 的来源绑定由父级接纳记录与运行清单认证；不从 Agent structured_result 构造这些事实。所有测试未触发 bridge 时必须发布可保存的 incomplete，不因 pytest exit 0 补记录。
- [ ] 首轮和 healing 重跑共用 host；首次 batch 与重跑索引分别保留，每个实际 case attempt 有自己的 execution_id。artifact/resource claims 同步新路径，禁止 Agent 自写同名原始证据成为权威材料。
- [ ] Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_verified_codegen.py packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-execution/tests/test_agent_skills.py packages/capabilities/assurance-execution/tests/test_verified_execution.py tests/product/test_change_local_output_routing.py -q`。提交 `feat(generation): connect generated cases to verified execution`。

## Task 7: 确定性质量判定、覆盖与报告

**Files:** 新建 quality `contracts/verification.py`、`operations/verification.py`；修改 `operations/{agent_skills,inspect,metrics,report}.py`、相关契约/schema/注册；同步 execution `resources/workflow/module.yaml` 的输入输出声明及 product `resources/workflow/main.yaml` 既有边的 guard。新增 `packages/capabilities/assurance-quality/tests/test_verification.py`。

**Interfaces:** 使用第 4 节完整签名的 `evaluate_verification`，作为本功能唯一业务评价算法；不 import execution 的 driver/observer。新 execution 收集状态通过原有 quality 节点进入评价，不提前自报业务 passed。

- [ ] 给裁判建立表驱动红灯用例，实际 observation 从 T3/T4 产出或对其做定点故障变体：全部正确→PASSED，明确错误值→FAILED，缺 observer/DB读取错→INCOMPLETE，错误值同时缺 telemetry→FAILED 且保留证据缺失。
- [ ] 实现结果优先级，检查身份/摘要/重复记录后再比较，不允许 caller 传 covered/total 决定新覆盖：

```python
def reduce_verdict(*, violated: bool, complete: bool, runner_ok: bool) -> str:
    if violated:
        return "FAILED"
    if not complete or not runner_ok:
        return "INCOMPLETE"
    return "PASSED"
```

```python
import pytest
from assurance_quality.operations.verification import reduce_verdict

@pytest.mark.parametrize("violated,complete,runner_ok,expected", [
    (False, True, True, "PASSED"),
    (True, True, True, "FAILED"),
    (True, False, True, "FAILED"),
    (False, False, True, "INCOMPLETE"),
    (False, True, False, "INCOMPLETE"),
])
def test_verdict_precedence(violated, complete, runner_ok, expected):
    assert reduce_verdict(violated=violated, complete=complete, runner_ok=runner_ok) == expected
```

- [ ] `required` 分母从冻结规范＋profile 重建；执行、评价、满足分别计数。失败断言属于已评价但不满足，跳过/缺失不能缩小分母；无业务断言时在编译阶段拒绝，不产生 100%。
- [ ] InspectFinalize 从权威执行引用读取 facts 并调用裁判，再生成/核对报告字段；Agent 的解释只能补充文字。给 report 输出增加逐项 obligation、actual/expected 安全差异、证据 ref、business/evidence 双维度。
- [ ] execution 的新 `collected` / `incomplete` 状态加入现有输出契约；既有 execute→quality / run→quality guard 接纳二者，legacy passed/failed 分支保持。quality 输出映射明确：PASSED→satisfied；已证实业务 FAILED→exhausted；遥测/环境未知→inconclusive。只有可确定的生成入口缺调用等可修复测试缺陷进入既有 repair_required，并受冻结义务保护；不把遥测缺失推成产品 bug。
- [ ] 对 `quality.inspect`/report 的真实 finalize 测试，不只测试 reducer；包括 Agent report 自报100%但缺DB evidence、old execution_id 和不一致manifest。验证同一原始材料重算结果稳定且不覆盖历史评价。
- [ ] Run: `uv run pytest packages/capabilities/assurance-quality/tests/test_verification.py packages/capabilities/assurance-quality/tests/test_agent_skills.py tests/product/test_execution_quality_flow.py tests/product/test_report_flow.py tests/product/test_full_graph_audit.py -q` 和 `uv run lint-imports`。提交 `feat(quality): judge required checks from authoritative evidence`。

## Task 8: healing 不得削弱业务义务

**Files:** 新建 healing `operations/verification_guard.py`；修改 `operations/{safety,workflow_state}.py`、`validators/repair.py`、coverage repair/fixer skills 与契约。新增 `packages/capabilities/assurance-healing/tests/test_verification_guard.py`。

**Interfaces:** `assert_same_obligations(before, after) -> None`，只消费 generation contracts；候选绑定允许指向重构后的新 SUT 摘要，但要求重新编译并保留规范。

- [ ] 先写删除 oracle、required 改 optional、expected 改实际值、增加超时、Trace 降级、换旧执行证据均被拒绝的参数测试。
- [ ] 将受保护字段形成规范化投影，必须包括规范/source摘要、断言引用/比较器、初态、profile、完成预算、Trace 关系语义。技术定位单独比较：

```python
PROTECTED_PLAN_FIELDS = (
    "spec_digest", "sources_digest", "validation_profile",
    "required_obligation_ids", "assertion_refs", "initial_state", "completion", "trace_requirements",
)

def assert_protected_fields_equal(before: dict, after: dict) -> None:
    changed = [key for key in PROTECTED_PLAN_FIELDS if before[key] != after[key]]
    if changed:
        raise ValueError("verification obligations changed: " + ", ".join(changed))
```

- [ ] 在实际 safety/repair 接纳入口调用，而不是只让 Agent 填 `assertion_expected_value_changes_detected=false`。缺基线材料同样拒绝修复接纳。旧测试维持原安全规则，新模式追加冻结计划保护。
- [ ] 增加正向例：只改 Python 函数定位、等价 ORM/SQL 绑定和新增 helper span，更新 SUT/技术配置摘要后重新编译，通过同一业务预期的真实执行；重跑新 execution_id，旧结果保留。
- [ ] Run: `uv run pytest packages/capabilities/assurance-healing/tests/test_verification_guard.py packages/capabilities/assurance-healing/tests/test_safety.py packages/capabilities/assurance-healing/tests/test_workflow_state.py tests/product/test_issue_healing_flow.py -q`。提交 `feat(healing): preserve frozen verification obligations`。

## Task 9: 接通真实 CLI 终态认证、achieved 与 export

**Files:** 新建 product `verification.py`；修改 `status.py`、`cli.py`、`export.py`、状态/发布 schema。新增 `tests/product/test_verified_delivery.py`，扩展 `test_achieved_terminal.py`、`test_cli_export.py`、`test_publish_recovery.py`。

**Interfaces:** 使用第 4 节完整签名的 `authenticate_verified_delivery`，从本 invocation 已接纳的执行索引选择当前有效 execution_id，并绑定 plan/evidence/verdict/生成物摘要；不是“取目录里最新文件”。

- [ ] 红灯测试必须经真实 CLI `_publish_projections` 调用路径：图 completed 而必需 DB evidence 缺失，status 不能 achieved；不能仅直接调用 `finalize_achieved` helper。
- [ ] 将 full 成功终态接入 `finalize_achieved`，先验证业务/证据与生成物，再写 apply manifest，最后发布 achieved。`_change_state` 对新模式不能仅凭 full+completed 返回 achieved；运行中状态和普通 status 读取不反复执行有副作用的终态发布。
- [ ] 终态分支使用以下明确顺序，并将已发布摘要作为幂等恢复依据：

```text
读取当前 invocation 的权威完成记录
→ 校验 selected cases/生成物/最新有效执行索引
→ 认证每项 VerificationVerdict=PASSED 且 evidence complete
→ 合并已认证生成物并构造 apply manifest
→ 原子保存 manifest 和 achieved 投影
→ export 再核对这些引用后发布
```

- [ ] 若 T7 结果 FAILED/INCOMPLETE/SKIPPED 或 T2 NOT_READY，保留诊断材料与明确状态；不写成功 apply manifest、不发布 export receipt。旧合法 legacy 结果按原版本读取，新 profile 不能借用 legacy status。
- [ ] 修复 `_require_execution_gate` 只读首次 execute-result 的问题：由本 invocation 的接受顺序和 case 选择解析有效重跑索引，保留原失败记录；旧成功不能遮盖新失败。发布后重复 export 返回同一 receipt；输入/证据漂移则拒绝而不是重算覆盖。
- [ ] Run: `uv run pytest tests/product/test_verified_delivery.py tests/product/test_achieved_terminal.py tests/product/test_cli_export.py tests/product/test_publish_recovery.py tests/product/test_export_security.py -q`。提交 `feat(product): certify verified results before achieved and export`。

## Task 10: User API＋DB 的真实 full benchmark

**Files:** 修改 benchmark `run_item.py`、`manifest.json`、`tests/validate_live_run.py`、T3 harness；新增 `benchmark/vue-fastapi-admin/benchmark/requirements/user-create-oracle.md`、`tests/product/test_user_oracle_full_workflow.py`；更新既有 manifest/layout 测试。

**Interfaces:** 新 manifest item ID 固定为 `opencode-user-api-db`；`entrypoint=full`、`run_mode=case`、`case_modules=['system/user']`、`selected_test_families=['api']`、`validation_profile='api_db.v1'`。新增 harness CLI `--fault`，闭集值为 `none,missing-binding,no-bridge,skip-oracle,wrong-value,rollback,rollback-success,db-unavailable,wrong-environment,unknown-http,forged-evidence,downgrade,missing-write`；第二阶段在 T13 扩展 Trace 值。故障选择在本次 full 启动前冻结。

- [ ] 更新 manifest 校验红灯测试，允许声明 API-only full；保留原 Dept 四 family 范围。必要节点必须真正 succeeded，不接受 `_status_steps` 当前把 stopped 算完成的做法。
- [ ] 需求文件收窄到 User 创建与本 spec 八条断言，不宣称整个 User 管理模块已覆盖。harness 的源文件摘要清单包含 app、migrations、配置及启动覆盖；原 SUT 源码漂移时预检失败，不能拿主仓库 Git HEAD 替代。
- [ ] 同一真实 full invocation 从空 change 开始，经 case/review/plan/review/codegen，执行本次生成测试，再质量判定/报告/retro/apply/achieved/export。沿用现有真实 Agent adapter；scripted Agent 只用于快速产品路由测试，不能充当此 live 门槛。
- [ ] 固定故障位置：missing-binding 在机器计划候选接纳前注入；no-bridge 在生成候选接纳前形成合法但未调用桥接的入口；skip-oracle 在父级已声明故障边界跳过观察；wrong-value 在 SUT 写入时更改 is_active；不事后改已冻结材料再补摘要。
- [ ] rollback 变体在原 UserController.create_user 外包 `in_transaction('sqlite')`，原函数真实写入后抛固定异常；独立 harness 记录事务连接/写入/回滚事实。rollback-success 在事务退出后返回成功 envelope。未包装事务就抛错不得当作 rollback 验收。
- [ ] 修改 benchmark 成功判据：正常项要求 achieved/export；故障项的 benchmark harness 成功表示正确得到预期 NOT_READY/FAILED/INCOMPLETE 且拒绝交付，业务用例自身保持失败状态。
- [ ] 运行以下新增 CLI 路径，保存 ledger、各版本摘要、manifest、DB原始观察与最终判定：

```bash
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode --fault no-bridge
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode --fault wrong-value
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db --adapter opencode --fault rollback-success
```

- [ ] 阶段一完成 spec A01–A09、A15–A19、A22–A25 的适用部分；运行 `uv run pytest tests/product/test_user_oracle_full_workflow.py tests/product/test_phase5_benchmark_manifest.py tests/product/test_phase5_benchmark_change_layout.py tests/phase6/test_final_benchmark_manifest.py -q`。提交 `test(benchmark): verify User API DB through full workflow`，保存阶段一验收结果再进入 T11。

## Task 11: 实际 Tortoise/SQLite OTel 接入与可重现依赖

**Files:** 完善 benchmark `fixtures/user-oracle/{bootstrap.py,collector.yaml,runtime-lock.json,requirements.in,requirements.lock}` 和 harness；修改 execution `pyproject.toml`、根 `uv.lock`；新增 `benchmark/assurance-product/tests/test_user_otel_compatibility.py`。

**Interfaces:** bootstrap 在创建 app/连接前安装 OTel；支持受控 flush/shutdown 回执。runtime-lock 保存实际源文件、bootstrap、完整 Python 依赖锁、Collector 平台/版本/制品摘要；所有 digest 来自实际文件，不填写占位值。

- [ ] 先用当前真实 User.save 写兼容测试：必须看到 FastAPI server 下、真实 SqliteClient 调用产生的 CLIENT span；普通路径和实际事务变体均验证，不能只测内存里手工创建 span。
- [ ] 添加官方 SDK、FastAPI/Tortoise instrumentation、OTLP HTTP exporter 的依赖输入，约束 benchmark 现有 FastAPI/Tortoise/aiosqlite 版本；解析并提交完整带 hash 的锁。版本选择作为本任务真实兼容实验输出，不在计划里捏造“已验证组合”。

```bash
uv lock
uv pip compile benchmark/assurance-product/fixtures/user-oracle/requirements.in --generate-hashes --output-file benchmark/assurance-product/fixtures/user-oracle/requirements.lock
```

- [ ] bootstrap 采用官方入口，所有 provider/采样/导出端点来自受控运行配置：

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

**Files:** 新建 execution `operations/telemetry.py`，扩展 verification contracts/host；修改 quality `operations/verification.py`；新增 execution `tests/test_telemetry.py`、quality `tests/test_trace_verification.py`。

**Interfaces:** `load_otlp_records(path: Path, execution_id: str) -> tuple[dict, ...]` 只归一实际原始数据；`check_trace_requirements(plan, manifest, spans, completion) -> tuple[ObservationV1, ...]` 为 execution 观察适配，quality 用这些事实做最终判定。重复 trace_id/span_id 去重，冲突内容拒绝。

- [ ] 红灯测试真实 OTLP fixture，覆盖业务/DB span 单独缺失、旧 execution_id、相同业务键但错实例、断父链、仅 oracle SELECT、仅角色/审计表写入，以及新增中间 helper 的正常父链。
- [ ] 父级 HTTP driver 创建 client span、注入 W3C trace context 与 execution_id；SUT FastAPI 请求 hook 仅允许测试身份字段。observer 的 span scope/service/role 明确标为 oracle，不能满足 `trace.user_write`。
- [ ] 用户写入匹配来自真实 Tortoise instrumentation scope，SQLite 数据库绑定一致，并归一为 User 持久化调用；不能匹配任意 DB span。允许等价 SQL，不比较完整 SQL字符串、调用次数或固定直接父节点；成功调用接受正确语义下的 UNSET，不要求强制 OK。
- [ ] 完成流程明确且有总预算：

```text
HTTP 动作有终态 → 独立 DB 查询结束 → client span 结束
→ driver provider flush/shutdown → managed SUT provider flush/shutdown
→ Collector 有序关闭 → 原始 JSONL 封存及摘要 → 判定
```

- [ ] 各阶段保存独立完成 receipt；全量采样不是完成证明。导出错、file 截断、必需字段丢失、drain 超时→INCOMPLETE；晚到数据不能直接覆盖封存评价，显式新评价仍引用原始材料集合。
- [ ] 增加完整组合测试：正常落库但缺 write span→INCOMPLETE；真实回滚且提前发 completed→FAILED；既有业务错误与遥测缺失并存→FAILED＋missing evidence；同一规范在 helper/函数/等价SQL重构后重新绑定新制品仍 PASSED。
- [ ] Run: `uv run pytest packages/capabilities/assurance-execution/tests/test_telemetry.py packages/capabilities/assurance-quality/tests/test_trace_verification.py packages/capabilities/assurance-quality/tests/test_verification.py -q`，再跑 T11 真实兼容命令。提交 `feat(quality): require correlated and complete User traces`。

## Task 13: 第二阶段 full 故障矩阵与最终交付

**Files:** 新增 manifest item `opencode-user-api-db-trace`；扩展 benchmark fault 枚举与 `tests/validate_live_run.py`，更新 `tests/product/test_user_oracle_full_workflow.py`、`.github/workflows/ci.yml`、`scripts/assurance_product_wheel_smoke_test.sh`；更新产品 README 使用说明与阶段验收记录。

**Interfaces:** 新故障值 `drop-business-span,drop-write-span,broken-context,stale-trace,drain-timeout,early-completed,refactor`；固定在 full 启动前绑定。`refactor` 是保持业务语义的正向变体，其余 Trace 故障按 spec A11–A14/A21/A26 分类。

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
- [ ] 打包验证 execution wheel 具备 HTTPX/OTel 等运行依赖、runner 镜像具备 pytest-json-report，SUT 独立锁能重建；所有受控 harness 变体和 bootstrap 已跟踪，不依赖忽略目录中的未提交改动。
- [ ] 在 CI 的确定性测试前执行 Task 4 镜像构建/资格步骤，并将生成记录传给真实 runner 测试；CI 不依赖开发者预先构建的镜像，也不因没有 Docker 而悄悄跳过。真实 Agent full benchmark 单独按本节命令验收，普通 CI 使用 scripted Agent 做路由测试，二者结果分开保存。
- [ ] 执行全部 CI（每项通过后才进入最终验收归档）：

```bash
uv run python scripts/build_verification_runner.py
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
```

- [ ] 记录每个 live 样例的 outcome、执行时长、环境启动成本、接入改动、误报与限制，不预设性能提升。正常样例达到 achieved 并 export；archive 仍为可选。提交 `test(product): certify full User trace verification and delivery`。

## 5. Spec 验收覆盖映射

| Spec 验收 | 实施/测试任务 |
| --- | --- |
| A01 正常 API＋DB | T1–T10，重点 T10 live full |
| A02 必需 binding/expected 遗漏 | T2 编译与评审、T10 full |
| A03/A04 空入口/未执行 oracle | T4/T6 权威执行、T7 判定、T10 full |
| A05 错误字段 | T3/T7、T10 full |
| A06/A07 真实回滚/伪成功响应 | T3 独立连接、T10 真事务变体；T13 带 Trace |
| A08/A09 DB错误/错环境/初态冲突 | T3/T4、T10 |
| A10 正常 API＋DB＋Trace | T11–T13 live full |
| A11/A12/A21 遥测缺失/旧证据/断关联/缺write | T12、T13 full |
| A13 回滚前错误 completed | T10 harness、T12 判定、T13 full |
| A14 正常重构 | T8 绑定保护、T11兼容、T12匹配、T13 full refactor |
| A15 弱化义务 | T2/T8/T9、两阶段 full |
| A16/A17 重跑/未知终态 | T3 身份、T4 恢复、T9 有效执行索引、两阶段 full |
| A18 伪造 evidence | T4 真实隔离、T6 接纳、T7/T9 验证、两阶段 full |
| A19 无显式assert的合法入口 | T4/T6、T10/T13正常 full |
| A20 legacy/profile降级 | T5/T8/T9/T13 |
| A22 其他DB操作冒充写入 | T3/T7/T10，T12/T13加Trace |
| A23/A24/A25 绕过生成/修复/最终门禁 | T6/T8/T9、T10/T13 full |
| A26 真驱动兼容及无凭据泄漏 | T4 子进程边界、T11 真实锁定兼容、T12/T13 |

## 6. 计划执行与文档维护

每个任务按红灯测试→最小实现→针对性绿灯→精确路径提交执行；不要把一组新增测试全部标 skip 来完成阶段。执行时发现定位随其他已合入改动变化，先更新本任务文件列表和接口，再继续；不据旧行号覆盖用户的新实现。

本次计划同步了 spec 的单一 execution_id、同一个 profile 参数和运行清单归属。未修改应用代码、配置或依赖锁；计划中的新增命令与 API 在对应任务实现前不可视为现有功能。

自检要求：每条 A01–A26 都有上表任务与真实 full 验收；新接口名称前后一致；新增路径和现有路径有明确区分；不把 schema/提示词、单独 helper 绿灯或 synthetic span 当作交付完成。
