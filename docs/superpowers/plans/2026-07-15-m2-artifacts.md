# M2 — 产物契约（artifacts/）与 `aa validate` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `assurance_agent/artifacts/`——全项目唯一的结构化产物契约来源（14 个 zod 验证器逐一迁移为 pydantic v2 模型 + 路径注册表），并交付 `aa validate` 命令。

**Architecture:** `artifacts/models/` 按产物领域分文件存放 pydantic 模型（cases / review / explore / execution / state / inspect / report / healing），`models/__init__.py` 统一 re-export；`artifacts/registry.py` 把 change 相对路径 glob 映射到模型并标注兼容性分级；`artifacts/validate.py` 扫描 `qa/changes/<id>/` 逐文件校验并产出 `ValidationReport`；`commands/validate_cmd.py` 只做参数解析、输出与退出码。M3 尚未交付 workflow schema loader，`--phase` 过滤所需的 produces 声明在本里程碑经一条**显式接缝**取得：`validate_change` 接受实现 `WorkflowSchemaLike` 协议的对象，缺省时对打包的 `_resources/schemas/workflow-schema.yaml` 做一次薄 YAML 读取（只取 `phases[].id` 与 `phases[].produces`，不实现任何 M3 语义）。

**Tech Stack:** Python 3.11+, uv, click, pydantic v2, PyYAML, pytest, ruff, pyright, import-linter。

## Global Constraints

- CLI 命令名 `aa`；Python 包名 `assurance_agent`；项目配置目录 `.aa/`。新文件中不得残留 `aws` 字样。
- 工具链固定：uv + pyproject.toml + pydantic v2 + click + ruff + pyright + pytest。
- TS 源仓库 `/Users/lvqingquan/skills/assurance-workflow-skills` **仅作规则参考**：只提取字段名、可选性、枚举取值与校验规则，不复制实现。
- `assurance_agent/artifacts/` 是全项目唯一的产物契约来源；后续里程碑（gate 求值、report、eval）必须 import 这里的模型，禁止私开字典解析。
- 分层约束（.importlinter，本计划 Task 8 更新）：`cli → commands → workflow → artifacts → config → resources`。`workflow/` 可消费 canonical artifact/state 模型；`artifacts/` 禁止反向 import `workflow`，但可 import `config`、`resources`、`exceptions`。
- 包内资源只经 `assurance_agent/resources.py` 访问（M1 已落地：`read_text` / `exists` / `iter_children`），禁止 `Path(__file__)` 相对路径。
- 每个 Task 结束必须通过 `uv run ruff check .` 与 `uv run pyright`，然后 `git commit`；提交信息用 conventional commits（feat/test/chore/docs）。
- 本计划中所有 pytest 命令在仓库根目录运行：`uv run pytest <path> -v`。

## 文件结构与注册表总览

新建文件及职责：

```
assurance_agent/artifacts/
├── __init__.py              # 空
├── models/
│   ├── __init__.py          # 全部模型 re-export（每个 Task 增补）
│   ├── common.py            # GateStatus 等共享 Literal + 维度小模型
│   ├── cases.py             # CaseYaml、QaYaml
│   ├── review.py            # Review
│   ├── explore.py           # Advisory、FactBaseline
│   ├── execution.py         # SelectedTargets、ExecutionManifest
│   ├── state.py             # WorkflowState
│   ├── inspect.py           # FailureAnalysis、QualityGateResult
│   ├── report.py            # QualityReport
│   └── healing.py           # FixProposal、ApplySummary、SafetyCheck
├── registry.py              # ArtifactSpec、REGISTRY、match_artifact
└── validate.py              # ArtifactResult、ValidationReport、validate_change
assurance_agent/commands/validate_cmd.py   # aa validate
assurance_agent/identifiers.py             # 全命令面共享的 change-id 路径安全合同
```

路径 → 模型注册表（glob 与 artifact_type 逐条对齐 TS 源 `src/schema/index.ts` 的 `ARTIFACT_SPECS`；`safety_check` 是有意的增补——`fixer-safety-gate` 读取该 JSON，且计划系列总览把 `SafetyCheck` 列为 M3+ 消费的核心模型，TS 注册表漏收它）：

| artifact_type | pattern（change 相对 glob） | 模型 | compat | TS 规则来源 |
|---|---|---|---|---|
| case_yaml | `cases/**/case.yaml` | CaseYaml | must_compat | src/schema/case_yaml.ts |
| qa_yaml | `.qa.yaml` | QaYaml | must_compat | src/schema/qa_yaml.ts |
| execution_manifest | `execution/execution-manifest.yaml` | ExecutionManifest | versioned | src/schema/execution_manifest.ts |
| failure_analysis | `inspect/failure-analysis.json` | FailureAnalysis | must_compat | src/schema/failure_analysis.ts |
| quality_gate_result | `inspect/quality-gate-result.json` | QualityGateResult | versioned | src/schema/quality_gate_result.ts |
| quality_report | `report/quality-report.json` | QualityReport | versioned | src/schema/quality_report.ts |
| fix_proposal | `healing/fix-proposal.json` | FixProposal | must_compat | src/schema/fix_proposal.ts |
| apply_summary | `healing/*-apply-summary.json` | ApplySummary | must_compat | src/schema/apply_summary.ts |
| safety_check | `healing/fixer-safety-check.json` | SafetyCheck | must_compat | src/workflow/core/healing_state.ts 的落盘 payload + fixer-safety-gate 表达式 |
| review | `review/*.json` | Review | must_compat | src/schema/review.ts + workflow-schema.yaml 各 review gate |
| fact_baseline | `facts/fact-baseline.json` | FactBaseline | must_compat | src/schema/fact_baseline.ts |
| advisory | `explore/advisory.json` | Advisory | must_compat | src/schema/advisory.ts |
| workflow_state | `workflow-state.yaml` | WorkflowState | versioned | src/schema/workflow_state.ts |

兼容性分级依据（spec 4a）：skill 直接读写、gate 表达式直接引用字段的产物为 `must_compat`（字段名与枚举取值不得改）；带 `schema_version` 且允许结构演进的 CLI 产物为 `versioned`。`free` 级产物（报告 markdown、events 扩展字段等）本就不进注册表，故 REGISTRY 中没有 `free` 条目。

两处相对 TS zod 验证器的**有意收紧**（must_compat 字段按 SKILL.md / workflow-schema.yaml 的引用做枚举约束，spec 4a 要求）：

1. `Review.decision` 从自由字符串收紧为枚举 `pass|approved|needs_fix|needs_human_review|changes_requested|reject`（workflow-schema.yaml 各 review gate 引用的全部取值）。
2. `FixProposal.summary`（含 `eligible_count`）从「未声明」提升为必填——healing loop 的 `allocate_on` 表达式直接引用 `fix_proposal.summary.eligible_count`，aws-fix-proposal SKILL.md 也始终要求写入。

---

### Task 1: 共享类型 + case 产物模型（CaseYaml、QaYaml）

**Files:**
- Create: `assurance_agent/artifacts/__init__.py`（空）
- Create: `assurance_agent/artifacts/models/__init__.py`
- Create: `assurance_agent/artifacts/models/common.py`
- Create: `assurance_agent/artifacts/models/cases.py`
- Create: `tests/unit/artifacts/__init__.py`（空）
- Test: `tests/unit/artifacts/test_models_cases.py`

**Interfaces:**
- Consumes: 无（本里程碑首个任务）。
- Produces: `common.py` 的 `GateStatus`（`Literal["PASS","PASS_WITH_WARNINGS","FAIL","SKIPPED"]`）、`NonEmptyStr`、`CaseId`、`FunctionalCounts`、`CoverageThreshold`、`FunctionalDimension`、`CoverageDimension`——Task 3/4/5 全部消费；`cases.py` 的 `CaseYaml`、`QaYaml`——Task 6 注册表消费。所有模型经 `assurance_agent.artifacts.models` 包级 import 使用。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/artifacts/test_models_cases.py
import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import CaseYaml, QaYaml


def make_case_entry(**overrides: object) -> dict:
    entry: dict = {
        "case_id": "TC_MENU_001",
        "title": "create menu happy path",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": "API",
        "module": "menus",
    }
    entry.update(overrides)
    return entry


def make_case_yaml(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "added": [make_case_entry()],
        "modified": [make_case_entry(case_id="TC_MENU_002", status="draft")],
        "removed": [{"case_id": "TC_MENU_003"}],
    }
    doc.update(overrides)
    return doc


def make_qa_yaml(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "schema": "qa-yaml/v1",
        "created_at": "2026-07-15T00:00:00Z",
        "change": {
            "change_id": "CH-1",
            "requirement_id": "REQ-1",
            "feature_name": "menu management",
            "status": "in_progress",
        },
        "targets": {
            "cases": [
                {
                    "module": "menus",
                    "change_case_file": "cases/menus/case.yaml",
                    "target_case_file": "qa/cases/menus/case.yaml",
                }
            ]
        },
    }
    doc.update(overrides)
    return doc


def test_case_yaml_valid_fixture_parses() -> None:
    model = CaseYaml.model_validate(make_case_yaml())
    assert model.added[0].case_id == "TC_MENU_001"
    assert model.modified[0].priority == "P1"
    assert model.removed[0].case_id == "TC_MENU_003"


