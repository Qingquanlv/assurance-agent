# M5 — 执行层（execution/）与报告层（report/）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付确定性执行层（四种 runner：API/E2E/Fuzz 走 pytest，Performance 走 Locust，`subprocess` 调用 + 结果解析 + `execution/runs/<batch-id>/` 落盘）与报告层（11 类失败分类器 + Quality Score + 四态 worst-wins Quality Gate + 报告三件套），并挂载 `aa run` / `aa report inspect|generate` / `aa heal` 命令。

**Architecture:** `workflow/execution/` 负责「跑测试 → 归一化结果 → 写产物证据」：`runners.py` 用 `subprocess.run` 驱动 pytest/locust，`pytest_parser.py` 把 pytest-json-report 归一化为内部 `TargetResult`，`evidence.py` 把一批结果写入 append-only 的 `execution/runs/<batch-id>/` 并刷新顶层最新指针，`runner.py` 的 `run_change` 编排全过程并返回 M2 的 `ExecutionManifest`（顶层 `final_status` = Quality Gate 裁决）。`workflow/report/` 负责「读证据 → 分类 → 打分 → 出报告」：`failure_classifier.py` 消费打包的 `_resources/rules/failure-classification.yaml` 规则表，`quality_gate.py`/`quality_score.py` 做确定性裁决与打分，`inspector.py` 写 `inspect/failure-analysis.json` + `quality-gate-result.json`，`report_builder.py` 写 `report/` 三件套。命令模块 `run_cmd.py`/`report_cmd.py`/`heal_cmd.py` 只做参数解析、事件写入与退出码。

**Tech Stack:** Python 3.11+, uv, click, pydantic v2, PyYAML, pytest, ruff, pyright, import-linter；被测系统（SUT）测试栈为 pytest / pytest-playwright / schemathesis / Locust。

## Global Constraints

- CLI 命令名 `aa`；Python 包名 `assurance_agent`；项目配置目录 `.aa/`。新文件中不得残留 `aws` 字样（recommended_action 等面向用户的文案里的旧命令示例一律改为 `aa ...`）。
- 工具链固定：uv + pyproject.toml + pydantic v2 + click + ruff + pyright + pytest。
- TS 源仓库 `/Users/lvqingquan/skills/assurance-workflow-skills` **仅作规则参考**：只提取分类规则表、Quality Score 权重公式、worst-wins 合并语义、结果字段形状，不复制实现。
- **单一产物契约来源（spec 4a）**：`ExecutionManifest` / `FailureAnalysis` / `FailureEntry` / `FailureCategory` / `QualityGateResult` / `QualityReport` / `FixProposal` / `SafetyCheck` / `SelectedTargets` / `FunctionalDimension` / `CoverageDimension` / `NonFunctionalDimension` / `PerformanceScenarioVerdict` 等模型全部 import 自 M2 的 `assurance_agent.artifacts.models`，禁止在 execution/report 内私开字典结构解析同一产物。每种 per-target 执行中间结果（`api-result.json` 等）是 CLI 内部 `free` 级产物，模型定义在 `workflow/execution/results.py`，不入 M2 注册表。
- **路径安全**：`run` / `report` / `heal` 的所有 `--change` 入口在构造 change 目录前调用 M2 `assert_change_id_safe`；内部 helper 接收外部 change id 时也必须自校验，不能只依赖 Click 层。
- **绝不伪造结果（spec 6）**：runner 只读取真实测试运行器写出的文件；测试目录缺失、runner 不可用、环境不可达一律落 `SKIPPED`，绝不编造 `passed`。
- **协议选型**：API/E2E/Fuzz 三层均基于 pytest，统一采用 pytest `--json-report` 协议（`pytest-json-report` 插件写出结构化的 per-test JSON：`nodeid`/`outcome`/各阶段 `longrepr`/`duration`），而非 JUnit XML。理由：SUT 栈是 Python，三层（pytest、pytest-playwright、schemathesis-via-pytest）共用同一 pytest 协议即可用同一解析器归一化，JSON 结构确定、无 XML 属性歧义；TS 源用 JUnit XML 仅为兼容其 TS 版 Playwright（本项目不复用该 runner），故此处按净室重写自由度改用 json-report。Performance 层用 Locust 自带 `--csv` 统计输出。
- **确定性打分（spec 7）**：Quality Score / Quality Gate final_status 全部由 CLI 计算，LLM 不参与；skill 只可润色文案，不得重算数字或改 final_status。
- 分层约束（累计 `.importlinter`，本计划 Task 9 只验证）：`cli → commands → risk → workflow → artifacts → config → resources`，并保留 M3 的 `core-below-orchestration` / `artifacts-below-workflow` forbidden 契约。`workflow.execution` / `workflow.report` 允许 import `artifacts`（M2 模型）、`workflow.core`（events/state/exit_codes）、`config`、`resources`；反向禁止。
- 包内资源只经 `assurance_agent/resources.py` 访问（分类规则表放 `_resources/rules/`，随 wheel 分发，pyproject 的 `artifacts = ["assurance_agent/_resources/**"]` 已覆盖，无需改打包配置）。
- 单测中**绝不真正调用** pytest/playwright/locust：一律 `monkeypatch` `subprocess.run`（stub 出写 canned report 文件的假实现）。
- 每个 Task 结束必须通过 `uv run ruff check .` 与 `uv run pyright`，然后 `git commit`；提交信息用 conventional commits（feat/test/chore/docs）。
- 本计划中所有 pytest 命令在仓库根目录运行：`uv run pytest <path> -v`。

## 消费的跨里程碑接口契约（BINDING，来自计划系列总览）

以下签名/模型是本里程碑消费前序里程碑产物的唯一依据，实现必须一致：

```python
# M1
from assurance_agent import resources               # read_text(*relpath) / exists / iter_children
from assurance_agent.config import AaConfig, load_config, ConfigNotFoundError
from assurance_agent.exceptions import AaError
# cli.py: click Group `main`；命令用 main.add_command() 挂载

# M2 artifacts（本里程碑消费的模型，全部来自 assurance_agent.artifacts.models）
#   SelectedTargets(api,e2e,fuzz,performance: bool)
#   ExecutionManifest(schema_version="1.0", change_id, batch_id, selected_targets,
#                     result_files: dict[str,str], tests_tree_sha256?, test_files_sha256?,
#                     product_tree_sha256?, final_status: GateStatus|None)
#   GateStatus = Literal["PASS","PASS_WITH_WARNINGS","FAIL","SKIPPED"]
#   FunctionalCounts(total,passed,failed); CoverageThreshold(line,branch,module_line?,diff_line?)
#   FunctionalDimension(status,api,e2e,fuzz?,unmapped_tests?)
#   CoverageDimension(status,available,line_coverage,branch_coverage,threshold,scope?)
#   NonFunctionalDimension(status, performance: list[PerformanceScenarioVerdict])
#   PerformanceScenarioVerdict(capability,endpoint,measured_p95_ms?,threshold_p95_ms,
#                              measured_error_rate?,threshold_error_rate_max,verdict)
#   QualityGateDimensions(functional,coverage,non_functional?)
#   QualityGateResult(schema_version="1.0",change_id,batch_id,dimensions,final_status,warnings?)
#   FailureCategory(18 值枚举); FailureSeverity=Literal["low","medium","high","critical"]
#   FailureEvidence(result_file,test_file,trace,screenshot,video,raw_log,log_excerpt)
#   FailureEntry(case_id,target,category,fix_proposal_eligible,severity,evidence,diagnosis,
#                recommended_action,id?,test?,recommended_next_action?,needs_review?,reclassified?)
#   FailureAnalysis(schema_version="1.0",change_id,source_manifest,inspection_status,batch_id,
#                   source_batch_id,final_status,inspect_mode,compat_fallback_reason?,classification_performed,status,
#                   failures,hard_fails,needs_review,known_product_issues,warnings?,coverage_gaps?)
#   CoverageGapEntry(file,line_coverage,threshold)
#   QualityScoreBreakdown(functional,coverage,fuzz,performance: float|Literal["N/A"])
#   ReportScope(cases:int,requirements:list[str]); ReportDefect(case_id,category,diagnosis)
#   ReportDefects(product,test,environment: list[ReportDefect]); ReportRiskLevel(LOW/MEDIUM/HIGH/CRITICAL)
#   QualityReport(schema_version="1.0",change_id,batch_id,final_status,quality_score,score_breakdown,
#                 scope,functional,coverage,defects,risk_level,risk_rationale,recommendation,
#                 human_decisions?,minimum_required_coverage?,non_functional?)
#   FixProposal(schema_version,summary:{eligible_count,...},proposals:[{target,eligible,...}])
#   SafetyCheck(schema_version,passed,needs_review,product_code_modified,skip_or_xfail_added,
#               unrelated_tests_modified,assertion_expected_value_changes_detected,
#               high_risk_proposal_applied,bare_return_added?,...)

# M3 core（本里程碑写事件时消费）
from assurance_agent.workflow.core.events import append_event_best_effort

# M4 exit codes（heal safety-check 消费）
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED, EXIT_HUMAN_REVIEW, EXIT_ERROR,
)  # 0 / 30 / 40
```

**本里程碑对上述契约零偏离**（若实现期发现前序落地与此不符，以落地代码为准并回改总览；见 Task 9 Step 7）。

## 明确的范围边界（M5 有意不做）

以下 TS 行为属于外围报告富化，本里程碑**不实现**，以保持任务可控（不视为契约偏离，因相关模型字段均为可选、且**非安全边界**）：

- coverage 的 `server-process` 模式（只实现 `pytest-cov` 模式）。
- `minimum_required_coverage` 矩阵与 `human_decisions` 富化——`QualityReport` 对应字段写 `None`/省略。

> **安全关键能力必须实现（P1 修订，原先误列为"不做"）**：test-tree / product-tree 哈希、healing 期变更守卫（防止 healing 越权改测试/产品代码）、`allow_test_changes` override evidence token、`execution-manifest.yaml` 的 `compat_fallback` 读取模式，以及 `aa heal record-apply` / `aa report reclassify` 子命令，均为**一一对应迁移**与安全边界的组成部分，见 Task 10/11（对齐 TS `workflow/core/healing_state.ts`、`workflow/core/override_evidence.ts`、`commands/heal.ts`、`commands/report.ts`）。

## 文件结构总览

新建文件及职责：

```
assurance_agent/workflow/execution/
├── __init__.py            # 空
├── case_id.py             # case-id 规范化 + 从测试名提取（对齐 TS CASE_ID_RE）
├── results.py             # per-target 内部结果模型：TargetResult/CaseResult/CoverageResult/PerformanceResult
├── exec_config.py         # 从 AaConfig 解析 coverage/performance 子配置（带默认值）
├── selection.py           # resolve_selected_targets(change_dir) -> SelectedTargets
├── pytest_parser.py       # parse_pytest_json(...) -> TargetResult（pytest-json-report 归一化）
├── runners.py             # run_pytest_target / run_performance_target（subprocess 调用 + 解析）
├── evidence.py            # publish_execution_evidence / load_execution_evidence（批次落盘 + 读取）
├── tree_hash.py           # sha256_file / hash_test_tree / hash_product_tree（安全边界哈希，Task 10）
└── runner.py              # run_change(project_root, change_dir, config, *, reservation=None) -> ExecutionManifest
assurance_agent/workflow/healing/          # 安全边界（Task 10；消费 M3 event-derived projection，不重复定义状态机）
├── __init__.py            # 空
├── safety.py              # projection adapter + 变更守卫 + record_apply_summary + fixer-safety-check
└── override_evidence.py   # write_test_changes_override_evidence（allow-test-changes 留痕）
assurance_agent/workflow/report/
├── __init__.py            # 空
├── quality_gate.py        # worst_status / build_quality_gate -> QualityGateResult（四态合并）
├── quality_score.py       # compute_quality_score（确定性权重公式）
├── failure_classifier.py  # classify_failure（读 YAML 规则表）
├── inspector.py           # inspect_change -> FailureAnalysis + QualityGateResult 落盘
└── report_builder.py      # generate_report -> report/ 三件套
assurance_agent/_resources/rules/failure-classification.yaml   # 11 类失败分类规则数据
assurance_agent/commands/run_cmd.py        # aa run [--allow-test-changes]
assurance_agent/commands/report_cmd.py     # aa report inspect|generate|reclassify
assurance_agent/commands/heal_cmd.py       # aa heal validate-proposal|eligibility-summary|safety-check|record-apply
```

产物落盘布局（change 相对，spec 4/6）：

```
qa/changes/<id>/execution/
├── runs/<batch-id>/                 # append-only 主证据源（永不覆盖）
│   ├── raw/{api,e2e,fuzz}.log, {api,e2e,fuzz}-report.json, coverage.json
│   ├── api-result.json / e2e-result.json / fuzz-result.json / performance-result.json
│   ├── coverage-result.json / summary.md
│   ├── quality-gate-result.json
│   └── execution-manifest.yaml
├── api-result.json ... performance-result.json   # 顶层最新指针（每次 run 覆盖）
├── coverage-result.json / summary.md / quality-gate-result.json
└── execution-manifest.yaml          # 顶层最新指针，final_status 权威来源
qa/changes/<id>/inspect/{failure-analysis.json, quality-gate-result.json, failure-summary.md}
qa/changes/<id>/report/{quality-report.json, quality-report.md, executive-summary.md}
```

新生成的 batch-id 格式：`YYYYMMDD-HHmmss-<8 lowercase hex>`（UTC 可排序时间戳 + 随机后缀，避免同秒运行碰撞）。读取端为历史兼容同时接受旧的 `YYYYMMDD-HHmmss`，但写入端只生成新格式。

---

### Task 1: 执行结果模型 + case-id 提取（results.py、case_id.py）

**Files:**
- Create: `assurance_agent/workflow/execution/__init__.py`（空）
- Create: `assurance_agent/workflow/execution/case_id.py`
- Create: `assurance_agent/workflow/execution/results.py`
- Create: `tests/unit/execution/__init__.py`（空）
- Test: `tests/unit/execution/test_results_and_case_id.py`

**Interfaces:**
- Consumes: M2 的 `assurance_agent.artifacts.models` 里的 `CoverageThreshold`、`GateStatus`、`PerformanceScenarioVerdict`。
- Produces: `case_id.canonicalize_case_id(raw: str) -> str`、`case_id.extract_case_id(text: str) -> str`；`results.py` 的 `ExecutionStatus`、`CaseResult`、`ResultSource`、`TargetResult`、`CoverageResult`、`PerformanceResult`。Task 2/3/4/5/8 全部消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/execution/test_results_and_case_id.py
from assurance_agent.workflow.execution.case_id import canonicalize_case_id, extract_case_id
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    PerformanceResult,
    TargetResult,
)


def test_canonicalize_uppercases_and_underscores() -> None:
    assert canonicalize_case_id("tc-menu-api-001") == "TC_MENU_API_001"
    assert canonicalize_case_id("TC_MENU_001") == "TC_MENU_001"


def test_extract_case_id_from_pytest_nodeid() -> None:
    nodeid = "tests/api/test_menu.py::test_tc_menu_api_001__create_menu_happy_path"
    assert extract_case_id(nodeid) == "TC_MENU_API_001"


def test_extract_case_id_accepts_legacy_hyphen_form() -> None:
    assert extract_case_id("TC-ROLE-002 role list") == "TC_ROLE_002"


def test_extract_case_id_returns_empty_when_absent() -> None:
    assert extract_case_id("test_plain_smoke_check") == ""


def test_target_result_defaults_and_roundtrip() -> None:
    result = TargetResult(
        change_id="CH-1",
        batch_id="20260715-101500",
        target="api",
        status="failed",
        command="uv run pytest tests/api",
        source={"framework": "pytest", "raw_log": "raw/api.log"},
        total=2,
        passed=1,
        failed=1,
        skipped=0,
        cases=[
            CaseResult(
                case_id="TC_MENU_001",
                status="failed",
                file="tests/api/test_menu.py",
                test_name="test_tc_menu_001__create",
                duration_ms=12,
                message="AssertionError",
            )
        ],
        unmapped_tests=[],
    )
    assert result.schema_version == "1.0"
    assert result.cases[0].trace == ""
    assert result.source.report_json == ""


def test_coverage_result_uses_shared_threshold() -> None:
    cov = CoverageResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        line_coverage=85.0,
        branch_coverage=70.0,
        threshold={"line": 70, "branch": 60},
        status="PASS",
    )
    assert cov.kind == "coverage"
    assert cov.threshold.line == 70


def test_performance_result_holds_scenario_verdicts() -> None:
    perf = PerformanceResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        status="FAIL",
        scenarios=[
            {
                "capability": "list_menus",
                "endpoint": "/api/v1/menus",
                "measured_p95_ms": 900.0,
                "threshold_p95_ms": 500.0,
                "measured_error_rate": 0.0,
                "threshold_error_rate_max": 0.01,
                "verdict": "FAIL",
            }
        ],
    )
    assert perf.scenarios[0].verdict == "FAIL"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/execution/test_results_and_case_id.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'assurance_agent.workflow.execution'`

- [ ] **Step 3: 实现 case_id.py 与 results.py**

```python
# assurance_agent/workflow/execution/case_id.py
"""Canonical case-id extraction from test names (aligned with TS CASE_ID_RE).

Canonical form is TC_<MODULE>[_<LAYER>]_<NNN> (underscore, upper). Matching is
case-insensitive and accepts the legacy hyphen form; the extracted id is
canonicalized so it matches case_id values in case.yaml.
"""
import re