def test_case_yaml_hyphen_case_id_rejected() -> None:
    doc = make_case_yaml(added=[make_case_entry(case_id="TC-MENU-001")])
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(doc)


def test_case_yaml_bad_priority_enum_rejected() -> None:
    doc = make_case_yaml(added=[make_case_entry(priority="P9")])
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(doc)


def test_case_yaml_missing_required_field_rejected() -> None:
    entry = make_case_entry()
    del entry["title"]
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(make_case_yaml(added=[entry]))


def test_qa_yaml_valid_fixture_parses() -> None:
    model = QaYaml.model_validate(make_qa_yaml())
    assert model.change.change_id == "CH-1"
    assert model.schema_ == "qa-yaml/v1"
    assert model.targets.cases[0].module == "menus"
    assert model.workflow is None


def test_qa_yaml_missing_change_id_rejected() -> None:
    doc = make_qa_yaml()
    del doc["change"]["change_id"]
    with pytest.raises(ValidationError):
        QaYaml.model_validate(doc)


def test_qa_yaml_workflow_optional_fields_parse() -> None:
    doc = make_qa_yaml(workflow={"current_step": "case-design", "next_step": "case-review"})
    model = QaYaml.model_validate(doc)
    assert model.workflow is not None
    assert model.workflow.next_step == "case-review"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/artifacts/test_models_cases.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'assurance_agent.artifacts'`

- [ ] **Step 3: 实现 common.py 与 cases.py**

```python
# assurance_agent/artifacts/models/common.py
"""Shared literals and dimension sub-models used across artifact models.

Enum values transcribed from the TS source `src/schema/contracts.ts`
(GateStatus, FunctionalCounts, CoverageThreshold, dimensions).
"""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

GateStatus = Literal["PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"]
ReportRiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
NonEmptyStr = Annotated[str, Field(min_length=1)]
# TS case_yaml.ts: case_id must be underscore-only (hyphens not allowed).
CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]


class FunctionalCounts(BaseModel):
    total: int
    passed: int
    failed: int


class CoverageThreshold(BaseModel):
    line: float
    branch: float
    module_line: float | None = None
    diff_line: float | None = None


class FunctionalDimension(BaseModel):
    status: GateStatus
    api: FunctionalCounts
    e2e: FunctionalCounts
    fuzz: FunctionalCounts | None = None
    unmapped_tests: int | None = None


class CoverageDimension(BaseModel):
    status: GateStatus
    available: bool
    line_coverage: float
    branch_coverage: float
    threshold: CoverageThreshold
    scope: Any = None
```

```python
# assurance_agent/artifacts/models/cases.py
"""cases/**/case.yaml and .qa.yaml — skill-authored case artifacts (must_compat).

Field names, optionality and enum values transcribed one-for-one from the TS
validators src/schema/case_yaml.ts and src/schema/qa_yaml.ts.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.common import CaseId, NonEmptyStr


class CaseEntry(BaseModel):
    case_id: CaseId
    title: NonEmptyStr
    status: Literal["draft", "active", "deprecated"]
    priority: Literal["P0", "P1", "P2", "P3"]
    severity: Literal["blocker", "critical", "major", "minor"]
    type: Literal["API", "E2E", "Fuzz", "Performance"]
    module: NonEmptyStr


class CaseRemoval(BaseModel):
    case_id: CaseId


class CaseYaml(BaseModel):
    schema_version: NonEmptyStr
    added: list[CaseEntry]
    modified: list[CaseEntry]
    removed: list[CaseRemoval]


class QaChange(BaseModel):
    change_id: NonEmptyStr
    requirement_id: NonEmptyStr
    feature_name: NonEmptyStr
    status: NonEmptyStr


class QaCaseTarget(BaseModel):
    module: NonEmptyStr
    change_case_file: NonEmptyStr
    target_case_file: NonEmptyStr


class QaTargets(BaseModel):
    cases: list[QaCaseTarget]


class QaWorkflow(BaseModel):
    current_step: str | None = None
    next_step: str | None = None


class QaYaml(BaseModel):
    # The artifact has a literal "schema" key; that name shadows a BaseModel
    # attribute, so the field is schema_ with an input alias.
    model_config = ConfigDict(populate_by_name=True)

    schema_version: NonEmptyStr
    schema_: NonEmptyStr = Field(alias="schema")
    created_at: NonEmptyStr
    change: QaChange
    targets: QaTargets
    workflow: QaWorkflow | None = None
```

```python
# assurance_agent/artifacts/models/__init__.py
"""Public surface of the artifact contract models (spec 4a).

Every module that consumes structured artifacts imports from here.
"""
from assurance_agent.artifacts.models.cases import CaseEntry, CaseRemoval, CaseYaml, QaYaml
from assurance_agent.artifacts.models.common import (
    CoverageDimension,
    CoverageThreshold,
    FunctionalCounts,
    FunctionalDimension,
    GateStatus,
    ReportRiskLevel,
)

__all__ = [
    "CaseEntry",
    "CaseRemoval",
    "CaseYaml",
    "CoverageDimension",
    "CoverageThreshold",
    "FunctionalCounts",
    "FunctionalDimension",
    "GateStatus",
    "QaYaml",
    "ReportRiskLevel",
]
```

同时创建两个空文件：`assurance_agent/artifacts/__init__.py`、`tests/unit/artifacts/__init__.py`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/artifacts/test_models_cases.py -v`
Expected: 7 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/artifacts tests/unit/artifacts
git commit -m "feat: add case_yaml and qa_yaml artifact models with shared literals"
```

---

### Task 2: Review + Explore 产物模型（Review、Advisory、FactBaseline）

**Files:**
- Create: `assurance_agent/artifacts/models/review.py`
- Create: `assurance_agent/artifacts/models/explore.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Test: `tests/unit/artifacts/test_models_review_explore.py`

**Interfaces:**
- Consumes: Task 1 的 `NonEmptyStr`。
- Produces: `Review`（含 must_compat 字段 `decision` / `auto_fix_allowed` / `human_review_required` / `codegen_readiness` / `risk_level` / `findings`）、`ReviewDecision`、`Advisory`、`FactBaseline`（RootModel，按 `source` 判别联合）。Task 6 注册表与 M3 gate 求值消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/artifacts/test_models_review_explore.py
import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import Advisory, FactBaseline, Review


def make_review(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "review_type": "api-plan",
        "change_id": "CH-1",
        "decision": "pass",
        "risk_level": "low",
        "codegen_readiness": "ready",
        "auto_fix_allowed": False,
        "human_review_required": False,
        "summary": "Short review summary.",
        "findings": [],
        "next_action": "continue",
    }
    doc.update(overrides)
    return doc


def make_advisory(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "context_ref": "explore/context.json",
        "generated_at": "2026-07-15T00:00:00Z",
        "executive_summary": "risk overview",
        "watchlist": [{"area": "auth"}],
        "evidence_inventory": {},
        "case_design_guidance": {},
        "minimum_required_coverage": {},
        "open_questions_for_case_design": [],
    }
    doc.update(overrides)
    return doc


def test_review_valid_full_fixture_parses_and_keeps_extras() -> None:
    model = Review.model_validate(make_review())
    assert model.decision == "pass"
    assert model.codegen_readiness == "ready"
    assert model.auto_fix_allowed is False
    assert model.human_review_required is False
    assert model.risk_level == "low"
    assert model.findings == []
    assert model.model_extra is not None
    assert model.model_extra["review_type"] == "api-plan"


def test_review_minimal_required_fields() -> None:
    model = Review.model_validate(
        {"schema_version": "1.0", "decision": "needs_fix", "findings": [{"id": "F1"}]}
    )
    assert model.auto_fix_allowed is None
    assert model.codegen_readiness is None


def test_review_decision_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        Review.model_validate(make_review(decision="maybe"))


def test_review_missing_findings_fails() -> None:
    doc = make_review()
    del doc["findings"]
    with pytest.raises(ValidationError):
        Review.model_validate(doc)


def test_review_codegen_readiness_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        Review.model_validate(make_review(codegen_readiness="almost_ready"))


def test_advisory_valid_fixture_parses() -> None:
    model = Advisory.model_validate(make_advisory())
    assert model.schema_version == "1.0"
    assert model.watchlist == [{"area": "auth"}]
    assert model.open_questions_for_case_design == []


def test_advisory_missing_watchlist_fails() -> None:
    doc = make_advisory()
    del doc["watchlist"]
    with pytest.raises(ValidationError):
        Advisory.model_validate(doc)


def test_fact_baseline_unavailable_variant_parses() -> None:
    model = FactBaseline.model_validate(
        {"source": "unavailable", "facts": None, "warnings": ["db unreachable"]}
    )
    assert model.root.source == "unavailable"


def test_fact_baseline_full_variant_requires_schema_version() -> None:
    with pytest.raises(ValidationError):
        FactBaseline.model_validate({"source": "db_probe", "warnings": []})
    model = FactBaseline.model_validate(
        {"source": "db_probe", "schema_version": "1.0", "warnings": [], "facts": {"users": 3}}
    )
    assert model.root.source == "db_probe"


def test_fact_baseline_unknown_source_fails() -> None:
    with pytest.raises(ValidationError):
        FactBaseline.model_validate({"source": "guesswork", "warnings": []})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/artifacts/test_models_review_explore.py -v`
Expected: FAIL with `ImportError`（`Review` 等不存在）

- [ ] **Step 3: 实现 review.py 与 explore.py**

```python
# assurance_agent/artifacts/models/review.py
"""review/*.json — reviewer-skill verdicts read by gates (must_compat).

decision / auto_fix_allowed / human_review_required / codegen_readiness /
risk_level / findings are referenced verbatim by workflow-schema.yaml gate
expressions — never rename them. The decision enum covers every value the
packaged schema's review gates compare against (deliberately stricter than
the TS validator, which accepted any non-empty string).
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models.common import NonEmptyStr

ReviewDecision = Literal[
    "pass", "approved", "needs_fix", "needs_human_review", "changes_requested", "reject"
]


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    decision: ReviewDecision
    findings: list[Any]
    auto_fix_allowed: bool | None = None
    human_review_required: bool | None = None
    codegen_readiness: Literal["ready", "ready_with_warnings", "not_ready"] | None = None
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
```

```python
# assurance_agent/artifacts/models/explore.py
"""explore/advisory.json and facts/fact-baseline.json (must_compat).

Transcribed from src/schema/advisory.ts and src/schema/fact_baseline.ts.
zod's z.any() fields accept absent keys, hence `Any = None` defaults here;
z.array(...) fields are required, hence no default.
"""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from assurance_agent.artifacts.models.common import NonEmptyStr


class Advisory(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    watchlist: list[Any]
    open_questions_for_case_design: list[Any]
    change_id: Any = None
    context_ref: Any = None
    generated_at: Any = None
    executive_summary: Any = None
    evidence_inventory: Any = None
    case_design_guidance: Any = None
    minimum_required_coverage: Any = None


class FactBaselineUnavailable(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: Literal["unavailable"]
    warnings: list[Any]
    facts: Any = None


class FactBaselineFull(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: Literal["seed_file", "db_probe", "both"]
    schema_version: NonEmptyStr
    warnings: list[Any]
    change_id: Any = None
    generated_at: Any = None
    seed_file: Any = None
    facts: Any = None


FactBaselineVariant = Annotated[
    FactBaselineUnavailable | FactBaselineFull, Field(discriminator="source")
]


class FactBaseline(RootModel[FactBaselineVariant]):
    """Discriminated union on `source`, mirroring the TS z.discriminatedUnion."""
```

`models/__init__.py` 增补 import 与 `__all__` 条目：`Advisory`、`FactBaseline`、`FactBaselineFull`、`FactBaselineUnavailable`、`Review`、`ReviewDecision`（`__all__` 保持字母序）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/artifacts/test_models_review_explore.py -v`
Expected: 10 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/artifacts/models tests/unit/artifacts/test_models_review_explore.py
git commit -m "feat: add review, advisory and fact-baseline artifact models"
```

---

### Task 3: 执行与状态产物模型（ExecutionManifest、WorkflowState）

**Files:**
- Create: `assurance_agent/artifacts/models/execution.py`
- Create: `assurance_agent/artifacts/models/state.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Test: `tests/unit/artifacts/test_models_execution_state.py`

**Interfaces:**
- Consumes: Task 1 的 `GateStatus`、`NonEmptyStr`。
- Produces: `SelectedTargets`、`ExecutionManifest`（must_compat 字段 `final_status` / `batch_id` / `selected_targets`；整体分级 versioned）；canonical state 类型 `PhaseState`、`HealingPhaseState`、`WorkflowPhases`、`WorkflowGates`、`RunContext`、`WorkflowState`。M3 state/DSL/healing projection 与 M4/M6 共同消费这些显式类型；`extra="allow"` 仅保留未建模 phase/扩展字段。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/artifacts/test_models_execution_state.py
import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import ExecutionManifest, WorkflowState


def make_manifest(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "batch-20260715-001",
        "selected_targets": {"api": True, "e2e": True, "fuzz": False, "performance": False},
        "result_files": {"api": "execution/runs/batch-20260715-001/api-result.json"},
        "final_status": "PASS",
    }
    doc.update(overrides)
    return doc


def test_execution_manifest_valid_fixture_parses() -> None:
    model = ExecutionManifest.model_validate(make_manifest())
    assert model.batch_id == "batch-20260715-001"
    assert model.selected_targets.api is True
    assert model.final_status == "PASS"
    assert model.tests_tree_sha256 is None


def test_execution_manifest_final_status_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(make_manifest(final_status="GREEN"))


def test_execution_manifest_final_status_optional() -> None:
    doc = make_manifest()
    del doc["final_status"]
    assert ExecutionManifest.model_validate(doc).final_status is None


def test_execution_manifest_missing_batch_id_fails() -> None:
    doc = make_manifest()
    del doc["batch_id"]
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(doc)


def test_execution_manifest_wrong_schema_version_fails() -> None:
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(make_manifest(schema_version="2.0"))


def test_workflow_state_minimal_doc_parses() -> None:
    model = WorkflowState.model_validate({})
    assert model.schema_version is None
    assert model.phases.healing.attempts_used == 0
    assert model.phases.execution is None


def test_workflow_state_extra_fields_preserved() -> None:
    model = WorkflowState.model_validate(
        {
            "schema_version": "1",
            "params": {"run_mode": "full"},
            "phases": {"explore": {"status": "done"}},
            "gates": {"healing_available": True},
        }
    )
    assert model.params == {"run_mode": "full"}
    assert model.gates.healing_available is True
    assert model.phases.model_extra is not None
    assert model.phases.model_extra["explore"]["status"] == "done"


def test_workflow_state_known_core_fields_are_typed() -> None:
    model = WorkflowState.model_validate({
        "run_context": {"active_scope": "execute"},
        "phases": {
            "execution": {"status": "FAIL", "batch_id": "b1"},
            "inspect": {"inspect_mode": "primary"},
            "healing": {"status": "pending", "attempts_used": 2, "all_fixers_no_op": False},
        },
    })
    assert model.run_context.active_scope == "execute"
    assert model.phases.execution is not None
    assert model.phases.execution.batch_id == "b1"
    assert model.phases.healing.attempts_used == 2


def test_workflow_state_negative_attempts_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowState.model_validate({"phases": {"healing": {"attempts_used": -1}}})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/artifacts/test_models_execution_state.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: 实现 execution.py 与 state.py**

```python
# assurance_agent/artifacts/models/execution.py
"""execution/execution-manifest.yaml — written by `aa run` (versioned).

Transcribed from src/schema/execution_manifest.ts. final_status / batch_id /
selected_targets are must_compat fields: healing gates and benchmark scripts
reference them by these exact names.
"""
from typing import Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models.common import GateStatus, NonEmptyStr


class SelectedTargets(BaseModel):
    api: bool
    e2e: bool
    fuzz: bool
    performance: bool


class ExecutionManifest(BaseModel):
    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    selected_targets: SelectedTargets
    result_files: dict[str, str]
    tests_tree_sha256: str | None = None
    test_files_sha256: dict[str, str] | None = None
    product_tree_sha256: str | None = None
    final_status: GateStatus | None = None
```

```python
# assurance_agent/artifacts/models/state.py
"""workflow-state.yaml — canonical cross-milestone state type (versioned).

This is the ONE state type the whole series binds to.  Known cross-milestone
fields are explicit and typed; `extra="allow"` is compatibility-only for phase
ids and extension data not yet promoted to the canonical contract.  In
particular `phases.healing.attempts_used` is never represented by an untyped
mapping, so a misspelling cannot silently cross the M2/M3/M6 boundary.
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PhaseState(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    skill_loaded: bool | None = None
    skill_md_path: str | None = None
    skill_loaded_at: str | None = None
    batch_id: str | None = None
    inspect_mode: str | None = None


class HealingPhaseState(PhaseState):
    attempts_used: int = Field(default=0, ge=0)
    all_fixers_no_op: bool = False


class WorkflowPhases(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill_registry_check: PhaseState | None = None
    execution: PhaseState | None = None
    inspect: PhaseState | None = None
    healing: HealingPhaseState = Field(default_factory=HealingPhaseState)


class WorkflowGates(BaseModel):
    model_config = ConfigDict(extra="allow")

    healing_available: bool | None = None


class RunContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    orchestrator_skill: str | None = None
    interaction_mode: str | None = None
    active_scope: str | None = None
    stamped_at: str | None = None


class WorkflowState(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    phases: WorkflowPhases = Field(default_factory=WorkflowPhases)
    gates: WorkflowGates = Field(default_factory=WorkflowGates)
    run_context: RunContext = Field(default_factory=RunContext)
```

> 冻结说明：healing 的唯一位置是 `state.phases.healing`；禁止创建顶层 `state.healing`。已知字段用属性访问，动态 phase id 经 `state.phases.model_extra` 或序列化视图访问。M3 构造 DSL scope 时使用 `model_dump(mode="json", exclude_none=True)`，既保留扩展字段，又让未出现的可选字段继续走 `MISSING` 语义。

`models/__init__.py` 增补：`ExecutionManifest`、`SelectedTargets`、`PhaseState`、`HealingPhaseState`、`WorkflowPhases`、`WorkflowGates`、`RunContext`、`WorkflowState`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/artifacts/test_models_execution_state.py -v`
Expected: 9 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/artifacts/models tests/unit/artifacts/test_models_execution_state.py
git commit -m "feat: add execution manifest and workflow-state artifact models"
```

---

### Task 4: 检视与报告产物模型（FailureAnalysis、QualityGateResult、QualityReport）

**Files:**
- Create: `assurance_agent/artifacts/models/inspect.py`
- Create: `assurance_agent/artifacts/models/report.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Test: `tests/unit/artifacts/test_models_inspect_report.py`

**Interfaces:**
- Consumes: Task 1 的 `GateStatus`、`ReportRiskLevel`、`FunctionalDimension`、`CoverageDimension`。
- Produces: `FailureCategory`（18 值枚举）、`FailureEntry`（must_compat：`classification` 即 `category` 枚举、`fix_proposal_eligible`）、`FailureAnalysis`（must_compat：`source_batch_id`）、`QualityGateResult`、`QualityReport`。M3 healing gate、M5 分类器与报告消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/artifacts/test_models_inspect_report.py
import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import FailureAnalysis, QualityGateResult, QualityReport


def make_failure_entry(**overrides: object) -> dict:
    entry: dict = {
        "case_id": "TC_MENU_001",
        "target": "api",
        "category": "assertion_failure",
        "fix_proposal_eligible": False,
        "severity": "high",
        "evidence": {
            "result_file": "execution/runs/b1/api-result.json",
            "test_file": "tests/api/test_menu.py",
            "trace": "",
            "screenshot": "",
            "video": "",
            "raw_log": "execution/runs/b1/api-raw.log",
            "log_excerpt": "AssertionError: expected 200 got 500",
        },
        "diagnosis": "endpoint returned 500",
        "recommended_action": "inspect server log",
    }
    entry.update(overrides)
    return entry


def make_failure_analysis(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "source_manifest": "execution/execution-manifest.yaml",
        "inspection_status": "completed",
        "batch_id": "b1",
        "source_batch_id": "b1",
        "final_status": "FAIL",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "failures": [make_failure_entry()],
        "hard_fails": [],
        "needs_review": [],
        "known_product_issues": [],
    }
    doc.update(overrides)
    return doc


def make_functional() -> dict:
    return {
        "status": "PASS",
        "api": {"total": 10, "passed": 10, "failed": 0},
        "e2e": {"total": 4, "passed": 4, "failed": 0},
    }


def make_coverage() -> dict:
    return {
        "status": "PASS",
        "available": True,
        "line_coverage": 85.0,
        "branch_coverage": 70.0,
        "threshold": {"line": 70, "branch": 60},
    }


def make_quality_gate_result(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "b1",
        "dimensions": {"functional": make_functional(), "coverage": make_coverage()},
        "final_status": "PASS",
    }
    doc.update(overrides)
    return doc


def make_quality_report(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "b1",
        "final_status": "PASS",
        "quality_score": 92.5,
        "score_breakdown": {"functional": 95, "coverage": 88, "fuzz": "N/A", "performance": "N/A"},
        "scope": {"cases": 14, "requirements": ["REQ-1"]},
        "functional": make_functional(),
        "coverage": make_coverage(),
        "defects": {"product": [], "test": [], "environment": []},
        "risk_level": "LOW",
        "risk_rationale": "all dimensions pass",
        "recommendation": "release",
    }
    doc.update(overrides)
    return doc


def test_failure_analysis_valid_fixture_parses() -> None:
    model = FailureAnalysis.model_validate(make_failure_analysis())
    assert model.source_batch_id == "b1"
    assert model.failures[0].category == "assertion_failure"
    assert model.failures[0].fix_proposal_eligible is False
    assert model.final_status == "FAIL"


def test_failure_analysis_category_enum_violation_fails() -> None:
    doc = make_failure_analysis(failures=[make_failure_entry(category="cosmic_rays")])
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate(doc)


def test_failure_analysis_missing_source_batch_id_fails() -> None:
    doc = make_failure_analysis()
    del doc["source_batch_id"]
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate(doc)


def test_failure_analysis_fix_proposal_eligible_required() -> None:
    entry = make_failure_entry()
    del entry["fix_proposal_eligible"]
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate(make_failure_analysis(failures=[entry]))


def test_failure_analysis_fuzz_and_perf_categories_accepted() -> None:
    doc = make_failure_analysis(
        failures=[
            make_failure_entry(target="fuzz", category="fuzz_stateful_failure"),
            make_failure_entry(target="performance", category="perf_threshold_exceeded"),
        ]
    )
    assert len(FailureAnalysis.model_validate(doc).failures) == 2


def test_quality_gate_result_valid_fixture_parses() -> None:
    model = QualityGateResult.model_validate(make_quality_gate_result())
    assert model.dimensions.functional.api.total == 10
    assert model.final_status == "PASS"


def test_quality_gate_result_final_status_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        QualityGateResult.model_validate(make_quality_gate_result(final_status="OK"))


def test_quality_report_valid_fixture_parses() -> None:
    model = QualityReport.model_validate(make_quality_report())
    assert model.quality_score == 92.5
    assert model.score_breakdown.fuzz == "N/A"
    assert model.risk_level == "LOW"


def test_quality_report_bad_risk_level_fails() -> None:
    with pytest.raises(ValidationError):
        QualityReport.model_validate(make_quality_report(risk_level="low"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/artifacts/test_models_inspect_report.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: 实现 inspect.py 与 report.py**

```python
# assurance_agent/artifacts/models/inspect.py
"""inspect/failure-analysis.json (must_compat) and inspect/quality-gate-result.json
(versioned).

Transcribed from src/schema/failure_analysis.ts, src/schema/quality_gate_result.ts
and the type definitions in src/schema/contracts.ts. Healing gates reference
source_batch_id, failures[].fix_proposal_eligible and final_status verbatim.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.common import (
    CoverageDimension,
    FunctionalDimension,
    GateStatus,
)

FailureCategory = Literal[
    "environment_failure",
    "test_data_failure",
    "locator_failure",
    "wait_strategy_failure",
    "assertion_failure",
    "assertion_expectation_error",
    "business_logic_failure",
    "case_semantic_failure",
    "test_code_error",
    "known_product_issue",
    "coverage_gap",
    "fuzz_configuration_error",
    "fuzz_stateful_failure",
    "perf_script_error",
    "perf_threshold_exceeded",
    "perf_environment",
    "manifest_asset_missing",
    "unknown",
]
FailureSeverity = Literal["low", "medium", "high", "critical"]


class FailureEvidence(BaseModel):
    result_file: str
    test_file: str
    trace: str
    screenshot: str
    video: str
    raw_log: str
    log_excerpt: str


class Reclassified(BaseModel):
    # The artifact key "from" is a Python keyword, hence the alias.
    model_config = ConfigDict(populate_by_name=True)

    from_: FailureCategory = Field(alias="from")
    evidence: str
    at: str


class FailureEntry(BaseModel):
    case_id: str
    target: Literal["api", "e2e", "fuzz", "performance", "coverage"]
    category: FailureCategory
    fix_proposal_eligible: bool
    severity: FailureSeverity
    evidence: FailureEvidence
    diagnosis: str
    recommended_action: str
    id: str | None = None
    test: str | None = None
    recommended_next_action: str | None = None
    needs_review: bool | None = None
    reclassified: Reclassified | None = None


class CoverageGapEntry(BaseModel):
    file: str
    line_coverage: float
    threshold: float


class FailureAnalysis(BaseModel):
    schema_version: Literal["1.0"]
    change_id: str
    source_manifest: str
    inspection_status: Literal["completed", "skipped", "failed"]
    batch_id: str
    source_batch_id: str
    final_status: GateStatus
    inspect_mode: Literal["primary", "compat_fallback"]
    compat_fallback_reason: str | None = None
    classification_performed: bool
    status: Literal["analyzed", "no_failures", "skipped", "failed"]
    failures: list[FailureEntry]
    hard_fails: list[FailureEntry]
    needs_review: list[FailureEntry]
    known_product_issues: list[FailureEntry]
    warnings: list[str] | None = None
    coverage_gaps: list[CoverageGapEntry] | None = None


class PerformanceScenarioVerdict(BaseModel):
    capability: str
    endpoint: str
    measured_p95_ms: float | None
    threshold_p95_ms: float
    measured_error_rate: float | None
    threshold_error_rate_max: float
    verdict: Literal["PASS", "FAIL", "SKIPPED"]


class NonFunctionalDimension(BaseModel):
    status: GateStatus
    performance: list[PerformanceScenarioVerdict]


class QualityGateDimensions(BaseModel):
    functional: FunctionalDimension
    coverage: CoverageDimension
    non_functional: NonFunctionalDimension | None = None


class QualityGateResult(BaseModel):
    schema_version: Literal["1.0"]
    change_id: str
    batch_id: str
    dimensions: QualityGateDimensions
    final_status: GateStatus
    warnings: list[str] | None = None
```

```python
# assurance_agent/artifacts/models/report.py
"""report/quality-report.json — written by `aa report generate` (versioned).

Transcribed from src/schema/quality_report.ts / src/schema/contracts.ts.
"""
from typing import Any, Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models.common import (
    CoverageDimension,
    FunctionalDimension,
    GateStatus,
    ReportRiskLevel,
)

ScoreValue = float | Literal["N/A"]


class QualityScoreBreakdown(BaseModel):
    functional: ScoreValue
    coverage: ScoreValue
    fuzz: ScoreValue
    performance: ScoreValue


class ReportScope(BaseModel):
    cases: int
    requirements: list[str]


class ReportDefect(BaseModel):
    case_id: str
    category: str
    diagnosis: str


class ReportDefects(BaseModel):
    product: list[ReportDefect]
    test: list[ReportDefect]
    environment: list[ReportDefect]


class QualityReport(BaseModel):
    schema_version: Literal["1.0"]
    change_id: str
    batch_id: str
    final_status: GateStatus
    quality_score: float
    score_breakdown: QualityScoreBreakdown
    scope: ReportScope
    functional: FunctionalDimension
    coverage: CoverageDimension
    defects: ReportDefects
    risk_level: ReportRiskLevel
    risk_rationale: str
    recommendation: str
    human_decisions: list[Any] | None = None
    minimum_required_coverage: Any = None
    non_functional: Any = None
```

`models/__init__.py` 增补：`CoverageGapEntry`、`FailureAnalysis`、`FailureCategory`、`FailureEntry`、`FailureEvidence`、`FailureSeverity`、`NonFunctionalDimension`、`PerformanceScenarioVerdict`、`QualityGateDimensions`、`QualityGateResult`、`QualityReport`、`QualityScoreBreakdown`、`Reclassified`、`ReportDefect`、`ReportDefects`、`ReportScope`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/artifacts/test_models_inspect_report.py -v`
Expected: 9 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/artifacts/models tests/unit/artifacts/test_models_inspect_report.py
git commit -m "feat: add failure-analysis, quality-gate and quality-report models"
```

---

### Task 5: Healing 产物模型（FixProposal、ApplySummary、SafetyCheck）

**Files:**
- Create: `assurance_agent/artifacts/models/healing.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Test: `tests/unit/artifacts/test_models_healing.py`

**Interfaces:**
- Consumes: Task 1 的 `NonEmptyStr`。
- Produces: `FixProposal`（must_compat：`summary.eligible_count` / `proposals[].target` / `proposals[].eligible`）、`FixProposalItem`、`FixProposalSummary`、`ApplySummary`、`SafetyCheck`（fixer-safety-gate 引用字段全部必填）。Task 6 注册表与 M3 healing gate 消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/artifacts/test_models_healing.py
import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import ApplySummary, FixProposal, SafetyCheck


def make_fix_proposal(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "source_analysis": "inspect/failure-analysis.json",
        "source_batch_id": "b1",
        "summary": {"eligible_count": 1, "not_eligible_count": 0, "targets": {"api": 1, "e2e": 0}},
        "proposals": [
            {
                "proposal_id": "FIX-001",
                "target": "api",
                "eligible": True,
                "risk_level": "low",
                "files_to_modify": ["tests/api/test_menu.py"],
            }
        ],
        "not_eligible": [],
    }
    doc.update(overrides)
    return doc


def make_safety_check(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "source": "cli",
        "change_id": "CH-1",
        "passed": True,
        "needs_review": False,
        "product_code_modified": False,
        "skip_or_xfail_added": False,
        "bare_return_added": False,
        "unrelated_tests_modified": False,
        "unrelated_test_files": [],
        "assertion_expected_value_changes_detected": False,
        "high_risk_proposal_applied": False,
    }
    doc.update(overrides)
    return doc


def test_fix_proposal_valid_fixture_parses_and_keeps_extras() -> None:
    model = FixProposal.model_validate(make_fix_proposal())
    assert model.summary.eligible_count == 1
    assert model.proposals[0].target == "api"
    assert model.proposals[0].eligible is True
    assert model.model_extra is not None
    assert model.model_extra["source_batch_id"] == "b1"


def test_fix_proposal_target_enum_violation_fails() -> None:
    doc = make_fix_proposal()
    doc["proposals"][0]["target"] = "database"
    with pytest.raises(ValidationError):
        FixProposal.model_validate(doc)


def test_fix_proposal_missing_summary_eligible_count_fails() -> None:
    with pytest.raises(ValidationError):
        FixProposal.model_validate(make_fix_proposal(summary={"targets": {}}))
    doc = make_fix_proposal()
    del doc["summary"]
    with pytest.raises(ValidationError):
        FixProposal.model_validate(doc)


def test_apply_summary_valid_fixture_parses() -> None:
    model = ApplySummary.model_validate(
        {"schema_version": "1.0", "target": "api", "applied": True, "modified_files": []}
    )
    assert model.target == "api"
    assert model.applied is True


def test_apply_summary_target_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        ApplySummary.model_validate({"schema_version": "1.0", "target": "fuzz", "applied": True})


def test_safety_check_valid_pass_fixture_parses() -> None:
    model = SafetyCheck.model_validate(make_safety_check())
    assert model.passed is True
    assert model.high_risk_proposal_applied is False


def test_safety_check_undetermined_skip_flag_parses() -> None:
    model = SafetyCheck.model_validate(
        make_safety_check(skip_or_xfail_added="undetermined", needs_review=True, passed=False)
    )
    assert model.skip_or_xfail_added == "undetermined"
    assert model.needs_review is True


def test_safety_check_missing_passed_fails() -> None:
    doc = make_safety_check()
    del doc["passed"]
    with pytest.raises(ValidationError):
        SafetyCheck.model_validate(doc)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/artifacts/test_models_healing.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: 实现 healing.py**

```python
# assurance_agent/artifacts/models/healing.py
"""healing/ artifacts (all must_compat).

- fix-proposal.json: src/schema/fix_proposal.ts + aws-fix-proposal SKILL.md.
  summary.eligible_count is required here (stricter than the TS validator)
  because the healing loop's allocate_on expression reads
  fix_proposal.summary.eligible_count directly.
- *-apply-summary.json: src/schema/apply_summary.ts.
- fixer-safety-check.json: payload written by the TS core healing_state.ts;
  required fields are exactly those the fixer-safety-gate expressions read.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models.common import NonEmptyStr

Undetermined = Literal["undetermined"]


class FixProposalSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    eligible_count: int


class FixProposalItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    target: Literal["api", "e2e", "fuzz", "performance"]
    eligible: bool


class FixProposal(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    summary: FixProposalSummary
    proposals: list[FixProposalItem]


class ApplySummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    target: Literal["api", "e2e"]
    applied: bool


class SafetyCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    passed: bool
    needs_review: bool
    product_code_modified: bool
    skip_or_xfail_added: bool | Undetermined
    unrelated_tests_modified: bool
    assertion_expected_value_changes_detected: bool
    high_risk_proposal_applied: bool
    bare_return_added: bool | Undetermined | None = None
    change_id: str | None = None
    source_batch_id: str | None = None
    attempt_key: str | None = None
    proposal_sha256: str | None = None
    unrelated_test_files: list[str] | None = None
    assertion_expected_value_changes: list[Any] | None = None
```

`models/__init__.py` 增补：`ApplySummary`、`FixProposal`、`FixProposalItem`、`FixProposalSummary`、`SafetyCheck`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/artifacts/test_models_healing.py -v`
Expected: 8 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/artifacts/models tests/unit/artifacts/test_models_healing.py
git commit -m "feat: add healing artifact models (fix proposal, apply summary, safety check)"
```

---

### Task 6: 路径注册表（registry.py）

**Files:**
- Create: `assurance_agent/artifacts/registry.py`
- Test: `tests/unit/artifacts/test_registry.py`

**Interfaces:**
- Consumes: Task 1–5 的全部产物模型（经 `assurance_agent.artifacts.models`）。
- Produces: `Compat = Literal["must_compat", "versioned", "free"]`；`ArtifactSpec(BaseModel)`——字段 `artifact_type: str`、`pattern: str`、`model: type[BaseModel]`、`compat: Compat`；`REGISTRY: list[ArtifactSpec]`（13 条）；`match_artifact(relpath: str) -> ArtifactSpec | None`。Task 7 validate 引擎与后续所有里程碑消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/artifacts/test_registry.py
from assurance_agent.artifacts.registry import REGISTRY, match_artifact


def test_registry_covers_every_expected_artifact_type() -> None:
    expected = {
        "advisory",
        "apply_summary",
        "case_yaml",
        "execution_manifest",
        "fact_baseline",
        "failure_analysis",
        "fix_proposal",
        "qa_yaml",
        "quality_gate_result",
        "quality_report",
        "review",
        "safety_check",
        "workflow_state",
    }
    assert {spec.artifact_type for spec in REGISTRY} == expected
    assert len(REGISTRY) == 13


def test_case_yaml_matches_nested_and_direct_paths() -> None:
    for rel in ("cases/menus/case.yaml", "cases/a/b/case.yaml", "cases/case.yaml"):
        spec = match_artifact(rel)
        assert spec is not None and spec.artifact_type == "case_yaml", rel
    assert match_artifact("cases/menus/notes.yaml") is None


def test_qa_yaml_exact_match_only() -> None:
    spec = match_artifact(".qa.yaml")
    assert spec is not None and spec.artifact_type == "qa_yaml"
    assert match_artifact("sub/.qa.yaml") is None


def test_review_glob_matches_any_review_json_in_review_dir() -> None:
    spec = match_artifact("review/api-plan-review.json")
    assert spec is not None and spec.artifact_type == "review"
    assert match_artifact("review/case-review-apply-summary.md") is None


def test_star_does_not_cross_directory_boundaries() -> None:
    assert match_artifact("review/nested/deep-review.json") is None


def test_apply_summary_wildcard_and_fixed_healing_paths() -> None:
    api = match_artifact("healing/api-apply-summary.json")
    assert api is not None and api.artifact_type == "apply_summary"
    fp = match_artifact("healing/fix-proposal.json")
    assert fp is not None and fp.artifact_type == "fix_proposal"
    sc = match_artifact("healing/fixer-safety-check.json")
    assert sc is not None and sc.artifact_type == "safety_check"


def test_unregistered_path_returns_none() -> None:
    assert match_artifact("proposal.md") is None
    assert match_artifact("explore/context.json") is None


def test_backslash_paths_normalized() -> None:
    spec = match_artifact("inspect\\failure-analysis.json")
    assert spec is not None and spec.artifact_type == "failure_analysis"


def test_compat_grades_match_spec_4a() -> None:
    grades = {spec.artifact_type: spec.compat for spec in REGISTRY}
    assert grades["review"] == "must_compat"
    assert grades["failure_analysis"] == "must_compat"
    assert grades["fix_proposal"] == "must_compat"
    assert grades["safety_check"] == "must_compat"
    assert grades["workflow_state"] == "versioned"
    assert grades["execution_manifest"] == "versioned"
    assert grades["quality_report"] == "versioned"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/artifacts/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'assurance_agent.artifacts.registry'`

- [ ] **Step 3: 实现 registry.py**

```python
# assurance_agent/artifacts/registry.py
"""Change-relative artifact path -> pydantic model registry (spec 4a).

Globs mirror ARTIFACT_SPECS in the TS source src/schema/index.ts, plus
healing/fixer-safety-check.json (read by fixer-safety-gate; the plan-series
contract lists SafetyCheck as a core model). Glob semantics: `*` matches
within one path segment, `**/` matches zero or more segments — same effective
behavior micromatch gave the TS globs. `free`-grade artifacts (markdown
reports, events extensions) are deliberately absent: they are not validated.
"""
import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    Advisory,
    ApplySummary,
    CaseYaml,
    ExecutionManifest,
    FactBaseline,
    FailureAnalysis,
    FixProposal,
    QaYaml,
    QualityGateResult,
    QualityReport,
    Review,
    SafetyCheck,
    WorkflowState,
)

Compat = Literal["must_compat", "versioned", "free"]


class ArtifactSpec(BaseModel):
    artifact_type: str
    pattern: str
    model: type[BaseModel]
    compat: Compat


REGISTRY: list[ArtifactSpec] = [
    ArtifactSpec(artifact_type="case_yaml", pattern="cases/**/case.yaml",
                 model=CaseYaml, compat="must_compat"),
    ArtifactSpec(artifact_type="qa_yaml", pattern=".qa.yaml",
                 model=QaYaml, compat="must_compat"),
    ArtifactSpec(artifact_type="execution_manifest", pattern="execution/execution-manifest.yaml",
                 model=ExecutionManifest, compat="versioned"),
    ArtifactSpec(artifact_type="failure_analysis", pattern="inspect/failure-analysis.json",
                 model=FailureAnalysis, compat="must_compat"),
    ArtifactSpec(artifact_type="quality_gate_result", pattern="inspect/quality-gate-result.json",
                 model=QualityGateResult, compat="versioned"),
    ArtifactSpec(artifact_type="quality_report", pattern="report/quality-report.json",
                 model=QualityReport, compat="versioned"),
    ArtifactSpec(artifact_type="fix_proposal", pattern="healing/fix-proposal.json",
                 model=FixProposal, compat="must_compat"),
    ArtifactSpec(artifact_type="apply_summary", pattern="healing/*-apply-summary.json",
                 model=ApplySummary, compat="must_compat"),
    ArtifactSpec(artifact_type="safety_check", pattern="healing/fixer-safety-check.json",
                 model=SafetyCheck, compat="must_compat"),
    ArtifactSpec(artifact_type="review", pattern="review/*.json",
                 model=Review, compat="must_compat"),
    ArtifactSpec(artifact_type="fact_baseline", pattern="facts/fact-baseline.json",
                 model=FactBaseline, compat="must_compat"),
    ArtifactSpec(artifact_type="advisory", pattern="explore/advisory.json",
                 model=Advisory, compat="must_compat"),
    ArtifactSpec(artifact_type="workflow_state", pattern="workflow-state.yaml",
                 model=WorkflowState, compat="versioned"),
]


@lru_cache(maxsize=None)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def match_artifact(relpath: str) -> ArtifactSpec | None:
    """Return the first registry spec whose glob matches the change-relative path."""
    norm = relpath.replace("\\", "/")
    for spec in REGISTRY:
        if _pattern_regex(spec.pattern).match(norm):
            return spec
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/artifacts/test_registry.py -v`
Expected: 9 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/artifacts/registry.py tests/unit/artifacts/test_registry.py
git commit -m "feat: add artifact path registry with compat grading"
```

---

### Task 7: 校验引擎（validate.py）

**Files:**
- Create: `assurance_agent/identifiers.py`
- Create: `assurance_agent/artifacts/validate.py`
- Test: `tests/unit/artifacts/test_validate.py`

**Interfaces:**
- Consumes: Task 6 的 `REGISTRY` / `match_artifact` / `ArtifactSpec`；M1 的 `resources.read_text`、`AaError`。
- Produces: 公共 `assert_path_segment_safe(value, label="identifier")` 与其 change-id 包装 `assert_change_id_safe(change_id)`（只接受首字符为字母/数字、其余为 `[A-Za-z0-9._-]` 的单路径段；拒绝空串、`.`、`..`、斜杠与绝对路径，所有 M2+ 文件入口必须先调用）；`ArtifactResult(BaseModel)`——`path: str; artifact_type: str; ok: bool; errors: list[str]`；`ValidationReport(BaseModel)`——`ok: bool; results: list[ArtifactResult]`；`WorkflowSchemaLike`（Protocol，方法 `phase_produces(phase_id: str) -> list[str] | None`）；`validate_change(change_dir: Path, phase: str | None = None, artifact: str | None = None, schema: WorkflowSchemaLike | None = None) -> ValidationReport`；异常 `ChangeNotFoundError(AaError)`、`UnknownPhaseError(AaError)`。Task 8 CLI 消费。**M3 接缝**：M3 的 `WorkflowSchema` 必须实现 `phase_produces`，届时 CLI 把加载好的 schema 传入 `schema=`，本模块的薄 YAML 读取仅作缺省回退。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/artifacts/test_validate.py
import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.validate import (
    ChangeNotFoundError,
    UnknownPhaseError,
    validate_change,
)
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe

VALID_REVIEW = {"schema_version": "1.0", "decision": "pass", "findings": []}
INVALID_REVIEW = {"schema_version": "1.0", "decision": "maybe", "findings": []}
VALID_CASE_YAML = """schema_version: "1.0"
added:
  - case_id: TC_MENU_001
    title: create menu
    status: active
    priority: P1
    severity: major
    type: API
    module: menus
modified: []
removed: []
"""
VALID_QA_YAML = """schema_version: "1.0"
schema: qa-yaml/v1
created_at: "2026-07-15T00:00:00Z"
change:
  change_id: CH-1
  requirement_id: REQ-1
  feature_name: menus
  status: in_progress
targets:
  cases:
    - module: menus
      change_case_file: cases/menus/case.yaml
      target_case_file: qa/cases/menus/case.yaml
"""


def write(change_dir: Path, rel: str, content: str) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def change_dir(tmp_path: Path) -> Path:
    root = tmp_path / "qa" / "changes" / "CH-1"
    root.mkdir(parents=True)
    return root


def test_missing_change_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ChangeNotFoundError):
        validate_change(tmp_path / "qa" / "changes" / "NOPE")


def test_full_scan_validates_known_and_skips_unknown(change_dir: Path) -> None:
    write(change_dir, ".qa.yaml", VALID_QA_YAML)
    write(change_dir, "cases/menus/case.yaml", VALID_CASE_YAML)
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))
    write(change_dir, "proposal.md", "# free-form, never validated")

    report = validate_change(change_dir)

    assert report.ok is True
    assert [r.path for r in report.results] == [
        ".qa.yaml",
        "cases/menus/case.yaml",
        "review/case-review.json",
    ]
    assert all(r.ok and r.errors == [] for r in report.results)


def test_invalid_artifact_reports_errors_and_ok_false(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", json.dumps(INVALID_REVIEW))

    report = validate_change(change_dir)

    assert report.ok is False
    (result,) = report.results
    assert result.artifact_type == "review"
    assert result.ok is False
    assert any("decision" in e for e in result.errors)


def test_parse_error_reported_as_validation_failure(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", "{not json")

    report = validate_change(change_dir)

    assert report.ok is False
    assert report.results[0].errors[0].startswith("parse error:")


def test_artifact_single_file_mode(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))
    write(change_dir, "review/other-review.json", json.dumps(INVALID_REVIEW))

    report = validate_change(change_dir, artifact="review/case-review.json")

    assert report.ok is True
    assert [r.path for r in report.results] == ["review/case-review.json"]


def test_artifact_missing_file_reports_not_found(change_dir: Path) -> None:
    report = validate_change(change_dir, artifact="review/case-review.json")

    assert report.ok is False
    (result,) = report.results
    assert result.errors == ["file not found"]
    assert result.artifact_type == "review"


def test_artifact_unregistered_existing_file_fails_closed(change_dir: Path) -> None:
    write(change_dir, "proposal.md", "# not a registered artifact")

    report = validate_change(change_dir, artifact="proposal.md")

    assert report.ok is False
    assert report.results[0].artifact_type == "unregistered"
    assert report.results[0].errors == ["no registered artifact contract"]


def test_full_scan_with_no_registered_artifacts_fails_closed(change_dir: Path) -> None:
    write(change_dir, "proposal.md", "# free-form artifact")

    report = validate_change(change_dir)

    assert report.ok is False
    assert report.results[0].path == "(change)"
    assert report.results[0].errors == ["no registered artifacts found"]


def test_phase_filter_uses_packaged_produces(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))
    write(change_dir, ".qa.yaml", VALID_QA_YAML)

    # packaged schema: case-review produces [review/case-review.json]
    report = validate_change(change_dir, phase="case-review")

    assert [r.path for r in report.results] == ["review/case-review.json"]


def test_phase_directory_produce_matches_by_prefix(change_dir: Path) -> None:
    write(change_dir, ".qa.yaml", VALID_QA_YAML)
    write(change_dir, "cases/menus/case.yaml", VALID_CASE_YAML)
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))

    # packaged schema: case-design produces [.qa.yaml, proposal.md, cases/]
    report = validate_change(change_dir, phase="case-design")

    assert [r.path for r in report.results] == [".qa.yaml", "cases/menus/case.yaml"]


def test_unknown_phase_raises(change_dir: Path) -> None:
    with pytest.raises(UnknownPhaseError):
        validate_change(change_dir, phase="no-such-phase")


def test_schema_provider_overrides_packaged_produces(change_dir: Path) -> None:
    write(change_dir, ".qa.yaml", VALID_QA_YAML)
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))

    class FakeSchema:
        def phase_produces(self, phase_id: str) -> list[str] | None:
            return [".qa.yaml"] if phase_id == "custom-phase" else None

    report = validate_change(change_dir, phase="custom-phase", schema=FakeSchema())
    assert [r.path for r in report.results] == [".qa.yaml"]

    with pytest.raises(UnknownPhaseError):
        validate_change(change_dir, phase="case-review", schema=FakeSchema())


@pytest.mark.parametrize("value", ["", ".", "..", "../CH-1", "a/b", "/tmp/x"])
def test_change_id_rejects_path_segments(value: str) -> None:
    with pytest.raises(UnsafeIdentifierError):
        assert_change_id_safe(value)


def test_change_id_accepts_canonical_values() -> None:
    assert_change_id_safe("REQ-001.login_v2")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/artifacts/test_validate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'assurance_agent.artifacts.validate'`