# Lookbehind (not \b) because `_` is a word char, so \b would not fire between
# `test_` and `tc_...`. The id terminates at its 3-digit numeric suffix.
_CASE_ID_RE = re.compile(
    r"(?<![A-Z0-9])(TC[-_][A-Z0-9]+(?:[-_][A-Z0-9]+)*[-_][0-9]{3})(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)


def canonicalize_case_id(raw: str) -> str:
    return raw.upper().replace("-", "_")


def extract_case_id(text: str) -> str:
    match = _CASE_ID_RE.search(text)
    return canonicalize_case_id(match.group(1)) if match else ""
```

```python
# assurance_agent/workflow/execution/results.py
"""Per-target execution result models (CLI-internal `free`-grade artifacts).

These are written to execution/runs/<batch-id>/*-result.json and the top-level
latest pointers. They are NOT in the M2 artifact registry: the registry only
owns cross-consumer contracts (manifest, failure-analysis, quality-gate, report).
Reuses M2 shared types so coverage/performance shapes stay consistent.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models import (
    CoverageThreshold,
    GateStatus,
    PerformanceScenarioVerdict,
)

ExecutionStatus = Literal["passed", "failed", "skipped"]
PytestTarget = Literal["api", "e2e", "fuzz"]


class CaseResult(BaseModel):
    case_id: str
    status: ExecutionStatus
    file: str
    test_name: str
    duration_ms: int
    message: str
    raw_log_ref: str = ""
    trace: str = ""
    screenshot: str = ""
    video: str = ""


class ResultSource(BaseModel):
    model_config = ConfigDict(extra="allow")

    framework: str
    raw_log: str
    report_json: str = ""


class TargetResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    change_id: str
    batch_id: str
    target: PytestTarget
    status: ExecutionStatus
    command: str
    source: ResultSource
    total: int
    passed: int
    failed: int
    skipped: int
    cases: list[CaseResult]
    unmapped_tests: list[CaseResult]


class CoverageResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    change_id: str
    batch_id: str
    kind: Literal["coverage"] = "coverage"
    available: bool
    line_coverage: float
    branch_coverage: float
    threshold: CoverageThreshold
    status: GateStatus
    skip_reason: str = ""
    uncovered_critical_files: list[dict] = Field(default_factory=list)
    source: dict[str, str] = Field(default_factory=dict)


class PerformanceResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    change_id: str
    batch_id: str
    kind: Literal["performance"] = "performance"
    available: bool
    status: Literal["PASS", "FAIL", "SKIPPED"]
    scenarios: list[PerformanceScenarioVerdict]
    command: str = ""
    source: dict[str, str] = Field(default_factory=dict)
```

同时创建空文件：`assurance_agent/workflow/execution/__init__.py`、`tests/unit/execution/__init__.py`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/execution/test_results_and_case_id.py -v`
Expected: 7 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/workflow/execution tests/unit/execution
git commit -m "feat: add execution result models and case-id extraction"
```

---

### Task 2: pytest-json-report 解析器（pytest_parser.py）

**Files:**
- Create: `assurance_agent/workflow/execution/pytest_parser.py`
- Test: `tests/unit/execution/test_pytest_parser.py`

**Interfaces:**
- Consumes: Task 1 的 `TargetResult`、`CaseResult`、`ResultSource`、`extract_case_id`。
- Produces: `parse_pytest_json(*, change_id: str, batch_id: str, target: PytestTarget, report_path: Path, raw_log_path: str, command: str) -> TargetResult`。缺失/损坏的报告文件 → `status="skipped"` 且带一条解释性 `unmapped_tests` 条目（绝不伪造 passed）。Task 4 runner 消费。

pytest-json-report 结构（本解析器依赖的字段，来自插件文档）：顶层 `{"tests": [...]}`；每个 test 有 `nodeid`、`outcome`（`passed|failed|error|skipped|xfailed|xpassed`）、可选的阶段对象 `setup`/`call`/`teardown`（各含 `outcome`、`duration`、`longrepr`）。归一化映射：`failed`/`error` → `failed`，`skipped`/`xfailed` → `skipped`，`passed`/`xpassed` → `passed`；message 取失败阶段的 `longrepr`；duration 取 `call.duration`（回退各阶段之和）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/execution/test_pytest_parser.py
import json
from pathlib import Path

from assurance_agent.workflow.execution.pytest_parser import parse_pytest_json


def write_report(tmp_path: Path, tests: list[dict]) -> Path:
    path = tmp_path / "api-report.json"
    path.write_text(json.dumps({"tests": tests}), encoding="utf-8")
    return path


def parse(tmp_path: Path, tests: list[dict], target: str = "api"):
    report = write_report(tmp_path, tests)
    return parse_pytest_json(
        change_id="CH-1",
        batch_id="20260715-101500",
        target=target,
        report_path=report,
        raw_log_path=str(tmp_path / "raw" / "api.log"),
        command="uv run pytest tests/api",
    )


def test_pass_fail_error_skip_matrix(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {"nodeid": "tests/api/test_m.py::test_tc_m_001__ok", "outcome": "passed",
             "call": {"outcome": "passed", "duration": 0.01}},
            {"nodeid": "tests/api/test_m.py::test_tc_m_002__bad", "outcome": "failed",
             "call": {"outcome": "failed", "duration": 0.02, "longrepr": "AssertionError: 200 != 500"}},
            {"nodeid": "tests/api/test_m.py::test_tc_m_003__boom", "outcome": "error",
             "setup": {"outcome": "error", "duration": 0.0, "longrepr": "ImportError: no module"}},
            {"nodeid": "tests/api/test_m.py::test_tc_m_004__skip", "outcome": "skipped",
             "setup": {"outcome": "skipped", "duration": 0.0, "longrepr": "Skipped: no data"}},
        ],
    )
    assert result.total == 4
    assert result.passed == 1
    assert result.failed == 2  # error folds into failed
    assert result.skipped == 1
    assert result.status == "failed"
    by_id = {c.case_id: c for c in result.cases}
    assert by_id["TC_M_002"].message == "AssertionError: 200 != 500"
    assert by_id["TC_M_002"].duration_ms == 20
    assert by_id["TC_M_003"].status == "failed"
    assert by_id["TC_M_003"].message == "ImportError: no module"


def test_unmapped_tests_have_no_case_id(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {"nodeid": "tests/api/test_x.py::test_plain_smoke", "outcome": "passed",
             "call": {"outcome": "passed", "duration": 0.0}},
        ],
    )
    assert result.cases == []
    assert len(result.unmapped_tests) == 1
    assert result.unmapped_tests[0].case_id == ""
    assert result.total == 1
    assert result.status == "passed"


def test_all_passed_status_passed(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {"nodeid": "tests/api/test_m.py::test_tc_m_001__ok", "outcome": "passed",
             "call": {"outcome": "passed", "duration": 0.0}},
        ],
    )
    assert result.status == "passed"
    assert result.failed == 0


def test_empty_tests_list_is_skipped_status(tmp_path: Path) -> None:
    result = parse(tmp_path, [])
    assert result.status == "skipped"
    assert result.total == 0


def test_missing_report_file_is_skipped_not_fabricated(tmp_path: Path) -> None:
    result = parse_pytest_json(
        change_id="CH-1",
        batch_id="b1",
        target="api",
        report_path=tmp_path / "does-not-exist.json",
        raw_log_path=str(tmp_path / "raw" / "api.log"),
        command="uv run pytest tests/api",
    )
    assert result.status == "skipped"
    assert result.total == 0
    assert result.passed == 0
    assert len(result.unmapped_tests) == 1
    assert "not found" in result.unmapped_tests[0].message.lower()


def test_corrupt_report_json_is_skipped(tmp_path: Path) -> None:
    bad = tmp_path / "api-report.json"
    bad.write_text("{not json", encoding="utf-8")
    result = parse_pytest_json(
        change_id="CH-1",
        batch_id="b1",
        target="api",
        report_path=bad,
        raw_log_path="raw/api.log",
        command="cmd",
    )
    assert result.status == "skipped"
    assert "parse" in result.unmapped_tests[0].message.lower()


def test_e2e_target_preserved(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [{"nodeid": "tests/e2e/test_login.py::test_tc_login_e2e_001__happy", "outcome": "passed",
          "call": {"outcome": "passed", "duration": 0.5}}],
        target="e2e",
    )
    assert result.target == "e2e"
    assert result.cases[0].case_id == "TC_LOGIN_E2E_001"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/execution/test_pytest_parser.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 pytest_parser.py**

```python
# assurance_agent/workflow/execution/pytest_parser.py
"""Normalise a pytest-json-report file into a TargetResult.

Only reads what the real test runner wrote; a missing or corrupt report yields
a SKIPPED result carrying the reason (never a fabricated pass).
"""
import json
from pathlib import Path
from typing import Any

from assurance_agent.workflow.execution.case_id import extract_case_id
from assurance_agent.workflow.execution.results import (
    CaseResult,
    ExecutionStatus,
    PytestTarget,
    ResultSource,
    TargetResult,
)

_OUTCOME_MAP: dict[str, ExecutionStatus] = {
    "passed": "passed",
    "xpassed": "passed",
    "failed": "failed",
    "error": "failed",
    "skipped": "skipped",
    "xfailed": "skipped",
}


def parse_pytest_json(
    *,
    change_id: str,
    batch_id: str,
    target: PytestTarget,
    report_path: Path,
    raw_log_path: str,
    command: str,
) -> TargetResult:
    source = ResultSource(framework="pytest", raw_log=raw_log_path, report_json=str(report_path))

    if not report_path.is_file():
        return _skipped(change_id, batch_id, target, command, source,
                        "pytest json report not found — pytest may not have run.")
    try:
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return _skipped(change_id, batch_id, target, command, source,
                        f"failed to parse pytest json report: {err}")

    cases: list[CaseResult] = []
    unmapped: list[CaseResult] = []
    for test in report.get("tests", []):
        entry = _to_case(test, raw_log_path)
        (cases if entry.case_id else unmapped).append(entry)

    all_cases = cases + unmapped
    passed = sum(1 for c in all_cases if c.status == "passed")
    failed = sum(1 for c in all_cases if c.status == "failed")
    skipped = sum(1 for c in all_cases if c.status == "skipped")

    if not all_cases:
        status: ExecutionStatus = "skipped"
    elif failed > 0:
        status = "failed"
    else:
        status = "passed"

    return TargetResult(
        change_id=change_id,
        batch_id=batch_id,
        target=target,
        status=status,
        command=command,
        source=source,
        total=len(all_cases),
        passed=passed,
        failed=failed,
        skipped=skipped,
        cases=cases,
        unmapped_tests=unmapped,
    )


def _to_case(test: dict[str, Any], raw_log_path: str) -> CaseResult:
    nodeid = str(test.get("nodeid", ""))
    outcome = _OUTCOME_MAP.get(str(test.get("outcome", "")), "failed")
    file = nodeid.split("::", 1)[0]
    test_name = nodeid.split("::")[-1] if "::" in nodeid else nodeid

    message = ""
    duration = 0.0
    for phase_name in ("call", "setup", "teardown"):
        phase = test.get(phase_name)
        if not isinstance(phase, dict):
            continue
        duration += float(phase.get("duration", 0.0) or 0.0)
        if not message and phase.get("outcome") in ("failed", "error"):
            message = _longrepr_text(phase.get("longrepr"))

    return CaseResult(
        case_id=extract_case_id(nodeid),
        status=outcome,
        file=file,
        test_name=test_name,
        duration_ms=round(duration * 1000),
        message=message,
        raw_log_ref=raw_log_path,
    )


def _longrepr_text(longrepr: Any) -> str:
    if isinstance(longrepr, str):
        return longrepr
    if isinstance(longrepr, dict):
        crash = longrepr.get("crash")
        if isinstance(crash, dict) and crash.get("message"):
            return str(crash["message"])
        if longrepr.get("message"):
            return str(longrepr["message"])
    return ""


def _skipped(
    change_id: str,
    batch_id: str,
    target: PytestTarget,
    command: str,
    source: ResultSource,
    reason: str,
) -> TargetResult:
    placeholder = CaseResult(
        case_id="", status="skipped", file="", test_name=reason,
        duration_ms=0, message=reason, raw_log_ref=source.raw_log,
    )
    return TargetResult(
        change_id=change_id, batch_id=batch_id, target=target, status="skipped",
        command=command, source=source, total=0, passed=0, failed=0, skipped=0,
        cases=[], unmapped_tests=[placeholder],
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/execution/test_pytest_parser.py -v`
Expected: 7 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/workflow/execution/pytest_parser.py tests/unit/execution/test_pytest_parser.py
git commit -m "feat: add pytest-json-report result parser"
```

---

### Task 3: Quality Gate 四态合并（report/quality_gate.py）

**Files:**
- Create: `assurance_agent/workflow/report/__init__.py`（空）
- Create: `assurance_agent/workflow/report/quality_gate.py`
- Create: `tests/unit/report/__init__.py`（空）
- Test: `tests/unit/report/test_quality_gate.py`

**Interfaces:**
- Consumes: Task 1 的 `TargetResult`、`CoverageResult`、`PerformanceResult`；M2 的 `QualityGateResult`、`QualityGateDimensions`、`FunctionalDimension`、`CoverageDimension`、`NonFunctionalDimension`、`FunctionalCounts`、`CoverageThreshold`、`GateStatus`。
- Produces: `worst_status(statuses: list[GateStatus]) -> GateStatus`（worst-wins：FAIL > PASS_WITH_WARNINGS > PASS > SKIPPED）；`build_quality_gate(*, change_id, batch_id, api, e2e, coverage, coverage_gate_mode, fuzz=None, performance=None) -> QualityGateResult`。Task 5 runner 与 Task 8 inspector 消费。

裁决规则（对齐 TS `quality_gate.ts`）：functional 维度合并 api+e2e+fuzz（fuzz 折入 functional）——任一层有 failed → FAIL；有跑且无 failed → PASS；无跑 → SKIPPED。coverage 维度：不可用 → SKIPPED；`status==PASS` → PASS；否则 `block` 模式升级为 FAIL，`warn` 模式为 PASS_WITH_WARNINGS。performance 单列 non_functional 维度。可追溯性守卫：跑过但无 case_id 映射的测试计数 `unmapped_tests>0` 时，PASS 的 functional 降级为 PASS_WITH_WARNINGS 并加 `TRACEABILITY-BROKEN` warning。final_status = worst-wins(functional, coverage, [non_functional])。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/report/test_quality_gate.py
import pytest

from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    PerformanceResult,
    TargetResult,
)
from assurance_agent.workflow.report.quality_gate import build_quality_gate, worst_status


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "SKIPPED"),
        (["SKIPPED", "SKIPPED"], "SKIPPED"),
        (["PASS", "SKIPPED"], "PASS"),
        (["PASS", "PASS_WITH_WARNINGS"], "PASS_WITH_WARNINGS"),
        (["PASS_WITH_WARNINGS", "FAIL"], "FAIL"),
        (["FAIL", "PASS"], "FAIL"),
    ],
)
def test_worst_status_matrix(statuses, expected) -> None:
    assert worst_status(statuses) == expected


def make_target(target: str, total: int, passed: int, failed: int, unmapped: int = 0) -> TargetResult:
    cases = [
        CaseResult(case_id=f"TC_{target.upper()}_{i:03d}", status="passed", file="f.py",
                   test_name="t", duration_ms=1, message="")
        for i in range(passed)
    ]
    unmapped_cases = [
        CaseResult(case_id="", status="passed", file="f.py", test_name="t", duration_ms=1, message="")
        for _ in range(unmapped)
    ]
    return TargetResult(
        change_id="CH-1", batch_id="b1", target=target,  # type: ignore[arg-type]
        status="failed" if failed else ("passed" if total else "skipped"),
        command="cmd", source={"framework": "pytest", "raw_log": "raw/x.log"},
        total=total, passed=passed, failed=failed, skipped=0,
        cases=cases, unmapped_tests=unmapped_cases,
    )


def make_coverage(status: str, available: bool = True, line: float = 85.0) -> CoverageResult:
    return CoverageResult(
        change_id="CH-1", batch_id="b1", available=available, line_coverage=line,
        branch_coverage=70.0, threshold={"line": 70, "branch": 60}, status=status,  # type: ignore[arg-type]
    )


def test_all_pass_gate_is_pass() -> None:
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1",
        api=make_target("api", 5, 5, 0), e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"), coverage_gate_mode="warn",
    )
    assert gate.dimensions.functional.status == "PASS"
    assert gate.final_status == "PASS"
    assert gate.warnings is None


def test_any_functional_fail_gate_is_fail() -> None:
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1",
        api=make_target("api", 5, 4, 1), e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"), coverage_gate_mode="warn",
    )
    assert gate.final_status == "FAIL"


def test_nothing_ran_gate_is_skipped() -> None:
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1", api=None, e2e=None,
        coverage=make_coverage("SKIPPED", available=False), coverage_gate_mode="warn",
    )
    assert gate.final_status == "SKIPPED"


def test_coverage_below_threshold_warn_is_pass_with_warnings() -> None:
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1",
        api=make_target("api", 3, 3, 0), e2e=None,
        coverage=make_coverage("PASS_WITH_WARNINGS", line=50.0), coverage_gate_mode="warn",
    )
    assert gate.dimensions.coverage.status == "PASS_WITH_WARNINGS"
    assert gate.final_status == "PASS_WITH_WARNINGS"


def test_coverage_below_threshold_block_is_fail() -> None:
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1",
        api=make_target("api", 3, 3, 0), e2e=None,
        coverage=make_coverage("PASS_WITH_WARNINGS", line=50.0), coverage_gate_mode="block",
    )
    assert gate.dimensions.coverage.status == "FAIL"
    assert gate.final_status == "FAIL"


def test_unmapped_tests_downgrade_functional_and_add_warning() -> None:
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1",
        api=make_target("api", 3, 3, 0, unmapped=2), e2e=None,
        coverage=make_coverage("PASS"), coverage_gate_mode="warn",
    )
    assert gate.dimensions.functional.status == "PASS_WITH_WARNINGS"
    assert gate.dimensions.functional.unmapped_tests == 2
    assert gate.final_status == "PASS_WITH_WARNINGS"
    assert gate.warnings is not None
    assert any("TRACEABILITY-BROKEN" in w for w in gate.warnings)


def test_fuzz_folds_into_functional() -> None:
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1",
        api=make_target("api", 2, 2, 0), e2e=None,
        coverage=make_coverage("PASS"), coverage_gate_mode="warn",
        fuzz=make_target("fuzz", 3, 2, 1),
    )
    assert gate.dimensions.functional.status == "FAIL"
    assert gate.dimensions.functional.fuzz is not None
    assert gate.dimensions.functional.fuzz.failed == 1
    assert gate.final_status == "FAIL"


def test_performance_fail_forms_non_functional_and_fails_gate() -> None:
    perf = PerformanceResult(
        change_id="CH-1", batch_id="b1", available=True, status="FAIL",
        scenarios=[{
            "capability": "c", "endpoint": "/e", "measured_p95_ms": 900.0,
            "threshold_p95_ms": 500.0, "measured_error_rate": 0.0,
            "threshold_error_rate_max": 0.01, "verdict": "FAIL",
        }],
    )
    gate = build_quality_gate(
        change_id="CH-1", batch_id="b1",
        api=make_target("api", 2, 2, 0), e2e=None,
        coverage=make_coverage("PASS"), coverage_gate_mode="warn", performance=perf,
    )
    assert gate.dimensions.non_functional is not None
    assert gate.dimensions.non_functional.status == "FAIL"
    assert gate.final_status == "FAIL"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/report/test_quality_gate.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 quality_gate.py**

```python
# assurance_agent/workflow/report/quality_gate.py
"""Deterministic, worst-wins Quality Gate (aligned with TS quality_gate.ts).

Functional folds api + e2e + fuzz; coverage and performance are separate
dimensions. final_status is the worst status across active dimensions.
"""
from typing import Literal

from assurance_agent.artifacts.models import (
    CoverageDimension,
    CoverageThreshold,
    FunctionalCounts,
    FunctionalDimension,
    GateStatus,
    NonFunctionalDimension,
    QualityGateDimensions,
    QualityGateResult,
)
from assurance_agent.workflow.execution.results import (
    CoverageResult,
    PerformanceResult,
    TargetResult,
)


def worst_status(statuses: list[GateStatus]) -> GateStatus:
    present = [s for s in statuses if s]
    if not present:
        return "SKIPPED"
    if "FAIL" in present:
        return "FAIL"
    if "PASS_WITH_WARNINGS" in present:
        return "PASS_WITH_WARNINGS"
    if "PASS" in present:
        return "PASS"
    return "SKIPPED"


def _counts(result: TargetResult | None) -> FunctionalCounts:
    if not result:
        return FunctionalCounts(total=0, passed=0, failed=0)
    return FunctionalCounts(total=result.total, passed=result.passed, failed=result.failed)


def _functional_status(*results: TargetResult | None) -> GateStatus:
    ran = [r for r in results if r and r.total > 0]
    if not ran:
        return "SKIPPED"
    if any(r.failed > 0 for r in ran):
        return "FAIL"
    return "PASS"


def _unmapped_count(*results: TargetResult | None) -> int:
    return sum(len(r.unmapped_tests) for r in results if r and r.total > 0)


def _coverage_status(coverage: CoverageResult | None, gate_mode: Literal["warn", "block"]) -> GateStatus:
    if not coverage or not coverage.available:
        return "SKIPPED"
    if coverage.status == "PASS":
        return "PASS"
    return "FAIL" if gate_mode == "block" else "PASS_WITH_WARNINGS"


def _non_functional(perf: PerformanceResult | None) -> NonFunctionalDimension | None:
    if not perf:
        return None
    if not perf.available:
        status: GateStatus = "SKIPPED"
    elif perf.status == "FAIL":
        status = "FAIL"
    elif perf.status == "PASS":
        status = "PASS"
    else:
        status = "SKIPPED"
    return NonFunctionalDimension(status=status, performance=perf.scenarios)


def build_quality_gate(
    *,
    change_id: str,
    batch_id: str,
    api: TargetResult | None,
    e2e: TargetResult | None,
    coverage: CoverageResult | None,
    coverage_gate_mode: Literal["warn", "block"],
    fuzz: TargetResult | None = None,
    performance: PerformanceResult | None = None,
) -> QualityGateResult:
    func_status = _functional_status(api, e2e, fuzz)
    cov_status = _coverage_status(coverage, coverage_gate_mode)
    non_functional = _non_functional(performance)
    warnings: list[str] = []

    unmapped = _unmapped_count(api, e2e, fuzz)
    if unmapped > 0:
        warnings.append(
            f"TRACEABILITY-BROKEN: {unmapped} executed test(s) have no case_id mapping. "
            "Test function names must use the test_<case_id lowercase>__<description> prefix "
            "(e.g. test_tc_user_api_001__list_users_happy_path)."
        )
        if func_status == "PASS":
            func_status = "PASS_WITH_WARNINGS"

    functional = FunctionalDimension(
        status=func_status,
        api=_counts(api),
        e2e=_counts(e2e),
        unmapped_tests=unmapped if unmapped > 0 else None,
    )
    if fuzz and fuzz.status != "skipped":
        functional.fuzz = _counts(fuzz)

    coverage_dim = CoverageDimension(
        status=cov_status,
        available=bool(coverage and coverage.available),
        line_coverage=coverage.line_coverage if coverage else 0.0,
        branch_coverage=coverage.branch_coverage if coverage else 0.0,
        threshold=coverage.threshold if coverage else CoverageThreshold(line=0, branch=0),
    )

    dimensions = QualityGateDimensions(functional=functional, coverage=coverage_dim)
    gate_statuses: list[GateStatus] = [func_status, cov_status]
    if non_functional is not None:
        dimensions.non_functional = non_functional
        gate_statuses.append(non_functional.status)

    return QualityGateResult(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        dimensions=dimensions,
        final_status=worst_status(gate_statuses),
        warnings=warnings or None,
    )
```

同时创建空文件：`assurance_agent/workflow/report/__init__.py`、`tests/unit/report/__init__.py`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/report/test_quality_gate.py -v`
Expected: 14 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/workflow/report tests/unit/report
git commit -m "feat: add worst-wins four-state quality gate"
```

---

### Task 4: 目标选择 + 执行子配置 + runner（selection.py、exec_config.py、runners.py）

**Files:**
- Create: `assurance_agent/workflow/execution/exec_config.py`
- Create: `assurance_agent/workflow/execution/selection.py`
- Create: `assurance_agent/workflow/execution/runners.py`
- Test: `tests/unit/execution/test_selection.py`
- Test: `tests/unit/execution/test_runners.py`

**Interfaces:**
- Consumes: Task 1 结果模型；Task 2 `parse_pytest_json`；M1 `AaConfig`；M2 `SelectedTargets`、`CoverageThreshold`、`PerformanceScenarioVerdict`。
- Produces:
  - `exec_config.CoverageConfig`（`enabled`/`gate_mode: Literal["warn","block"]`/`target_package`/`threshold: CoverageThreshold`）与 `load_coverage_config(config: AaConfig) -> CoverageConfig`；`exec_config.PerfConfig`（`enabled`/`base_url`/`default_load: dict`）与 `load_perf_config(config: AaConfig) -> PerfConfig`。
  - `selection.resolve_selected_targets(change_dir: Path) -> SelectedTargets`（workflow-state → plans → 全选）。
  - `runners.run_pytest_target(*, project_root, batch_dir, change_id, batch_id, target, test_dir, cov_package=None) -> TargetResult`；`runners.parse_coverage_result(*, change_id, batch_id, batch_dir, threshold) -> CoverageResult`；`runners.run_performance_target(*, project_root, change_dir, batch_dir, change_id, batch_id, perf_config) -> PerformanceResult`；纯函数 `runners.parse_locust_stats(csv_path: Path)` 与 `runners.build_scenario_verdicts(scenarios, stats_by_name)`。Task 5 runner 消费。

- [ ] **Step 1: 写失败测试（selection）**

```python
# tests/unit/execution/test_selection.py
from pathlib import Path

from assurance_agent.workflow.execution.selection import resolve_selected_targets


def test_defaults_to_all_when_no_state_or_plans(tmp_path: Path) -> None:
    targets = resolve_selected_targets(tmp_path)
    assert (targets.api, targets.e2e, targets.fuzz, targets.performance) == (True, True, True, True)


def test_reads_selected_targets_from_workflow_state(tmp_path: Path) -> None:
    (tmp_path / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    targets = resolve_selected_targets(tmp_path)
    assert targets.api is True
    assert targets.e2e is False
    assert targets.fuzz is False


def test_falls_back_to_layers_key(tmp_path: Path) -> None:
    (tmp_path / "workflow-state.yaml").write_text(
        "layers:\n  api: false\n  e2e: true\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    targets = resolve_selected_targets(tmp_path)
    assert targets.e2e is True
    assert targets.api is False


def test_plan_presence_selects_targets(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "api-codegen-plan.md").write_text("plan", encoding="utf-8")
    (plans / "e2e-codegen-plan.md").write_text("plan", encoding="utf-8")
    targets = resolve_selected_targets(tmp_path)
    assert targets.api is True
    assert targets.e2e is True
    assert targets.fuzz is False
    assert targets.performance is False
```

- [ ] **Step 2: 写失败测试（runners）**

```python
# tests/unit/execution/test_runners.py
import json
import subprocess
from pathlib import Path

from assurance_agent.artifacts.models import CoverageThreshold
from assurance_agent.workflow.execution import runners
from assurance_agent.workflow.execution.runners import (
    build_scenario_verdicts,
    parse_coverage_result,
    parse_locust_stats,
    run_pytest_target,
)


def _stub_pytest(report_tests: list[dict], coverage_totals: dict | None = None):
    """Return a fake subprocess.run that writes the canned report/coverage files."""
    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": report_tests}), encoding="utf-8")
        if coverage_totals is not None:
            cov_arg = next((a for a in args if a.startswith("--cov-report=json:")), None)
            if cov_arg:
                cov_path = Path(cov_arg.split("json:", 1)[1])
                cov_path.parent.mkdir(parents=True, exist_ok=True)
                cov_path.write_text(json.dumps({"totals": coverage_totals}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="pytest output", stderr="")
    return fake_run


def test_run_pytest_target_parses_stubbed_report(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    monkeypatch.setattr(
        runners.subprocess, "run",
        _stub_pytest([
            {"nodeid": "tests/api/t.py::test_tc_api_001__ok", "outcome": "passed",
             "call": {"outcome": "passed", "duration": 0.01}},
        ]),
    )
    result = run_pytest_target(
        project_root=tmp_path, batch_dir=tmp_path / "batch", change_id="CH-1",
        batch_id="b1", target="api", test_dir="tests/api",
    )
    assert result.status == "passed"
    assert result.total == 1
    assert result.cases[0].case_id == "TC_API_001"
    assert (tmp_path / "batch" / "raw" / "api.log").is_file()


def test_run_pytest_target_missing_dir_is_skipped_no_subprocess(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("subprocess must not run when the test dir is absent")
    monkeypatch.setattr(runners.subprocess, "run", boom)
    result = run_pytest_target(
        project_root=tmp_path, batch_dir=tmp_path / "batch", change_id="CH-1",
        batch_id="b1", target="fuzz", test_dir="tests/fuzz",
    )
    assert result.status == "skipped"
    assert result.total == 0
    assert "SKIPPED" in result.unmapped_tests[0].message


def test_run_pytest_target_collects_coverage(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    monkeypatch.setattr(
        runners.subprocess, "run",
        _stub_pytest(
            [{"nodeid": "tests/api/t.py::test_tc_api_001__ok", "outcome": "passed",
              "call": {"outcome": "passed", "duration": 0.0}}],
            coverage_totals={"percent_covered": 88.0, "num_branches": 10, "covered_branches": 7},
        ),
    )
    batch_dir = tmp_path / "batch"
    run_pytest_target(
        project_root=tmp_path, batch_dir=batch_dir, change_id="CH-1", batch_id="b1",
        target="api", test_dir="tests/api", cov_package="app",
    )
    cov = parse_coverage_result(
        change_id="CH-1", batch_id="b1", batch_dir=batch_dir,
        threshold=CoverageThreshold(line=70, branch=60),
    )
    assert cov.available is True
    assert cov.line_coverage == 88.0
    assert cov.branch_coverage == 70.0
    assert cov.status == "PASS"


def test_parse_coverage_missing_is_skipped(tmp_path: Path) -> None:
    cov = parse_coverage_result(
        change_id="CH-1", batch_id="b1", batch_dir=tmp_path,
        threshold=CoverageThreshold(line=70, branch=60),
    )
    assert cov.available is False
    assert cov.status == "SKIPPED"


def test_parse_locust_stats_reads_p95_and_counts(tmp_path: Path) -> None:
    csv = tmp_path / "locust_stats.csv"
    csv.write_text(
        "Type,Name,Request Count,Failure Count,Median Response Time,95%\n"
        "GET,list_menus,100,2,120,450\n"
        "Aggregated,Aggregated,100,2,120,450\n",
        encoding="utf-8",
    )
    rows = parse_locust_stats(csv)
    assert len(rows) == 1  # Aggregated excluded
    assert rows[0]["name"] == "list_menus"
    assert rows[0]["requests"] == 100
    assert rows[0]["failures"] == 2
    assert rows[0]["p95"] == 450


def test_build_scenario_verdicts_pass_fail_skip() -> None:
    scenarios = [
        {"capability": "fast", "endpoint": "/f", "thresholds": {"p95_ms": 500, "error_rate_max": 0.01}},
        {"capability": "slow", "endpoint": "/s", "thresholds": {"p95_ms": 500, "error_rate_max": 0.01}},
        {"capability": "quiet", "endpoint": "/q", "thresholds": {"p95_ms": 500, "error_rate_max": 0.01}},
    ]
    stats = {
        "fast": {"p95": 200, "requests": 50, "failures": 0},
        "slow": {"p95": 900, "requests": 50, "failures": 0},
    }
    verdicts = build_scenario_verdicts(scenarios, stats)
    by_cap = {v.capability: v.verdict for v in verdicts}
    assert by_cap["fast"] == "PASS"
    assert by_cap["slow"] == "FAIL"
    assert by_cap["quiet"] == "SKIPPED"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/execution/test_selection.py tests/unit/execution/test_runners.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: 实现 exec_config.py、selection.py、runners.py**

```python
# assurance_agent/workflow/execution/exec_config.py
"""Coverage/performance sub-config extracted from AaConfig (extra='allow').

AaConfig (M1) does not declare coverage/performance keys but preserves them in
model_extra because of extra='allow'; these loaders read them with defaults so
run_change never crashes on a minimal config.
"""
from typing import Any, Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models import CoverageThreshold
from assurance_agent.config import AaConfig


class CoverageConfig(BaseModel):
    enabled: bool = True
    gate_mode: Literal["warn", "block"] = "warn"
    target_package: str = "app"
    threshold: CoverageThreshold


class PerfConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://localhost:8000"
    default_load: dict[str, Any] = {"users": 10, "spawn_rate": 2, "run_time_s": 30}


def _section(config: AaConfig, key: str) -> dict[str, Any]:
    value = getattr(config, key, None)
    return value if isinstance(value, dict) else {}


def load_coverage_config(config: AaConfig) -> CoverageConfig:
    raw = _section(config, "coverage")
    thr = raw.get("threshold") if isinstance(raw.get("threshold"), dict) else {}
    gate_mode = raw.get("gate_mode", "warn")
    return CoverageConfig(
        enabled=bool(raw.get("enabled", True)),
        gate_mode="block" if gate_mode == "block" else "warn",
        target_package=str(raw.get("target_package", "app")),
        threshold=CoverageThreshold(
            line=float(thr.get("line", 70)),
            branch=float(thr.get("branch", 60)),
            module_line=thr.get("module_line"),
            diff_line=thr.get("diff_line"),
        ),
    )


def load_perf_config(config: AaConfig) -> PerfConfig:
    raw = _section(config, "performance")
    load = raw.get("default_load") if isinstance(raw.get("default_load"), dict) else {}
    return PerfConfig(
        enabled=bool(raw.get("enabled", True)),
        base_url=str(raw.get("base_url", "http://localhost:8000")),
        default_load={
            "users": int(load.get("users", 10)),
            "spawn_rate": int(load.get("spawn_rate", 2)),
            "run_time_s": int(load.get("run_time_s", 30)),
        },
    )
```

```python
# assurance_agent/workflow/execution/selection.py
"""Resolve which test layers to run for a change (aligned with TS resolveSelectedTargets).

Precedence: explicit workflow-state (selected_targets or layers) → codegen plan
presence → all four targets.
"""
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models import SelectedTargets

_KEYS = ("api", "e2e", "fuzz", "performance")


def resolve_selected_targets(change_dir: Path) -> SelectedTargets:
    from_state = _from_workflow_state(change_dir)
    if from_state is not None:
        return from_state

    plans_dir = change_dir / "plans"
    plan_selection = SelectedTargets(
        api=(plans_dir / "api-codegen-plan.md").is_file(),
        e2e=(plans_dir / "e2e-codegen-plan.md").is_file(),
        fuzz=(plans_dir / "fuzz-codegen-plan.md").is_file(),
        performance=(plans_dir / "performance-codegen-plan.md").is_file(),
    )
    if any(getattr(plan_selection, k) for k in _KEYS):
        return plan_selection

    return SelectedTargets(api=True, e2e=True, fuzz=True, performance=True)


def _from_workflow_state(change_dir: Path) -> SelectedTargets | None:
    state_path = change_dir / "workflow-state.yaml"
    if not state_path.is_file():
        return None
    try:
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    return _normalise(doc.get("selected_targets")) or _normalise(doc.get("layers"))


def _normalise(value: Any) -> SelectedTargets | None:
    if not isinstance(value, dict):
        return None
    if not any(isinstance(value.get(k), bool) for k in _KEYS):
        return None
    return SelectedTargets(
        api=value.get("api") is True,
        e2e=value.get("e2e") is True,
        fuzz=value.get("fuzz") is True,
        performance=value.get("performance") is True,
    )
```

```python
# assurance_agent/workflow/execution/runners.py
"""subprocess-driven test runners + result parsing.

pytest (api/e2e/fuzz) uses the --json-report protocol; performance uses Locust's
--csv output. Never fabricates: a missing test dir / runner / traffic yields a
SKIPPED result. subprocess.run is monkeypatched in unit tests.
"""
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models import CoverageThreshold, PerformanceScenarioVerdict
from assurance_agent.workflow.execution.exec_config import PerfConfig
from assurance_agent.workflow.execution.pytest_parser import parse_pytest_json
from assurance_agent.workflow.execution.results import (
    CoverageResult,
    PerformanceResult,
    PytestTarget,
    ResultSource,
    TargetResult,
)

_RAW = "raw"


def run_pytest_target(
    *,
    project_root: Path,
    batch_dir: Path,
    change_id: str,
    batch_id: str,
    target: PytestTarget,
    test_dir: str,
    cov_package: str | None = None,
) -> TargetResult:
    raw_dir = batch_dir / _RAW
    log_path = raw_dir / f"{target}.log"
    report_path = raw_dir / f"{target}-report.json"

    if not (project_root / test_dir).exists():
        source = ResultSource(framework="pytest", raw_log=str(log_path), report_json=str(report_path))
        reason = f"No test targets found under {test_dir} — {target} SKIPPED."
        return _skipped_target(change_id, batch_id, target, f"uv run pytest {test_dir}", source, reason)

    raw_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "uv", "run", "pytest", test_dir,
        "-p", "no:cacheprovider",
        "--json-report", f"--json-report-file={report_path}",
    ]
    if cov_package:
        cov_json = raw_dir / "coverage.json"
        args += [f"--cov={cov_package}", "--cov-branch", f"--cov-report=json:{cov_json}"]
    command = " ".join(args)

    proc = subprocess.run(args, cwd=str(project_root), capture_output=True, text=True)  # noqa: S603
    log_path.write_text(f"$ {command}\n\n{proc.stdout or ''}\n{proc.stderr or ''}", encoding="utf-8")

    return parse_pytest_json(
        change_id=change_id, batch_id=batch_id, target=target,
        report_path=report_path, raw_log_path=str(log_path), command=command,
    )


def _skipped_target(
    change_id: str, batch_id: str, target: PytestTarget, command: str,
    source: ResultSource, reason: str,
) -> TargetResult:
    from assurance_agent.workflow.execution.results import CaseResult
    return TargetResult(
        change_id=change_id, batch_id=batch_id, target=target, status="skipped",
        command=command, source=source, total=0, passed=0, failed=0, skipped=0,
        cases=[], unmapped_tests=[CaseResult(
            case_id="", status="skipped", file="", test_name=reason,
            duration_ms=0, message=reason, raw_log_ref=source.raw_log,
        )],
    )


def parse_coverage_result(
    *, change_id: str, batch_id: str, batch_dir: Path, threshold: CoverageThreshold,
) -> CoverageResult:
    cov_json = batch_dir / _RAW / "coverage.json"
    if not cov_json.is_file():
        return CoverageResult(
            change_id=change_id, batch_id=batch_id, available=False,
            line_coverage=0.0, branch_coverage=0.0, threshold=threshold,
            status="SKIPPED", skip_reason="coverage.json not produced (pytest-cov unavailable or disabled)",
        )
    try:
        totals = json.loads(cov_json.read_text(encoding="utf-8")).get("totals", {})
    except (OSError, json.JSONDecodeError):
        totals = {}
    return _coverage_from_totals(change_id, batch_id, totals, threshold)


def _coverage_from_totals(
    change_id: str, batch_id: str, totals: dict[str, Any], threshold: CoverageThreshold,
) -> CoverageResult:
    line = float(totals.get("percent_covered", 0.0) or 0.0)
    num_branches = float(totals.get("num_branches", 0) or 0)
    covered_branches = float(totals.get("covered_branches", 0) or 0)
    branch = round(covered_branches / num_branches * 100, 2) if num_branches > 0 else 100.0
    status = "PASS" if line >= threshold.line and branch >= threshold.branch else "PASS_WITH_WARNINGS"
    return CoverageResult(
        change_id=change_id, batch_id=batch_id, available=True,
        line_coverage=line, branch_coverage=branch, threshold=threshold, status=status,
        source={"coverage_json": "raw/coverage.json"},
    )


# ── Performance (Locust) ─────────────────────────────────────────────────────

def run_performance_target(
    *,
    project_root: Path,
    change_dir: Path,
    batch_dir: Path,
    change_id: str,
    batch_id: str,
    perf_config: PerfConfig,
) -> PerformanceResult:
    raw_dir = batch_dir / _RAW
    log_path = raw_dir / "performance.log"

    def skipped(reason: str) -> PerformanceResult:
        raw_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(reason, encoding="utf-8")
        return PerformanceResult(
            change_id=change_id, batch_id=batch_id, available=False, status="SKIPPED",
            scenarios=[], command="", source={"raw_log": str(log_path)},
        )

    if not perf_config.enabled:
        return skipped("Performance disabled in .aa/config.yaml (performance.enabled=false).")
    scenarios = load_perf_scenarios(change_dir)
    if not scenarios:
        return skipped("No type:Performance cases with thresholds found — performance SKIPPED.")
    perf_dir = project_root / "tests" / "perf"
    locustfiles = sorted(perf_dir.glob("locustfile*.py")) if perf_dir.is_dir() else []
    if not locustfiles:
        return skipped("No locustfiles under tests/perf/ — performance SKIPPED.")

    raw_dir.mkdir(parents=True, exist_ok=True)
    load = perf_config.default_load
    stats: dict[str, dict[str, float]] = {}
    commands: list[str] = []
    any_traffic = False
    for locustfile in locustfiles:
        prefix = raw_dir / f"locust_{locustfile.stem}"
        args = [
            "uv", "run", "locust", "-f", str(locustfile), "--headless",
            "-u", str(load["users"]), "-r", str(load["spawn_rate"]),
            "-t", f"{load['run_time_s']}s", "--host", perf_config.base_url,
            "--csv", str(prefix), "--only-summary",
        ]
        commands.append(" ".join(args))
        proc = subprocess.run(args, cwd=str(project_root), capture_output=True, text=True)  # noqa: S603
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(args)}\n\n{proc.stdout or ''}\n{proc.stderr or ''}\n")
        stats_csv = prefix.with_name(prefix.name + "_stats.csv")
        if stats_csv.is_file():
            for row in parse_locust_stats(stats_csv):
                if row["requests"] > 0:
                    any_traffic = True
                stats[row["name"]] = row

    if not any_traffic:
        return skipped("Locust ran but recorded no successful traffic (environment likely unreachable) — SKIPPED.")

    verdicts = build_scenario_verdicts(scenarios, stats)
    if any(v.verdict == "FAIL" for v in verdicts):
        status = "FAIL"
    elif any(v.verdict == "PASS" for v in verdicts):
        status = "PASS"
    else:
        status = "SKIPPED"
    return PerformanceResult(
        change_id=change_id, batch_id=batch_id, available=True, status=status,
        scenarios=verdicts, command=" && ".join(commands), source={"raw_log": str(log_path)},
    )


def parse_locust_stats(csv_path: Path) -> list[dict[str, Any]]:
    lines = [ln for ln in csv_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = [h.strip().lower() for h in lines[0].split(",")]
    idx_name = header.index("name") if "name" in header else -1
    idx_req = header.index("request count") if "request count" in header else -1
    idx_fail = header.index("failure count") if "failure count" in header else -1
    idx_p95 = header.index("95%") if "95%" in header else -1
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        cols = line.split(",")
        name = cols[idx_name].strip() if idx_name >= 0 and idx_name < len(cols) else ""
        if not name or name.lower() == "aggregated":
            continue
        rows.append({
            "name": name,
            "requests": _to_num(cols, idx_req),
            "failures": _to_num(cols, idx_fail),
            "p95": _to_num(cols, idx_p95),
        })
    return rows


def build_scenario_verdicts(
    scenarios: list[dict[str, Any]], stats_by_name: dict[str, dict[str, float]],
) -> list[PerformanceScenarioVerdict]:
    verdicts: list[PerformanceScenarioVerdict] = []
    for sc in scenarios:
        thr = sc["thresholds"]
        stat = stats_by_name.get(sc["capability"]) or stats_by_name.get(sc["endpoint"])
        if not stat or stat["requests"] == 0:
            verdicts.append(PerformanceScenarioVerdict(
                capability=sc["capability"], endpoint=sc["endpoint"],
                measured_p95_ms=None, threshold_p95_ms=float(thr["p95_ms"]),
                measured_error_rate=None, threshold_error_rate_max=float(thr["error_rate_max"]),
                verdict="SKIPPED",
            ))
            continue
        error_rate = stat["failures"] / stat["requests"]
        passed = stat["p95"] <= thr["p95_ms"] and error_rate <= thr["error_rate_max"]
        verdicts.append(PerformanceScenarioVerdict(
            capability=sc["capability"], endpoint=sc["endpoint"],
            measured_p95_ms=float(stat["p95"]), threshold_p95_ms=float(thr["p95_ms"]),
            measured_error_rate=round(error_rate, 4), threshold_error_rate_max=float(thr["error_rate_max"]),
            verdict="PASS" if passed else "FAIL",
        ))
    return verdicts


def load_perf_scenarios(change_dir: Path) -> list[dict[str, Any]]:
    cases_dir = change_dir / "cases"
    scenarios: list[dict[str, Any]] = []
    if not cases_dir.is_dir():
        return scenarios
    for path in cases_dir.rglob("*.y*ml"):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        for case in _collect_cases(doc):
            parsed = _parse_perf_case(case)
            if parsed:
                scenarios.append(parsed)
    return scenarios


def _collect_cases(doc: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(doc, dict):
        return out
    for key in ("added", "modified", "cases"):
        value = doc.get(key)
        if isinstance(value, list):
            out.extend(item for item in value if isinstance(item, dict))
    return out


def _parse_perf_case(case: dict[str, Any]) -> dict[str, Any] | None:
    if case.get("type") != "Performance":
        return None
    nested = (((case.get("automation") or {}).get("performance") or {}).get("scenario")) or {}
    thresholds = nested.get("thresholds") if nested.get("thresholds") else case.get("thresholds")
    if not isinstance(thresholds, dict) or thresholds.get("p95_ms") is None:
        return None
    perf = case.get("performance") or ((case.get("automation") or {}).get("performance")) or {}
    return {
        "capability": nested.get("capability") or perf.get("capability") or case.get("case_id") or "unknown",
        "endpoint": nested.get("endpoint") or perf.get("endpoint") or "",
        "thresholds": {
            "p95_ms": float(thresholds["p95_ms"]),
            "error_rate_max": float(thresholds.get("error_rate_max", 0.01)),
        },
    }


def _to_num(cols: list[str], idx: int) -> float:
    if idx < 0 or idx >= len(cols):
        return 0.0
    try:
        return float(cols[idx].strip())
    except ValueError:
        return 0.0
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/execution/test_selection.py tests/unit/execution/test_runners.py -v`
Expected: 4 passed（selection）+ 6 passed（runners）= 10 passed

- [ ] **Step 6: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错（`# noqa: S603` 保留 subprocess 调用；如 ruff 未启用 flake8-bandit 规则可删掉该注释）

- [ ] **Step 7: Commit**

```bash
git add assurance_agent/workflow/execution/exec_config.py \
        assurance_agent/workflow/execution/selection.py \
        assurance_agent/workflow/execution/runners.py \
        tests/unit/execution/test_selection.py tests/unit/execution/test_runners.py
git commit -m "feat: add target selection, exec sub-config and subprocess runners"
```

---

### Task 5: 证据落盘/读取 + run_change 编排（evidence.py、runner.py）

**Files:**
- Create: `assurance_agent/workflow/execution/evidence.py`
- Create: `assurance_agent/workflow/execution/runner.py`
- Test: `tests/unit/execution/test_evidence.py`
- Test: `tests/unit/execution/test_runner.py`

**Interfaces:**
- Consumes: Task 1 结果模型；Task 3 `build_quality_gate`；Task 4 `run_pytest_target`/`parse_coverage_result`/`run_performance_target`/`resolve_selected_targets`/`load_coverage_config`/`load_perf_config`；M2 `ExecutionManifest`/`QualityGateResult`/`SelectedTargets`/`CoverageThreshold`；M1 `AaConfig`。
- Produces:
  - `evidence.reserve_execution_batch(execution_dir, batch_id) -> BatchReservation` 使用 `mkdir(exist_ok=False)` 在任何 runner 写 raw 文件前独占批次目录；碰撞 fail-closed。
  - `evidence.publish_execution_evidence(*, execution_dir, change_id, batch_id, selected_targets, api, e2e, fuzz, coverage, performance, quality_gate, summary, reservation=None) -> ExecutionManifest`（写 `runs/<batch-id>/` + 顶层指针；直接调用时自行预留，runner 调用时验证传入 reservation；已存在 manifest 永不覆盖）。
  - `evidence.ExecutionEvidence`（模型：`manifest`/`batch_id`/`selected_targets`/`api`/`e2e`/`fuzz`/`coverage`/`performance`/`quality_gate`/`result_paths: dict[str,str]`/`integrity_issues: list[IntegrityIssue]`）、`evidence.IntegrityIssue(target,path,reason)`、`evidence.EvidenceError(AaError)`、`evidence.load_execution_evidence(execution_dir: Path, *, batch_id: str | None = None) -> ExecutionEvidence`。`batch_id=None` 读顶层 pointer；显式 batch 允许新格式及历史格式 `^[0-9]{8}-[0-9]{6}(?:-[0-9a-f]{8})?$`，读 `runs/<batch>/execution-manifest.yaml`，并要求 manifest 内 `batch_id` 与所选目录一致。manifest 的 result path 必须是相对 `execution_dir` 的路径，绝对路径即使仍位于 execution 内也拒绝，`..` 逃逸同样拒绝。
  - `runner.generate_batch_id() -> str`（`YYYYMMDD-HHmmss-<8 lowercase hex>`，测试可 monkeypatch）；`runner.run_change(project_root: Path, change_dir: Path, config: AaConfig, *, reservation: BatchReservation | None = None) -> ExecutionManifest`（**BINDING 签名**）。通常由 runner 自行预留；Task 10 需要先在同一批次写 override evidence 时，由 `run_cmd` 先预留并传入同一 reservation。Task 6 `aa run` 与 Task 8 inspector 消费。

- [ ] **Step 1: 写失败测试（evidence）**

```python
# tests/unit/execution/test_evidence.py
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.workflow.execution.evidence import (
    EvidenceError,
    load_execution_evidence,
    publish_execution_evidence,
)
from assurance_agent.workflow.execution.results import CoverageResult, TargetResult
from assurance_agent.workflow.report.quality_gate import build_quality_gate


def make_api(passed: int = 2, failed: int = 0) -> TargetResult:
    return TargetResult(
        change_id="CH-1", batch_id="20260715-000000", target="api",
        status="failed" if failed else "passed", command="cmd",
        source={"framework": "pytest", "raw_log": "raw/api.log"},
        total=passed + failed, passed=passed, failed=failed, skipped=0,
        cases=[], unmapped_tests=[],
    )


def make_cov(available: bool = True) -> CoverageResult:
    return CoverageResult(
        change_id="CH-1", batch_id="20260715-000000", available=available,
        line_coverage=90.0, branch_coverage=80.0, threshold={"line": 70, "branch": 60},
        status="PASS" if available else "SKIPPED",
    )


def publish(tmp_path: Path, api: TargetResult, cov: CoverageResult):
    execution_dir = tmp_path / "execution"
    api_result = api
    gate = build_quality_gate(
        change_id="CH-1", batch_id="20260715-000000", api=api_result, e2e=None,
        coverage=cov, coverage_gate_mode="warn",
    )
    manifest = publish_execution_evidence(
        execution_dir=execution_dir, change_id="CH-1", batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api_result, e2e=None, fuzz=None, coverage=cov, performance=None,
        quality_gate=gate, summary="# summary\n",
    )
    return execution_dir, manifest


def test_publish_writes_batch_dir_and_latest_pointers(tmp_path: Path) -> None:
    execution_dir, manifest = publish(tmp_path, make_api(), make_cov())
    batch_dir = execution_dir / "runs" / "20260715-000000"
    assert (batch_dir / "api-result.json").is_file()
    assert (batch_dir / "coverage-result.json").is_file()
    assert (batch_dir / "quality-gate-result.json").is_file()
    assert (batch_dir / "execution-manifest.yaml").is_file()
    assert (execution_dir / "api-result.json").is_file()
    assert (execution_dir / "execution-manifest.yaml").is_file()
    assert manifest.final_status == "PASS"
    assert manifest.result_files["api"] == "runs/20260715-000000/api-result.json"


def test_publish_same_batch_twice_is_rejected_without_overwrite(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest_path = execution_dir / "runs/20260715-000000/execution-manifest.yaml"
    before = manifest_path.read_bytes()
    with pytest.raises(EvidenceError, match="already (exists|published)"):
        publish(tmp_path, make_api(failed=1), make_cov())
    assert manifest_path.read_bytes() == before


def test_load_evidence_round_trips(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(failed=1), make_cov())
    evidence = load_execution_evidence(execution_dir)
    assert evidence.batch_id == "20260715-000000"
    assert evidence.api is not None
    assert evidence.api.failed == 1
    assert evidence.coverage is not None
    assert evidence.quality_gate is not None
    assert evidence.quality_gate.final_status == "FAIL"
    assert evidence.integrity_issues == []


def test_load_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError):
        load_execution_evidence(tmp_path / "execution")


def test_selected_target_with_missing_result_is_integrity_issue(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    (execution_dir / "runs" / "20260715-000000" / "api-result.json").unlink()
    evidence = load_execution_evidence(execution_dir)
    assert evidence.api is None
    assert len(evidence.integrity_issues) == 1
    assert evidence.integrity_issues[0].target == "api"


def test_load_rejects_result_path_escape(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest = execution_dir / "execution-manifest.yaml"
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    doc["result_files"]["api"] = "../../outside.json"
    manifest.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="escapes execution directory"):
        load_execution_evidence(execution_dir)


def test_load_rejects_absolute_result_path_even_inside_execution(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest = execution_dir / "execution-manifest.yaml"
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    doc["result_files"]["api"] = str(
        (execution_dir / "runs/20260715-000000/api-result.json").resolve()
    )
    manifest.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="must be relative"):
        load_execution_evidence(execution_dir)


def test_explicit_batch_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest = execution_dir / "runs/20260715-000000/execution-manifest.yaml"
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    doc["batch_id"] = "20260715-999999"
    manifest.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="batch id mismatch"):
        load_execution_evidence(execution_dir, batch_id="20260715-000000")


def test_result_identity_mismatch_is_integrity_issue(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    result_path = execution_dir / "runs/20260715-000000/api-result.json"
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    doc["batch_id"] = "20260715-999999"
    result_path.write_text(json.dumps(doc), encoding="utf-8")
    evidence = load_execution_evidence(execution_dir)
    assert any("identity mismatch" in issue.reason for issue in evidence.integrity_issues)


def test_coverage_identity_mismatch_is_integrity_issue(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    result_path = execution_dir / "runs/20260715-000000/coverage-result.json"
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    doc["change_id"] = "CH-OTHER"
    result_path.write_text(json.dumps(doc), encoding="utf-8")
    evidence = load_execution_evidence(execution_dir)
    assert any(
        issue.target == "coverage" and "identity mismatch" in issue.reason
        for issue in evidence.integrity_issues
    )
```

- [ ] **Step 2: 写失败测试（runner）**

```python
# tests/unit/execution/test_runner.py
import json
import subprocess
from pathlib import Path

import pytest

from assurance_agent.config import AaConfig
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.execution.runner import run_change


def make_config() -> AaConfig:
    return AaConfig.model_validate({
        "version": 1,
        "sources": {"frontend": "./frontend", "backend": "./backend"},
        "qa": {"cases": "./qa/cases", "changes": "./qa/changes"},
        "tests": {"root": "./tests", "api": "./tests/api", "e2e": "./tests/e2e"},
        "frameworks": {"api": {"enabled": True, "name": "pytest"},
                       "e2e": {"enabled": True, "name": "playwright"}},
        "generation": {"prd_input_mode": "prompt", "e2e": {"default_pom": False}},
        "execution": {"entry": "cli", "self_healing": {"mode": "proposal-only"}},
        "coverage": {"enabled": False, "gate_mode": "warn", "threshold": {"line": 70, "branch": 60}},
        "performance": {"enabled": False},
    })


def stub_pytest_run(outcome_by_target: dict[str, str]):
    """subprocess.run stub keyed by which target dir appears in argv."""
    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        target = "api" if "tests/api" in args else "e2e" if "tests/e2e" in args else "fuzz"
        outcome = outcome_by_target.get(target, "passed")
        tests = [{
            "nodeid": f"tests/{target}/t.py::test_tc_{target}_001__x",
            "outcome": outcome,
            "call": {"outcome": outcome, "duration": 0.0,
                     "longrepr": "" if outcome == "passed" else "AssertionError: boom"},
        }]
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": tests}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return fake_run


@pytest.fixture
def change_dir(tmp_path: Path) -> Path:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    # workflow-state selects only api + e2e
    (change / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: true\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    return change


def test_run_change_all_pass_final_status_pass(tmp_path: Path, change_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000000")
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed", "e2e": "passed"}))
    manifest = run_change(tmp_path, change_dir, make_config())
    assert manifest.batch_id == "20260715-000000"
    assert manifest.final_status == "PASS"
    assert manifest.selected_targets.api is True
    assert (change_dir / "execution" / "runs" / "20260715-000000" / "api-result.json").is_file()
    assert (change_dir / "execution" / "execution-manifest.yaml").is_file()


def test_run_change_api_fail_final_status_fail(tmp_path: Path, change_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000001")
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "failed", "e2e": "passed"}))
    manifest = run_change(tmp_path, change_dir, make_config())
    assert manifest.final_status == "FAIL"


def test_run_change_missing_test_dirs_all_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000002")

    def boom(*a, **k):
        raise AssertionError("no subprocess when all layers skip")
    monkeypatch.setattr(runners_mod.subprocess, "run", boom)

    change = tmp_path / "qa" / "changes" / "CH-2"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    manifest = run_change(tmp_path, change, make_config())
    assert manifest.final_status == "SKIPPED"


def test_run_change_batch_collision_fails_before_any_runner(
    tmp_path: Path, change_dir: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000003")
    existing = change_dir / "execution/runs/20260715-000003"
    existing.mkdir(parents=True)

    def must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("runner called before batch reservation")

    monkeypatch.setattr(runners_mod.subprocess, "run", must_not_run)
    with pytest.raises(EvidenceError, match="already exists"):
        run_change(tmp_path, change_dir, make_config())
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/execution/test_evidence.py tests/unit/execution/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: 实现 evidence.py 与 runner.py**

```python
# assurance_agent/workflow/execution/evidence.py
"""Publish/read one execution batch.

publish writes the append-only runs/<batch-id>/ archive plus the top-level
latest pointers; the manifest (written last) is the primary evidence marker.
load reads the top-level manifest or an explicitly selected safe batch manifest,
loads each selected target result, and flags selected-but-missing results as
integrity issues.
"""
import re
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import BaseModel

from assurance_agent.artifacts.models import ExecutionManifest, QualityGateResult, SelectedTargets
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.execution.results import (
    CoverageResult,
    PerformanceResult,
    TargetResult,
)


class EvidenceError(AaError):
    pass


_BATCH_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}(?:-[0-9a-f]{8})?")


class BatchReservation(BaseModel):
    batch_id: str
    path: Path
    token: str


def _assert_batch_id_safe(batch_id: str) -> None:
    if not _BATCH_ID_RE.fullmatch(batch_id):
        raise EvidenceError(f"unsafe execution batch id: {batch_id!r}")


def reserve_execution_batch(execution_dir: Path, batch_id: str) -> BatchReservation:
    """Atomically claim a never-before-used batch directory before raw writes."""
    _assert_batch_id_safe(batch_id)
    runs_dir = execution_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = runs_dir / batch_id
    try:
        batch_dir.mkdir(exist_ok=False)
    except FileExistsError as err:
        raise EvidenceError(f"execution batch already exists: {batch_id}") from err
    token = uuid4().hex
    (batch_dir / ".reservation").write_text(token, encoding="ascii")
    return BatchReservation(batch_id=batch_id, path=batch_dir, token=token)


class IntegrityIssue(BaseModel):
    target: str
    path: str
    reason: str


class ExecutionEvidence(BaseModel):
    manifest: ExecutionManifest
    batch_id: str
    selected_targets: SelectedTargets
    api: TargetResult | None
    e2e: TargetResult | None
    fuzz: TargetResult | None
    coverage: CoverageResult | None
    performance: PerformanceResult | None
    quality_gate: QualityGateResult | None
    result_paths: dict[str, str]
    integrity_issues: list[IntegrityIssue]


def _write_json(path: Path, model: BaseModel) -> None:
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def publish_execution_evidence(
    *,
    execution_dir: Path,
    change_id: str,
    batch_id: str,
    selected_targets: SelectedTargets,
    api: TargetResult | None,
    e2e: TargetResult | None,
    fuzz: TargetResult | None,
    coverage: CoverageResult | None,
    performance: PerformanceResult | None,
    quality_gate: QualityGateResult,
    summary: str,
    reservation: BatchReservation | None = None,
) -> ExecutionManifest:
    _assert_batch_id_safe(batch_id)
    owned = reservation or reserve_execution_batch(execution_dir, batch_id)
    batch_dir = owned.path
    expected_dir = execution_dir / "runs" / batch_id
    marker = batch_dir / ".reservation"
    if owned.batch_id != batch_id or batch_dir != expected_dir or not marker.is_file():
        raise EvidenceError(f"invalid execution batch reservation: {batch_id}")
    if marker.read_text(encoding="ascii") != owned.token:
        raise EvidenceError(f"execution batch reservation token mismatch: {batch_id}")
    if (batch_dir / "execution-manifest.yaml").exists():
        raise EvidenceError(f"execution batch already published: {batch_id}")

    result_files: dict[str, str] = {}
    named: list[tuple[str, str, BaseModel | None]] = [
        ("api", "api-result.json", api),
        ("e2e", "e2e-result.json", e2e),
        ("fuzz", "fuzz-result.json", fuzz),
        ("performance", "performance-result.json", performance),
        ("coverage", "coverage-result.json", coverage),
    ]
    for key, name, value in named:
        if value is None:
            continue
        _write_json(batch_dir / name, value)
        result_files[key] = f"runs/{batch_id}/{name}"

    (batch_dir / "summary.md").write_text(summary, encoding="utf-8")
    result_files["summary"] = f"runs/{batch_id}/summary.md"
    _write_json(batch_dir / "quality-gate-result.json", quality_gate)

    manifest = ExecutionManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        selected_targets=selected_targets,
        result_files=result_files,
        final_status=quality_gate.final_status,
    )
    (batch_dir / "execution-manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    marker.unlink()

    # Latest pointers (overwritten each run).
    for _key, name, value in named:
        pointer = execution_dir / name
        if pointer.exists():
            pointer.unlink()
        if value is not None:
            _write_json(pointer, value)
    (execution_dir / "summary.md").write_text(summary, encoding="utf-8")
    _write_json(execution_dir / "quality-gate-result.json", quality_gate)
    (execution_dir / "execution-manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    return manifest


def _load_target(path: Path) -> TargetResult | None:
    if not path.is_file():
        return None
    try:
        return TargetResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_execution_evidence(
    execution_dir: Path, *, batch_id: str | None = None,
) -> ExecutionEvidence:
    if batch_id is not None:
        _assert_batch_id_safe(batch_id)
    manifest_path = (
        execution_dir / "execution-manifest.yaml"
        if batch_id is None
        else execution_dir / "runs" / batch_id / "execution-manifest.yaml"
    )
    if not manifest_path.is_file():
        raise EvidenceError(
            f"execution-manifest.yaml not found under {execution_dir}. Run `aa run` first."
        )
    try:
        manifest = ExecutionManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, yaml.YAMLError) as err:
        raise EvidenceError(f"execution-manifest.yaml invalid: {err}") from err

    if not _BATCH_ID_RE.fullmatch(manifest.batch_id):
        raise EvidenceError(f"unsafe manifest batch id: {manifest.batch_id!r}")
    if batch_id is not None and manifest.batch_id != batch_id:
        raise EvidenceError(
            f"execution manifest batch id mismatch: requested {batch_id}, got {manifest.batch_id}"
        )

    execution_root = execution_dir.resolve()

    def safe_path(rel: str) -> Path:
        candidate = Path(rel)
        if candidate.is_absolute():
            raise EvidenceError(f"manifest result path must be relative: {rel!r}")
        resolved = (execution_root / candidate).resolve()
        if not resolved.is_relative_to(execution_root):
            raise EvidenceError(f"manifest result path escapes execution directory: {rel!r}")
        return resolved

    result_paths = {k: str(safe_path(v)) for k, v in manifest.result_files.items()}

    def abs_path(key: str, fallback: str) -> Path:
        rel = manifest.result_files.get(key, fallback)
        return safe_path(rel)

    api = _load_target(abs_path("api", "")) if manifest.selected_targets.api else None
    e2e = _load_target(abs_path("e2e", "")) if manifest.selected_targets.e2e else None
    fuzz = _load_target(abs_path("fuzz", "")) if manifest.selected_targets.fuzz else None

    coverage: CoverageResult | None = None
    if manifest.selected_targets.api:
        cov_path = abs_path("coverage", "")
        if cov_path.is_file():
            try:
                coverage = CoverageResult.model_validate_json(cov_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                coverage = None

    performance: PerformanceResult | None = None
    if manifest.selected_targets.performance:
        perf_path = abs_path("performance", "")
        if perf_path.is_file():
            try:
                performance = PerformanceResult.model_validate_json(
                    perf_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                performance = None

    integrity: list[IntegrityIssue] = []
    for target, selected, result in (
        ("api", manifest.selected_targets.api, api),
        ("e2e", manifest.selected_targets.e2e, e2e),
        ("fuzz", manifest.selected_targets.fuzz, fuzz),
        ("coverage", manifest.selected_targets.api, coverage),
        ("performance", manifest.selected_targets.performance, performance),
    ):
        if selected and result is None:
            integrity.append(IntegrityIssue(
                target=target,
                path=result_paths.get(target, ""),
                reason="selected target declared in manifest but result file is absent or unreadable",
            ))
        elif result is not None and (
            result.batch_id != manifest.batch_id or result.change_id != manifest.change_id
        ):
            integrity.append(IntegrityIssue(
                target=target,
                path=result_paths.get(target, ""),
                reason="result identity mismatch with execution manifest",
            ))

    gate_path = execution_dir / "runs" / manifest.batch_id / "quality-gate-result.json"
    quality_gate: QualityGateResult | None = None
    if gate_path.is_file():
        try:
            quality_gate = QualityGateResult.model_validate_json(gate_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            quality_gate = None
    if quality_gate is not None and (
        quality_gate.batch_id != manifest.batch_id or quality_gate.change_id != manifest.change_id
    ):
        integrity.append(IntegrityIssue(
            target="quality_gate",
            path=str(gate_path),
            reason="quality gate identity mismatch with execution manifest",
        ))

    return ExecutionEvidence(
        manifest=manifest,
        batch_id=manifest.batch_id,
        selected_targets=manifest.selected_targets,
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        coverage=coverage,
        performance=performance,
        quality_gate=quality_gate,
        result_paths=result_paths,
        integrity_issues=integrity,
    )
```

```python
# assurance_agent/workflow/execution/runner.py
"""Execution orchestrator: run selected layers, build the gate, publish evidence.

run_change is the public entry consumed by `aa run`. It never fabricates: an
unselected or missing layer becomes a SKIPPED result and the quality gate
degrades accordingly.
"""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from assurance_agent.artifacts.models import ExecutionManifest
from assurance_agent.config import AaConfig
from assurance_agent.workflow.execution.evidence import (
    BatchReservation,
    publish_execution_evidence,
    reserve_execution_batch,
)
from assurance_agent.workflow.execution.exec_config import load_coverage_config, load_perf_config
from assurance_agent.workflow.execution.results import CoverageResult
from assurance_agent.workflow.execution.runners import (
    parse_coverage_result,
    run_performance_target,
    run_pytest_target,
)
from assurance_agent.workflow.execution.selection import resolve_selected_targets
from assurance_agent.workflow.report.quality_gate import build_quality_gate


def generate_batch_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _strip(rel: str) -> str:
    return rel[2:] if rel.startswith("./") else rel


def _test_dir(config: AaConfig, attr: str, default: str) -> str:
    tests = getattr(config, "tests", None)
    value = getattr(tests, attr, None) if tests is not None else None
    return _strip(value) if isinstance(value, str) else default


def run_change(
    project_root: Path,
    change_dir: Path,
    config: AaConfig,
    *,
    reservation: BatchReservation | None = None,
) -> ExecutionManifest:
    change_id = change_dir.name
    batch_id = reservation.batch_id if reservation is not None else generate_batch_id()
    execution_dir = change_dir / "execution"

    selected = resolve_selected_targets(change_dir)
    cov_config = load_coverage_config(config)
    perf_config = load_perf_config(config)
    # Must happen before any runner writes raw output. mkdir(exist_ok=False)
    # makes append-only ownership atomic and turns same-id races into errors.
    reservation = reservation or reserve_execution_batch(execution_dir, batch_id)
    batch_dir = reservation.path

    cov_package = cov_config.target_package if cov_config.enabled else None
    api = run_pytest_target(
        project_root=project_root, batch_dir=batch_dir, change_id=change_id, batch_id=batch_id,
        target="api", test_dir=_test_dir(config, "api", "tests/api"), cov_package=cov_package,
    ) if selected.api else None
    e2e = run_pytest_target(
        project_root=project_root, batch_dir=batch_dir, change_id=change_id, batch_id=batch_id,
        target="e2e", test_dir=_test_dir(config, "e2e", "tests/e2e"),
    ) if selected.e2e else None
    fuzz = run_pytest_target(
        project_root=project_root, batch_dir=batch_dir, change_id=change_id, batch_id=batch_id,
        target="fuzz", test_dir=_test_dir(config, "fuzz", "tests/fuzz"),
    ) if selected.fuzz else None

    if selected.api:
        coverage = parse_coverage_result(
            change_id=change_id, batch_id=batch_id, batch_dir=batch_dir, threshold=cov_config.threshold,
        )
    else:
        # Coverage derives from the API pytest run; write an explicit SKIPPED result
        # so downstream inspect/report never read a stale coverage file.
        coverage = CoverageResult(
            change_id=change_id, batch_id=batch_id, available=False,
            line_coverage=0.0, branch_coverage=0.0, threshold=cov_config.threshold,
            status="SKIPPED", skip_reason="api_unselected",
        )

    performance = run_performance_target(
        project_root=project_root, change_dir=change_dir, batch_dir=batch_dir,
        change_id=change_id, batch_id=batch_id, perf_config=perf_config,
    ) if selected.performance else None

    quality_gate = build_quality_gate(
        change_id=change_id, batch_id=batch_id, api=api, e2e=e2e, coverage=coverage,
        coverage_gate_mode=cov_config.gate_mode, fuzz=fuzz, performance=performance,
    )
    summary = _build_summary(change_id, batch_id, api, e2e, fuzz, coverage, performance, quality_gate)

    return publish_execution_evidence(
        execution_dir=execution_dir, change_id=change_id, batch_id=batch_id,
        selected_targets=selected, api=api, e2e=e2e, fuzz=fuzz, coverage=coverage,
        performance=performance, quality_gate=quality_gate, summary=summary,
        reservation=reservation,
    )


def _build_summary(change_id, batch_id, api, e2e, fuzz, coverage, performance, gate) -> str:  # noqa: ANN001
    def line(label: str, r) -> str:  # noqa: ANN001
        if r is None:
            return f"- {label}: unselected"
        return f"- {label}: {r.status} total={r.total} passed={r.passed} failed={r.failed}"
    parts = [
        f"# Execution Summary — {change_id}",
        "",
        f"- Batch: {batch_id}",
        f"- Final Status: {gate.final_status}",
        "",
        line("API", api),
        line("E2E", e2e),
        line("Fuzz", fuzz),
        f"- Coverage: {coverage.status} line={coverage.line_coverage}%" if coverage else "- Coverage: unselected",
        f"- Performance: {performance.status}" if performance else "- Performance: unselected",
        "",
    ]
    return "\n".join(parts)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/execution/test_evidence.py tests/unit/execution/test_runner.py -v`
Expected: 10 passed（evidence）+ 4 passed（runner）= 14 passed

- [ ] **Step 6: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 7: Commit**

```bash
git add assurance_agent/workflow/execution/evidence.py assurance_agent/workflow/execution/runner.py \
        tests/unit/execution/test_evidence.py tests/unit/execution/test_runner.py
git commit -m "feat: add execution evidence publishing and run_change orchestrator"
```

---

### Task 6: `aa run` 命令（commands/run_cmd.py + cli 注册）

**Files:**
- Create: `assurance_agent/commands/run_cmd.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/unit/commands/__init__.py`（空）
- Test: `tests/unit/commands/test_run_cmd.py`

**Interfaces:**
- Consumes: Task 5 `run_change`；M1 `load_config`/`AaConfig`/`AaError`；M3 `append_event_best_effort`。`aa run` 的 start/end/manifest-written 都是遥测，不属于 M3 冻结的 audit union；真正的 phase outcome 由 M6→M4 progression 边界提交。
- Produces: click 命令 `run_command`（`aa run --change <id> [--rerun-reason <text>]`）。退出码对齐 TS `run.ts`：`FAIL` → 1；`PASS`/`PASS_WITH_WARNINGS` → 0；`SKIPPED` 且无任何 target 被选 → 0（no-op），有 target 被选但全 SKIPPED → 1。

命令行为（transcribe 自 TS `run.ts`，去掉本里程碑不做的 healing/override 守卫）：
1. `project_root = Path.cwd()`，`change_dir = project_root / <qa.changes 去掉 "./"> / change_id`；change 目录不存在 → 报错退出 1。
2. best-effort 事件 `execution_start`（含 `targets` 与可选 `rerun_reason`）→ 调 `run_change` → best-effort `execution_manifest_written`（含 `batch_id`/`final_status`）→ best-effort `execution_end`（含 `per_target` 摘要）。执行清单本身是 canonical evidence；其写入失败由 `run_change` 报错，遥测失败不得把一次已落盘执行伪装成失败。
3. 打印四层（API/E2E/FUZZ/PERF）+ COV 状态与产物路径，按 `final_status` 打印下一步提示并 `SystemExit`。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/commands/test_run_cmd.py
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.commands import run_cmd as run_cmd_mod
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod

_CONFIG = """version: 1
sources: {frontend: ./frontend, backend: ./backend}
qa: {cases: ./qa/cases, changes: ./qa/changes}
tests: {root: ./tests, api: ./tests/api, e2e: ./tests/e2e}
frameworks:
  api: {enabled: true, name: pytest}
  e2e: {enabled: true, name: playwright}
generation: {prd_input_mode: prompt, e2e: {default_pom: false}}
execution: {entry: cli, self_healing: {mode: proposal-only}}
coverage: {enabled: false, gate_mode: warn, threshold: {line: 70, branch: 60}}
performance: {enabled: false}
"""


def _stub_pytest(outcome: str):
    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        target = "api" if "tests/api" in args else "e2e"
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": [{
            "nodeid": f"tests/{target}/t.py::test_tc_{target}_001__x",
            "outcome": outcome,
            "call": {"outcome": outcome, "duration": 0.0,
                     "longrepr": "" if outcome == "passed" else "AssertionError: boom"},
        }]}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return fake_run


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "selected_targets: {api: true, e2e: true, fuzz: false, performance: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000000")
    # Do not depend on M3 event file format in this unit test.
    monkeypatch.setattr(run_cmd_mod, "append_event_best_effort", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    return tmp_path, change


def test_run_pass_exit_zero(project, monkeypatch) -> None:
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))
    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])
    assert result.exit_code == 0
    assert "PASS" in result.output
    _, change = project
    assert (change / "execution" / "execution-manifest.yaml").is_file()


def test_run_fail_exit_one(project, monkeypatch) -> None:
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("failed"))
    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_run_unknown_change_exit_one(project) -> None:
    result = CliRunner().invoke(main, ["run", "--change", "NOPE"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/commands/test_run_cmd.py -v`
Expected: FAIL with `ModuleNotFoundError` 或 `no such command 'run'`

- [ ] **Step 3: 实现 run_cmd.py 并在 cli.py 注册**

```python
# assurance_agent/commands/run_cmd.py
"""`aa run` command: execute selected test layers and publish execution evidence."""
from pathlib import Path

import click

from assurance_agent.config import AaConfig, load_config
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.execution.runner import run_change

_STATUS_COLOR = {"passed": "green", "failed": "red", "skipped": "yellow"}


@click.command("run")
@click.option("--change", "change_id", required=True, help="Change ID (e.g. REQ-002-user-logout).")
@click.option("--rerun-reason", "rerun_reason", default=None,
              help="Required when re-running after execution is already done.")
def run_command(change_id: str, rerun_reason: str | None) -> None:
    """Execute API/E2E/Fuzz/Performance tests for a change and write normalized results."""
    project_root = Path.cwd()
    try:
        config = load_config(project_root)
        change_dir = _change_dir(project_root, config, change_id)
    except AaError as err:
        click.secho(f"Run failed: {err}", fg="red")
        raise SystemExit(1) from err

    if not change_dir.is_dir():
        click.secho(f"Run failed: change directory not found: {change_dir}", fg="red")
        raise SystemExit(1)

    click.secho(f"\naa run — change: {change_id}\n", bold=True)

    try:
        manifest = _execute(project_root, change_dir, config, change_id, rerun_reason)
    except AaError as err:
        click.secho(f"Run failed: {err}", fg="red")
        raise SystemExit(1) from err

    _print_manifest(manifest, change_dir)
    raise SystemExit(_exit_code(manifest))


def _execute(project_root: Path, change_dir: Path, config: AaConfig, change_id: str,
             rerun_reason: str | None):
    append_event_best_effort(change_dir, {
        "source": "run", "type": "execution_start",
        **({"rerun_reason": rerun_reason} if rerun_reason else {}),
    })
    manifest = run_change(project_root, change_dir, config)
    append_event_best_effort(change_dir, {
        "source": "run", "type": "execution_manifest_written",
        "batch_id": manifest.batch_id, "final_status": manifest.final_status,
    })
    append_event_best_effort(change_dir, {
        "source": "run", "type": "execution_end", "batch_id": manifest.batch_id,
    })
    return manifest


def _change_dir(project_root: Path, config: AaConfig, change_id: str) -> Path:
    assert_change_id_safe(change_id)
    rel = config.qa.changes
    rel = rel[2:] if rel.startswith("./") else rel
    return project_root / rel / change_id


def _print_manifest(manifest, change_dir: Path) -> None:  # noqa: ANN001
    execution_dir = change_dir / "execution"
    color = {"PASS": "green", "PASS_WITH_WARNINGS": "yellow", "FAIL": "red", "SKIPPED": "yellow"}
    click.echo("Execution Results")
    click.echo("  Final Status : " + click.style(manifest.final_status, fg=color[manifest.final_status], bold=True))
    click.echo(f"  Batch ID     : {manifest.batch_id}")
    for key in ("api", "e2e", "fuzz", "performance", "coverage"):
        sel = getattr(manifest.selected_targets, key, None)
        rel = manifest.result_files.get(key)
        if rel:
            click.echo(f"  {key:<12}: {execution_dir / rel}")
        elif sel is False:
            click.echo(f"  {key:<12}: unselected")
    click.echo(f"  manifest     : {execution_dir / 'execution-manifest.yaml'}")

    if manifest.final_status == "FAIL":
        click.secho("→ Quality gate failed. Run: aa report inspect --change <id>", fg="red")
    elif manifest.final_status == "PASS":
        click.secho("→ Quality gate passed. Proceed to archive-for-qa.", fg="green")
    elif manifest.final_status == "PASS_WITH_WARNINGS":
        click.secho("→ Passed with warnings. Run: aa report inspect --change <id>", fg="yellow")
    else:
        click.secho("→ No results produced. See summary.md / run report inspect.", fg="cyan")


def _exit_code(manifest) -> int:  # noqa: ANN001
    if manifest.final_status in ("PASS", "PASS_WITH_WARNINGS"):
        return 0
    if manifest.final_status == "FAIL":
        return 1
    any_selected = any(
        getattr(manifest.selected_targets, k) for k in ("api", "e2e", "fuzz", "performance")
    )
    return 1 if any_selected else 0
```

`cli.py` 追加注册：

```python
from assurance_agent.commands.run_cmd import run_command
# ...
main.add_command(run_command)
```

同时创建空文件 `tests/unit/commands/__init__.py`（若 M1 已存在则跳过）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/commands/test_run_cmd.py -v`
Expected: 3 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/commands/run_cmd.py assurance_agent/cli.py tests/unit/commands
git commit -m "feat: add aa run command wiring run_change to the CLI"
```

---

### Task 7: 失败分类器 + 规则数据表（report/failure_classifier.py + _resources/rules/failure-classification.yaml）

**Files:**
- Create: `assurance_agent/_resources/rules/failure-classification.yaml`
- Create: `assurance_agent/workflow/report/failure_classifier.py`
- Test: `tests/unit/report/test_failure_classifier.py`

**Interfaces:**
- Consumes: M1 `resources.read_text`；M2 `FailureCategory`、`FailureSeverity`。
- Produces: `Classification`（`category`/`fix_proposal_eligible`/`severity`/`needs_review`）；`classify_failure(*, message: str, log_excerpt: str, target: Literal["api","e2e","fuzz"], has_trace: bool = False, has_screenshot: bool = False) -> Classification`。Task 8 inspector 消费。

**分类逻辑（从 TS `failure_classifier.ts` 提取，规则表外置为 YAML 数据）：** 分类器把 `message + " " + log_excerpt` 转小写后，按 `target` 选择流水线（`fuzz` 走 fuzz 流水线，`api`/`e2e` 走 default 流水线），按顺序对每条规则做正则匹配，命中即定类；`target` 约束的规则只对指定层生效；全不命中落各自 fallback（default→`unknown`，fuzz→`fuzz_stateful_failure`）。`fix_proposal_eligible = (fix_proposal[category] is True)`；`needs_review = (category == "unknown") or (fix_proposal[category] == "review")`；`severity = severity[category]`。规则数据文件随 wheel 分发（`_resources/**`），仅经 `resources.read_text` 读取。

- [ ] **Step 1: 写规则数据表**

```yaml
# assurance_agent/_resources/rules/failure-classification.yaml
# Failure classification rule table (extracted from TS failure_classifier.ts).
# Patterns are matched case-insensitively against lower(message + " " + log_excerpt).
schema_version: "1.0"

patterns:
  environment: "connection refused|cannot connect|econnrefused|service unavailable|host not found|timeout connecting|failed to start server|502 bad gateway|503 service|no such file or directory.*server"
  known_product_marker: "expected-product-fail|known product issue|known-product"
  anomaly_token: "anomaly-\\d+"
  fact_baseline: "fact-baseline"
  locator: "locator|selector|element not found|no element|waiting for selector|unable to find element|strict mode violation|ambiguous|getbytext|getbyrole|getbylabel|getbyplaceholder|getbytestid"
  wait_strategy: "timed? ?out|timeout exceeded|exceeded.*ms|waitfor|networkidle|domcontentloaded|load.*event"
  fuzz_configuration: "schemathesis|openapi|schema.*not found|failed to load schema|cannot fetch schema|invalid schema|no api definition|hypothesis.*could not|unsatisfied|failed health check|filtered out|base url"
  fuzz_stateful: "stateful|state machine|apistatemachine|sequence|transition|link|rule .* failed"
  test_data: "fixture|test data|seed|database.*empty|no.*record|not found.*user|not found.*product|not found.*order|factory|invalid data|missing.*field|required.*field"
  assertion: "assertionerror|assert.*expected|expected.*received|to equal|to be|tobecalled|tohavetext|tohavevalue|statuscode.*expected|response.*expected"
  business_logic: "400 bad request|401 unauthorized|403 forbidden|404 not found|422 unprocessable|500 internal server|business rule|validation error|permission denied|insufficient"
  server_error_5xx: "server error|5\\d\\d|internal server"
  test_code: "syntaxerror|typeerror|nameerror|importerror|attributeerror|referenceerror|cannot read properties|is not a function|is not defined|indentationerror"
  case_semantic: "step not covered|missing step|precondition not met|out of scope|no acceptance criteria"

pipelines:
  # api / e2e failures
  default:
    fallback: unknown
    rules:
      - category: known_product_issue
        match:
          any_of:
            - known_product_marker
            - all_of: [anomaly_token, fact_baseline]
      - {category: environment_failure, match: environment}
      - {category: locator_failure, target: [e2e], match: locator}
      - {category: wait_strategy_failure, target: [e2e], match: wait_strategy}
      - {category: test_data_failure, match: test_data}
      - {category: assertion_failure, match: assertion}
      - {category: business_logic_failure, match: business_logic}
      - {category: test_code_error, match: test_code}
      - {category: case_semantic_failure, match: case_semantic}
  # fuzz failures (schemathesis via pytest)
  fuzz:
    fallback: fuzz_stateful_failure
    rules:
      - {category: fuzz_configuration_error, match: fuzz_configuration}
      - {category: environment_failure, match: environment}
      - {category: fuzz_stateful_failure, match: fuzz_stateful}
      - category: business_logic_failure
        match:
          any_of: [business_logic, server_error_5xx]
      - {category: test_code_error, match: test_code}

# fix_proposal eligibility: true = auto-fixable, false = hard/no-fix, review = needs human review
fix_proposal:
  locator_failure: true
  wait_strategy_failure: true
  test_data_failure: true
  test_code_error: true
  environment_failure: false
  assertion_failure: false
  assertion_expectation_error: review
  business_logic_failure: false
  case_semantic_failure: false
  known_product_issue: false
  coverage_gap: false
  fuzz_configuration_error: false
  fuzz_stateful_failure: review
  perf_script_error: false
  perf_threshold_exceeded: review
  perf_environment: false
  manifest_asset_missing: false
  unknown: false

severity:
  environment_failure: critical
  business_logic_failure: critical
  manifest_asset_missing: critical
  fuzz_stateful_failure: high
  perf_threshold_exceeded: high
  assertion_failure: high
  assertion_expectation_error: high
  case_semantic_failure: high
  locator_failure: medium
  wait_strategy_failure: medium
  test_code_error: medium
  test_data_failure: medium
  fuzz_configuration_error: medium
  perf_script_error: medium
  perf_environment: medium
  known_product_issue: low
  coverage_gap: low
  unknown: low
```

- [ ] **Step 2: 写失败测试**

```python
# tests/unit/report/test_failure_classifier.py
import pytest

from assurance_agent.workflow.report.failure_classifier import classify_failure


@pytest.mark.parametrize(
    "message,target,expected_category,expected_eligible,expected_review",
    [
        ("Connection refused: cannot connect to server", "api", "environment_failure", False, False),
        ("expected-product-fail: known product issue documented", "api", "known_product_issue", False, False),
        ("Locator resolve failed: waiting for selector '#submit'", "e2e", "locator_failure", True, False),
        ("Timeout exceeded 30000ms waiting for networkidle", "e2e", "wait_strategy_failure", True, False),
        ("Fixture 'seed_user' not found: database empty", "api", "test_data_failure", True, False),
        ("AssertionError: expected 200 received 404", "api", "assertion_failure", False, False),
        ("403 forbidden: permission denied for role", "api", "business_logic_failure", False, False),
        ("TypeError: object is not a function", "api", "test_code_error", True, False),
        ("Step not covered: precondition not met", "api", "case_semantic_failure", False, False),
        ("some totally opaque failure with no signal", "api", "unknown", False, True),
        ("schemathesis: failed to load schema from openapi", "fuzz", "fuzz_configuration_error", False, False),
        ("stateful state machine transition failed", "fuzz", "fuzz_stateful_failure", False, True),
        ("Server error: 503 during generated sequence", "fuzz", "environment_failure", False, False),
        ("500 internal server error on generated input", "fuzz", "business_logic_failure", False, False),
    ],
)
def test_classification_golden(message, target, expected_category, expected_eligible, expected_review) -> None:
    result = classify_failure(message=message, log_excerpt="", target=target)
    assert result.category == expected_category
    assert result.fix_proposal_eligible is expected_eligible
    assert result.needs_review is expected_review


def test_e2e_locator_pattern_does_not_fire_for_api_target() -> None:
    # "selector" text on an api failure must NOT become locator_failure (e2e-only rule).
    result = classify_failure(message="invalid selector syntax in query", log_excerpt="", target="api")
    assert result.category != "locator_failure"


def test_severity_matches_category() -> None:
    env = classify_failure(message="connection refused", log_excerpt="", target="api")
    assert env.severity == "critical"
    loc = classify_failure(message="element not found", log_excerpt="", target="e2e")
    assert loc.severity == "medium"


def test_anomaly_needs_fact_baseline_context() -> None:
    bare = classify_failure(message="anomaly-7 detected in response", log_excerpt="", target="api")
    assert bare.category != "known_product_issue"
    with_ctx = classify_failure(
        message="anomaly-7 detected", log_excerpt="fact-baseline: documented", target="api"
    )
    assert with_ctx.category == "known_product_issue"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/report/test_failure_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: 实现 failure_classifier.py**

```python
# assurance_agent/workflow/report/failure_classifier.py
"""Data-driven failure classifier (rules in _resources/rules/failure-classification.yaml).

Pipeline order and regexes are transcribed from TS failure_classifier.ts; the
rule table lives in packaged YAML so classifications can evolve without code
changes. The matcher is deterministic: first matching rule wins.
"""
import re
from functools import lru_cache
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from assurance_agent import resources
from assurance_agent.artifacts.models import FailureCategory, FailureSeverity

Target = Literal["api", "e2e", "fuzz"]


class Classification(BaseModel):
    category: FailureCategory
    fix_proposal_eligible: bool
    severity: FailureSeverity
    needs_review: bool


class _Rules:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.patterns: dict[str, re.Pattern[str]] = {
            name: re.compile(expr, re.IGNORECASE) for name, expr in doc["patterns"].items()
        }
        self.pipelines: dict[str, dict[str, Any]] = doc["pipelines"]
        self.fix_proposal: dict[str, Any] = doc["fix_proposal"]
        self.severity: dict[str, str] = doc["severity"]


@lru_cache(maxsize=1)
def _rules() -> _Rules:
    doc = yaml.safe_load(resources.read_text("rules", "failure-classification.yaml"))
    return _Rules(doc)


def _matches(match: Any, text: str, rules: _Rules) -> bool:
    if isinstance(match, str):
        return rules.patterns[match].search(text) is not None
    if isinstance(match, dict):
        if "any_of" in match:
            return any(_matches(item, text, rules) for item in match["any_of"])
        if "all_of" in match:
            return all(_matches(item, text, rules) for item in match["all_of"])
    return False


def classify_failure(
    *,
    message: str,
    log_excerpt: str,
    target: Target,
    has_trace: bool = False,
    has_screenshot: bool = False,
) -> Classification:
    rules = _rules()
    text = f"{message} {log_excerpt}".lower()
    pipeline = rules.pipelines["fuzz" if target == "fuzz" else "default"]

    category: str = pipeline["fallback"]
    for rule in pipeline["rules"]:
        allowed_targets = rule.get("target")
        if allowed_targets and target not in allowed_targets:
            continue
        if _matches(rule["match"], text, rules):
            category = rule["category"]
            break

    allowed = rules.fix_proposal.get(category, False)
    return Classification(
        category=category,  # type: ignore[arg-type]
        fix_proposal_eligible=allowed is True,
        severity=rules.severity.get(category, "low"),  # type: ignore[arg-type]
        needs_review=category == "unknown" or allowed == "review",
    )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/report/test_failure_classifier.py -v`
Expected: 18 passed（14 参数化 + 4 单例）

- [ ] **Step 6: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 7: Commit**

```bash
git add assurance_agent/_resources/rules/failure-classification.yaml \
        assurance_agent/workflow/report/failure_classifier.py \
        tests/unit/report/test_failure_classifier.py
git commit -m "feat: add data-driven failure classifier with packaged rule table"
```

---

### Task 8: Quality Score + inspector + report_builder（report/quality_score.py、inspector.py、report_builder.py）

**Files:**
- Create: `assurance_agent/workflow/report/quality_score.py`
- Create: `assurance_agent/workflow/report/inspector.py`
- Create: `assurance_agent/workflow/report/report_builder.py`
- Test: `tests/unit/report/test_quality_score.py`
- Test: `tests/unit/report/test_inspector.py`
- Test: `tests/unit/report/test_report_builder.py`

**Interfaces:**
- Consumes: Task 3 `build_quality_gate`；Task 5 `load_execution_evidence`；Task 7 `classify_failure`；M2 `FailureAnalysis`/`FailureEntry`/`FailureEvidence`/`CoverageGapEntry`/`QualityGateResult`/`QualityReport`/`QualityScoreBreakdown`/`ReportScope`/`ReportDefect`/`ReportDefects`。
- Produces:
  - `quality_score.ScoreDimension`、`quality_score.compute_quality_score(dims) -> tuple[int, QualityScoreBreakdown]`（确定性权重公式）。
  - `inspector.inspect_change(project_root: Path, change_id: str) -> InspectResult`（写 `inspect/failure-analysis.json` + `failure-summary.md` + `quality-gate-result.json`）。
  - `report_builder.generate_report(project_root: Path, change_id: str) -> GenerateReportResult`（写 `report/quality-report.json` + `quality-report.md` + `executive-summary.md`）。Task 9 `aa report inspect|generate` 消费。

**Quality Score 权重公式（从 TS `quality_score.ts` + `report_generator.ts` 提取，`docs/schemas.md` 一致）：** 维度键 `functional`/`coverage`/`fuzz`/`performance`，各带 `active`/`ratio∈[0,1]`/`weight`。仅 `active` 维度计入，`activeWeight = Σ active weight`；若 `activeWeight<=0` → 分数 0、全 `N/A`。否则每个 active 维度 `points = weight/activeWeight*100*clamp01(ratio)`，`breakdown[k]=round1(points)`，`score = round(Σ points)`。权重档位：无 fuzz/performance 在场时用 **M1 权重**（functional 70、coverage 30）；一旦 fuzz 或 performance active，用 **M3 权重**（functional 50、coverage 20、fuzz 15、performance 15）。functional 比率只算 api+e2e（fuzz 单列 fuzz 维度）。

- [ ] **Step 1: 写失败测试（quality_score，手算期望值）**

```python
# tests/unit/report/test_quality_score.py
from assurance_agent.workflow.report.quality_score import ScoreDimension, compute_quality_score


def dims(**kw: ScoreDimension) -> dict:
    base = {
        "functional": ScoreDimension(active=False, ratio=0.0, weight=0),
        "coverage": ScoreDimension(active=False, ratio=0.0, weight=0),
        "fuzz": ScoreDimension(active=False, ratio=0.0, weight=0),
        "performance": ScoreDimension(active=False, ratio=0.0, weight=0),
    }
    base.update(kw)
    return base


def test_m1_functional_only_full_pass_is_100() -> None:
    score, bd = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=1.0, weight=70),
    ))
    assert score == 100
    assert bd.functional == 100.0
    assert bd.coverage == "N/A"


def test_m1_functional_and_coverage_hand_computed() -> None:
    # activeWeight=100; func=70*0.8=56; cov=30*1.0=30; total=86
    score, bd = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=0.8, weight=70),
        coverage=ScoreDimension(active=True, ratio=1.0, weight=30),
    ))
    assert score == 86
    assert bd.functional == 56.0
    assert bd.coverage == 30.0
    assert bd.fuzz == "N/A"


def test_m3_all_active_partial_functional() -> None:
    # activeWeight=100; func=50*0.5=25; cov=20; fuzz=15; perf=15; total=75
    score, bd = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=0.5, weight=50),
        coverage=ScoreDimension(active=True, ratio=1.0, weight=20),
        fuzz=ScoreDimension(active=True, ratio=1.0, weight=15),
        performance=ScoreDimension(active=True, ratio=1.0, weight=15),
    ))
    assert score == 75
    assert bd.functional == 25.0
    assert bd.performance == 15.0


def test_no_active_dimension_is_zero() -> None:
    score, bd = compute_quality_score(dims())
    assert score == 0
    assert bd.functional == "N/A"


def test_ratio_is_clamped() -> None:
    score, _ = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=5.0, weight=70),
    ))
    assert score == 100
```

- [ ] **Step 2: 实现 quality_score.py（先让 score 测试转绿）**

```python
# assurance_agent/workflow/report/quality_score.py
"""Deterministic Quality Score (M1 + M3 weights). CLI is the only scorer.

Inactive dimensions are dropped and remaining weights renormalised so active
dimensions can still reach 100. Transcribed from TS quality_score.ts.
"""
from pydantic import BaseModel

from assurance_agent.artifacts.models import QualityScoreBreakdown

_KEYS = ("functional", "coverage", "fuzz", "performance")


class ScoreDimension(BaseModel):
    active: bool
    ratio: float
    weight: float


def _clamp01(n: float) -> float:
    if n != n:  # NaN
        return 0.0
    return max(0.0, min(1.0, n))


def _round1(n: float) -> float:
    return round(n * 10) / 10


def compute_quality_score(
    dims: dict[str, ScoreDimension],
) -> tuple[int, QualityScoreBreakdown]:
    active_weight = sum(dims[k].weight for k in _KEYS if dims[k].active)
    parts: dict[str, float | str] = {k: "N/A" for k in _KEYS}
    if active_weight <= 0:
        return 0, QualityScoreBreakdown.model_validate(parts)

    total = 0.0
    for key in _KEYS:
        dim = dims[key]
        if not dim.active:
            continue
        points = (dim.weight / active_weight) * 100 * _clamp01(dim.ratio)
        parts[key] = _round1(points)
        total += points
    return round(total), QualityScoreBreakdown.model_validate(parts)
```

- [ ] **Step 3: 跑 score 测试确认通过**

Run: `uv run pytest tests/unit/report/test_quality_score.py -v`
Expected: 5 passed

- [ ] **Step 4: 写失败测试（inspector + report_builder）**

```python
# tests/unit/report/test_inspector.py
from pathlib import Path

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, TargetResult
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.quality_gate import build_quality_gate


def _seed_change(tmp_path: Path, api: TargetResult, cov: CoverageResult) -> str:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    gate = build_quality_gate(
        change_id="CH-1", batch_id="20260715-000000", api=api, e2e=None,
        coverage=cov, coverage_gate_mode="warn",
    )
    publish_execution_evidence(
        execution_dir=change_dir / "execution", change_id="CH-1", batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api, e2e=None, fuzz=None, coverage=cov, performance=None,
        quality_gate=gate, summary="# summary\n",
    )
    return "CH-1"


def _api(failed_message: str | None) -> TargetResult:
    cases = [CaseResult(case_id="TC_API_001", status="passed", file="tests/api/t.py",
                        test_name="test_tc_api_001__ok", duration_ms=1, message="")]
    failed = 0
    if failed_message is not None:
        failed = 1
        cases.append(CaseResult(case_id="TC_API_002", status="failed", file="tests/api/t.py",
                                test_name="test_tc_api_002__x", duration_ms=1, message=failed_message))
    return TargetResult(
        change_id="CH-1", batch_id="20260715-000000", target="api",
        status="failed" if failed else "passed", command="cmd",
        source={"framework": "pytest", "raw_log": "raw/api.log"},
        total=len(cases), passed=len(cases) - failed, failed=failed, skipped=0,
        cases=cases, unmapped_tests=[],
    )


def _cov() -> CoverageResult:
    return CoverageResult(
        change_id="CH-1", batch_id="20260715-000000", available=True, line_coverage=90.0,
        branch_coverage=80.0, threshold={"line": 70, "branch": 60}, status="PASS",
    )


def test_inspect_no_failures_writes_analysis(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    result = inspect_change(tmp_path, change_id)
    assert result.analysis.status == "no_failures"
    assert result.analysis.final_status == "PASS"
    assert (tmp_path / "qa" / "changes" / "CH-1" / "inspect" / "failure-analysis.json").is_file()
    assert (tmp_path / "qa" / "changes" / "CH-1" / "inspect" / "quality-gate-result.json").is_file()


def test_inspect_classifies_locator_failure_as_fixable(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api("AssertionError: expected 200 received 500"), _cov())
    result = inspect_change(tmp_path, change_id)
    assert result.analysis.status == "analyzed"
    assert result.analysis.final_status == "FAIL"
    assert len(result.analysis.failures) == 1
    assert result.analysis.failures[0].category == "assertion_failure"
    assert result.analysis.failures[0].id == "FAIL-001"


def test_inspect_missing_manifest_raises(tmp_path: Path) -> None:
    (tmp_path / "qa" / "changes" / "CH-9").mkdir(parents=True)
    import pytest

    from assurance_agent.workflow.execution.evidence import EvidenceError
    with pytest.raises(EvidenceError):
        inspect_change(tmp_path, "CH-9")
```

```python
# tests/unit/report/test_report_builder.py
from pathlib import Path

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, TargetResult
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from assurance_agent.workflow.report.report_builder import generate_report


def _api(failed_message: str | None) -> TargetResult:
    cases = [CaseResult(case_id="TC_API_001", status="passed", file="tests/api/t.py",
                        test_name="test_tc_api_001__ok", duration_ms=1, message="")]
    failed = 0
    if failed_message is not None:
        failed = 1
        cases.append(CaseResult(case_id="TC_API_002", status="failed", file="tests/api/t.py",
                                test_name="test_tc_api_002__x", duration_ms=1, message=failed_message))
    return TargetResult(
        change_id="CH-1", batch_id="20260715-000000", target="api",
        status="failed" if failed else "passed", command="cmd",
        source={"framework": "pytest", "raw_log": "raw/api.log"},
        total=len(cases), passed=len(cases) - failed, failed=failed, skipped=0,
        cases=cases, unmapped_tests=[],
    )


def _cov() -> CoverageResult:
    return CoverageResult(
        change_id="CH-1", batch_id="20260715-000000", available=True, line_coverage=90.0,
        branch_coverage=80.0, threshold={"line": 70, "branch": 60}, status="PASS",
    )


def _seed_change(tmp_path: Path, api: TargetResult, cov: CoverageResult) -> str:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    gate = build_quality_gate(
        change_id="CH-1", batch_id="20260715-000000", api=api, e2e=None,
        coverage=cov, coverage_gate_mode="warn",
    )
    publish_execution_evidence(
        execution_dir=change_dir / "execution", change_id="CH-1", batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api, e2e=None, fuzz=None, coverage=cov, performance=None,
        quality_gate=gate, summary="# summary\n",
    )
    return "CH-1"


def test_generate_report_all_pass(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.final_status == "PASS"
    assert result.report.quality_score == 100
    assert result.report.risk_level == "LOW"
    report_dir = tmp_path / "qa" / "changes" / "CH-1" / "report"
    assert (report_dir / "quality-report.json").is_file()
    assert (report_dir / "quality-report.md").is_file()
    assert (report_dir / "executive-summary.md").is_file()


def test_generate_report_business_defect_is_high_risk(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api("500 internal server error"), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.final_status == "FAIL"
    assert result.report.risk_level == "HIGH"
    assert len(result.report.defects.product) == 1
```

- [ ] **Step 5: 跑测试确认失败**

Run: `uv run pytest tests/unit/report/test_inspector.py tests/unit/report/test_report_builder.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 6: 实现 inspector.py 与 report_builder.py**

```python
# assurance_agent/workflow/report/inspector.py
"""Classify execution failures → inspect/ artifacts (failure-analysis + quality-gate).

Reads the primary execution evidence, classifies every failed case, and derives
FailureAnalysis. Manifest integrity issues (a selected target with no result
file) are treated as critical manifest_asset_missing failures, not silent skips.
"""
from pathlib import Path

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    CoverageGapEntry,
    FailureAnalysis,
    FailureEntry,
    FailureEvidence,
    QualityGateResult,
)
from assurance_agent.workflow.execution.evidence import ExecutionEvidence, load_execution_evidence
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.execution.results import TargetResult
from assurance_agent.workflow.report.failure_classifier import classify_failure


class InspectResult(BaseModel):
    analysis: FailureAnalysis
    quality_gate: QualityGateResult
    analysis_path: str
    summary_path: str
    quality_gate_path: str


def inspect_change(project_root: Path, change_id: str) -> InspectResult:
    assert_change_id_safe(change_id)
    change_base = project_root / "qa" / "changes" / change_id
    execution_dir = change_base / "execution"
    inspect_dir = change_base / "inspect"

    evidence = load_execution_evidence(execution_dir)
    gate = evidence.quality_gate
    if gate is None:  # defensive: runner always writes it, but recompute if absent
        from assurance_agent.workflow.report.quality_gate import build_quality_gate
        gate = build_quality_gate(
            change_id=change_id, batch_id=evidence.batch_id, api=evidence.api, e2e=evidence.e2e,
            coverage=evidence.coverage, coverage_gate_mode="warn",
            fuzz=evidence.fuzz, performance=evidence.performance,
        )

    manifest_path = str(execution_dir / "execution-manifest.yaml")

    if evidence.integrity_issues:
        integrity = _integrity_failures(evidence)
        _complete(integrity)
        analysis = _analysis(change_id, manifest_path, evidence.batch_id, "FAIL",
                             "failed", "failed", integrity, integrity, [], [], [])
        gate = gate.model_copy(update={"final_status": "FAIL"})
        _write(inspect_dir, change_id, analysis, gate)
        return _result(analysis, gate, inspect_dir)

    failures: list[FailureEntry] = []
    for result, target in ((evidence.api, "api"), (evidence.e2e, "e2e"), (evidence.fuzz, "fuzz")):
        failures.extend(_classify_target(result, target, evidence, change_id))  # type: ignore[arg-type]

    coverage_gaps = _coverage_gaps(evidence)
    if gate.dimensions.coverage.status == "FAIL":
        failures.extend(_coverage_failures(evidence, coverage_gaps))

    _complete(failures)
    hard = [f for f in failures if not f.fix_proposal_eligible and not f.needs_review]
    review = [f for f in failures if f.needs_review]
    known = [f for f in failures if f.category == "known_product_issue"]
    status = "no_failures" if not failures else "analyzed"
    analysis = _analysis(change_id, manifest_path, evidence.batch_id, gate.final_status,
                         "completed", status, failures, hard, review, known, coverage_gaps)
    _write(inspect_dir, change_id, analysis, gate)
    return _result(analysis, gate, inspect_dir)


def _classify_target(
    result: TargetResult | None, target: str, evidence: ExecutionEvidence, change_id: str,
) -> list[FailureEntry]:
    if result is None:
        return []
    entries: list[FailureEntry] = []
    for case in [*result.cases, *result.unmapped_tests]:
        if case.status != "failed":
            continue
        classification = classify_failure(
            message=case.message, log_excerpt="", target=target,  # type: ignore[arg-type]
        )
        entries.append(FailureEntry(
            case_id=case.case_id or case.test_name,
            target=target,  # type: ignore[arg-type]
            category=classification.category,
            fix_proposal_eligible=classification.fix_proposal_eligible,
            severity=classification.severity,
            needs_review=classification.needs_review,
            evidence=FailureEvidence(
                result_file=evidence.result_paths.get(target, ""),
                test_file=case.file, trace=case.trace, screenshot=case.screenshot,
                video=case.video, raw_log=case.raw_log_ref, log_excerpt=case.message[:400],
            ),
            diagnosis=_diagnosis(case.message, classification.category),
            recommended_action=_recommended_action(classification.category, change_id),
        ))
    return entries


def _coverage_gaps(evidence: ExecutionEvidence) -> list[CoverageGapEntry]:
    cov = evidence.coverage
    if not cov or not cov.available:
        return []
    return [
        CoverageGapEntry(file=str(f.get("file", "")), line_coverage=float(f.get("line_coverage", 0.0)),
                         threshold=cov.threshold.line)
        for f in cov.uncovered_critical_files
    ]


def _coverage_failures(evidence: ExecutionEvidence, gaps: list[CoverageGapEntry]) -> list[FailureEntry]:
    cov = evidence.coverage
    line = cov.line_coverage if cov else 0.0
    threshold = cov.threshold.line if cov else 0.0
    resolved = gaps or [CoverageGapEntry(file="coverage", line_coverage=line, threshold=threshold)]
    return [FailureEntry(
        case_id=gap.file, target="coverage", category="coverage_gap",
        fix_proposal_eligible=False, severity="high", needs_review=False,
        evidence=FailureEvidence(
            result_file=evidence.result_paths.get("coverage", ""), test_file=gap.file,
            trace="", screenshot="", video="", raw_log="",
            log_excerpt=f"line coverage {gap.line_coverage}% below threshold {gap.threshold}%",
        ),
        diagnosis=f"Coverage gate failed for {gap.file}: {gap.line_coverage}% below threshold {gap.threshold}%.",
        recommended_action="Add or improve tests for the uncovered critical file. Coverage gaps are never auto-fixed.",
    ) for gap in resolved]


def _integrity_failures(evidence: ExecutionEvidence) -> list[FailureEntry]:
    return [FailureEntry(
        case_id=f"manifest:{issue.target}", target=issue.target,  # type: ignore[arg-type]
        category="manifest_asset_missing", fix_proposal_eligible=False, severity="critical",
        needs_review=False,
        evidence=FailureEvidence(
            result_file=issue.path, test_file="", trace="", screenshot="", video="",
            raw_log="", log_excerpt=issue.reason,
        ),
        diagnosis=f"Execution asset missing for target '{issue.target}': {issue.path}",
        recommended_action="Re-run `aa run` to regenerate the missing execution asset before archiving.",
    ) for issue in evidence.integrity_issues]


def _complete(failures: list[FailureEntry]) -> None:
    for index, failure in enumerate(failures, start=1):
        failure.id = failure.id or f"FAIL-{index:03d}"
        failure.test = failure.test or failure.evidence.test_file or failure.case_id
        failure.recommended_next_action = failure.recommended_next_action or failure.recommended_action


def _analysis(change_id, manifest_path, batch_id, final_status, inspection_status, status,  # noqa: ANN001
              failures, hard, review, known, coverage_gaps) -> FailureAnalysis:
    return FailureAnalysis(
        schema_version="1.0", change_id=change_id, source_manifest=manifest_path,
        inspection_status=inspection_status, batch_id=batch_id, source_batch_id=batch_id,
        final_status=final_status, inspect_mode="primary", classification_performed=status != "failed",
        status=status, failures=failures, hard_fails=hard, needs_review=review,
        known_product_issues=known, coverage_gaps=coverage_gaps or None,
    )


def _write(inspect_dir: Path, change_id: str, analysis: FailureAnalysis, gate: QualityGateResult) -> None:
    inspect_dir.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "failure-analysis.json").write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    (inspect_dir / "quality-gate-result.json").write_text(gate.model_dump_json(indent=2), encoding="utf-8")
    (inspect_dir / "failure-summary.md").write_text(_summary_md(change_id, analysis), encoding="utf-8")


def _result(analysis: FailureAnalysis, gate: QualityGateResult, inspect_dir: Path) -> InspectResult:
    return InspectResult(
        analysis=analysis, quality_gate=gate,
        analysis_path=str(inspect_dir / "failure-analysis.json"),
        summary_path=str(inspect_dir / "failure-summary.md"),
        quality_gate_path=str(inspect_dir / "quality-gate-result.json"),
    )


def _summary_md(change_id: str, analysis: FailureAnalysis) -> str:
    lines = [
        f"# Failure Analysis — {change_id}", "",
        f"- Final Status: {analysis.final_status}",
        f"- Batch: {analysis.batch_id}",
        f"- Failures: {len(analysis.failures)} (hard={len(analysis.hard_fails)}, review={len(analysis.needs_review)})",
        "",
    ]
    for failure in analysis.failures:
        flag = "fix-allowed" if failure.fix_proposal_eligible else ("review" if failure.needs_review else "no-fix")
        lines.append(f"- `{failure.case_id}` [{failure.target}] {failure.category} ({flag}): {failure.diagnosis}")
    lines.append("")
    return "\n".join(lines)


_DIAGNOSIS_PREFIX = {
    "locator_failure": "Element locator failed.",
    "wait_strategy_failure": "Wait/timeout strategy failed.",
    "assertion_failure": "Assertion mismatch — expected value differs from actual.",
    "environment_failure": "Environment or connectivity issue prevented test execution.",
    "test_data_failure": "Test data or fixture not available.",
    "business_logic_failure": "Server returned an error suggesting a product-level issue.",
    "known_product_issue": "Known product issue matched from failure evidence.",
    "fuzz_configuration_error": "Fuzz setup/config error. Not a product bug.",
    "fuzz_stateful_failure": "Fuzzing surfaced a server fault under generated input.",
    "test_code_error": "Test code has a syntax or runtime error.",
    "case_semantic_failure": "Test case does not align with acceptance criteria.",
}


def _diagnosis(message: str, category: str) -> str:
    first = (message or "").split("\n")[0][:200]
    prefix = _DIAGNOSIS_PREFIX.get(category)
    return f"{prefix} {first}".strip() if prefix else (first or "Unknown failure.")


_ACTION = {
    "locator_failure": "Generate a Fix Proposal to update the locator strategy after review. Run `aa heal validate-proposal --change {cid}`.",
    "wait_strategy_failure": "Generate a Fix Proposal to adjust wait conditions. Run `aa heal validate-proposal --change {cid}`.",
    "assertion_failure": "Investigate whether product behaviour changed or the expected value is wrong. Do not auto-fix.",
    "environment_failure": "Fix the environment (server, database, network) and rerun. Do not generate a Fix Proposal.",
    "test_data_failure": "Review test fixtures and seed data. A Fix Proposal may be generated with manual review.",
    "business_logic_failure": "File a bug against the product team. This is not a test issue.",
    "known_product_issue": "Documented product issue — route to product/developer tracking, do not generate a test fix.",
    "test_code_error": "Fix the syntax/import error in the test file and rerun.",
    "case_semantic_failure": "Review the test case against requirements. Update the case YAML if necessary.",
    "coverage_gap": "Add or improve tests for the uncovered critical file. Coverage gaps are never auto-fixed.",
}


def _recommended_action(category: str, change_id: str) -> str:
    template = _ACTION.get(category, "Investigate manually. Classification is uncertain.")
    return template.format(cid=change_id)
```

```python
# assurance_agent/workflow/report/report_builder.py
"""Deterministic Quality Report trio (quality-report.json/.md + executive-summary.md).

Consumes the inspect artifacts + execution evidence, scores the run, buckets
defects, and derives risk/recommendation. CLI is the only trusted scorer.
"""
import re
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    FailureAnalysis,
    QualityGateResult,
    QualityReport,
    ReportDefect,
    ReportDefects,
    ReportScope,
)
from assurance_agent.workflow.execution.evidence import load_execution_evidence
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.report.quality_score import ScoreDimension, compute_quality_score

_PRODUCT = {"business_logic_failure", "fuzz_stateful_failure", "perf_threshold_exceeded"}
_ENVIRONMENT = {"environment_failure", "perf_environment"}

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class GenerateReportResult(BaseModel):
    report: QualityReport
    json_path: str
    md_path: str
    exec_summary_path: str


def generate_report(project_root: Path, change_id: str) -> GenerateReportResult:
    assert_change_id_safe(change_id)
    change_base = project_root / "qa" / "changes" / change_id
    inspect_dir = change_base / "inspect"
    report_dir = change_base / "report"

    evidence = load_execution_evidence(change_base / "execution")
    gate = _load(inspect_dir / "quality-gate-result.json", QualityGateResult)
    if gate is None:
        raise FileNotFoundError(
            f"quality-gate-result.json not found for '{change_id}'. Run `aa report inspect` first."
        )
    analysis = _load(inspect_dir / "failure-analysis.json", FailureAnalysis)

    score, breakdown = compute_quality_score(_dimensions(gate))
    defects = _bucket_defects(analysis)
    risk_level, risk_rationale = _risk(gate, defects)
    recommendation = _recommendation(gate.final_status, defects)

    report = QualityReport(
        schema_version="1.0", change_id=change_id, batch_id=gate.batch_id or evidence.batch_id,
        final_status=gate.final_status, quality_score=score, score_breakdown=breakdown,
        scope=_scope(change_base), functional=gate.dimensions.functional,
        coverage=gate.dimensions.coverage, defects=defects, risk_level=risk_level,
        risk_rationale=risk_rationale, recommendation=recommendation,
        non_functional=gate.dimensions.non_functional,
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "quality-report.json"
    md_path = report_dir / "quality-report.md"
    exec_path = report_dir / "executive-summary.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(_report_md(report), encoding="utf-8")
    exec_path.write_text(_exec_summary(report), encoding="utf-8")
    return GenerateReportResult(
        report=report, json_path=str(json_path), md_path=str(md_path), exec_summary_path=str(exec_path),
    )


def _dimensions(gate: QualityGateResult) -> dict[str, ScoreDimension]:
    func = gate.dimensions.functional
    func_total = func.api.total + func.e2e.total
    func_passed = func.api.passed + func.e2e.passed
    cov = gate.dimensions.coverage
    cov_ratio = cov.line_coverage / cov.threshold.line if (cov.available and cov.threshold.line > 0) else 0.0

    fuzz_active = bool(func.fuzz and func.fuzz.total > 0)
    fuzz_ratio = (func.fuzz.passed / func.fuzz.total) if fuzz_active and func.fuzz else 0.0

    non_func = gate.dimensions.non_functional
    ran = [s for s in non_func.performance if s.verdict != "SKIPPED"] if non_func else []
    perf_active = bool(non_func and non_func.status != "SKIPPED" and ran)
    perf_ratio = (sum(1 for s in ran if s.verdict == "PASS") / len(ran)) if perf_active else 0.0

    m3 = fuzz_active or perf_active
    weights = ({"functional": 50, "coverage": 20, "fuzz": 15, "performance": 15}
               if m3 else {"functional": 70, "coverage": 30, "fuzz": 0, "performance": 0})
    return {
        "functional": ScoreDimension(active=func_total > 0,
                                     ratio=(func_passed / func_total) if func_total > 0 else 0.0,
                                     weight=weights["functional"]),
        "coverage": ScoreDimension(active=cov.available, ratio=cov_ratio, weight=weights["coverage"]),
        "fuzz": ScoreDimension(active=fuzz_active, ratio=fuzz_ratio, weight=weights["fuzz"]),
        "performance": ScoreDimension(active=perf_active, ratio=perf_ratio, weight=weights["performance"]),
    }


def _bucket_defects(analysis: FailureAnalysis | None) -> ReportDefects:
    product: list[ReportDefect] = []
    test: list[ReportDefect] = []
    environment: list[ReportDefect] = []
    for failure in (analysis.failures if analysis else []):
        defect = ReportDefect(case_id=failure.case_id, category=failure.category, diagnosis=failure.diagnosis)
        if failure.category in _PRODUCT:
            product.append(defect)
        elif failure.category in _ENVIRONMENT:
            environment.append(defect)
        else:
            test.append(defect)
    return ReportDefects(product=product, test=test, environment=environment)


def _risk(gate: QualityGateResult, defects: ReportDefects) -> tuple[str, str]:
    if defects.product:
        return "HIGH", f"Detected {len(defects.product)} product-level defect(s); product behaviour is incorrect."
    status = gate.final_status
    if status == "FAIL":
        return "HIGH", "Functional gate failed — one or more selected test targets did not pass."
    if status == "PASS_WITH_WARNINGS":
        unmapped = gate.dimensions.functional.unmapped_tests or 0
        reasons = []
        if gate.dimensions.coverage.status == "PASS_WITH_WARNINGS":
            reasons.append("coverage below threshold")
        if unmapped > 0:
            reasons.append(f"{unmapped} executed test(s) not traceable to case IDs")
        if defects.environment:
            reasons.append(f"{len(defects.environment)} environment issue(s)")
        if defects.test:
            reasons.append(f"{len(defects.test)} test-level issue(s)")
        level = "MEDIUM" if (unmapped > 0 or defects.test or defects.environment) else "LOW"
        return level, f"All functional tests passed; warnings: {', '.join(reasons) or 'none'}."
    if status == "SKIPPED":
        return "MEDIUM", "No test targets ran — quality cannot be assessed."
    return "LOW", "All dimensions passed with no defects."


def _recommendation(status: str, defects: ReportDefects) -> str:
    if status == "FAIL":
        return "Do not release: failing tests or hard blockers must be resolved first."
    if defects.product:
        return "Release only with caution — unresolved product defects exist; track them as known issues."
    if status == "PASS_WITH_WARNINGS":
        return "Release is acceptable but address the noted warnings (e.g. coverage)."
    if status == "SKIPPED":
        return "No tests ran — run the suite before deciding on release."
    return "Safe to release."


_CASE_ID_RE = re.compile(r"^\s*case_id\s*:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)
_REQ_RE = re.compile(r"requirement_id\s*:\s*[\"']?([A-Za-z0-9_-]+)")


def _scope(change_base: Path) -> ReportScope:
    cases_dir = change_base / "cases"
    case_ids: set[str] = set()
    requirements: set[str] = set()
    if cases_dir.is_dir():
        for path in cases_dir.rglob("*.y*ml"):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            case_ids.update(_CASE_ID_RE.findall(content))
            requirements.update(_REQ_RE.findall(content))
    return ReportScope(cases=len(case_ids), requirements=sorted(requirements))


def _load(path: Path, model: type[_ModelT]) -> _ModelT | None:
    if not path.is_file():
        return None
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fmt(value: float | str) -> str:
    return "N/A" if value == "N/A" else str(value)


def _report_md(r: QualityReport) -> str:
    cov = r.coverage
    lines = [
        f"# Quality Report — {r.change_id}", "",
        f"- **Batch**: {r.batch_id or '(unknown)'}",
        f"- **Final Status**: {r.final_status}",
        f"- **Quality Score**: {r.quality_score} / 100",
        f"- **Risk Level**: {r.risk_level}", "",
        "## Score Breakdown", "",
        "| Dimension | Points |", "|-----------|--------|",
        f"| Functional | {_fmt(r.score_breakdown.functional)} |",
        f"| Coverage | {_fmt(r.score_breakdown.coverage)} |",
        f"| Fuzz | {_fmt(r.score_breakdown.fuzz)} |",
        f"| Performance | {_fmt(r.score_breakdown.performance)} |", "",
        "## Scope", "",
        f"- Cases: {r.scope.cases}",
        f"- Requirements: {', '.join(r.scope.requirements) or '(none detected)'}", "",
        "## Functional", "",
        f"- API: total={r.functional.api.total} passed={r.functional.api.passed} failed={r.functional.api.failed}",
        f"- E2E: total={r.functional.e2e.total} passed={r.functional.e2e.passed} failed={r.functional.e2e.failed}",
        "", "## Coverage", "",
        (f"- Line: {cov.line_coverage}% (threshold {cov.threshold.line}%)\n"
         f"- Branch: {cov.branch_coverage}% (threshold {cov.threshold.branch}%)\n- Status: {cov.status}"
         if cov.available else "- Not collected (treated as a warning, not a failure)."),
        "", "## Defects", "",
        f"- Product: {len(r.defects.product)}",
        f"- Test: {len(r.defects.test)}",
        f"- Environment: {len(r.defects.environment)}", "",
        *_defect_section("Product Defects", r.defects.product),
        *_defect_section("Test Defects", r.defects.test),
        *_defect_section("Environment Defects", r.defects.environment),
        "## Risk & Recommendation", "",
        f"- **Risk Level**: {r.risk_level}",
        f"- **Rationale**: {r.risk_rationale}",
        f"- **Recommendation**: {r.recommendation}", "",
    ]
    return "\n".join(lines)


def _defect_section(title: str, defects: list[ReportDefect]) -> list[str]:
    if not defects:
        return []
    out = [f"### {title}", ""]
    out += [f"- `{d.case_id}` ({d.category}): {d.diagnosis}" for d in defects]
    out.append("")
    return out


def _exec_summary(r: QualityReport) -> str:
    func = r.functional
    passed = func.api.passed + func.e2e.passed
    total = func.api.total + func.e2e.total
    coverage = f" Coverage: {r.coverage.line_coverage}% line." if r.coverage.available else " Coverage: not collected."
    return "\n".join([
        f"# Executive Summary — {r.change_id}", "",
        f"**Final Status**: {r.final_status}  |  **Quality Score**: {r.quality_score}/100  |  **Risk**: {r.risk_level}",
        "", r.risk_rationale, "",
        f"**Recommendation**: {r.recommendation}", "",
        f"Functional: {passed}/{total} passed.{coverage}", "",
    ])
```

- [ ] **Step 7: 跑测试确认通过**

Run: `uv run pytest tests/unit/report/test_quality_score.py tests/unit/report/test_inspector.py tests/unit/report/test_report_builder.py -v`
Expected: 5 + 3 + 2 = 10 passed

- [ ] **Step 8: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 9: Commit**

```bash
git add assurance_agent/workflow/report/quality_score.py \
        assurance_agent/workflow/report/inspector.py \
        assurance_agent/workflow/report/report_builder.py \
        tests/unit/report/test_quality_score.py \
        tests/unit/report/test_inspector.py \
        tests/unit/report/test_report_builder.py
git commit -m "feat: add quality score, inspector and quality report builder"
```

---

### Task 9: `aa report inspect|generate` + `aa heal` 命令 + 分层收口（report_cmd.py、heal_cmd.py、.importlinter）

**Files:**
- Create: `assurance_agent/commands/report_cmd.py`
- Create: `assurance_agent/commands/heal_cmd.py`
- Modify: `assurance_agent/cli.py`
- Modify: `.importlinter`
- Test: `tests/unit/commands/test_report_cmd.py`
- Test: `tests/unit/commands/test_heal_cmd.py`

**Interfaces:**
- Consumes: Task 8 `inspect_change`/`generate_report`；Task 5 `EvidenceError`；M2 `FixProposal`/`SafetyCheck`/`FailureAnalysis`；M4 `EXIT_COMPLETED`/`EXIT_HUMAN_REVIEW`/`EXIT_ERROR`；M1 `AaError`。
- Produces：`report_group`（`aa report inspect|generate --change <id>`）与 `heal_group`（`aa heal validate-proposal|eligibility-summary|safety-check --change <id>`）；`.importlinter` 追加 `artifacts` 层（若 M2 已加则保持不变）。

`aa heal` 子命令（deterministic 读/校验，surface 对齐 TS `heal.ts` 的「只产 CLI-owned 产物」原则，扩展出 M2 模型校验）：
- `validate-proposal --change <id> [--file <path>]`：读 `healing/fix-proposal.json`，用 `FixProposal` 校验；打印 `summary.eligible_count` 与各 `proposals[].target/eligible`；文件缺失或 schema 非法 → `EXIT_ERROR`，否则 `EXIT_COMPLETED`。
- `eligibility-summary --change <id>`：读 `inspect/failure-analysis.json`（`FailureAnalysis`），汇总 fixable / needs-review / hard 数量并打印；始终 `EXIT_COMPLETED`（纯汇总）。
- `safety-check --change <id> [--file <path>]`：读 `healing/fixer-safety-check.json`（M2 注册、`fixer-safety-gate` 消费的同一产物），用 `SafetyCheck` 校验；`passed=false` → `EXIT_ERROR`，`needs_review=true` → `EXIT_HUMAN_REVIEW`，否则 `EXIT_COMPLETED`。

- [ ] **Step 1: 写失败测试（report_cmd）**

```python
# tests/unit/commands/test_report_cmd.py
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.cli import main
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, TargetResult
from assurance_agent.workflow.report.quality_gate import build_quality_gate


def _seed(root: Path, failed: bool) -> None:
    cases = [CaseResult(case_id="TC_API_001", status="passed", file="f.py",
                        test_name="test_tc_api_001__ok", duration_ms=1, message="")]
    n_failed = 0
    if failed:
        n_failed = 1
        cases.append(CaseResult(case_id="TC_API_002", status="failed", file="f.py",
                                test_name="test_tc_api_002__x", duration_ms=1,
                                message="500 internal server error"))
    api = TargetResult(
        change_id="CH-1", batch_id="20260715-000000", target="api",
        status="failed" if failed else "passed", command="cmd",
        source={"framework": "pytest", "raw_log": "raw/api.log"},
        total=len(cases), passed=len(cases) - n_failed, failed=n_failed, skipped=0,
        cases=cases, unmapped_tests=[],
    )
    cov = CoverageResult(change_id="CH-1", batch_id="20260715-000000", available=True,
                         line_coverage=90.0, branch_coverage=80.0,
                         threshold={"line": 70, "branch": 60}, status="PASS")
    gate = build_quality_gate(change_id="CH-1", batch_id="20260715-000000", api=api, e2e=None,
                              coverage=cov, coverage_gate_mode="warn")
    publish_execution_evidence(
        execution_dir=root / "qa" / "changes" / "CH-1" / "execution", change_id="CH-1",
        batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api, e2e=None, fuzz=None, coverage=cov, performance=None, quality_gate=gate,
        summary="# summary\n",
    )


def test_report_inspect_then_generate_pass(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, failed=False)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    inspect = runner.invoke(main, ["report", "inspect", "--change", "CH-1"])
    assert inspect.exit_code == 0
    assert "PASS" in inspect.output
    generate = runner.invoke(main, ["report", "generate", "--change", "CH-1"])
    assert generate.exit_code == 0
    assert "100" in generate.output
    assert (tmp_path / "qa" / "changes" / "CH-1" / "report" / "quality-report.json").is_file()


def test_report_inspect_fail_exit_one(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, failed=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["report", "inspect", "--change", "CH-1"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_report_inspect_missing_evidence_exit_one(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "qa" / "changes" / "CH-2").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["report", "inspect", "--change", "CH-2"])
    assert result.exit_code == 1
    assert "run" in result.output.lower()
```

- [ ] **Step 2: 写失败测试（heal_cmd）**

```python
# tests/unit/commands/test_heal_cmd.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _change(root: Path) -> Path:
    change = root / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    return change


def test_validate_proposal_ok(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(change / "healing" / "fix-proposal.json", {
        "schema_version": "1.0",
        "summary": {"eligible_count": 1},
        "proposals": [{"target": "e2e", "eligible": True}],
    })
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "validate-proposal", "--change", "CH-1"])
    assert result.exit_code == 0
    assert "eligible_count" in result.output


def test_validate_proposal_missing_exit_40(tmp_path: Path, monkeypatch) -> None:
    _change(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "validate-proposal", "--change", "CH-1"])
    assert result.exit_code == 40


def test_safety_check_passed_exit_0(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(change / "healing" / "fixer-safety-check.json", _safety(passed=True, needs_review=False))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "safety-check", "--change", "CH-1"])
    assert result.exit_code == 0


def test_safety_check_needs_review_exit_30(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(change / "healing" / "fixer-safety-check.json", _safety(passed=True, needs_review=True))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "safety-check", "--change", "CH-1"])
    assert result.exit_code == 30


def test_safety_check_failed_exit_40(tmp_path: Path, monkeypatch) -> None:
    change = _change(tmp_path)
    _write(change / "healing" / "fixer-safety-check.json", _safety(passed=False, needs_review=False))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["heal", "safety-check", "--change", "CH-1"])
    assert result.exit_code == 40


def _safety(*, passed: bool, needs_review: bool) -> dict:
    return {
        "schema_version": "1.0", "passed": passed, "needs_review": needs_review,
        "product_code_modified": False, "skip_or_xfail_added": False,
        "unrelated_tests_modified": False, "assertion_expected_value_changes_detected": False,
        "high_risk_proposal_applied": False,
    }
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/commands/test_report_cmd.py tests/unit/commands/test_heal_cmd.py -v`
Expected: FAIL with `no such command 'report'` / `'heal'`

- [ ] **Step 4: 实现 report_cmd.py 与 heal_cmd.py**

```python
# assurance_agent/commands/report_cmd.py
"""`aa report inspect|generate` commands."""
from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.report_builder import generate_report

_GATE_COLOR = {"PASS": "green", "PASS_WITH_WARNINGS": "yellow", "FAIL": "red", "SKIPPED": "yellow"}


@click.group("report")
def report_group() -> None:
    """Inspect execution results and generate quality reports."""


@report_group.command("inspect")
@click.option("--change", "change_id", required=True, help="Change ID.")
def inspect_cmd(change_id: str) -> None:
    """Classify failures → inspect/failure-analysis.json + quality-gate-result.json."""
    try:
        result = inspect_change(Path.cwd(), change_id)
    except (EvidenceError, AaError) as err:
        click.secho(f"Inspect failed: {err}", fg="red")
        raise SystemExit(1) from err

    analysis = result.analysis
    gate = result.quality_gate
    click.secho(f"\naa report inspect — change: {change_id}\n", bold=True)
    click.echo("  Final Status : " + click.style(gate.final_status, fg=_GATE_COLOR[gate.final_status], bold=True))
    click.echo(f"  Batch ID     : {analysis.batch_id or '(unknown)'}")
    click.echo(f"  Failures     : {len(analysis.failures)} "
               f"(hard={len(analysis.hard_fails)}, review={len(analysis.needs_review)})")
    for failure in analysis.failures:
        flag = "fix-allowed" if failure.fix_proposal_eligible else ("review" if failure.needs_review else "no-fix")
        click.echo(f"    {failure.category:<28} {failure.case_id}  [{flag}]")
    click.echo(f"  failure-analysis.json    → {result.analysis_path}")
    click.echo(f"  quality-gate-result.json → {result.quality_gate_path}")
    raise SystemExit(1 if gate.final_status == "FAIL" else 0)


@report_group.command("generate")
@click.option("--change", "change_id", required=True, help="Change ID.")
def generate_cmd(change_id: str) -> None:
    """Quality Score → report/ trio (quality-report.json/.md + executive-summary.md)."""
    try:
        result = generate_report(Path.cwd(), change_id)
    except (EvidenceError, FileNotFoundError, AaError) as err:
        click.secho(f"Report generation failed: {err}", fg="red")
        raise SystemExit(1) from err

    report = result.report
    click.secho(f"\naa report generate — change: {change_id}\n", bold=True)
    click.echo("  Final Status  : " + click.style(report.final_status, fg=_GATE_COLOR[report.final_status], bold=True))
    click.echo(f"  Quality Score : {report.quality_score} / 100")
    click.echo(f"  Risk Level    : {report.risk_level}")
    click.echo(f"  Recommendation: {report.recommendation}")
    click.echo(f"  quality-report.json  → {result.json_path}")
    click.echo(f"  quality-report.md    → {result.md_path}")
    click.echo(f"  executive-summary.md → {result.exec_summary_path}")
    raise SystemExit(0)
```

```python
# assurance_agent/commands/heal_cmd.py
"""`aa heal` healing-support commands: validate fix proposals and safety checks.

All subcommands are deterministic read/validate over M2 healing artifacts; they
never mutate product/test code. Fixer execution itself lives outside the CLI.
"""
import json
from pathlib import Path

import click
from pydantic import ValidationError

from assurance_agent.artifacts.models import FailureAnalysis, FixProposal, SafetyCheck
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.exit_codes import EXIT_COMPLETED, EXIT_ERROR, EXIT_HUMAN_REVIEW


@click.group("heal")
def heal_group() -> None:
    """Healing-support commands (fix-proposal validation, eligibility, safety checks)."""


def _change_base(change_id: str) -> Path:
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    return Path.cwd() / "qa" / "changes" / change_id


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@heal_group.command("validate-proposal")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--file", "file_path", default=None, help="Override path to fix-proposal.json.")
def validate_proposal(change_id: str, file_path: str | None) -> None:
    """Validate healing/fix-proposal.json against the FixProposal contract."""
    path = Path(file_path) if file_path else _change_base(change_id) / "healing" / "fix-proposal.json"
    raw = _load_json(path)
    if raw is None:
        click.secho(f"fix-proposal.json not found or unreadable: {path}", fg="red")
        raise SystemExit(EXIT_ERROR)
    try:
        proposal = FixProposal.model_validate(raw)
    except ValidationError as err:
        click.secho(f"fix-proposal.json is invalid:\n{err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    click.secho(f"\naa heal validate-proposal — change: {change_id}\n", bold=True)
    click.echo(f"  eligible_count : {proposal.summary.eligible_count}")
    for index, item in enumerate(proposal.proposals, start=1):
        flag = click.style("eligible", fg="green") if item.eligible else click.style("not-eligible", fg="yellow")
        click.echo(f"  [{index}] target={item.target}  {flag}")
    raise SystemExit(EXIT_COMPLETED)


@heal_group.command("eligibility-summary")
@click.option("--change", "change_id", required=True, help="Change ID.")
def eligibility_summary(change_id: str) -> None:
    """Summarise fix eligibility from inspect/failure-analysis.json."""
    path = _change_base(change_id) / "inspect" / "failure-analysis.json"
    raw = _load_json(path)
    if raw is None:
        click.secho(f"failure-analysis.json not found: {path}. Run `aa report inspect` first.", fg="red")
        raise SystemExit(EXIT_ERROR)
    analysis = FailureAnalysis.model_validate(raw)
    fixable = [f for f in analysis.failures if f.fix_proposal_eligible]
    review = [f for f in analysis.failures if f.needs_review]

    click.secho(f"\naa heal eligibility-summary — change: {change_id}\n", bold=True)
    click.echo(f"  Total failures : {len(analysis.failures)}")
    click.echo(f"  Fixable        : {len(fixable)}")
    click.echo(f"  Needs review   : {len(review)}")
    click.echo(f"  Hard fails     : {len(analysis.hard_fails)}")
    for failure in fixable:
        click.echo(f"    fixable: {failure.case_id} ({failure.category})")
    raise SystemExit(EXIT_COMPLETED)


@heal_group.command("safety-check")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--file", "file_path", default=None, help="Override path to fixer-safety-check.json.")
def safety_check(change_id: str, file_path: str | None) -> None:
    """Validate healing/fixer-safety-check.json and map its verdict to an exit code.

    Path is `healing/fixer-safety-check.json` — the SAME artifact M2 registers and
    the `fixer-safety-gate` expression consumes. Reading a different filename would
    let the gate pass on an unvalidated (or absent) safety report.
    """
    path = (
        Path(file_path)
        if file_path
        else _change_base(change_id) / "healing" / "fixer-safety-check.json"
    )
    raw = _load_json(path)
    if raw is None:
        click.secho(f"fixer-safety-check.json not found or unreadable: {path}", fg="red")
        raise SystemExit(EXIT_ERROR)
    try:
        check = SafetyCheck.model_validate(raw)
    except ValidationError as err:
        click.secho(f"fixer-safety-check.json is invalid:\n{err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    click.secho(f"\naa heal safety-check — change: {change_id}\n", bold=True)
    click.echo(f"  passed              : {check.passed}")
    click.echo(f"  needs_review        : {check.needs_review}")
    click.echo(f"  product_code_modified: {check.product_code_modified}")
    if not check.passed:
        click.secho("→ Safety check failed. Do not apply the fix.", fg="red")
        raise SystemExit(EXIT_ERROR)
    if check.needs_review:
        click.secho("→ Safety check needs human review before applying.", fg="yellow")
        raise SystemExit(EXIT_HUMAN_REVIEW)
    click.secho("→ Safety check passed.", fg="green")
    raise SystemExit(EXIT_COMPLETED)
```

`cli.py` 追加注册：

```python
from assurance_agent.commands.report_cmd import report_group
from assurance_agent.commands.heal_cmd import heal_group
# ...
main.add_command(report_group)
main.add_command(heal_group)
```

- [ ] **Step 5: 验证累计 `.importlinter` 契约（本任务不改文件）**

`artifacts` 层已由 M2 冻结，`risk` 层已由 M4 加入；M5 不引入新顶层层级，因此不得重写 `.importlinter`。运行时必须同时保留下面 3 个累计契约：

```ini
[importlinter]
root_package = assurance_agent

[importlinter:contract:layers]
name = commands depend on domain, never the reverse
type = layers
layers =
    assurance_agent.cli
    assurance_agent.commands
    assurance_agent.risk
    assurance_agent.workflow
    assurance_agent.artifacts
    assurance_agent.config
    assurance_agent.resources

[importlinter:contract:core-below-orchestration]
name = orchestration depends on core, never the reverse
type = forbidden
source_modules =
    assurance_agent.workflow.core
forbidden_modules =
    assurance_agent.workflow.orchestration

[importlinter:contract:artifacts-below-workflow]
name = artifacts never depend on workflow
type = forbidden
source_modules =
    assurance_agent.artifacts
forbidden_modules =
    assurance_agent.workflow
```

> 说明：layers 契约允许高层 import 低层，`workflow`（含 `workflow.execution`/`workflow.report`）在 `artifacts` 之上，故 execution/report 模块 import M2 模型合法；`commands` 在最上层，可 import `workflow`/`artifacts`/`config`/`resources`。反向 import 会被 `lint-imports` 拦截。

- [ ] **Step 6: 跑全量测试 + 质量门禁**

Run: `uv run pytest tests/unit/commands/test_report_cmd.py tests/unit/commands/test_heal_cmd.py -v`
Expected: 3（report）+ 5（heal）= 8 passed

Run: `uv run pytest tests/unit/execution tests/unit/report tests/unit/commands -v`
Expected: 全绿（Task 1–9 累计）

Run: `uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 均无报错；`lint-imports` 报告 **3 contracts kept, 0 broken**，且契约数量减少即失败。

- [ ] **Step 7: 契约一致性核对（BINDING）**

对照 `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`「接口契约」逐项核对本里程碑实际落地：
- `run_change(project_root, change_dir, config, *, reservation=None) -> ExecutionManifest` 签名一致；无 override 时自行预留，override 时消费调用方的同一 reservation；`execution-manifest.yaml` 顶层 `final_status ∈ {PASS,PASS_WITH_WARNINGS,FAIL,SKIPPED}`。
- `failure-classification.yaml` 路径为 `assurance_agent/_resources/rules/failure-classification.yaml`。
- 消费的 M2 模型字段与 M2 计划一致（本计划顶部「消费的跨里程碑接口契约」）。
- 退出码复用 M4 `exit_codes`（heal safety-check）。

若发现前序里程碑实际落地与此不一致：以**落地代码为准**，回改本计划相应片段与总览「接口契约」，并在提交信息中标注 `docs: reconcile M5 contract with actual M2/M3/M4`。当前预期**零偏离**。

- [ ] **Step 8: Commit**

```bash
git add assurance_agent/commands/report_cmd.py assurance_agent/commands/heal_cmd.py \
        assurance_agent/cli.py \
        tests/unit/commands/test_report_cmd.py tests/unit/commands/test_heal_cmd.py
git commit -m "feat: add aa report inspect/generate and aa heal commands"
```

---

### Task 10: Healing 安全边界（tree-hash + 变更守卫 + override evidence + record-apply）

> **P1 修订**：这是防止 healing 越权修改测试/产品代码的**安全边界**，非可选报告字段。整任务对齐 TS
> `src/workflow/core/healing_state.ts`（1–1262 行）、`src/workflow/core/override_evidence.ts`、`src/utils/hash.ts`。
> 分层放在 `assurance_agent/workflow/healing/`（在 `execution` 之上、`commands` 之下）。

**Files:**
- Create: `assurance_agent/workflow/execution/tree_hash.py`
- Create: `assurance_agent/workflow/healing/__init__.py`（空）
- Create: `assurance_agent/workflow/healing/safety.py`
- Create: `assurance_agent/workflow/healing/override_evidence.py`
- Modify: `assurance_agent/workflow/execution/runner.py`（run_change 计算并写入 manifest tree-hash）
- Modify: `assurance_agent/commands/run_cmd.py`（`--allow-test-changes` + 守卫 + override evidence）
- Modify: `assurance_agent/commands/heal_cmd.py`（`aa heal record-apply`）
- Test: `tests/unit/healing/test_tree_hash.py`、`tests/unit/healing/test_guards.py`、`tests/unit/healing/test_record_apply.py`、`tests/unit/commands/test_run_guard.py`
- Test: `tests/integration/test_cli_heal_record_apply.py`（路径穿越必须在任何文件写入前拒绝）

**Interfaces:**
- `tree_hash.py`：
  - `sha256_file(path: Path) -> str | None`（文件缺失/不可读返回 None）。
  - `TreeHash`（dataclass：`aggregate: str`、`files: dict[str, str]`；`files` 键为 change-relative POSIX 路径、值为文件 sha256；`aggregate` 为对 `sorted(files.items())` 的确定性 sha256）。
  - `hash_test_tree(project_root: Path, roots=("tests",)) -> TreeHash`、`hash_product_tree(project_root: Path, roots: list[str]) -> TreeHash`（忽略 `__pycache__`/`.pyc`；roots 缺失即空树）。
- `healing/safety.py`（只实现安全边界；**不再定义第二套 healing projection**）：
  - Consumes M3 `derive_healing_state(change_dir) -> HealingStateSnapshot`、`read_events`、`append_event_strict`、`capture_files/restore_files`。`attempts_used/status/episode_id/attempt_id` 永远直接来自 M3 projection。
  - `HealingGuardContext(snapshot: HealingStateSnapshot, source_batch_id: str | None, proposal_sha256: str | None, attempt_key: str | None)`；`derive_guard_context(project_root, change_id) -> HealingGuardContext` 只把当前 episode 最新 allocation 与 fix-proposal hash 关联起来，不自行计数、不从 apply-summary 推断 attempt。
  - 所有接收 `change_id` 的公开/内部 helper（`derive_guard_context`、`is_healing_run_context`、两个 tree guard、`pin_healing_applied_test_tree`、`record_apply_summary`、override writer）都必须先通过同一个 `_change_dir()` 调用 M2 `assert_change_id_safe`，禁止依赖 CLI 已校验这一隐含前提。
  - `is_healing_run_context(project_root, change_id) -> bool`。
  - `load_product_code_roots(project_root) -> list[str]`（读 `.aa/config.yaml` `execution.product_code_roots`，缺省 `["app","web/src","src"]`）。
  - `assert_test_tree_unchanged_or_healing(project_root, change_id, allow_test_changes: bool) -> TestTreeIntegrity`——非 healing 且测试树相对上次 manifest 变更且未 override → 抛 `HealingGuardError("TESTS-CHANGED-WITHOUT-HEALING: ...")`；healing `applied` 状态额外校验 `healing/applied-test-tree.json` pin 一致。
  - `assert_product_tree_unchanged_in_healing(project_root, change_id, roots=None) -> ProductTreeIntegrity`——healing 期产品树变更 → 抛 `HealingGuardError("PRODUCT-CHANGED-DURING-HEALING: ...")`。
  - `record_apply_summary(project_root, change_id, target, proposal_ids) -> RecordApplySummaryResult`——从当前测试树 diff 生成 `healing/<target>-apply-summary.json` + `.md`，校验只改授权文件，并在同一 file-snapshot 边界追加 frozen `heal_record_apply`（字段严格为 `target/proposal_sha256/source_batch_id/attempt_key/summary_sha256/files_modified`）。缺少 active allocation 或 source batch 时 fail closed。
  - `pin_healing_applied_test_tree` / `write_cli_fixer_safety_check`；entry baseline + `healing_entry_baseline_pinned` 由 M6 执行 M3 `HealingAttemptIntent(pin_entry_baseline=True)` 时一次性写入，M5 不抢占 allocation owner。
  - `class HealingGuardError(AaError)`。
- `healing/override_evidence.py`：`write_test_changes_override_evidence(*, project_root, change_id, batch_id, batch_dir, reason, integrity, created_at=None) -> OverrideEvidenceResult`——落 `execution/runs/<batch-id>/test-changes-override.json` + `test-changes-override.diff`（`git diff -- tests/`），返回 relPath/sha256/changed_files_count（转录 TS `override_evidence.ts`）。
- `run_change` 变更：计算 `hash_test_tree` / `hash_product_tree(load_product_code_roots())` 并把 `tests_tree_sha256`/`test_files_sha256`/`product_tree_sha256` 写进 `ExecutionManifest`（不再写 None）；接受 Task 5 的可选 `reservation`，不得为同一次 override run 再生成/预留第二个 batch。
- `aa run` 变更：新增 `--allow-test-changes`（+ 复用 `--rerun-reason`）；执行前调用两个守卫，`allow_test_changes` 时先调用 `reserve_execution_batch`，再在该 reservation 目录落 override evidence并把同一 reservation 传给 `run_change`（要求同时给 `--rerun-reason` 作为审计原因，否则抛错）。守卫抛错 → 退出 40 且不产出批次。
- `aa heal record-apply --change <id> --target api|e2e --proposal <id> [--proposal <id> ...]`——调 `record_apply_summary`；拒绝占位符（如 `all`），非法/未授权 proposal id 按 M4 command/data-error 契约退出 40；成功退出 0。

- [ ] **Step 1: 写失败测试（tree hash + 守卫 + record-apply + run 守卫）**

```python
# tests/unit/healing/test_guards.py
from pathlib import Path

import pytest
import yaml

from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.healing.safety import (
    HealingGuardError,
    assert_product_tree_unchanged_in_healing,
    assert_test_tree_unchanged_or_healing,
    derive_guard_context,
    pin_healing_applied_test_tree,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _manifest(change_dir: Path, tests_sha: str, files: dict[str, str], product_sha: str) -> None:
    _write(
        change_dir / "execution" / "execution-manifest.yaml",
        yaml.safe_dump(
            {
                "batch_id": "20260101-000000",
                "tests_tree_sha256": tests_sha,
                "test_files_sha256": files,
                "product_tree_sha256": product_sha,
                "final_status": "PASS",
                "result_files": {},
            }
        ),
    )


def test_tests_changed_without_healing_is_blocked(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 1\n")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    prior = hash_test_tree(tmp_path)
    _manifest(change_dir, prior.aggregate, prior.files, "p0")
    # Mutate a test file after the recorded baseline; not in healing context.
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 2  # tampered\n")
    with pytest.raises(HealingGuardError, match="TESTS-CHANGED-WITHOUT-HEALING"):
        assert_test_tree_unchanged_or_healing(tmp_path, "CH-1", allow_test_changes=False)


def test_allow_test_changes_override_permits(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 1\n")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    prior = hash_test_tree(tmp_path)
    _manifest(change_dir, prior.aggregate, prior.files, "p0")
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 2\n")
    result = assert_test_tree_unchanged_or_healing(tmp_path, "CH-1", allow_test_changes=True)
    assert result.tests_changed is True
    assert len(result.changed_files) == 1


def test_no_prior_manifest_is_noop(tmp_path: Path) -> None:
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    result = assert_test_tree_unchanged_or_healing(tmp_path, "CH-1", allow_test_changes=False)
    assert result.tests_changed is False


@pytest.mark.parametrize(
    "helper",
    [
        lambda root: derive_guard_context(root, "../outside"),
        lambda root: assert_test_tree_unchanged_or_healing(root, "../outside"),
        lambda root: assert_product_tree_unchanged_in_healing(root, "../outside"),
        lambda root: pin_healing_applied_test_tree(root, "../outside"),
    ],
)
def test_internal_healing_helpers_reject_unsafe_change_id(tmp_path: Path, helper) -> None:  # noqa: ANN001
    with pytest.raises(UnsafeIdentifierError):
        helper(tmp_path)
    assert not (tmp_path / "qa" / "outside").exists()
```

- [ ] **Step 1b: 写完整 tree-hash 回归测试**

```python
# tests/unit/healing/test_tree_hash.py
from pathlib import Path

from assurance_agent.workflow.execution.tree_hash import diff_trees, hash_test_tree


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_tree_hash_is_order_independent(tmp_path: Path) -> None:
    _write(tmp_path / "tests/b/test_b.py", "x = 1\n")
    _write(tmp_path / "tests/a/test_a.py", "x = 2\n")
    first = hash_test_tree(tmp_path)
    _write(tmp_path / "tests/a/test_a.py", "x = 2\n")
    second = hash_test_tree(tmp_path)
    assert first.aggregate == second.aggregate
    assert first.files == second.files


def test_tree_hash_ignores_pycache_and_pyc(tmp_path: Path) -> None:
    _write(tmp_path / "tests/api/test_x.py", "def test_x(): pass\n")
    baseline = hash_test_tree(tmp_path)
    _write(tmp_path / "tests/api/__pycache__/ignored.py", "ignored\n")
    (tmp_path / "tests/api/stale.pyc").write_bytes(b"pyc")
    assert hash_test_tree(tmp_path).aggregate == baseline.aggregate


def test_tree_hash_reports_added_removed_and_modified_files(tmp_path: Path) -> None:
    _write(tmp_path / "tests/api/keep.py", "a = 1\n")
    _write(tmp_path / "tests/api/gone.py", "b = 1\n")
    baseline = hash_test_tree(tmp_path).files
    _write(tmp_path / "tests/api/keep.py", "a = 2\n")
    (tmp_path / "tests/api/gone.py").unlink()
    _write(tmp_path / "tests/api/new.py", "c = 1\n")
    assert diff_trees(baseline, hash_test_tree(tmp_path).files) == [
        "tests/api/gone.py", "tests/api/keep.py", "tests/api/new.py"
    ]
```

- [ ] **Step 1c: 写完整 record-apply / projection 回归测试**

在 `tests/unit/healing/test_record_apply.py` 中实现以下可执行用例；复用本文件的 `_manifest`、`_proposal`、`_seed_healing_episode` fixture helper（helper 的输入均为完整 manifest、eligible proposal 和 frozen allocation event，不手写半成品对象）：

```python
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.core.events import (
    EventWriteError,
    append_event_strict,
    read_events,
)
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.healing.safety import (
    HealingGuardError,
    derive_guard_context,
    record_apply_summary,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_healing_episode(
    change_dir: Path,
    *,
    source_batch: str = "20260101-000000",
) -> None:
    append_event_strict(change_dir, {
        "source": "heal", "type": "healing_entry_baseline_pinned",
        "artifact_file": "healing/entry-baseline.json", "artifact_sha256": "x",
        "entry_batch_id": source_batch, "episode_id": "e1",
    })
    append_event_strict(change_dir, {
        "source": "progression", "type": "healing_attempt_allocated",
        "episode_id": "e1", "attempt_id": "a1", "attempt_number": 1,
        "operation_id": "op1", "source_batch_id": source_batch,
    })


def _manifest(change_dir: Path, files: dict[str, str], aggregate: str) -> None:
    _write(change_dir / "execution/execution-manifest.yaml", yaml.safe_dump({
        "batch_id": "20260101-000000", "tests_tree_sha256": aggregate,
        "test_files_sha256": files, "product_tree_sha256": "p0",
        "final_status": "PASS", "result_files": {},
    }))


def _proposal(change_dir: Path) -> None:
    _write(change_dir / "healing/fix-proposal.json", json.dumps({
        "schema_version": "1.0", "summary": {"eligible_count": 1},
        "proposals": [{
            "proposal_id": "FIX-001", "target": "api", "eligible": True,
            "files_to_modify": ["tests/api/test_menu.py"],
        }],
    }))


def _seed_complete_record_apply_case(tmp_path: Path) -> Path:
    change_dir = tmp_path / "qa/changes/CH-1"
    change_dir.mkdir(parents=True)
    _write(tmp_path / "tests/api/test_menu.py", "v1\n")
    baseline = hash_test_tree(tmp_path)
    _manifest(change_dir, baseline.files, baseline.aggregate)
    _seed_healing_episode(change_dir, source_batch="20260101-000000")
    _proposal(change_dir)
    return change_dir


def test_record_apply_rejects_without_active_allocation(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa/changes/CH-1"
    change_dir.mkdir(parents=True)
    _proposal(change_dir)
    with pytest.raises(HealingGuardError, match="no active healing allocation"):
        record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])


def test_record_apply_rejects_file_outside_authorized_proposals(tmp_path: Path) -> None:
    change_dir = _seed_complete_record_apply_case(tmp_path)
    _write(tmp_path / "tests/api/other.py", "tampered\n")
    with pytest.raises(HealingGuardError, match="outside authorized proposals"):
        record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])


def test_record_apply_writes_summary_and_exact_frozen_event(tmp_path: Path) -> None:
    change_dir = _seed_complete_record_apply_case(tmp_path)
    _write(tmp_path / "tests/api/test_menu.py", "v2\n")
    result = record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])
    event = next(e for e in read_events(change_dir) if e.get("type") == "heal_record_apply")
    proposal_sha = derive_guard_context(tmp_path, "CH-1").proposal_sha256
    assert set(event) == {
        "seq", "at", "source", "type", "target", "proposal_sha256",
        "source_batch_id", "attempt_key", "summary_sha256", "files_modified",
    }
    assert event["attempt_key"] == f"{proposal_sha}:20260101-000000"
    assert event["summary_sha256"] == result.summary_sha256
    assert event["files_modified"] == ["tests/api/test_menu.py"]


def test_record_apply_event_failure_restores_both_summary_files(
    tmp_path: Path, monkeypatch
) -> None:
    change_dir = _seed_complete_record_apply_case(tmp_path)
    _write(tmp_path / "tests/api/test_menu.py", "v2\n")
    monkeypatch.setattr(
        "assurance_agent.workflow.healing.safety.append_event_strict",
        lambda *_a, **_k: (_ for _ in ()).throw(EventWriteError("simulated")),
    )
    with pytest.raises(EventWriteError):
        record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])
    assert not (change_dir / "healing/api-apply-summary.json").exists()
    assert not (change_dir / "healing/api-apply-summary.md").exists()


def test_guard_context_uses_m3_projection_attempt_count(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa/changes/CH-1"
    change_dir.mkdir(parents=True)
    _seed_healing_episode(change_dir)
    allocation = next(e for e in read_events(change_dir) if e["type"] == "healing_attempt_allocated")
    append_event_strict(change_dir, allocation)
    assert derive_guard_context(tmp_path, "CH-1").snapshot.attempts_used == 1
```

- [ ] **Step 1d: 写 run guard 的原子性测试**

`tests/unit/commands/test_run_guard.py` 使用一个带上次 manifest 的 `guarded_project` fixture；四个测试必须包含下面的关键断言（完整 CLI argv 明示，不用伪代码）：

```python
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.cli import main
from assurance_agent.commands import run_cmd as run_cmd_mod
from assurance_agent.workflow.core.events import EventWriteError, read_events
from assurance_agent.workflow.execution.tree_hash import hash_test_tree

_CONFIG = """version: 1
sources: {frontend: ./frontend, backend: ./backend}
qa: {cases: ./qa/cases, changes: ./qa/changes}
tests: {root: ./tests, api: ./tests/api, e2e: ./tests/e2e}
frameworks:
  api: {enabled: true, name: pytest}
  e2e: {enabled: true, name: playwright}
generation: {prd_input_mode: prompt, e2e: {default_pom: false}}
execution: {entry: cli, self_healing: {mode: proposal-only}}
coverage: {enabled: false, gate_mode: warn, threshold: {line: 70, branch: 60}}
performance: {enabled: false}
"""


def _pass_manifest():
    return SimpleNamespace(
        final_status="PASS",
        batch_id="20260715-000001-deadbeef",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        result_files={},
    )


@pytest.fixture
def guarded_project(tmp_path: Path, monkeypatch):
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text(_CONFIG, encoding="utf-8")
    test_file = tmp_path / "tests/api/test_x.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_x(): assert 1\n", encoding="utf-8")
    change_dir = tmp_path / "qa/changes/CH-1"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets: {api: true, e2e: false, fuzz: false, performance: false}\n",
        encoding="utf-8",
    )
    tree = hash_test_tree(tmp_path)
    execution = change_dir / "execution"
    execution.mkdir()
    (execution / "execution-manifest.yaml").write_text(
        yaml.safe_dump({
            "batch_id": "20260101-000000",
            "tests_tree_sha256": tree.aggregate,
            "test_files_sha256": tree.files,
            "product_tree_sha256": "p0",
            "final_status": "PASS",
            "result_files": {},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_cmd_mod, "generate_batch_id", lambda: "20260715-000001-deadbeef")
    monkeypatch.chdir(tmp_path)
    return tmp_path, change_dir


def test_run_guard_blocks_tampered_tests_before_runner(guarded_project, monkeypatch) -> None:
    root, _change = guarded_project
    calls = []
    monkeypatch.setattr(run_cmd_mod, "run_change", lambda *_a, **_k: calls.append("run"))
    (root / "tests/api/test_x.py").write_text("def test_x(): assert 2\n")
    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])
    assert result.exit_code != 0
    assert calls == []
    assert "TESTS-CHANGED-WITHOUT-HEALING" in result.output


def test_allow_test_changes_requires_reason(guarded_project) -> None:
    root, _change = guarded_project
    (root / "tests/api/test_x.py").write_text("def test_x(): assert 2\n")
    result = CliRunner().invoke(main, ["run", "--change", "CH-1", "--allow-test-changes"])
    assert result.exit_code != 0
    assert "rerun-reason" in result.output.lower()


def test_override_writes_evidence_and_human_decision_before_runner(
    guarded_project, monkeypatch
) -> None:
    root, change_dir = guarded_project
    observed = []
    monkeypatch.setattr(
        run_cmd_mod,
        "run_change",
        lambda *_a, **_k: observed.append([e["type"] for e in read_events(change_dir)]) or _pass_manifest(),
    )
    (root / "tests/api/test_x.py").write_text("def test_x(): assert 2\n")
    result = CliRunner().invoke(
        main,
        ["run", "--change", "CH-1", "--allow-test-changes", "--rerun-reason", "manual fix"],
    )
    assert result.exit_code == 0
    assert "human_decision" in observed[0]
    assert list((change_dir / "execution/runs").rglob("test-changes-override.json"))


def test_override_event_failure_removes_evidence_and_does_not_run(
    guarded_project, monkeypatch
) -> None:
    root, change_dir = guarded_project
    calls = []
    monkeypatch.setattr(run_cmd_mod, "run_change", lambda *_a, **_k: calls.append("run"))
    monkeypatch.setattr(
        run_cmd_mod,
        "append_event_strict",
        lambda *_a, **_k: (_ for _ in ()).throw(EventWriteError("simulated")),
    )
    (root / "tests/api/test_x.py").write_text("def test_x(): assert 2\n")
    result = CliRunner().invoke(
        main,
        ["run", "--change", "CH-1", "--allow-test-changes", "--rerun-reason", "manual fix"],
    )
    assert result.exit_code != 0
    assert calls == []
    assert not list((change_dir / "execution/runs").rglob("test-changes-override.json"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/healing tests/unit/commands/test_run_guard.py tests/integration/test_cli_heal_record_apply.py -v`
Expected: FAIL（模块/命令尚未创建，且内部 helper 尚未自校验 change-id）。

- [ ] **Step 3: 实现四个模块并接线（逐项使用下方确定代码与边界）**

`tree_hash.py` 的完整实现：

```python
# assurance_agent/workflow/execution/tree_hash.py
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TreeHash:
    aggregate: str
    files: dict[str, str]


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _hash_roots(project_root: Path, roots: tuple[str, ...] | list[str]) -> TreeHash:
    files: dict[str, str] = {}
    for root in roots:
        root_path = project_root / root
        if not root_path.is_dir():
            continue
        for path in sorted(root_path.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            digest = sha256_file(path)
            if digest is not None:
                files[path.relative_to(project_root).as_posix()] = digest
    canonical = "\n".join(f"{name}:{digest}" for name, digest in sorted(files.items()))
    return TreeHash(aggregate=hashlib.sha256(canonical.encode()).hexdigest(), files=files)


def hash_test_tree(project_root: Path, roots: tuple[str, ...] = ("tests",)) -> TreeHash:
    return _hash_roots(project_root, roots)


def hash_product_tree(project_root: Path, roots: list[str]) -> TreeHash:
    return _hash_roots(project_root, roots)


def diff_trees(baseline: dict[str, str], current: dict[str, str]) -> list[str]:
    return sorted(key for key in set(baseline) | set(current) if baseline.get(key) != current.get(key))
```

override writer 必须验证 change-id 与 batch-dir 边界后才能创建文件；完整实现如下：

```python
# assurance_agent/workflow/healing/override_evidence.py
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.execution.tree_hash import TreeHash


class OverrideEvidenceError(AaError):
    pass


@dataclass(frozen=True)
class OverrideEvidenceResult:
    rel_path: str
    sha256: str
    changed_files_count: int


def _validated_batch_dir(project_root: Path, change_id: str, batch_dir: Path) -> Path:
    assert_change_id_safe(change_id)
    change_dir = project_root / "qa" / "changes" / change_id
    runs_root = (change_dir / "execution/runs").resolve()
    resolved = batch_dir.resolve()
    if not resolved.is_relative_to(runs_root):
        raise OverrideEvidenceError("override evidence batch_dir must stay under change execution/runs")
    if not resolved.is_dir() or not (resolved / ".reservation").is_file():
        raise OverrideEvidenceError("override evidence requires an active batch reservation")
    return resolved


def write_test_changes_override_evidence(
    *,
    project_root: Path,
    change_id: str,
    batch_id: str,
    batch_dir: Path,
    reason: str,
    integrity: TreeHash,
    created_at: str | None = None,
) -> OverrideEvidenceResult:
    batch_dir = _validated_batch_dir(project_root, change_id, batch_dir)
    if batch_dir.name != batch_id:
        raise OverrideEvidenceError("override evidence batch id/path mismatch")
    payload = {
        "change_id": change_id,
        "batch_id": batch_id,
        "reason": reason,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "tests_tree_sha256": integrity.aggregate,
        "test_files_sha256": integrity.files,
        "changed_files_count": len(integrity.files),
    }
    json_text = json.dumps(payload, indent=2)
    json_path = batch_dir / "test-changes-override.json"
    json_path.write_text(json_text, encoding="utf-8")
    try:
        diff = subprocess.run(
            ["git", "diff", "--", "tests/"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        diff = ""
    (batch_dir / "test-changes-override.diff").write_text(diff or "", encoding="utf-8")
    return OverrideEvidenceResult(
        rel_path=f"runs/{batch_id}/test-changes-override.json",
        sha256=hashlib.sha256(json_text.encode()).hexdigest(),
        changed_files_count=len(integrity.files),
    )
```

`safety.py` 的完整实现如下；`_change_dir` 是唯一 change-id 路径入口，projection 只调用 M3：

```python
# assurance_agent/workflow/healing/safety.py
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel

from assurance_agent.config import load_config
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.core.events import (
    HealRecordApplyEvent,
    append_event_strict,
    read_events,
)
from assurance_agent.workflow.core.snapshot import capture_files, restore_files
from assurance_agent.workflow.execution.tree_hash import (
    diff_trees,
    hash_product_tree,
    hash_test_tree,
    sha256_file,
)
from assurance_agent.workflow.orchestration.healing_state import (
    HealingStateSnapshot,
    derive_healing_state,
)

_DEFAULT_PRODUCT_ROOTS = ["app", "web/src", "src"]


class HealingGuardError(AaError):
    pass


class TestTreeIntegrity(BaseModel):
    tests_changed: bool
    changed_files: list[str]


class ProductTreeIntegrity(BaseModel):
    product_changed: bool
    changed_files: list[str]


@dataclass(frozen=True)
class HealingGuardContext:
    snapshot: HealingStateSnapshot
    source_batch_id: str | None
    proposal_sha256: str | None
    attempt_key: str | None


class RecordApplySummaryResult(BaseModel):
    json_path: str
    md_path: str
    summary_sha256: str
    files_modified: list[str]


def _change_dir(project_root: Path, change_id: str) -> Path:
    assert_change_id_safe(change_id)
    return project_root / "qa" / "changes" / change_id


def derive_guard_context(project_root: Path, change_id: str) -> HealingGuardContext:
    change_dir = _change_dir(project_root, change_id)
    snapshot = derive_healing_state(change_dir)
    allocations = [
        event for event in read_events(change_dir)
        if event.get("type") == "healing_attempt_allocated"
        and event.get("episode_id") == snapshot.episode_id
    ]
    latest = max(
        allocations,
        key=lambda event: int(event.get("seq", 0)),
        default=None,
    )
    proposal_sha = sha256_file(change_dir / "healing/fix-proposal.json")
    source_batch = str(latest["source_batch_id"]) if latest is not None else None
    return HealingGuardContext(
        snapshot=snapshot,
        source_batch_id=source_batch,
        proposal_sha256=proposal_sha,
        attempt_key=(f"{proposal_sha}:{source_batch}" if proposal_sha and source_batch else None),
    )


def is_healing_run_context(project_root: Path, change_id: str) -> bool:
    snapshot = derive_healing_state(_change_dir(project_root, change_id))
    return snapshot.episode_id is not None and snapshot.status != "not_needed"


def load_product_code_roots(project_root: Path) -> list[str]:
    try:
        execution = getattr(load_config(project_root), "execution", None)
        roots = getattr(execution, "product_code_roots", None)
        if isinstance(roots, list) and roots:
            return [str(root) for root in roots]
    except AaError:
        pass
    return list(_DEFAULT_PRODUCT_ROOTS)


def _load_manifest_hashes(change_dir: Path) -> tuple[str | None, dict[str, str], str | None]:
    path = change_dir / "execution/execution-manifest.yaml"
    if not path.is_file():
        return None, {}, None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None, {}, None
    files = raw.get("test_files_sha256") or {}
    return (
        str(raw["tests_tree_sha256"]) if raw.get("tests_tree_sha256") else None,
        {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {},
        str(raw["product_tree_sha256"]) if raw.get("product_tree_sha256") else None,
    )


def assert_test_tree_unchanged_or_healing(
    project_root: Path,
    change_id: str,
    *,
    allow_test_changes: bool = False,
) -> TestTreeIntegrity:
    change_dir = _change_dir(project_root, change_id)
    baseline_sha, baseline_files, _ = _load_manifest_hashes(change_dir)
    if baseline_sha is None:
        return TestTreeIntegrity(tests_changed=False, changed_files=[])
    current = hash_test_tree(project_root)
    changed = diff_trees(baseline_files, current.files)
    if not changed and current.aggregate == baseline_sha:
        return TestTreeIntegrity(tests_changed=False, changed_files=[])
    if allow_test_changes:
        return TestTreeIntegrity(tests_changed=True, changed_files=changed)
    if not is_healing_run_context(project_root, change_id):
        raise HealingGuardError(
            f"TESTS-CHANGED-WITHOUT-HEALING: {len(changed)} test file(s) changed since last run"
        )
    snapshot = derive_healing_state(change_dir)
    if snapshot.status == "applied":
        pin_path = change_dir / "healing/applied-test-tree.json"
        try:
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise HealingGuardError(
                f"TESTS-CHANGED-WITHOUT-HEALING: missing/unreadable applied-test-tree pin: {err}"
            ) from err
        if str(pin.get("aggregate", "")) != current.aggregate:
            raise HealingGuardError(
                "TESTS-CHANGED-WITHOUT-HEALING: applied test-tree pin mismatch"
            )
    return TestTreeIntegrity(tests_changed=True, changed_files=changed)


def assert_product_tree_unchanged_in_healing(
    project_root: Path,
    change_id: str,
    *,
    roots: list[str] | None = None,
) -> ProductTreeIntegrity:
    change_dir = _change_dir(project_root, change_id)
    if not is_healing_run_context(project_root, change_id):
        return ProductTreeIntegrity(product_changed=False, changed_files=[])
    _, _, baseline_sha = _load_manifest_hashes(change_dir)
    if baseline_sha is None:
        return ProductTreeIntegrity(product_changed=False, changed_files=[])
    current = hash_product_tree(
        project_root,
        roots if roots is not None else load_product_code_roots(project_root),
    )
    if current.aggregate == baseline_sha:
        return ProductTreeIntegrity(product_changed=False, changed_files=[])
    raise HealingGuardError(
        "PRODUCT-CHANGED-DURING-HEALING: product code tree changed during healing"
    )


def pin_healing_applied_test_tree(project_root: Path, change_id: str) -> Path:
    change_dir = _change_dir(project_root, change_id)
    path = change_dir / "healing/applied-test-tree.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = hash_test_tree(project_root)
    path.write_text(
        json.dumps({"aggregate": current.aggregate, "files": current.files}, indent=2),
        encoding="utf-8",
    )
    return path


def write_cli_fixer_safety_check(change_dir: Path, payload: dict) -> Path:
    path = change_dir / "healing/fixer-safety-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _authorized_files(raw: dict, proposal_ids: list[str], target: str) -> set[str]:
    authorized: set[str] = set()
    for proposal in raw.get("proposals", []):
        if not isinstance(proposal, dict):
            continue
        if (
            str(proposal.get("proposal_id", "")) in proposal_ids
            and str(proposal.get("target", "")) == target
            and proposal.get("eligible", False)
        ):
            authorized.update(
                str(path).replace("\\", "/")
                for path in proposal.get("files_to_modify", [])
            )
    return authorized


def record_apply_summary(
    project_root: Path,
    change_id: str,
    target: str,
    proposal_ids: list[str],
) -> RecordApplySummaryResult:
    change_dir = _change_dir(project_root, change_id)
    if target not in {"api", "e2e"}:
        raise HealingGuardError(f"unsupported heal target: {target}")
    if not proposal_ids or any(proposal_id == "all" for proposal_id in proposal_ids):
        raise HealingGuardError("explicit proposal ids are required")
    context = derive_guard_context(project_root, change_id)
    if context.source_batch_id is None or context.attempt_key is None or context.proposal_sha256 is None:
        raise HealingGuardError("no active healing allocation for record-apply")
    proposal_path = change_dir / "healing/fix-proposal.json"
    try:
        proposal_raw = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise HealingGuardError(f"fix-proposal.json unreadable: {err}") from err
    authorized = _authorized_files(proposal_raw, proposal_ids, target)
    if not authorized:
        raise HealingGuardError("no authorized proposals matched the given proposal ids")
    _, baseline_files, _ = _load_manifest_hashes(change_dir)
    current = hash_test_tree(project_root)
    changed = diff_trees(baseline_files, current.files)
    unauthorized = [path for path in changed if path not in authorized]
    if unauthorized:
        raise HealingGuardError(
            f"modified files outside authorized proposals: {', '.join(unauthorized)}"
        )
    modified = [path for path in changed if path in authorized]
    summary = {
        "schema_version": "1.0",
        "target": target,
        "applied": bool(modified),
        "proposal_ids": proposal_ids,
        "files_modified": modified,
        "source_batch_id": context.source_batch_id,
        "attempt_key": context.attempt_key,
    }
    summary_text = json.dumps(summary, indent=2)
    md_text = "\n".join([
        f"# Apply Summary — {target}", "", f"- Applied: {bool(modified)}",
        f"- Files modified: {len(modified)}", *(f"- {path}" for path in modified), "",
    ])
    json_path = change_dir / f"healing/{target}-apply-summary.json"
    md_path = change_dir / f"healing/{target}-apply-summary.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    snapshots = capture_files((json_path, md_path, change_dir / "events.jsonl"))
    try:
        json_path.write_text(summary_text, encoding="utf-8")
        md_path.write_text(md_text, encoding="utf-8")
        summary_sha = hashlib.sha256(summary_text.encode()).hexdigest()
        append_event_strict(change_dir, HealRecordApplyEvent(
            target=target,  # type: ignore[arg-type]
            proposal_sha256=context.proposal_sha256,
            source_batch_id=context.source_batch_id,
            attempt_key=context.attempt_key,
            summary_sha256=summary_sha,
            files_modified=modified,
        ))
    except Exception:  # noqa: BLE001 - file/event transaction restores every snapshot
        restore_files(snapshots)
        raise
    return RecordApplySummaryResult(
        json_path=str(json_path),
        md_path=str(md_path),
        summary_sha256=summary_sha,
        files_modified=modified,
    )
```

`run_cmd._execute` 的 reservation/override 顺序必须精确如下；这段替换原有“先向未预留目录写 override，再让 runner 建目录”的竞态实现：

```python
reservation: BatchReservation | None = None
if integrity.tests_changed and allow_test_changes:
    if not rerun_reason:
        raise HealingGuardError("--allow-test-changes requires --rerun-reason for audit trail")
    batch_id = generate_batch_id()
    reservation = reserve_execution_batch(change_dir / "execution", batch_id)
    override_json = reservation.path / "test-changes-override.json"
    override_diff = reservation.path / "test-changes-override.diff"
    events_path = change_dir / "events.jsonl"
    snapshots = capture_files((override_json, override_diff, events_path))
    try:
        evidence = write_test_changes_override_evidence(
            project_root=project_root,
            change_id=change_id,
            batch_id=batch_id,
            batch_dir=reservation.path,
            reason=rerun_reason,
            integrity=hash_test_tree(project_root),
        )
        append_event_strict(
            change_dir,
            HumanDecisionEvent(
                checkpoint="test-tree-guard",
                action="allow_test_changes",
                reason=rerun_reason,
                who="cli",
                review_file=evidence.rel_path,
                review_sha256=evidence.sha256,
            ),
        )
    except (EventWriteError, OSError):
        restore_files(snapshots)
        raise

manifest = run_change(
    project_root,
    change_dir,
    config,
    reservation=reservation,
)
```

`run_cmd.py` 为此显式 import `BatchReservation`、`reserve_execution_batch`；没有 override 时传 `None`，由 `run_change` 自己生成并预留批次。任何路径都只能创建一次 reservation。

同时从 M4 import `EXIT_ERROR`，把 `run_command` 与 `heal record-apply` 捕获 `AaError` / `HealingGuardError` / `UnsafeIdentifierError` 的分支统一为 `raise SystemExit(EXIT_ERROR)`；Click 缺参仍由 Click 自己返回 2。

新增 CLI 穿越回归测试：

```python
# tests/integration/test_cli_heal_record_apply.py
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def test_record_apply_rejects_unsafe_change_before_writing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        ["heal", "record-apply", "--change", "../outside", "--target", "api", "--proposal", "FIX-1"],
    )
    assert result.exit_code != 0
    assert "change-id" in result.output.lower()
    assert not (tmp_path / "qa" / "outside").exists()
```

- [ ] **Step 4: 跑测试确认通过 + 质量门禁 + Commit**

```bash
uv run pytest tests/unit/healing tests/unit/commands/test_run_guard.py tests/integration/test_cli_heal_record_apply.py -v
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/execution/tree_hash.py assurance_agent/workflow/healing \
        assurance_agent/workflow/execution/runner.py assurance_agent/commands/run_cmd.py \
        assurance_agent/commands/heal_cmd.py tests/unit/healing tests/unit/commands/test_run_guard.py \
        tests/integration/test_cli_heal_record_apply.py
git commit -m "feat: add healing safety boundary (tree hash, change guards, override evidence, record-apply)"
```

---

### Task 11: `aa report reclassify` + inspector compat_fallback 读取模式

> **P1 修订**：`aa report reclassify` 是一一对应迁移的子命令；`compat_fallback` 是 inspector 在顶层 manifest
> 指向的批次缺失/不可读时回退读取历史兼容批次的安全读取路径（对齐 TS `commands/report.ts` + inspector）。

**Files:**
- Modify: `assurance_agent/workflow/report/inspector.py`（支持 `inspect_mode ∈ {"primary","compat_fallback"}`）
- Modify: `assurance_agent/commands/report_cmd.py`（新增 `reclassify` 子命令）
- Test: `tests/unit/report/test_compat_fallback.py`、`tests/unit/commands/test_report_reclassify.py`

**Interfaces:**
- `inspect_change(project_root, change_id, *, batch_id: str | None = None)`：默认先读顶层 manifest；若读取报 `EvidenceError`，或 evidence 有 `integrity_issues` / 缺 `quality_gate`，按 batch-id 倒序尝试 `runs/<batch>/execution-manifest.yaml`，选择首个完整批次并把 `FailureAnalysis.inspect_mode="compat_fallback"`、`compat_fallback_reason=<primary failure>`。无完整批次则抛原始错误。显式 `batch_id` 时**不 fallback**，避免把用户指定批次悄悄换成别的批次。任何 fallback 都只影响读取与新生成的 `inspect/*`，绝不改写 execution 顶层 pointer。
- `aa report reclassify --change <id> [--batch <batch-id>]`——对指定批次（缺省为顶层当前批次）重新执行确定性分类，不运行 SUT；输出仍是 canonical `inspect/failure-analysis.json` / `quality-gate-result.json`，其中 `source_batch_id` 明确指向实际读取批次。成功后写 best-effort `reclassified` telemetry；退出码同 `aa report inspect`。不得宣称存在未定义的 per-batch `inspect/` 目录。

- [ ] **Step 1: 写失败测试**

`tests/unit/report/test_compat_fallback.py` 必须包含以下三个独立用例（每个用 `publish_execution_evidence` 建两个真实 batch，不手写不完整 artifact）：

```python
def test_primary_batch_remains_primary_when_complete(tmp_path: Path) -> None:
    _publish(tmp_path, "20260715-000001", failed=False)
    result = inspect_change(tmp_path, "CH-1")
    assert result.analysis.inspect_mode == "primary"
    assert result.analysis.compat_fallback_reason is None
    assert result.analysis.source_batch_id == "20260715-000001"


def test_corrupt_latest_pointer_falls_back_to_newest_complete_batch(tmp_path: Path) -> None:
    _publish(tmp_path, "20260715-000001", failed=True)
    _publish(tmp_path, "20260715-000002", failed=False)
    latest = tmp_path / "qa/changes/CH-1/execution/runs/20260715-000002/api-result.json"
    latest.unlink()
    result = inspect_change(tmp_path, "CH-1")
    assert result.analysis.inspect_mode == "compat_fallback"
    assert result.analysis.compat_fallback_reason
    assert result.analysis.source_batch_id == "20260715-000001"


def test_explicit_bad_batch_fails_without_fallback(tmp_path: Path) -> None:
    _publish(tmp_path, "20260715-000001", failed=False)
    with pytest.raises(EvidenceError):
        inspect_change(tmp_path, "CH-1", batch_id="20260715-999999")
```

`tests/unit/commands/test_report_reclassify.py` 必须先 seed 一个 failed batch，再 monkeypatch 分类规则或原始错误文本使分类变化，并断言：命令不调用 runner、退出码与重算 gate 一致、canonical analysis 的 `source_batch_id` 等于 `--batch`、目标 failure 的 `reclassified` 字段更新、telemetry 使用 best-effort writer。另加 unsafe batch id（`../x`）退出 1 的用例。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/report/test_compat_fallback.py tests/unit/commands/test_report_reclassify.py -v`
Expected: FAIL（缺 `batch_id` 参数 / `reclassify` 子命令）

- [ ] **Step 3: 实现可读批次选择与 reclassify**

```python
# inspector.py — 唯一 fallback 选择器
def _load_for_inspection(execution_dir: Path, batch_id: str | None):
    if batch_id is not None:
        return load_execution_evidence(execution_dir, batch_id=batch_id), "primary", None
    primary_error: str | None = None
    try:
        primary = load_execution_evidence(execution_dir)
        if not primary.integrity_issues and primary.quality_gate is not None:
            return primary, "primary", None
        primary_error = "latest execution evidence is incomplete"
    except EvidenceError as err:
        primary_error = str(err)
    runs = execution_dir / "runs"
    candidates = (p.name for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else ()
    for candidate in sorted(candidates, reverse=True):
        try:
            evidence = load_execution_evidence(execution_dir, batch_id=candidate)
        except EvidenceError:
            continue
        if not evidence.integrity_issues and evidence.quality_gate is not None:
            return evidence, "compat_fallback", primary_error
    raise EvidenceError(primary_error or "no readable execution evidence")
```

将 `inspect_change` 的读取改为该 helper，并把 mode/reason 传入 `_analysis`；所有 `_analysis` 分支都必须保留这两个字段。`report_cmd.py` 新增 `reclassify_cmd`：先读取旧 canonical analysis（若同一 source batch），再调用 `inspect_change(Path.cwd(), change_id, batch_id=batch_id)`；以 `id`（缺失时用 `target+case_id`）匹配前后 failure，category 变化时写 `Reclassified(from=<旧 category>, evidence="deterministic rules re-run", at=<UTC ISO8601>)` 并原子重写 analysis。最后 `append_event_best_effort(change_dir, {"source":"report","type":"reclassified","batch_id": result.analysis.source_batch_id})`；不得调用 `run_change`。

- [ ] **Step 4: 门禁 + Commit**

Run: `uv run pytest tests/unit/report/test_compat_fallback.py tests/unit/commands/test_report_reclassify.py -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全绿

```bash
git add assurance_agent/workflow/report/inspector.py assurance_agent/commands/report_cmd.py \
        tests/unit/report/test_compat_fallback.py tests/unit/commands/test_report_reclassify.py
git commit -m "feat: add aa report reclassify and inspector compat_fallback read mode"
```

---

## 里程碑完成校验（DoD）

- [ ] `uv run pytest tests/unit/execution tests/unit/report tests/unit/commands tests/unit/healing -v` 全绿。
- [ ] `uv run ruff check . && uv run pyright && uv run lint-imports` 全过。
- [ ] `aa run --change <id> [--allow-test-changes --rerun-reason <text>]` / `aa report inspect|generate|reclassify --change <id>` / `aa heal validate-proposal|eligibility-summary|safety-check|record-apply --change <id>` 均可运行并给出正确退出码。
- [ ] Healing 安全边界生效：非 healing 期篡改测试树被 `aa run` 拒绝（退出 1）；healing 期改产品树被拒绝；`--allow-test-changes` 走 override evidence 留痕；manifest 写入 `tests_tree_sha256`/`test_files_sha256`/`product_tree_sha256`。
- [ ] 新增文件无 `aws` 残留（面向用户文案改用 `aa`）。
- [ ] 产物落盘符合 spec 4/6：`execution/runs/<batch-id>/` append-only + 顶层最新指针 + `inspect/` + `report/` 三件套。
- [ ] 全部产物模型来自 M2 `artifacts.models`，execution/report 内未私开重复结构。