- [ ] **Step 3: 实现 validate.py**

```python
# assurance_agent/identifiers.py
"""Identifiers that are safe to use as one filesystem path segment."""
import re

from assurance_agent.exceptions import AaError

_CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UnsafeIdentifierError(AaError):
    pass


def assert_path_segment_safe(value: str, *, label: str = "identifier") -> None:
    if not _CHANGE_ID_RE.fullmatch(value):
        raise UnsafeIdentifierError(f"unsafe {label}: {value!r}")


def assert_change_id_safe(change_id: str) -> None:
    assert_path_segment_safe(change_id, label="change id")
```

```python
# assurance_agent/artifacts/validate.py
"""Deterministic artifact validation for qa/changes/<id>/ (spec 4a).

Registered artifacts are validated deterministically. Free-form files remain
outside schema validation, but an explicit unregistered `--artifact` and a scan
that finds no registered artifact both fail closed so CI cannot report a false
green. A requested-but-missing file is also a validation failure. Phase
filtering matches artifacts against the phase's `produces` declarations.

M3 seam: `schema` accepts any object implementing WorkflowSchemaLike; the M3
WorkflowSchema loader must implement phase_produces(). Until then (and as the
default fallback) the packaged workflow-schema.yaml is read with a thin YAML
pass that only extracts phases[].id and phases[].produces — no M3 semantics.
"""
import json
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ValidationError

from assurance_agent import resources
from assurance_agent.artifacts.registry import ArtifactSpec, match_artifact
from assurance_agent.exceptions import AaError


class ChangeNotFoundError(AaError):
    pass


class UnknownPhaseError(AaError):
    pass


class WorkflowSchemaLike(Protocol):
    def phase_produces(self, phase_id: str) -> list[str] | None:
        """Return the phase's produces list, or None when the phase is unknown."""
        ...


class ArtifactResult(BaseModel):
    path: str
    artifact_type: str
    ok: bool
    errors: list[str]


class ValidationReport(BaseModel):
    ok: bool
    results: list[ArtifactResult]


def validate_change(
    change_dir: Path,
    phase: str | None = None,
    artifact: str | None = None,
    schema: WorkflowSchemaLike | None = None,
) -> ValidationReport:
    if not change_dir.is_dir():
        raise ChangeNotFoundError(f"change directory not found: {change_dir}")

    if artifact is not None:
        rel_paths = [artifact.replace("\\", "/")]
    elif phase is not None:
        produces = (
            schema.phase_produces(phase) if schema is not None else _packaged_phase_produces(phase)
        )
        if produces is None:
            raise UnknownPhaseError(f"unknown phase '{phase}'")
        rel_paths = [
            rel
            for rel in _collect_artifact_paths(change_dir)
            if any(_belongs_to_produce(rel, produce) for produce in produces)
        ]
    else:
        rel_paths = _collect_artifact_paths(change_dir)

    results: list[ArtifactResult] = []
    for rel in rel_paths:
        abs_path = change_dir / rel
        spec = match_artifact(rel)
        if not abs_path.is_file():
            results.append(ArtifactResult(
                path=rel,
                artifact_type=spec.artifact_type if spec else "unknown",
                ok=False,
                errors=["file not found"],
            ))
            continue
        if spec is None:
            # Explicit --artifact must never become a successful no-op.
            results.append(ArtifactResult(
                path=rel,
                artifact_type="unregistered",
                ok=False,
                errors=["no registered artifact contract"],
            ))
            continue
        results.append(_validate_file(spec, abs_path, rel))

    if not results:
        results.append(ArtifactResult(
            path=f"(phase: {phase})" if phase is not None else "(change)",
            artifact_type="unregistered",
            ok=False,
            errors=["no registered artifacts found"],
        ))

    return ValidationReport(ok=all(r.ok for r in results), results=results)


def _collect_artifact_paths(change_dir: Path) -> list[str]:
    out: list[str] = []
    for path in change_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(change_dir).as_posix()
        if match_artifact(rel) is not None:
            out.append(rel)
    return sorted(out)


def _belongs_to_produce(rel: str, produce: str) -> bool:
    """Mirror the TS artifactBelongsToProduce: registered produces match exactly,
    directory produces (trailing slash) match by prefix, others match exactly."""
    prod = produce.replace("\\", "/")
    if match_artifact(prod) is not None:
        return rel == prod
    if prod.endswith("/"):
        return rel.startswith(prod)
    return rel == prod


def _packaged_phase_produces(phase_id: str) -> list[str] | None:
    doc = yaml.safe_load(resources.read_text("schemas", "workflow-schema.yaml"))
    for entry in doc.get("phases", []):
        if entry.get("id") == phase_id:
            return [str(p) for p in entry.get("produces", [])]
    return None


def _validate_file(spec: ArtifactSpec, abs_path: Path, rel: str) -> ArtifactResult:
    text = abs_path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text) if abs_path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as err:
        return ArtifactResult(
            path=rel, artifact_type=spec.artifact_type, ok=False, errors=[f"parse error: {err}"]
        )
    try:
        spec.model.model_validate(raw)
    except ValidationError as err:
        return ArtifactResult(
            path=rel, artifact_type=spec.artifact_type, ok=False, errors=_format_errors(err)
        )
    return ArtifactResult(path=rel, artifact_type=spec.artifact_type, ok=True, errors=[])


def _format_errors(err: ValidationError) -> list[str]:
    out: list[str] = []
    for issue in err.errors():
        loc = ".".join(str(part) for part in issue["loc"]) or "(root)"
        out.append(f"{loc}: {issue['msg']}")
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/artifacts/test_validate.py -v`
Expected: 14 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/identifiers.py assurance_agent/artifacts/validate.py \
        tests/unit/artifacts/test_validate.py
git commit -m "feat: add validate_change artifact validation engine"
```

---

### Task 8: `aa validate` 命令 + 分层契约 + 收尾

**Files:**
- Create: `assurance_agent/commands/validate_cmd.py`
- Modify: `assurance_agent/cli.py`
- Modify: `.importlinter`
- Modify: `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`
- Test: `tests/integration/test_cli_validate.py`

**Interfaces:**
- Consumes: Task 7 的 `validate_change` / `ValidationReport` / `ChangeNotFoundError` / `UnknownPhaseError`；M1 的 click Group `main`。
- Produces: `aa validate --change <id> [--phase <phase>] [--artifact <relpath>] [--json]`。`--json` 输出 `{ok, results}`；退出码 0 全部通过 / 1 校验失败、change/artifact 缺失、显式 artifact 未注册或零注册产物 / 2 用法错误（未知 phase 等）。M4+ 所有命令沿用更新后的 `.importlinter` 分层。

- [ ] **Step 1: 写失败集成测试**

```python
# tests/integration/test_cli_validate.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main

VALID_REVIEW = '{"schema_version": "1.0", "decision": "pass", "findings": []}'
INVALID_REVIEW = '{"schema_version": "1.0", "decision": "maybe", "findings": []}'


def make_change(change_id: str = "CH-1") -> Path:
    change_dir = Path("qa/changes") / change_id
    (change_dir / "review").mkdir(parents=True)
    return change_dir


def test_validate_missing_change_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["validate", "--change", "NOPE"])
        assert result.exit_code == 1
        assert "not found" in result.output


def test_validate_rejects_unsafe_change_id() -> None:
    result = CliRunner().invoke(main, ["validate", "--change", "../outside"])
    assert result.exit_code == 1
    assert "unsafe change id" in result.output


def test_validate_all_pass_exits_0_with_human_output() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(VALID_REVIEW, encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1"])
        assert result.exit_code == 0, result.output
        assert "review/case-review.json [review]" in result.output


def test_validate_failure_exits_1_and_lists_errors() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(INVALID_REVIEW, encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1"])
        assert result.exit_code == 1
        assert "decision" in result.output


def test_validate_json_outputs_ok_and_results() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(VALID_REVIEW, encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1", "--json"])
        assert result.exit_code == 0
        doc = json.loads(result.output)
        assert doc["ok"] is True
        assert doc["results"][0]["artifact_type"] == "review"
        assert doc["results"][0]["errors"] == []


def test_validate_unknown_phase_exits_2() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["validate", "--change", "CH-1", "--phase", "bogus"])
        assert result.exit_code == 2
        assert "unknown phase" in result.output


def test_validate_phase_filters_to_produced_artifacts() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(VALID_REVIEW, encoding="utf-8")
        (change_dir / "review/plan-review.json").write_text(VALID_REVIEW, encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1", "--phase", "case-review", "--json"])
        assert result.exit_code == 0
        doc = json.loads(result.output)
        assert [r["path"] for r in doc["results"]] == ["review/case-review.json"]


def test_validate_single_artifact_missing_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main, ["validate", "--change", "CH-1", "--artifact", "review/case-review.json"]
        )
        assert result.exit_code == 1
        assert "file not found" in result.output


def test_validate_no_registered_artifact_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "proposal.md").write_text("# free-form", encoding="utf-8")
        result = runner.invoke(main, ["validate", "--change", "CH-1"])
        assert result.exit_code == 1
        assert "no registered artifacts found" in result.output
```

- [ ] **Step 2: 跑集成测试确认失败**

Run: `uv run pytest tests/integration/test_cli_validate.py -v`
Expected: FAIL（`No such command 'validate'`）

- [ ] **Step 3: 实现 validate_cmd.py 并挂载**

```python
# assurance_agent/commands/validate_cmd.py
from pathlib import Path

import click

from assurance_agent.artifacts.validate import (
    UnknownPhaseError,
    ValidationReport,
    validate_change,
)
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe


@click.command("validate")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", default=None, help="Only validate artifacts the phase produces.")
@click.option("--artifact", default=None, help="Validate a single change-relative artifact path.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output {ok, results}.")
def validate_command(change_id: str, phase: str | None, artifact: str | None, as_json: bool) -> None:
    """Validate per-change YAML/JSON artifacts against their schemas (deterministic, no LLM)."""
    try:
        assert_change_id_safe(change_id)
        change_dir = Path.cwd() / "qa" / "changes" / change_id
        report = validate_change(change_dir, phase=phase, artifact=artifact)
    except UnknownPhaseError as err:
        raise click.UsageError(str(err)) from err  # click exits 2
    except AaError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err

    if as_json:
        click.echo(report.model_dump_json(indent=2))
    else:
        _print_report(change_id, report)
    raise SystemExit(0 if report.ok else 1)


def _print_report(change_id: str, report: ValidationReport) -> None:
    click.secho(f"aa validate — {change_id}", bold=True)
    click.echo()
    for result in report.results:
        if result.ok:
            click.secho(f"✓ {result.path} [{result.artifact_type}]", fg="green")
        else:
            click.secho(f"✗ {result.path} [{result.artifact_type}]", fg="red")
            for error in result.errors:
                click.echo(f"    · {error}")
    click.echo()
```

```python
# assurance_agent/cli.py（追加 import 与挂载，保持既有内容不变）
from assurance_agent.commands.validate_cmd import validate_command

main.add_command(validate_command)
```

- [ ] **Step 4: 跑集成测试确认通过**

Run: `uv run pytest tests/integration/test_cli_validate.py -v`
Expected: 9 passed

- [ ] **Step 5: 更新 .importlinter 分层契约（遵循系列「Import 契约（冻结）」，只增不换）**

> **冻结原则**：`.importlinter` 由 M1 建立，此后每个里程碑**只在既有 `layers` 内新增本里程碑的模块、或新增独立命名的 forbidden 契约**，**绝不整文件替换、绝不反转既有层序**（修复 Standards P1：M2/M5/M6 曾各自整文件覆盖、依赖方向反复翻转）。
>
> **方向裁定**：`artifacts` 是全项目唯一产物契约层，位于 `workflow` **之下**（`workflow → artifacts` 允许、`artifacts → workflow` 禁止）。这是**唯一正确**方向，理由：M3 `workflow/core/state.py` import `artifacts.WorkflowState`、M5 `workflow/execution|report` import artifacts 模型。此前 M2 把 artifacts 放在 workflow 之上是错的，本步改正。

编辑现有 `.importlinter`（**不整文件替换**），把 `assurance_agent.artifacts` 插入到 `assurance_agent.workflow` **下一行**：

```ini
[importlinter:contract:layers]
name = commands depend on domain, never the reverse
type = layers
layers =
    assurance_agent.cli
    assurance_agent.commands
    assurance_agent.workflow
    assurance_agent.artifacts
    assurance_agent.config
    assurance_agent.resources
```

> M3 会再新增 `core-below-orchestration` 与 `artifacts-below-workflow` 两条 forbidden 契约（独立命名，不动本 `layers`）。eval/retro 层由 M8 在其创建时插入 `commands` 之下、`workflow` 之上。

Run: `uv run lint-imports`
Expected: Contracts: 1 kept, 0 broken.

- [ ] **Step 6: 回改计划系列总览的接口契约**

M2 契约中 `schema` 参数原写作前向引用 `"WorkflowSchema | None"`；落地为结构化协议 `WorkflowSchemaLike`（M3 的 `WorkflowSchema` 实现之）。按总览「变更需先改本节」约定，编辑 `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`，把「M2 产物契约」代码块中的：

```python
def validate_change(change_dir: Path, phase: str | None = None,
                    artifact: str | None = None,
                    schema: "WorkflowSchema | None" = None) -> ValidationReport
```

替换为：

```python
class WorkflowSchemaLike(Protocol):   # M3 WorkflowSchema 必须实现本协议
    def phase_produces(self, phase_id: str) -> list[str] | None: ...

def validate_change(change_dir: Path, phase: str | None = None,
                    artifact: str | None = None,
                    schema: WorkflowSchemaLike | None = None) -> ValidationReport
```

- [ ] **Step 7: 全量回归 + 质量门禁**

Run: `uv run pytest -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过（M1 既有测试 + 本里程碑新增 73 个）

- [ ] **Step 8: Commit**

```bash
git add assurance_agent/commands/validate_cmd.py assurance_agent/cli.py .importlinter \
        tests/integration/test_cli_validate.py docs/superpowers/plans/2026-07-14-python-migration-plan-series.md
git commit -m "feat: add aa validate command with phase and artifact filters"
```

---

## M2 验收清单

- `uv run aa validate --change <id>` 在含有效/无效产物的 change 目录上分别返回 0 / 1；`--phase` 未知时返回 2；`--json` 输出 `{ok, results}`。
- 13 个注册表条目 glob 与 TS 源 `src/schema/index.ts` 一致（外加 `safety_check` 增补），compat 分级符合 spec 4a。
- 全部模型字段名 / 可选性 / 枚举值与 TS 验证器逐一对拍；must_compat 字段（review 的 `decision` 等 6 字段、failure_analysis 的 `source_batch_id` / `category` / `fix_proposal_eligible`、fix_proposal 的 `summary.eligible_count` / `proposals[].target` / `proposals[].eligible`、execution_manifest 的 `final_status` / `batch_id` / `selected_targets`）与 workflow-schema.yaml gate 表达式引用一致。
- `uv run pytest` / `ruff` / `pyright` / `lint-imports` 全绿；新文件无 `aws` 残留。
- 计划系列总览的 M2 接口契约已按落地代码回改（`WorkflowSchemaLike`）。
