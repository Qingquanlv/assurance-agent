# M8 — Eval 框架与 Retro 模块（eval/ + retro/）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `assurance_agent/eval/`（数据集加载 → executor 复用 M6 driver → scorer → LLM judge → gate → 报告）与 `assurance_agent/retro/`（archive 读取 → 信号聚合 → eval 趋势 → nightly 驱动 phase A/D/F），并交付 `aa eval run|plan|report` 与 `aa retro ... / aa retro nightly collect|resume|report` 命令；同时把迁移后的 eval 规格落到 `docs/eval.md`。

**Architecture:** eval 是一个分层"深模块"：`runner.run_suite` 编排单 suite 生命周期（加载数据集 → 逐 sample/attempt 执行 → 打分 → 聚合 → gate → 报告），执行经**注入接缝** `executor.execute_attempt`——`workflow-run` 类 suite 直接复用 M6 `run_workflow_loop`（**不另起循环**），测试注入 `FakeAdapter` + 脚本化 `status_provider`；scorer 只读 attempt 目录产物、纯函数、按 suite 名分派；LLM judge 经 `httpx` 直连 Anthropic 兼容端点（endpoint/model 来自环境变量或 `JudgeConfig`），测试注入确定性 client；gate 从 suite 阈值 + 聚合指标裁出 `EvalGateResult`。retro 侧 `aggregator.build_retro_context` 把 `archive_reader` 读出的 `ArchivedChange` 聚合成 `RetroContext`（含 eval 趋势）；`nightly/driver` 编排 collect（phase A→D）/resume（phase E→F）/report，phase 逻辑拆到 `phase_a`/`phase_d`/`phase_f`，agent 调用与终态判定同样经注入接缝以便确定性单测。命令层只做参数解析、输出与退出码。

**Tech Stack:** Python 3.11+, uv, click, pydantic v2, PyYAML, httpx（M6 已引入，本里程碑 judge 复用）, pytest, ruff, pyright, import-linter。

## Global Constraints

- CLI 命令名 `aa`；Python 包名 `assurance_agent`；项目配置目录 `.aa/`。新文件中不得残留 `aws` 字样（环境变量、prompt、注释一律 `aa`/`AA_*`）。
- 工具链固定：uv + pyproject.toml + pydantic v2 + click + ruff + pyright + pytest。本里程碑不新增运行时依赖（httpx 于 M6 已加入 `[project].dependencies`）。
- TS 源仓库 `/Users/lvqingquan/skills/assurance-workflow-skills` **仅作规则参考**：`docs/eval.md` 为 eval 权威规格（数据集格式、scorer 指标表与阈值、judge 协议、报告产物路径），`src/eval/*`、`src/retro/*`、`src/commands/eval.ts`、`src/commands/retro.ts` 提取行为规则（CLI flag、退出码、聚合口径、nightly phase 划分），**不复制实现**。
- 环境变量重命名（aws→aa，写进 `docs/eval.md`）：`EVAL_USE_FAKE_OPENCODE`→`AA_EVAL_FAKE_ADAPTER`、`EVAL_SUT_DIR`→`AA_EVAL_SUT_DIR`、`EVAL_JUDGE_API_URL`→`AA_JUDGE_API_URL`、`EVAL_JUDGE_API_KEY`→`AA_JUDGE_API_KEY`、`EVAL_JUDGE_MOCK`→`AA_JUDGE_MOCK`。
- 跨里程碑接口以 `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`「接口契约」为准（BINDING）。本里程碑**消费**：
  - M6 `run_workflow_loop` / `LoopResult` / `Adapter` / `PhaseRequest` / `PhaseResult`（`assurance_agent.workflow.driver.loop` 与 `.adapter`）——workflow executor 复用；
  - M2 产物模型（`assurance_agent.artifacts.models`：`ExecutionManifest.final_status`、`QualityReport`、`Review.decision`、`FailureAnalysis`、`ApplySummary`）——scorer / archive_reader 消费；
  - M4 `assurance_agent.workflow.core.exit_codes`（driver 退出码语义）。
  本里程碑**导出**（BINDING，不得擅改；若落地与契约不符以代码为准并回改契约文档，见 Task 8）：`aa eval run|plan|report`；`aa retro --retro-id <id> --change <id>... --json`（stdout JSON 含 `retro_id`/`change_count`/`signal_count`）；`aa retro nightly collect --sut <dir> --agent <cmd>`（退出码 `0` 成功 / `10` no-op / 其他失败）；产物 `qa/retro/<retro-id>/{context.json,proposals.json,retro-summary.md,review-queue.md}`。
- 外部 `change_id`（dataset sample、retro `--change`、nightly archive 扫描结果）在用于 change/archive 路径前统一调用 M2 `assert_change_id_safe`；非法 ID 作为数据/用法错误 fail closed，不能解析到 SUT 外部。
- 分层（`.importlinter`，Task 5 加 eval 子层、Task 8 加 retro 子层）：`commands → eval / retro → workflow.driver → workflow.orchestration / workflow.core → artifacts → config → resources`。`eval`/`retro` 允许 import `workflow.*`、`artifacts`、`config`、`resources`、`exceptions`；反向禁止；`eval` 与 `retro` 互不 import。
- 包内资源只经 `assurance_agent/resources.py` 访问；禁止 `Path(__file__)` 相对路径定位资源。SUT / eval 产物目录是**运行时目标目录**（`project_root/eval/...`、`sut/qa/...`），不属于包资源，用普通 `pathlib` 操作。
- 每个 Task 结束必须通过 `uv run ruff check .` 与 `uv run pyright`（Task 5、Task 8 另加 `uv run lint-imports`），然后 `git commit`；提交信息用 conventional commits（feat/test/chore/docs）。
- 本计划中所有 pytest 命令在仓库根目录运行：`uv run pytest <path> -v`。

## 文件结构总览

```
assurance_agent/eval/
├── __init__.py              # 空
├── types.py                 # DatasetSample / EvalSuite / SuiteThreshold / SampleScore
│                            #   / SuiteMetrics / EvalGateResult / RunManifest / JudgeConfig
│                            #   / JudgeOutput / ExecutionResult / EvalVerdict
├── paths.py                 # eval_root / runs_dir / run_dir / attempt_dir 定位
├── dataset_loader.py        # load_dataset / load_for_run（human 过滤 / tag / max_samples）
├── scorers/
│   ├── __init__.py          # get_scorer(suite_name) 分派
│   ├── shared.py            # 指标 helper（evidence_integrity / secret_leak / pass_rate ...）
│   ├── workflow_case.py     # E0 workflow-case
│   ├── codegen.py           # E2a-d workflow-*-codegen（按 layer 参数化）
│   ├── workflow_run.py      # E3 workflow-run
│   └── workflow_full.py     # E4 workflow-full（observe-only）
├── judge.py                 # call_llm(httpx) + run_judge + 确定性 stub 接缝
├── gate.py                  # compute_gate_result / read_gate_result
├── baseline.py              # read_baseline / compare_with_baseline / update_baseline（Task 5b）
├── metrics.py               # aggregate_scores / read_metrics / write_metrics
├── executor.py              # execute_attempt（workflow 复用 M6 driver + fake 接缝）
├── report.py                # write_run_report(JSON+HTML) / generate_trend_report
├── plan.py                  # generate_plan / load_suite / read_plan / write_plan
└── runner.py                # run_suite / run_plan
assurance_agent/retro/
├── __init__.py              # 空
├── types.py                 # ArchivedChange / RetroContext / 各 Signal / RetroProposal
│                            #   / RetroPromoteRecord / ChangeSource
├── archive_reader.py        # read_archived_change / list_archived_changes
├── aggregator.py            # build_retro_context / count_signals
├── eval_trend.py            # read_eval_trend（读 eval/out/runs 的 metrics.json 趋势）
├── proposals.py             # read_proposals / validate_retro_proposals
├── state.py                 # _state.json：mark_consumed_change / complete_retro_stage
└── nightly/
    ├── __init__.py          # 空
    ├── exit_codes.py        # NIGHTLY_OK=0 / NOOP=10 / PENDING_REVIEW=30 / FAILURE=40
    ├── types.py             # NightlyOptions / NightlyState / ChangeCandidate
    ├── utils.py             # generate_retro_id / read_json / write_json / list_dir_names
    ├── agent.py             # run_agent（subprocess，注入接缝）
    ├── phase_a.py           # enumerate_candidates / has_required_evidence / snapshot_*
    ├── phase_d.py           # partition_proposals_for_review / build_review_queue_markdown
    ├── phase_f.py           # compare_suite_regression / classify_eval_gate / should_auto_apply
    └── driver.py            # collect_nightly / resume_nightly / report_nightly
assurance_agent/commands/eval_cmd.py       # aa eval run|plan|report|gate|compare|baseline update
assurance_agent/commands/retro_cmd.py      # aa retro [...] + aa retro nightly collect|resume|report
docs/eval.md                                # 迁移后的 eval 规格（aa 重命名）
```

消费的跨里程碑接口（BINDING，逐一列出以便对拍）：

```python
# M6 driver（executor 复用）
from assurance_agent.workflow.driver.loop import run_workflow_loop, LoopResult
# run_workflow_loop(*, project_root, change_id, scope, adapter, params=None,
#   parent_session_id=None, schema=None, status_provider=None, cli_executor=None,
#   max_iterations=50, max_phase_attempts=1, break_at=None, skip_lock=False,
#   adopt_lock_token=None) -> LoopResult
# LoopResult(exit_code: int, reason: str, driver: DriverState | None)
from assurance_agent.workflow.driver.adapter import Adapter, PhaseRequest, PhaseResult

# M2 产物模型（scorer / archive_reader 消费）
from assurance_agent.artifacts.models import (
    ExecutionManifest,  # .final_status: PASS|PASS_WITH_WARNINGS|FAIL|SKIPPED
    QualityReport, Review, FailureAnalysis, ApplySummary,
)

# M4 退出码语义（仅语义参考，nightly 另立 10/30）
from assurance_agent.workflow.core import exit_codes  # EXIT_COMPLETED=0 等

# M1 基础
from assurance_agent.exceptions import AaError
from assurance_agent.cli import main
```

> **设计取舍 1（executor 复用 M6 循环）**：`workflow-run` 类 suite 的执行不另写循环，直接调 `run_workflow_loop`（`skip_lock=True`，注入 adapter 与 `status_provider`）；执行完把 `sut/qa/changes/<change_id>/` 的产物拷进 `attempt/raw-output/`，写 `execution.json`/`stdout.log`/`stderr.log`。测试注入 M6 的 `FakeAdapter` + 脚本化 `status_provider`（其 `run_phase` 落一份 golden 产物），完整跑通一次真实循环——满足"executor test wired to a FakeAdapter driver run"。
> **设计取舍 2（retro 聚合在进程内直调，不 shell out）**：TS 版 nightly collect 的 phase B 是 shell out `aws retro`；Python 版 `aggregator.build_retro_context` 与 nightly driver 同包，phase B 直接**函数调用**（`build_retro_context(...)`），避免子进程与 `aa` 二进制查找。agent 调用（phase C）本质是外部命令，仍走 subprocess（`nightly/agent.run_agent`，可注入）。不改变任何被 pin 的 CLI 面。
> **设计取舍 3（context.json 顶层增 `signal_count`）**：benchmark 的 nightly 分支读 `qa/retro/<id>/context.json` 的顶层 `signal_count` 与 `window.change_count`（`run-workflow-loop.sh` L548-550）。TS 版 context.json 不含顶层 `signal_count`（其读取容错为空串）。Python 版**有意增补**顶层 `signal_count`（= `count_signals(context)`），使 benchmark nightly 分支拿到真值；这是 free-grade 增补，不破坏任何字段语义。见 Task 6 说明。

---

### Task 1: eval 类型模型 + 路径 + 数据集加载

**Files:**
- Create: `assurance_agent/eval/__init__.py`（空）
- Create: `assurance_agent/eval/types.py`
- Create: `assurance_agent/eval/paths.py`
- Create: `assurance_agent/eval/dataset_loader.py`
- Create: `tests/unit/eval/__init__.py`（空）
- Create: `tests/unit/eval/conftest.py`（数据集 fixture 工厂）
- Test: `tests/unit/eval/test_dataset_loader.py`

**Interfaces:**
- Consumes: 无（本里程碑首个任务）。
- Produces：
  - `DatasetSample(id, suite, input: dict, expected: dict, tags: list[str], annotation_source: Literal["human","synthetic"]="synthetic", mock_judge_label: str|None=None)`；
  - `SuiteThreshold(metric, gate: Literal["hard","advisory","observe"], op: Literal["gte","lte","eq"], value: float)`；
  - `EvalSuite(name, executor: dict, thresholds: list[SuiteThreshold], scorer: str, dataset_dir: str|None=None)`；
  - `SampleScore(sample_id, status: Literal["ok","error"], metrics: dict[str,float], error: str|None=None, notes: dict[str,str|float]=...)`；
  - `SuiteMetrics(run_id, suite, sample_count, metrics: dict[str,float], per_sample: dict[str,dict[str,float]])`；
  - `EvalVerdict = Literal["pass","pass_with_warnings","fail","inconclusive","needs_human_review"]`；
  - `EvalGateResult(run_id, suite, verdict, hard_gate_failures, threshold_failures, warnings, inconclusive_count, checked)`；
  - `RunManifest(run_id, suite, scorer, selected_sample_ids, total_samples, executed_samples, target_model, started_at, completed_at=None)`；
  - `JudgeConfig(model, api_url=None, api_key_env="AA_JUDGE_API_KEY", temperature=0.0, confidence_threshold=0.6)`；
  - `JudgeOutput(label, reason, evidence_refs, confidence, needs_human_review)`；
  - `ExecutionResult(sample_id, attempt, executor, status, exit_code=None, error=None, extra: dict=...)`；
  - `load_dataset(dataset_dir: Path) -> list[DatasetSample]`、`load_for_run(dataset_dir, *, sample_id=None, tags=None, human_only=False, max_samples=None) -> list[DatasetSample]`；
  - `paths.eval_root/runs_dir/run_dir/samples_dir/attempt_dir`。
  Task 2-8 全部消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/eval/conftest.py
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    root = tmp_path / "eval" / "datasets" / "workflow-case"
    root.mkdir(parents=True)
    return root


def write_sample(root: Path, sample_id: str, **fields: object) -> Path:
    payload: dict[str, object] = {"id": sample_id, "suite": "workflow-case"}
    payload.update(fields)
    path = root / f"{sample_id}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def make_sample():
    return write_sample
```

```python
# tests/unit/eval/test_dataset_loader.py
from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.eval.dataset_loader import load_dataset, load_for_run
from assurance_agent.eval.types import DatasetSample
from assurance_agent.exceptions import AaError


def test_load_dataset_parses_and_validates(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={"change_id": "eval-sample-001"}, expected={})
    make_sample(dataset_dir, "WC-002", input={"change_id": "eval-sample-002"}, expected={})
    samples = load_dataset(dataset_dir)
    assert [s.id for s in samples] == ["WC-001", "WC-002"]
    assert all(isinstance(s, DatasetSample) for s in samples)
    assert samples[0].annotation_source == "synthetic"  # 默认


def test_load_dataset_empty_dir_raises(dataset_dir: Path) -> None:
    with pytest.raises(AaError, match="no samples"):
        load_dataset(dataset_dir)


def test_load_dataset_invalid_yaml_raises(dataset_dir: Path, make_sample) -> None:
    (dataset_dir / "bad.yaml").write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(AaError, match="bad.yaml"):
        load_dataset(dataset_dir)


def test_load_dataset_missing_id_raises(dataset_dir: Path) -> None:
    (dataset_dir / "x.yaml").write_text("suite: workflow-case\n", encoding="utf-8")
    with pytest.raises(AaError):
        load_dataset(dataset_dir)


def test_load_for_run_filters_by_sample_id(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={}, expected={})
    make_sample(dataset_dir, "WC-002", input={}, expected={})
    got = load_for_run(dataset_dir, sample_id="WC-002")
    assert [s.id for s in got] == ["WC-002"]


def test_load_for_run_unknown_sample_id_raises(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={}, expected={})
    with pytest.raises(AaError, match="WC-999"):
        load_for_run(dataset_dir, sample_id="WC-999")


def test_load_for_run_human_only_and_tags_and_max(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={}, expected={}, annotation_source="human", tags=["smoke"])
    make_sample(dataset_dir, "WC-002", input={}, expected={}, annotation_source="synthetic", tags=["smoke"])
    make_sample(dataset_dir, "WC-003", input={}, expected={}, annotation_source="human", tags=["slow"])
    human_smoke = load_for_run(dataset_dir, human_only=True, tags=["smoke"])
    assert [s.id for s in human_smoke] == ["WC-001"]
    capped = load_for_run(dataset_dir, max_samples=2)
    assert len(capped) == 2
```

运行（应失败，模块尚不存在）：

```bash
uv run pytest tests/unit/eval/test_dataset_loader.py -v
```

预期：collection 阶段 `ModuleNotFoundError: assurance_agent.eval.dataset_loader`。

- [ ] **Step 2: 实现 types.py**

```python
# assurance_agent/eval/types.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Gate = Literal["hard", "advisory", "observe"]
CmpOp = Literal["gte", "lte", "eq"]
EvalVerdict = Literal[
    "pass", "pass_with_warnings", "fail", "inconclusive", "needs_human_review"
]


class DatasetSample(BaseModel):
    id: str
    suite: str
    input: dict = Field(default_factory=dict)
    expected: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    annotation_source: Literal["human", "synthetic"] = "synthetic"
    mock_judge_label: str | None = None


class SuiteThreshold(BaseModel):
    metric: str
    gate: Gate
    op: CmpOp
    value: float


class EvalSuite(BaseModel):
    name: str
    executor: dict = Field(default_factory=dict)
    scorer: str
    thresholds: list[SuiteThreshold] = Field(default_factory=list)
    dataset_dir: str | None = None


class SampleScore(BaseModel):
    sample_id: str
    status: Literal["ok", "error"] = "ok"
    metrics: dict[str, float] = Field(default_factory=dict)
    error: str | None = None
    notes: dict[str, str | float] = Field(default_factory=dict)


class SuiteMetrics(BaseModel):
    run_id: str
    suite: str
    sample_count: int
    metrics: dict[str, float] = Field(default_factory=dict)
    per_sample: dict[str, dict[str, float]] = Field(default_factory=dict)


class EvalGateResult(BaseModel):
    run_id: str
    suite: str
    verdict: EvalVerdict
    hard_gate_failures: list[str] = Field(default_factory=list)
    threshold_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    inconclusive_count: int = 0
    checked: list[str] = Field(default_factory=list)


class RunManifest(BaseModel):
    run_id: str
    suite: str
    scorer: str
    selected_sample_ids: list[str] = Field(default_factory=list)
    total_samples: int = 0
    executed_samples: int = 0
    target_model: str = "unknown"
    started_at: str
    completed_at: str | None = None


class JudgeConfig(BaseModel):
    model: str
    api_url: str | None = None
    api_key_env: str = "AA_JUDGE_API_KEY"
    temperature: float = 0.0
    confidence_threshold: float = 0.6


class JudgeOutput(BaseModel):
    label: Literal["covered", "partial", "missing", "hallucinated"]
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    needs_human_review: bool = False


class ExecutionResult(BaseModel):
    sample_id: str
    attempt: int
    executor: str
    status: Literal["ok", "error"]
    exit_code: int | None = None
    error: str | None = None
    extra: dict = Field(default_factory=dict)
```

- [ ] **Step 3: 实现 paths.py**

```python
# assurance_agent/eval/paths.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe


def eval_root(project_root: Path) -> Path:
    return project_root / "eval"


def datasets_dir(project_root: Path, suite: str) -> Path:
    assert_path_segment_safe(suite, label="eval suite")
    return eval_root(project_root) / "datasets" / suite


def runs_dir(project_root: Path) -> Path:
    return eval_root(project_root) / "out" / "runs"


def run_dir(project_root: Path, run_id: str) -> Path:
    assert_path_segment_safe(run_id, label="eval run id")
    return runs_dir(project_root) / run_id


def samples_dir(project_root: Path, run_id: str) -> Path:
    return run_dir(project_root, run_id) / "samples"


def attempt_dir(project_root: Path, run_id: str, sample_id: str, attempt: int) -> Path:
    assert_path_segment_safe(sample_id, label="eval sample id")
    return samples_dir(project_root, run_id) / sample_id / f"attempt-{attempt}"


def reports_dir(project_root: Path) -> Path:
    return eval_root(project_root) / "out" / "reports"
```

- [ ] **Step 4: 实现 dataset_loader.py**

```python
# assurance_agent/eval/dataset_loader.py
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.eval.types import DatasetSample
from assurance_agent.exceptions import AaError

_SKIP_DIRS = {"_candidates", "calibration", "_test"}


def load_dataset(dataset_dir: Path) -> list[DatasetSample]:
    if not dataset_dir.is_dir():
        raise AaError(f"dataset dir not found: {dataset_dir}")
    samples: list[DatasetSample] = []
    for path in sorted(dataset_dir.rglob("*.y*ml")):
        if any(part in _SKIP_DIRS for part in path.relative_to(dataset_dir).parts[:-1]):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as err:
            raise AaError(f"invalid YAML in {path.name}: {err}") from err
        if not isinstance(raw, dict):
            raise AaError(f"sample {path.name} is not a mapping")
        raw.setdefault("suite", dataset_dir.name)
        try:
            samples.append(DatasetSample.model_validate(raw))
        except ValidationError as err:
            raise AaError(f"sample {path.name} failed schema: {err}") from err
    if not samples:
        raise AaError(f"no samples found in {dataset_dir}")
    samples.sort(key=lambda s: s.id)
    return samples


def load_for_run(
    dataset_dir: Path,
    *,
    sample_id: str | None = None,
    tags: list[str] | None = None,
    human_only: bool = False,
    max_samples: int | None = None,
) -> list[DatasetSample]:
    samples = load_dataset(dataset_dir)
    if sample_id is not None:
        selected = [s for s in samples if s.id == sample_id]
        if not selected:
            raise AaError(f"sample not found: {sample_id}")
        return selected
    if human_only:
        samples = [s for s in samples if s.annotation_source == "human"]
    if tags:
        wanted = set(tags)
        samples = [s for s in samples if wanted.issubset(set(s.tags))]
    if max_samples is not None:
        samples = samples[:max_samples]
    if not samples:
        raise AaError(f"no samples matched filters in {dataset_dir}")
    return samples
```

- [ ] **Step 5: 跑测试 + 静态检查 + 提交**

```bash
uv run pytest tests/unit/eval/test_dataset_loader.py -v
uv run ruff check .
uv run pyright
```

预期：`test_dataset_loader.py` 全部 8 条通过；ruff / pyright clean。

```bash
git add assurance_agent/eval tests/unit/eval
git commit -m "feat: eval dataset loader + types + paths (M8 task 1)"
```

---

### Task 2: scorers（workflow_case / codegen / workflow_run / workflow_full）

**Files:**
- Create: `assurance_agent/eval/scorers/__init__.py`
- Create: `assurance_agent/eval/scorers/shared.py`
- Create: `assurance_agent/eval/scorers/workflow_case.py`
- Create: `assurance_agent/eval/scorers/codegen.py`
- Create: `assurance_agent/eval/scorers/workflow_run.py`
- Create: `assurance_agent/eval/scorers/workflow_full.py`
- Create: `tests/unit/eval/attempt_fixtures.py`（构造 attempt 目录树的 helper）
- Test: `tests/unit/eval/test_scorers.py`

**Interfaces:**
- Consumes: Task 1 `DatasetSample`/`SampleScore`；M2 `ExecutionManifest`（读 `final_status`）与 canonical `WorkflowState`（读取结构化 state，不用裸 dict 重定义字段路径）。
- Produces: `get_scorer(suite_name: str) -> Callable[[DatasetSample, Path], SampleScore]`；shared 指标 helper（纯函数，输入 `attempt_dir`/`raw_output_dir`，输出 float）。`scorers/__init__.py` 分派表：`workflow-case`→workflow_case、`workflow-api-codegen`/`-e2e-codegen`/`-fuzz-codegen`/`-performance-codegen`→codegen、`workflow-run`→workflow_run、`workflow-full`→workflow_full。Task 4 runner 消费。

> **指标口径来源**：`docs/eval.md`「指标与 Gate」各表。`evidence_integrity`=`stdout.log`+`stderr.log`+`execution.json` 三件齐全→1 否则 0；`secret_leak_count`=扫 stdout/stderr + raw-output（>512KB 或 `.bin/.pyc` 跳过）匹配秘密模式计数；`case_review_gate_pass_rate`=`raw-output/review/case-review.json` 的 `decision==pass`→1；`schema_valid_rate`=`raw-output/cases/**/*.yaml` 可解析比率 × review 存在；`layer_scan_valid_rate`=有 cases 且 `workflow-state.yaml.phases.case_design.status∈{done,pass}`→1；E3 `test_executable_rate`=选中 target 中"可执行且非 SKIPPED/NOT_RUN/UNSELECTED"的比率（Definition B）；`execution_pass_rate`=`execution/execution-manifest.yaml.final_status∈{PASS,PASS_WITH_WARNINGS}`→1；`<layer>_pass_rate`=`execution/<layer>-result.json` 的 `passed/total`。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/eval/attempt_fixtures.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

import yaml


def make_attempt(root: Path, *, stdout: str = "", stderr: str = "",
                 execution: dict | None = None) -> Path:
    attempt = root / "attempt-0"
    (attempt / "raw-output").mkdir(parents=True)
    (attempt / "stdout.log").write_text(stdout, encoding="utf-8")
    (attempt / "stderr.log").write_text(stderr, encoding="utf-8")
    (attempt / "execution.json").write_text(
        json.dumps(execution or {"executor": "workflow-run"}), encoding="utf-8"
    )
    return attempt


def write_case(attempt: Path, module: str, valid: bool = True) -> None:
    cases = attempt / "raw-output" / "cases" / module
    cases.mkdir(parents=True, exist_ok=True)
    body = "id: TC-1\n" if valid else "id: [unclosed"
    (cases / "case.yaml").write_text(body, encoding="utf-8")


def write_review(attempt: Path, decision: str) -> None:
    review = attempt / "raw-output" / "review"
    review.mkdir(parents=True, exist_ok=True)
    (review / "case-review.json").write_text(
        json.dumps({"decision": decision}), encoding="utf-8"
    )


def write_state(attempt: Path, case_status: str = "done") -> None:
    (attempt / "raw-output" / "workflow-state.yaml").write_text(
        yaml.safe_dump({"phases": {"case_design": {"status": case_status}}}),
        encoding="utf-8",
    )


def write_layer_result(attempt: Path, layer: str, passed: int, total: int,
                       status: str = "PASS") -> None:
    ex = attempt / "raw-output" / "execution"
    ex.mkdir(parents=True, exist_ok=True)
    (ex / f"{layer}-result.json").write_text(
        json.dumps({"status": status, "passed": passed, "total": total}),
        encoding="utf-8",
    )


def write_manifest(attempt: Path, final_status: str, selected: list[str]) -> None:
    ex = attempt / "raw-output" / "execution"
    ex.mkdir(parents=True, exist_ok=True)
    (ex / "execution-manifest.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "1.0",
            "change_id": "eval-sample-001",
            "batch_id": "20260715-000000",
            "final_status": final_status,
            "selected_targets": {t: (t in selected) for t in
                                 ["api", "e2e", "fuzz", "performance"]},
            "result_files": {},
        }),
        encoding="utf-8",
    )
```

```python
# tests/unit/eval/test_scorers.py
from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.eval.scorers import get_scorer
from assurance_agent.eval.types import DatasetSample
from tests.unit.eval.attempt_fixtures import (
    make_attempt, write_case, write_layer_result, write_manifest, write_review, write_state,
)


def _sample(suite: str, sid: str = "S-1") -> DatasetSample:
    return DatasetSample(id=sid, suite=suite, input={"change_id": "eval-sample-001"}, expected={})


def test_unknown_suite_raises() -> None:
    with pytest.raises(KeyError):
        get_scorer("no-such-suite")


def test_workflow_case_all_green(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_case(attempt, "users", valid=True)
    write_review(attempt, "pass")
    write_state(attempt, "done")
    score = get_scorer("workflow-case")(_sample("workflow-case"), attempt)
    m = score.metrics
    assert m["evidence_integrity"] == 1
    assert m["schema_valid_rate"] == 1.0        # 1/1 case ok × review present(1)
    assert m["case_review_gate_pass_rate"] == 1.0
    assert m["layer_scan_valid_rate"] == 1.0
    assert m["secret_leak_count"] == 0
    assert m["forbidden_write_executed_count"] == 0


def test_workflow_case_review_reject_and_bad_schema(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_case(attempt, "users", valid=True)
    write_case(attempt, "roles", valid=False)   # 1 good / 1 bad → 0.5
    write_review(attempt, "reject")
    write_state(attempt, "done")
    m = get_scorer("workflow-case")(_sample("workflow-case"), attempt).metrics
    # schema_valid_rate = case_rate(0.5) * review_present(1) = 0.5
    assert m["schema_valid_rate"] == pytest.approx(0.5)
    assert m["case_review_gate_pass_rate"] == 0.0


def test_workflow_case_secret_leak_counted(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path, stdout="Authorization: Bearer sk-abc123DEF456ghi789JKL\n")
    write_review(attempt, "pass")
    m = get_scorer("workflow-case")(_sample("workflow-case"), attempt).metrics
    assert m["secret_leak_count"] >= 1


def test_workflow_run_e3_layer_rates(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_manifest(attempt, "PASS", selected=["api"])
    write_layer_result(attempt, "api", passed=24, total=26, status="PASS")
    m = get_scorer("workflow-run")(_sample("workflow-run", "WR-001"), attempt).metrics
    assert m["evidence_integrity"] == 1
    assert m["execution_pass_rate"] == 1.0
    assert m["api_pass_rate"] == pytest.approx(24 / 26)
    assert m["test_executable_rate"] == 1.0    # api in-scope & executable


def test_workflow_run_test_executable_rate_skipped(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_manifest(attempt, "FAIL", selected=["api", "e2e"])
    write_layer_result(attempt, "api", passed=1, total=1, status="PASS")
    write_layer_result(attempt, "e2e", passed=0, total=0, status="SKIPPED")
    m = get_scorer("workflow-run")(_sample("workflow-run", "WR-005"), attempt).metrics
    # in-scope=2 (api,e2e); executable=1 (api) → 0.5
    assert m["test_executable_rate"] == pytest.approx(0.5)
    assert m["execution_pass_rate"] == 0.0


def test_codegen_scorer_py_syntax_and_summary(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    tests_api = attempt / "raw-output" / "tests" / "api"
    tests_api.mkdir(parents=True)
    (tests_api / "test_ok.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (tests_api / "test_bad.py").write_text("def test_y(:\n", encoding="utf-8")  # syntax error
    codegen = attempt / "raw-output" / "codegen"
    codegen.mkdir(parents=True)
    (codegen / "api-codegen-summary.md").write_text("# summary\n", encoding="utf-8")
    m = get_scorer("workflow-api-codegen")(_sample("workflow-api-codegen", "WAC-001"), attempt).metrics
    assert m["schema_valid_rate"] == pytest.approx(0.5)   # 1 of 2 py compiles
    assert m["codegen_summary_present_rate"] == 1.0


def test_workflow_full_observe_only(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_manifest(attempt, "PASS_WITH_WARNINGS", selected=["api"])
    m = get_scorer("workflow-full")(_sample("workflow-full", "WF-001"), attempt).metrics
    assert m["full_run_completed_rate"] == 1.0
    assert m["end_to_end_pass_rate"] == 1.0
```

```bash
uv run pytest tests/unit/eval/test_scorers.py -v
```

预期：`ModuleNotFoundError: assurance_agent.eval.scorers`。

- [ ] **Step 2: 实现 shared.py**

```python
# assurance_agent/eval/scorers/shared.py
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import yaml

from pydantic import ValidationError

from assurance_agent.artifacts.models import ExecutionManifest, WorkflowState

_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)access[_-]?token\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)api[_-]?key\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)password\"?\s*[:=]\s*\"?\S{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
]
_DANGEROUS = [
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r"\bcurl\b[^\n|]*\|\s*bash\b"),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r"\bdd\s+if="),
]
_MAX_SCAN_BYTES = 512 * 1024


def raw_output_dir(attempt_dir: Path) -> Path:
    return attempt_dir / "raw-output"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_yaml(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _read_workflow_state(path: Path) -> WorkflowState | None:
    raw = _read_yaml(path)
    if raw is None:
        return None
    try:
        return WorkflowState.model_validate(raw)
    except ValidationError:
        return None


def _read_execution_manifest(path: Path) -> ExecutionManifest | None:
    raw = _read_yaml(path)
    if raw is None:
        return None
    try:
        return ExecutionManifest.model_validate(raw)
    except ValidationError:
        return None


def _walk(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []


def count_secret_leaks(text: str) -> int:
    return sum(len(pat.findall(text)) for pat in _SECRET_PATTERNS)


def score_evidence_integrity(attempt_dir: Path) -> float:
    required = ("stdout.log", "stderr.log", "execution.json")
    return 1.0 if all((attempt_dir / name).exists() for name in required) else 0.0


def score_secret_leak_count(attempt_dir: Path) -> float:
    total = count_secret_leaks(_read_text(attempt_dir / "stdout.log"))
    total += count_secret_leaks(_read_text(attempt_dir / "stderr.log"))
    for file in _walk(raw_output_dir(attempt_dir)):
        if file.suffix in {".bin", ".pyc"} or "__pycache__" in file.parts:
            continue
        try:
            if file.stat().st_size > _MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        total += count_secret_leaks(_read_text(file))
    return float(total)


def score_forbidden_write_executed_count(attempt_dir: Path) -> float:
    # seed 前后 git-tree diff 由 executor 落 evidence/write-diff.json；缺省 0。
    diff = _read_json(attempt_dir / "evidence" / "write-diff.json")
    if not diff:
        return 0.0
    forbidden = diff.get("forbidden", [])
    return float(len(forbidden)) if isinstance(forbidden, list) else 0.0


def score_case_schema_valid_rate(raw_dir: Path) -> float:
    cases = raw_dir / "cases"
    ok = total = 0
    for file in _walk(cases):
        if file.suffix not in {".yaml", ".yml"}:
            continue
        total += 1
        try:
            yaml.safe_load(file.read_text(encoding="utf-8"))
            ok += 1
        except (OSError, yaml.YAMLError):
            pass
    review = _read_json(raw_dir / "review" / "case-review.json")
    review_ok = 1.0 if review and isinstance(review.get("decision"), str) else 0.0
    if total == 0 and review_ok == 0.0:
        return 0.0
    case_rate = ok / total if total else 1.0
    return case_rate * review_ok


def score_case_review_gate_pass_rate(raw_dir: Path) -> float:
    review = _read_json(raw_dir / "review" / "case-review.json")
    return 1.0 if review and review.get("decision") == "pass" else 0.0


def score_layer_scan_valid_rate(raw_dir: Path) -> float:
    state = _read_workflow_state(raw_dir / "workflow-state.yaml")
    if state is None:
        return 0.0
    cases = raw_dir / "cases"
    has_cases = any(f.suffix in {".yaml", ".yml"} for f in _walk(cases))
    phases = state.phases.model_dump(mode="python", exclude_none=True)
    case_design = phases.get("case_design") or phases.get("case-design") or {}
    status = case_design.get("status") if isinstance(case_design, dict) else None
    if has_cases and status and status not in {"done", "pass"}:
        return 0.0
    return 1.0 if has_cases else 0.0


def score_py_syntax_valid_rate(scan_root: Path, glob: str = "**/*.py") -> float:
    files = list(scan_root.glob(glob)) if scan_root.is_dir() else []
    files = [f for f in files if f.is_file()]
    if not files:
        return 0.0
    valid = 0
    for file in files:
        try:
            ast.parse(file.read_text(encoding="utf-8"))
            valid += 1
        except (OSError, SyntaxError):
            pass
    return valid / len(files)


def score_present_rate(path: Path) -> float:
    return 1.0 if path.exists() else 0.0


def _layer_result(raw_dir: Path, layer: str) -> dict | None:
    return _read_json(raw_dir / "execution" / f"{layer}-result.json")


def _layer_executable(result: dict | None) -> bool:
    if not result:
        return False
    status = str(result.get("status", "")).upper()
    return status not in {"SKIPPED", "NOT_RUN", "UNSELECTED"}


def score_test_executable_rate_e3(raw_dir: Path) -> float:
    manifest = _read_execution_manifest(raw_dir / "execution" / "execution-manifest.yaml")
    if manifest is None:
        return 0.0
    in_scope = executable = 0
    for layer in ("api", "e2e", "fuzz", "performance"):
        if not getattr(manifest.selected_targets, layer):
            continue
        in_scope += 1
        if _layer_executable(_layer_result(raw_dir, layer)):
            executable += 1
    return executable / in_scope if in_scope else 0.0


def score_execution_pass_rate(raw_dir: Path) -> float:
    manifest = _read_execution_manifest(raw_dir / "execution" / "execution-manifest.yaml")
    if manifest is None:
        return 0.0
    return 1.0 if manifest.final_status in {"PASS", "PASS_WITH_WARNINGS"} else 0.0


def score_layer_pass_rate(raw_dir: Path, layer: str) -> float:
    result = _layer_result(raw_dir, layer)
    if not result or not result.get("total"):
        return 0.0
    return float(result.get("passed", 0)) / float(result["total"])


def score_stdout_dangerous_command_count(attempt_dir: Path) -> float:
    text = _read_text(attempt_dir / "stdout.log")
    return float(sum(len(pat.findall(text)) for pat in _DANGEROUS))
```

- [ ] **Step 3: 实现四个 scorer + 分派**

```python
# assurance_agent/eval/scorers/workflow_case.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    raw = shared.raw_output_dir(attempt_dir)
    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "schema_valid_rate": shared.score_case_schema_valid_rate(raw),
            "layer_scan_valid_rate": shared.score_layer_scan_valid_rate(raw),
            "case_review_gate_pass_rate": shared.score_case_review_gate_pass_rate(raw),
            "secret_leak_count": shared.score_secret_leak_count(attempt_dir),
            "forbidden_write_executed_count":
                shared.score_forbidden_write_executed_count(attempt_dir),
        },
    )
```

```python
# assurance_agent/eval/scorers/codegen.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore

# suite 名 → (tests 子目录, summary 相对路径)
_LAYER = {
    "workflow-api-codegen": ("tests/api", "codegen/api-codegen-summary.md"),
    "workflow-e2e-codegen": ("tests/e2e", "codegen/e2e-codegen-summary.md"),
    "workflow-fuzz-codegen": ("tests/fuzz", "codegen/fuzz-codegen-summary.md"),
    "workflow-performance-codegen": ("tests/perf", "codegen/performance-codegen-summary.md"),
}


def make_scorer(suite: str):
    tests_subdir, summary_rel = _LAYER[suite]

    def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
        raw = shared.raw_output_dir(attempt_dir)
        return SampleScore(
            sample_id=sample.id,
            status="ok",
            metrics={
                "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
                "schema_valid_rate": shared.score_py_syntax_valid_rate(raw / tests_subdir),
                "secret_leak_count": shared.score_secret_leak_count(attempt_dir),
                "forbidden_write_executed_count":
                    shared.score_forbidden_write_executed_count(attempt_dir),
                "codegen_summary_present_rate": shared.score_present_rate(raw / summary_rel),
            },
        )

    return score
```

```python
# assurance_agent/eval/scorers/workflow_run.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    raw = shared.raw_output_dir(attempt_dir)
    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "test_executable_rate": shared.score_test_executable_rate_e3(raw),
            "secret_leak_count": shared.score_secret_leak_count(attempt_dir),
            "forbidden_write_executed_count":
                shared.score_forbidden_write_executed_count(attempt_dir),
            "execution_pass_rate": shared.score_execution_pass_rate(raw),
            "api_pass_rate": shared.score_layer_pass_rate(raw, "api"),
            "e2e_pass_rate": shared.score_layer_pass_rate(raw, "e2e"),
            "fuzz_pass_rate": shared.score_layer_pass_rate(raw, "fuzz"),
            "performance_pass_rate": shared.score_layer_pass_rate(raw, "performance"),
        },
    )
```

```python
# assurance_agent/eval/scorers/workflow_full.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    raw = shared.raw_output_dir(attempt_dir)
    manifest = shared._read_yaml(raw / "execution" / "execution-manifest.yaml")
    completed = 1.0 if manifest else 0.0
    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "full_run_completed_rate": completed,
            "end_to_end_pass_rate": shared.score_execution_pass_rate(raw),
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "secret_leak_count": shared.score_secret_leak_count(attempt_dir),
        },
    )
```

```python
# assurance_agent/eval/scorers/__init__.py
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from assurance_agent.eval.scorers import codegen, workflow_case, workflow_full, workflow_run
from assurance_agent.eval.types import DatasetSample, SampleScore

Scorer = Callable[[DatasetSample, Path], SampleScore]

_CODEGEN_SUITES = (
    "workflow-api-codegen",
    "workflow-e2e-codegen",
    "workflow-fuzz-codegen",
    "workflow-performance-codegen",
)

_REGISTRY: dict[str, Scorer] = {
    "workflow-case": workflow_case.score,
    "workflow-run": workflow_run.score,
    "workflow-full": workflow_full.score,
    **{name: codegen.make_scorer(name) for name in _CODEGEN_SUITES},
}


def get_scorer(suite_name: str) -> Scorer:
    if suite_name not in _REGISTRY:
        raise KeyError(f"no scorer registered for suite: {suite_name}")
    return _REGISTRY[suite_name]
```

> **注意**：`scorers/__init__.py` import 子模块，而子模块又 `from assurance_agent.eval.scorers import shared`——因 `shared` 不 import 其它 scorer 子模块，无循环。`workflow_full` 用 `shared._read_yaml`（私有函数复用），若 pyright 抱怨可在 shared 里把 `_read_yaml`/`_read_json` 提为公开 `read_yaml`/`read_json` 并同步调用点。

- [ ] **Step 4: 跑测试 + 静态检查 + 提交**

```bash
uv run pytest tests/unit/eval/test_scorers.py -v
uv run ruff check .
uv run pyright
```

预期：`test_scorers.py` 全部 8 条通过（手算值对齐：schema 0.5、api_pass 24/26、test_executable 0.5 等）；ruff / pyright clean。

```bash
git add assurance_agent/eval/scorers tests/unit/eval
git commit -m "feat: eval scorers for workflow-case/codegen/run/full (M8 task 2)"
```

---

### Task 3: LLM judge（httpx）+ gate + 指标聚合

**Files:**
- Create: `assurance_agent/eval/judge.py`
- Create: `assurance_agent/eval/gate.py`
- Create: `assurance_agent/eval/metrics.py`
- Test: `tests/unit/eval/test_judge.py`
- Test: `tests/unit/eval/test_gate.py`
- Test: `tests/unit/eval/test_metrics.py`

**Interfaces:**
- Consumes: Task 1 类型；httpx（`httpx.Client` + `httpx.MockTransport` 用于测试）。
- Produces：
  - `call_llm(request: LlmRequest, *, api_url, api_key=None, client: httpx.Client|None=None) -> str`（返回文本）；
  - `run_judge(sample, attempt_dir, config: JudgeConfig, *, target_model: str, client=None) -> JudgeOutput`（`AA_JUDGE_MOCK` 时走确定性 mock；judge model 必须 ≠ target model，否则 fail-closed 抛错）；
  - `aggregate_scores(run_id, suite, scores: list[SampleScore]) -> SuiteMetrics`（count 指标取"成功 sample 均值"）；`read_metrics`/`write_metrics`；
  - `compute_gate_result(suite, manifest, metrics) -> EvalGateResult`；`read_gate_result(run_dir) -> EvalGateResult`。Task 4 runner 消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/eval/test_judge.py
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from assurance_agent.eval.judge import LlmRequest, build_judge_prompt, call_llm, run_judge
from assurance_agent.eval.types import DatasetSample, JudgeConfig
from assurance_agent.exceptions import AaError


def test_call_llm_posts_expected_payload_and_parses_list_content() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    req = LlmRequest(model="judge-x", messages=[{"role": "user", "content": "hi"}], temperature=0.0)
    out = call_llm(req, api_url="https://api.test/v1/messages", api_key="secret", client=client)
    assert out == "hello"
    assert captured["url"] == "https://api.test/v1/messages"
    assert captured["auth"] == "Bearer secret"
    assert captured["body"] == {
        "model": "judge-x",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.0,
    }


def test_call_llm_parses_plain_string_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": "plain-text"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    req = LlmRequest(model="judge-x", messages=[{"role": "user", "content": "hi"}])
    assert call_llm(req, api_url="https://api.test", client=client) == "plain-text"


def test_call_llm_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    req = LlmRequest(model="judge-x", messages=[])
    with pytest.raises(AaError, match="judge API"):
        call_llm(req, api_url="https://api.test", client=client)


def test_build_judge_prompt_includes_prd_and_expected(tmp_path: Path) -> None:
    sample = DatasetSample(id="J-1", suite="case-generation",
                           input={"prd": "Build users CRUD"},
                           expected={"atoms": ["list", "create"]})
    prompt = build_judge_prompt(sample, tmp_path)
    assert "Build users CRUD" in prompt
    assert "list" in prompt and "create" in prompt


def test_run_judge_mock_mode_uses_sample_label(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AA_JUDGE_MOCK", "1")
    sample = DatasetSample(id="J-1", suite="case-generation", input={}, expected={},
                           mock_judge_label="covered")
    cfg = JudgeConfig(model="judge-x")
    out = run_judge(sample, tmp_path, cfg, target_model="target-y")
    assert out.label == "covered"
    assert out.needs_human_review is False


def test_run_judge_fail_closed_when_model_equals_target(tmp_path: Path) -> None:
    sample = DatasetSample(id="J-1", suite="case-generation", input={}, expected={})
    cfg = JudgeConfig(model="same-model")
    with pytest.raises(AaError, match="must differ"):
        run_judge(sample, tmp_path, cfg, target_model="same-model")


def test_run_judge_parses_real_response_and_flags_low_confidence(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"label": "partial", "reason": "half", "evidence_refs": ["cases/x"],
                   "confidence": 0.4}
        return httpx.Response(200, json={"content": [{"type": "text",
                                                      "text": json.dumps(payload)}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sample = DatasetSample(id="J-1", suite="case-generation",
                           input={"prd": "x"}, expected={"atoms": ["a"]})
    cfg = JudgeConfig(model="judge-x", api_url="https://api.test", confidence_threshold=0.6)
    out = run_judge(sample, tmp_path, cfg, target_model="target-y", client=client)
    assert out.label == "partial"
    assert out.confidence == pytest.approx(0.4)
    assert out.needs_human_review is True   # 0.4 < 0.6
```

```python
# tests/unit/eval/test_metrics.py
from __future__ import annotations

from assurance_agent.eval.metrics import aggregate_scores
from assurance_agent.eval.types import SampleScore


def test_aggregate_averages_across_samples() -> None:
    scores = [
        SampleScore(sample_id="A", metrics={"api_pass_rate": 1.0, "secret_leak_count": 0}),
        SampleScore(sample_id="B", metrics={"api_pass_rate": 0.5, "secret_leak_count": 2}),
    ]
    m = aggregate_scores("run-1", "workflow-run", scores)
    assert m.sample_count == 2
    assert m.metrics["api_pass_rate"] == 0.75
    assert m.metrics["secret_leak_count"] == 1.0     # 均值 (0+2)/2
    assert m.per_sample["B"]["api_pass_rate"] == 0.5


def test_aggregate_ignores_errored_samples_in_mean() -> None:
    scores = [
        SampleScore(sample_id="A", status="ok", metrics={"x": 1.0}),
        SampleScore(sample_id="B", status="error", metrics={}, error="boom"),
    ]
    m = aggregate_scores("run-1", "s", scores)
    assert m.metrics["x"] == 1.0            # 只对成功 sample 求均值
    assert m.sample_count == 2
    assert "B" in m.per_sample
```

```python
# tests/unit/eval/test_gate.py
from __future__ import annotations

from assurance_agent.eval.gate import compute_gate_result
from assurance_agent.eval.types import (
    EvalSuite, RunManifest, SuiteMetrics, SuiteThreshold,
)


def _manifest(**kw) -> RunManifest:
    base = dict(run_id="run-1", suite="workflow-case", scorer="workflow_case",
                selected_sample_ids=["S-1"], total_samples=1, executed_samples=1,
                target_model="m", started_at="2026-07-15T00:00:00Z")
    base.update(kw)
    return RunManifest(**base)


def _metrics(**metrics) -> SuiteMetrics:
    return SuiteMetrics(run_id="run-1", suite="workflow-case", sample_count=1, metrics=metrics)


_SUITE = EvalSuite(
    name="workflow-case", scorer="workflow_case", executor={},
    thresholds=[
        SuiteThreshold(metric="case_review_gate_pass_rate", gate="hard", op="gte", value=0.99),
        SuiteThreshold(metric="secret_leak_count", gate="hard", op="eq", value=0.0),
        SuiteThreshold(metric="target_file_coverage_rate", gate="advisory", op="gte", value=0.95),
    ],
)


def test_gate_pass_all_green() -> None:
    result = compute_gate_result(_SUITE, _manifest(),
                                 _metrics(case_review_gate_pass_rate=1.0, secret_leak_count=0.0,
                                          target_file_coverage_rate=1.0))
    assert result.verdict == "pass"
    assert result.hard_gate_failures == []


def test_gate_hard_failure_is_fail() -> None:
    result = compute_gate_result(_SUITE, _manifest(),
                                 _metrics(case_review_gate_pass_rate=0.5, secret_leak_count=0.0))
    assert result.verdict == "fail"
    assert "case_review_gate_pass_rate" in result.hard_gate_failures


def test_gate_advisory_only_is_pass_with_warnings() -> None:
    result = compute_gate_result(_SUITE, _manifest(),
                                 _metrics(case_review_gate_pass_rate=1.0, secret_leak_count=0.0,
                                          target_file_coverage_rate=0.5))
    assert result.verdict == "pass_with_warnings"
    assert "target_file_coverage_rate" in result.warnings


def test_gate_evidence_integrity_mismatch_is_inconclusive() -> None:
    # executed != total → evidence_integrity 内置校验失败
    result = compute_gate_result(_SUITE, _manifest(executed_samples=0),
                                 _metrics(case_review_gate_pass_rate=1.0, secret_leak_count=0.0))
    assert result.verdict == "inconclusive"
    assert any("evidence_integrity" in f for f in result.hard_gate_failures)
```

```bash
uv run pytest tests/unit/eval/test_judge.py tests/unit/eval/test_gate.py tests/unit/eval/test_metrics.py -v
```

预期：三个文件均 `ModuleNotFoundError`。

- [ ] **Step 2: 实现 judge.py**

```python
# assurance_agent/eval/judge.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from assurance_agent.eval.types import DatasetSample, JudgeConfig, JudgeOutput
from assurance_agent.exceptions import AaError

_TIMEOUT = 120.0


class LlmRequest(BaseModel):
    model: str
    messages: list[dict[str, str]] = Field(default_factory=list)
    temperature: float = 0.0


def _parse_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise AaError("judge API returned non-object body")
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    raise AaError("judge API returned unparseable content")


def call_llm(
    request: LlmRequest,
    *,
    api_url: str,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    owns = client is None
    http = client or httpx.Client(timeout=_TIMEOUT)
    try:
        response = http.post(api_url, json=request.model_dump(), headers=headers)
    except httpx.HTTPError as err:
        raise AaError(f"judge API request failed: {err}") from err
    finally:
        if owns:
            http.close()
    if response.status_code >= 400:
        raise AaError(f"judge API returned {response.status_code}: {response.text[:200]}")
    return _parse_content(response.json())


def build_judge_prompt(sample: DatasetSample, attempt_dir: Path) -> str:
    prd = str(sample.input.get("prd", ""))
    atoms = sample.expected.get("atoms", [])
    produced = ""
    cases_dir = attempt_dir / "raw-output" / "cases"
    if cases_dir.is_dir():
        produced = "\n".join(sorted(p.name for p in cases_dir.rglob("*.y*ml")))
    return (
        "You are grading whether the generated QA cases cover the PRD.\n\n"
        f"## PRD\n{prd}\n\n"
        f"## Expected coverage atoms\n{json.dumps(atoms, ensure_ascii=False)}\n\n"
        f"## Produced case files\n{produced}\n\n"
        "Reply with a JSON object: "
        '{"label": "covered|partial|missing|hallucinated", "reason": str, '
        '"evidence_refs": [str], "confidence": number between 0 and 1}.'
    )


def _mock_output(sample: DatasetSample) -> JudgeOutput:
    label = sample.mock_judge_label or "covered"
    return JudgeOutput(label=label, reason="mock", evidence_refs=[], confidence=1.0,
                       needs_human_review=False)


def run_judge(
    sample: DatasetSample,
    attempt_dir: Path,
    config: JudgeConfig,
    *,
    target_model: str,
    client: httpx.Client | None = None,
) -> JudgeOutput:
    if os.environ.get("AA_JUDGE_MOCK"):
        return _mock_output(sample)
    if config.model == target_model:
        raise AaError(f"judge model must differ from target model: {config.model}")
    api_url = config.api_url or os.environ.get("AA_JUDGE_API_URL")
    if not api_url:
        raise AaError("judge API url not configured (AA_JUDGE_API_URL or JudgeConfig.api_url)")
    api_key = os.environ.get(config.api_key_env)
    prompt = build_judge_prompt(sample, attempt_dir)
    request = LlmRequest(
        model=config.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=config.temperature,
    )
    text = call_llm(request, api_url=api_url, api_key=api_key, client=client)
    try:
        parsed = json.loads(text)
        output = JudgeOutput.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as err:
        raise AaError(f"judge output invalid: {err}") from err
    output.needs_human_review = output.confidence < config.confidence_threshold
    return output
```

- [ ] **Step 3: 实现 metrics.py 与 gate.py**

```python
# assurance_agent/eval/metrics.py
from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.eval.types import SampleScore, SuiteMetrics


def aggregate_scores(run_id: str, suite: str, scores: list[SampleScore]) -> SuiteMetrics:
    per_sample: dict[str, dict[str, float]] = {}
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for score in scores:
        per_sample[score.sample_id] = dict(score.metrics)
        if score.status != "ok":
            continue
        for name, value in score.metrics.items():
            sums[name] = sums.get(name, 0.0) + float(value)
            counts[name] = counts.get(name, 0) + 1
    metrics = {name: sums[name] / counts[name] for name in sums if counts[name]}
    return SuiteMetrics(
        run_id=run_id, suite=suite, sample_count=len(scores),
        metrics=metrics, per_sample=per_sample,
    )


def write_metrics(run_dir: Path, metrics: SuiteMetrics) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics.model_dump(), indent=2), encoding="utf-8"
    )


def read_metrics(run_dir: Path) -> SuiteMetrics:
    data = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    return SuiteMetrics.model_validate(data)
```

```python
# assurance_agent/eval/gate.py
from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.eval.types import (
    EvalGateResult, EvalSuite, RunManifest, SuiteMetrics, SuiteThreshold,
)

_OPS = {
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "eq": lambda v, t: v == t,
}


def _passes(value: float, threshold: SuiteThreshold) -> bool:
    return _OPS[threshold.op](value, threshold.value)


def _evidence_ok(manifest: RunManifest, metrics: SuiteMetrics) -> bool:
    return (
        manifest.executed_samples == manifest.total_samples
        and metrics.run_id == manifest.run_id
        and metrics.sample_count == len(manifest.selected_sample_ids)
    )


def compute_gate_result(
    suite: EvalSuite, manifest: RunManifest, metrics: SuiteMetrics
) -> EvalGateResult:
    hard_failures: list[str] = []
    warnings: list[str] = []
    threshold_failures: list[str] = []
    inconclusive = 0
    checked: list[str] = []

    if not _evidence_ok(manifest, metrics):
        hard_failures.append("evidence_integrity: manifest/metrics cross-check failed")

    for threshold in suite.thresholds:
        if threshold.gate == "observe":
            continue
        checked.append(threshold.metric)
        if threshold.metric not in metrics.metrics:
            inconclusive += 1
            threshold_failures.append(f"{threshold.metric}: missing")
            continue
        value = metrics.metrics[threshold.metric]
        if _passes(value, threshold):
            continue
        detail = f"{threshold.metric}: {value} !{threshold.op} {threshold.value}"
        threshold_failures.append(detail)
        if threshold.gate == "hard":
            hard_failures.append(threshold.metric)
        else:
            warnings.append(threshold.metric)

    if hard_failures and any(f.startswith("evidence_integrity") for f in hard_failures):
        verdict = "inconclusive"
    elif hard_failures:
        verdict = "fail"
    elif inconclusive:
        verdict = "inconclusive"
    elif warnings:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"

    return EvalGateResult(
        run_id=manifest.run_id, suite=suite.name, verdict=verdict,
        hard_gate_failures=hard_failures, threshold_failures=threshold_failures,
        warnings=warnings, inconclusive_count=inconclusive, checked=checked,
    )


def write_gate_result(run_dir: Path, result: EvalGateResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "gate-result.json").write_text(
        json.dumps(result.model_dump(), indent=2), encoding="utf-8"
    )


def read_gate_result(run_dir: Path) -> EvalGateResult:
    data = json.loads((run_dir / "gate-result.json").read_text(encoding="utf-8"))
    return EvalGateResult.model_validate(data)
```

> **口径说明**：gate 裁决把"evidence_integrity 内置校验失败"归为 `inconclusive`（对齐 TS `gate.ts` 的 hard-but-inconclusive 语义——证据链不完整时不宣判 fail，而是要求重跑）；`--calibrate` 的 judge 校准在本里程碑不改变 gate 数值（judge 结果落 sample notes，供报告展示），保持 gate 纯由阈值裁决，`aa eval run --calibrate` flag 在 Task 5 接受但仅触发 judge 落盘。

- [ ] **Step 4: 跑测试 + 静态检查 + 提交**

```bash
uv run pytest tests/unit/eval/test_judge.py tests/unit/eval/test_gate.py tests/unit/eval/test_metrics.py -v
uv run ruff check .
uv run pyright
```

预期：judge 7 条 + gate 4 条 + metrics 2 条全部通过；ruff / pyright clean。

```bash
git add assurance_agent/eval tests/unit/eval
git commit -m "feat: eval LLM judge (httpx) + gate + metrics aggregation (M8 task 3)"
```

---

### Task 4: executor（复用 M6 driver）+ runner + report + plan

**Files:**
- Create: `assurance_agent/eval/executor.py`
- Create: `assurance_agent/eval/report.py`
- Create: `assurance_agent/eval/plan.py`
- Create: `assurance_agent/eval/runner.py`
- Test: `tests/unit/eval/test_executor.py`
- Test: `tests/unit/eval/test_runner.py`
- Test: `tests/unit/eval/test_report.py`

**Interfaces:**
- Consumes: Task 1-3 全部；M6 `run_workflow_loop`/`LoopResult`/`Adapter`/`PhaseRequest`/`PhaseResult`。
- Produces：
  - `execute_attempt(sample, attempt_dir, *, suite, sut_dir, adapter, scope="full", loop_runner=run_workflow_loop, status_provider=None, cli_executor=None, expected_outputs=None) -> ExecutionResult`——复用 M6 循环；测试显式注入 outcome executor，不能意外启动真实 `aa state apply` 子进程；
  - `run_suite(*, suite_file, project_root, sut_dir, sample_id=None, repeat=1, calibrate=False, run_id=None, extra_memory_dir=None, adapter_factory=None, status_provider_factory=None, cli_executor_factory=None, loop_runner=run_workflow_loop) -> tuple[str, EvalGateResult]`。`repeat` 生成独立 attempt 目录和唯一 score key，不能在 `per_sample` 中互相覆盖；`extra_memory_dir` 只 overlay 到每个隔离 attempt 的 `.aa/memory/`，不得修改源 SUT；
  - `run_plan(*, plan_path, project_root, sut_dir, adapter_factory=None, status_provider_factory=None) -> tuple[str, list[EvalGateResult]]`；
  - `write_run_report(run_dir, manifest, metrics, gate) -> None`（写 `report.json` + `report.html` + `report.md`）；`generate_trend_report`；
  - `generate_plan(event, changed_files, suite) / load_suite / read_plan / write_plan`。Task 5 CLI 消费。

> **executor 复用 M6 循环**：`workflow-run` executor 不另起循环——`loop_runner` 缺省绑定 `run_workflow_loop`，以 `skip_lock=True` + 注入 `adapter` + `status_provider` 运行；测试注入 M6 `FakeAdapter`（其 `run_phase` 向 `sut/qa/changes/<id>/` 落 golden 产物）+ 脚本化 `status_provider`（先 dispatch 再 terminal），完成一次**真实** driver 循环，executor 再拷 raw-output。这满足"executor test wired to a FakeAdapter driver run"。

- [ ] **Step 1: 写失败测试（executor 复用真实 M6 循环）**

```python
# tests/unit/eval/test_executor.py
from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.eval.executor import execute_attempt
from assurance_agent.eval.types import DatasetSample
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.orchestration.engine import (
    DispatchEntry, PhaseView, Terminal, WorkflowStatus,
)


class FakeAdapter:
    """FakeAdapter：run_phase 落一份 golden 产物到 change 目录，再返回成功。"""

    def __init__(self, change_dir: Path) -> None:
        self.change_dir = change_dir
        self.requests: list[PhaseRequest] = []

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.requests.append(request)
        review = self.change_dir / "review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "case-review.json").write_text(json.dumps({"decision": "pass"}),
                                                  encoding="utf-8")
        return PhaseResult(ok=True, output="done")


class AuditOutcomeExecutor:
    def run_cli_phase(self, entry, ctx):  # noqa: ANN001, ANN201
        raise AssertionError("skill-only eval must not run a cli phase")

    def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
        append_event_strict(ctx.change_dir, {
            "source": "progression", "type": "phase_outcome_committed",
            "phase": entry.phase_id, "attempt_id": attempt_id, "gate_report": None,
        })
        return PhaseResult(ok=True, output="committed")


def _scripted_status(steps: list[WorkflowStatus]):
    seq = iter(steps)

    def provider() -> WorkflowStatus:
        return next(seq)

    return provider


def test_execute_attempt_runs_m6_loop_and_copies_raw_output(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    change_dir = sut / "qa" / "changes" / "eval-sample-001"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    adapter = FakeAdapter(change_dir)
    status = _scripted_status([
        WorkflowStatus(
            phases=[PhaseView(id="case-design", status="ready")],
            next_dispatch=[DispatchEntry(phase_id="case-design", skill="aa-case-design",
                                         agent=None, kind="skill")],
            terminal=None,
        ),
        WorkflowStatus(phases=[PhaseView(id="case-design", status="done")],
                       next_dispatch=[], terminal=Terminal(kind="completed", reason="ok")),
    ])
    sample = DatasetSample(id="WC-001", suite="workflow-case",
                           input={"change_id": "eval-sample-001", "run_mode": "case-only"},
                           expected={})
    attempt = tmp_path / "run" / "samples" / "WC-001" / "attempt-0"

    result = execute_attempt(
        sample, attempt, suite="workflow-case", sut_dir=sut,
        adapter=adapter, scope="full", status_provider=status,
        cli_executor=AuditOutcomeExecutor(),
    )

    assert result.status == "ok"
    assert result.exit_code == 0
    # M6 循环真跑过一轮 skill 阶段
    assert [r.phase_id for r in adapter.requests] == ["case-design"]
    # executor 已把 change 产物拷进 attempt/raw-output
    copied = attempt / "raw-output" / "review" / "case-review.json"
    assert json.loads(copied.read_text())["decision"] == "pass"
    assert (attempt / "execution.json").exists()
    assert (attempt / "stdout.log").exists()


def test_execute_attempt_error_exit_recorded(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    (sut / "qa" / "changes" / "eval-sample-002").mkdir(parents=True)
    status = _scripted_status([
        WorkflowStatus(phases=[], next_dispatch=[],
                       terminal=Terminal(kind="stopped", reason="gate reject")),
    ])
    sample = DatasetSample(id="WC-002", suite="workflow-case",
                           input={"change_id": "eval-sample-002"}, expected={})
    attempt = tmp_path / "run" / "s" / "WC-002" / "attempt-0"

    class NoopAdapter:
        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            return PhaseResult(ok=True, output="")

    result = execute_attempt(sample, attempt, suite="workflow-case", sut_dir=sut,
                             adapter=NoopAdapter(), status_provider=status)
    assert result.status == "error"
    assert result.exit_code == 20    # EXIT_STOPPED
```

```python
# tests/unit/eval/test_runner.py
from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.eval import runner as runner_mod
from assurance_agent.eval.runner import run_suite
from assurance_agent.eval.types import JudgeOutput
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.orchestration.engine import (
    DispatchEntry, PhaseView, Terminal, WorkflowStatus,
)


def _seed_suite(project_root: Path) -> Path:
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True)
    suite_file = suites / "workflow-case.yaml"
    suite_file.write_text(yaml.safe_dump({
        "name": "workflow-case",
        "scorer": "workflow-case",
        "executor": {"type": "workflow-run", "scope": "full"},
        "thresholds": [
            {"metric": "case_review_gate_pass_rate", "gate": "hard", "op": "gte", "value": 0.99},
            {"metric": "secret_leak_count", "gate": "hard", "op": "eq", "value": 0.0},
        ],
    }), encoding="utf-8")
    ds = project_root / "eval" / "datasets" / "workflow-case"
    ds.mkdir(parents=True)
    (ds / "WC-001.yaml").write_text(yaml.safe_dump({
        "id": "WC-001", "suite": "workflow-case",
        "input": {"change_id": "eval-sample-001", "run_mode": "case-only"}, "expected": {},
    }), encoding="utf-8")
    return suite_file


def test_run_suite_end_to_end_pass_and_persists_calibration(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_suite(project_root)
    sut = tmp_path / "sut"
    change_dir = sut / "qa" / "changes" / "eval-sample-001"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")

    class GreenAdapter:
        def __init__(self, workspace: Path) -> None:
            self.change_dir = workspace / "qa/changes/eval-sample-001"

        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            review = self.change_dir / "review"
            review.mkdir(parents=True, exist_ok=True)
            (review / "case-review.json").write_text(json.dumps({"decision": "pass"}))
            return PhaseResult(ok=True, output="done")

    calls = {"n": 0}

    class AuditOutcomeExecutor:
        def run_cli_phase(self, entry, ctx):  # noqa: ANN001, ANN201
            raise AssertionError("skill-only eval")

        def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
            append_event_strict(ctx.change_dir, {
                "source": "progression", "type": "phase_outcome_committed",
                "phase": entry.phase_id, "attempt_id": attempt_id, "gate_report": None,
            })
            return PhaseResult(ok=True, output="committed")

    def status_provider() -> WorkflowStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            return WorkflowStatus(
                phases=[PhaseView(id="case-design", status="ready")],
                next_dispatch=[DispatchEntry(phase_id="case-design", skill="aa-case-design",
                                             agent=None, kind="skill")],
                terminal=None)
        return WorkflowStatus(phases=[PhaseView(id="case-design", status="done")],
                              next_dispatch=[], terminal=Terminal(kind="completed", reason="ok"))

    monkeypatch.setenv("AA_JUDGE_MODEL", "judge-model")
    monkeypatch.setattr(
        runner_mod,
        "run_judge",
        lambda *a, **k: JudgeOutput(label="covered", confidence=0.9),
    )

    run_id, gate = run_suite(
        suite_file=suite_file, project_root=project_root, sut_dir=sut,
        calibrate=True,
        adapter_factory=lambda sut_dir, **_: GreenAdapter(sut_dir),
        status_provider_factory=lambda **_: status_provider,
        cli_executor_factory=lambda **_: AuditOutcomeExecutor(),
    )
    assert gate.verdict == "pass"
    run_dir = project_root / "eval" / "out" / "runs" / run_id
    assert (run_dir / "metrics.json").exists()
    assert (run_dir / "gate-result.json").exists()
    assert (run_dir / "report.json").exists()
    judge = run_dir / "samples/WC-001/attempt-0/judge.json"
    assert json.loads(judge.read_text())["label"] == "covered"


def test_run_suite_repeat_uses_isolated_workspaces_and_unique_score_keys(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_suite(project_root)
    sut = tmp_path / "sut"
    seed_change = sut / "qa/changes/eval-sample-001"
    seed_change.mkdir(parents=True)
    (seed_change / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    staged_memory = tmp_path / "staged-memory"
    staged_memory.mkdir()
    (staged_memory / "retro.md").write_text("candidate memory\n", encoding="utf-8")

    class IsolatedAdapter:
        def __init__(self, workspace: Path, attempt: int) -> None:
            self.change = workspace / "qa/changes/eval-sample-001"
            assert (workspace / ".aa/memory/retro.md").read_text() == "candidate memory\n"
            marker = self.change / "attempt-marker"
            assert not marker.exists()  # prior attempt must not leak into this workspace
            marker.write_text(str(attempt), encoding="utf-8")

        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            review = self.change / "review"
            review.mkdir(parents=True, exist_ok=True)
            (review / "case-review.json").write_text('{"decision":"pass"}', encoding="utf-8")
            return PhaseResult(ok=True)

    class AuditOutcome:
        def run_cli_phase(self, entry, ctx):  # noqa: ANN001, ANN201
            raise AssertionError("skill-only")

        def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
            append_event_strict(ctx.change_dir, {
                "source": "progression", "type": "phase_outcome_committed",
                "phase": entry.phase_id, "attempt_id": attempt_id, "gate_report": None,
            })
            return PhaseResult(ok=True)

    def status_factory(**_):
        calls = {"n": 0}

        def status() -> WorkflowStatus:
            calls["n"] += 1
            if calls["n"] == 1:
                return WorkflowStatus(
                    phases=[PhaseView(id="case-design", status="ready")],
                    next_dispatch=[DispatchEntry(
                        phase_id="case-design", skill="aa-case-design", agent=None, kind="skill",
                    )], terminal=None,
                )
            return WorkflowStatus(
                phases=[PhaseView(id="case-design", status="done")], next_dispatch=[],
                terminal=Terminal(kind="completed"),
            )

        return status

    run_id, gate = run_suite(
        suite_file=suite_file, project_root=project_root, sut_dir=sut, repeat=2,
        extra_memory_dir=staged_memory,
        adapter_factory=lambda sut_dir, attempt, **_: IsolatedAdapter(sut_dir, attempt),
        status_provider_factory=status_factory,
        cli_executor_factory=lambda **_: AuditOutcome(),
    )
    assert gate.verdict == "pass"
    metrics = json.loads((project_root / "eval/out/runs" / run_id / "metrics.json").read_text())
    assert set(metrics["per_sample"]) == {"WC-001#attempt-0", "WC-001#attempt-1"}
    assert not (sut / ".aa/memory/retro.md").exists()  # overlay never mutates source SUT
```

```python
# tests/unit/eval/test_report.py
from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.eval.report import write_run_report
from assurance_agent.eval.types import EvalGateResult, RunManifest, SuiteMetrics


def test_write_run_report_json_html_md(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="run-1", suite="workflow-case", scorer="workflow_case",
                           selected_sample_ids=["WC-001"], total_samples=1, executed_samples=1,
                           target_model="m", started_at="2026-07-15T00:00:00Z",
                           completed_at="2026-07-15T00:01:00Z")
    metrics = SuiteMetrics(run_id="run-1", suite="workflow-case", sample_count=1,
                           metrics={"case_review_gate_pass_rate": 1.0},
                           per_sample={"WC-001": {"case_review_gate_pass_rate": 1.0}})
    gate = EvalGateResult(run_id="run-1", suite="workflow-case", verdict="pass")
    write_run_report(tmp_path, manifest, metrics, gate)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["run_id"] == "run-1"
    assert report["verdict"] == "pass"
    assert report["metrics"]["case_review_gate_pass_rate"] == 1.0
    html = (tmp_path / "report.html").read_text()
    assert "workflow-case" in html and "pass" in html
    assert (tmp_path / "report.md").exists()
```

```bash
uv run pytest tests/unit/eval/test_executor.py tests/unit/eval/test_runner.py tests/unit/eval/test_report.py -v
```

预期：三文件 `ModuleNotFoundError`。

> **实现者注意**：上面 executor 测试直接构造 `WorkflowStatus`/`DispatchEntry`/`Terminal`/`PhaseView`（M3 落地类型）。若 M3 实际字段名与此不同（例如 `PhaseView` 不存在），以落地代码为准调整测试 import——executor 本身**不**直接依赖这些类型，只把 `status_provider` 透传给 `run_workflow_loop`。

- [ ] **Step 2: 实现 executor.py**

```python
# assurance_agent/eval/executor.py
from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from assurance_agent.eval.types import DatasetSample, ExecutionResult
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.driver.adapter import Adapter
from assurance_agent.workflow.driver.loop import CliPhaseExecutor, LoopResult, run_workflow_loop

LoopRunner = Callable[..., LoopResult]


def _copy_change_artifacts(change_dir: Path, raw_output: Path) -> None:
    raw_output.mkdir(parents=True, exist_ok=True)
    if not change_dir.is_dir():
        return
    for entry in change_dir.iterdir():
        target = raw_output / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def execute_attempt(
    sample: DatasetSample,
    attempt_dir: Path,
    *,
    suite: str,
    sut_dir: Path,
    adapter: Adapter,
    scope: str = "full",
    loop_runner: LoopRunner = run_workflow_loop,
    status_provider: Callable[[], object] | None = None,
    cli_executor: CliPhaseExecutor | None = None,
    expected_outputs: list[str] | None = None,
) -> ExecutionResult:
    change_id = sample.input.get("change_id")
    if not change_id:
        raise AaError(f"sample {sample.id} missing input.change_id")
    assert_change_id_safe(change_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    loop = loop_runner(
        project_root=sut_dir,
        change_id=change_id,
        scope=scope,
        adapter=adapter,
        status_provider=status_provider,
        cli_executor=cli_executor,
        skip_lock=True,
    )

    change_dir = sut_dir / "qa" / "changes" / change_id
    _copy_change_artifacts(change_dir, attempt_dir / "raw-output")

    status = "ok" if loop.exit_code == 0 else "error"
    (attempt_dir / "stdout.log").write_text(loop.reason + "\n", encoding="utf-8")
    (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
    execution = {
        "executor": f"workflow-run:{suite}",
        "change_id": change_id,
        "scope": scope,
        "exit_code": loop.exit_code,
        "reason": loop.reason,
    }
    (attempt_dir / "execution.json").write_text(json.dumps(execution, indent=2),
                                                encoding="utf-8")

    missing = [
        rel for rel in (expected_outputs or [])
        if not (attempt_dir / "raw-output" / rel).exists()
    ]
    if missing:
        status = "error"

    return ExecutionResult(
        sample_id=sample.id, attempt=0, executor="workflow-run", status=status,
        exit_code=loop.exit_code, error=None if status == "ok" else loop.reason,
        extra={"missing_outputs": missing},
    )
```

- [ ] **Step 3: 实现 report.py 与 plan.py**

```python
# assurance_agent/eval/report.py
from __future__ import annotations

import html
import json
from pathlib import Path

from assurance_agent.eval.paths import reports_dir, runs_dir
from assurance_agent.eval.types import EvalGateResult, RunManifest, SuiteMetrics


def _build_report(manifest: RunManifest, metrics: SuiteMetrics,
                  gate: EvalGateResult) -> dict:
    return {
        "run_id": manifest.run_id,
        "suite": manifest.suite,
        "verdict": gate.verdict,
        "started_at": manifest.started_at,
        "completed_at": manifest.completed_at,
        "sample_ids": manifest.selected_sample_ids,
        "metrics": metrics.metrics,
        "per_sample": metrics.per_sample,
        "hard_gate_failures": gate.hard_gate_failures,
        "warnings": gate.warnings,
    }


def _render_html(report: dict) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>"
        for k, v in sorted(report["metrics"].items())
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>eval {html.escape(report['run_id'])}</title></head><body>"
        f"<h1>{html.escape(report['suite'])}</h1>"
        f"<p>run_id: {html.escape(report['run_id'])}</p>"
        f"<p>verdict: <strong>{html.escape(report['verdict'])}</strong></p>"
        f"<table border='1'><tr><th>metric</th><th>value</th></tr>{rows}</table>"
        "</body></html>"
    )


def _render_md(report: dict) -> str:
    lines = [f"# eval report — {report['suite']}", "",
             f"- run_id: `{report['run_id']}`",
             f"- verdict: **{report['verdict']}**", "", "## Metrics", ""]
    for name, value in sorted(report["metrics"].items()):
        lines.append(f"- `{name}`: {value}")
    return "\n".join(lines) + "\n"


def write_run_report(run_dir: Path, manifest: RunManifest, metrics: SuiteMetrics,
                     gate: EvalGateResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    report = _build_report(manifest, metrics, gate)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (run_dir / "report.html").write_text(_render_html(report), encoding="utf-8")
    (run_dir / "report.md").write_text(_render_md(report), encoding="utf-8")


def generate_trend_report(project_root: Path, suite: str, *, date_from: str | None = None,
                          date_to: str | None = None, html_out: Path | None = None) -> Path:
    points: list[dict] = []
    root = runs_dir(project_root)
    if root.is_dir():
        for run in sorted(root.iterdir()):
            report_path = run / "report.json"
            if not report_path.exists():
                continue
            data = json.loads(report_path.read_text(encoding="utf-8"))
            if data.get("suite") != suite:
                continue
            started = data.get("started_at") or ""
            if date_from and started < date_from:
                continue
            if date_to and started > date_to:
                continue
            points.append(data)
    out = html_out or (reports_dir(project_root) / f"trend-{suite}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f"<tr><td>{html.escape(p['run_id'])}</td><td>{html.escape(p['verdict'])}</td></tr>"
        for p in points
    )
    out.write_text(
        f"<!doctype html><html><body><h1>trend — {html.escape(suite)}</h1>"
        f"<table border='1'><tr><th>run</th><th>verdict</th></tr>{rows}</table>"
        "</body></html>",
        encoding="utf-8",
    )
    return out
```

```python
# assurance_agent/eval/plan.py
from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.eval.paths import eval_root
from assurance_agent.eval.types import EvalSuite
from assurance_agent.exceptions import AaError


def load_suite(project_root: Path, suite_name: str) -> tuple[EvalSuite, Path]:
    path = eval_root(project_root) / "suites" / f"{suite_name}.yaml"
    if not path.exists():
        raise AaError(f"suite not found: {suite_name} ({path})")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return EvalSuite.model_validate(data), path


def load_suite_file(suite_file: Path) -> EvalSuite:
    data = yaml.safe_load(suite_file.read_text(encoding="utf-8"))
    return EvalSuite.model_validate(data)


def generate_plan(event: str, changed_files: list[str] | None, suite: str | None) -> dict:
    suites: list[str] = []
    if suite:
        suites.append(suite)
    return {"event": event, "changed_files": changed_files or [], "suites": suites}


def write_plan(plan: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")


def read_plan(path: Path) -> dict:
    if not path.exists():
        raise AaError(f"plan not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
```

- [ ] **Step 4: 实现 runner.py**

```python
# assurance_agent/eval/runner.py
from __future__ import annotations

import json
import os
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.eval.dataset_loader import load_for_run
from assurance_agent.eval.executor import execute_attempt
from assurance_agent.eval.gate import compute_gate_result, write_gate_result
from assurance_agent.eval.judge import run_judge
from assurance_agent.eval.metrics import aggregate_scores, write_metrics
from assurance_agent.eval.paths import attempt_dir as attempt_dir_for
from assurance_agent.eval.paths import datasets_dir, run_dir as run_dir_for
from assurance_agent.eval.plan import load_suite_file, read_plan, load_suite
from assurance_agent.eval.report import write_run_report
from assurance_agent.eval.scorers import get_scorer
from assurance_agent.eval.types import (
    EvalGateResult, EvalSuite, JudgeConfig, RunManifest, SampleScore,
)
from assurance_agent.workflow.driver.adapter import Adapter
from assurance_agent.workflow.driver.loop import CliPhaseExecutor, run_workflow_loop


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id(suite: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"eval-{stamp}-{secrets.token_hex(4)}"


def _copy_attempt_workspace(
    source: Path,
    attempt: Path,
    *,
    extra_memory_dir: Path | None = None,
) -> Path:
    """Create an isolated SUT snapshot; never recurse into eval/out or VCS/env data."""
    target = attempt / "sut"

    def ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).resolve().relative_to(source.resolve())
        ignored = {name for name in names if name in {".git", ".venv", "__pycache__"}}
        ignored.update(name for name in names if (Path(directory) / name).is_symlink())
        if rel == Path("eval"):
            ignored.add("out")
        return ignored

    shutil.copytree(source, target, ignore=ignore)
    if extra_memory_dir is not None:
        if not extra_memory_dir.is_dir():
            raise ValueError(f"extra memory directory not found: {extra_memory_dir}")
        shutil.copytree(
            extra_memory_dir,
            target / ".aa" / "memory",
            dirs_exist_ok=True,
        )
    return target


def run_suite(
    *,
    suite_file: Path,
    project_root: Path,
    sut_dir: Path,
    sample_id: str | None = None,
    repeat: int = 1,
    calibrate: bool = False,
    run_id: str | None = None,
    extra_memory_dir: Path | None = None,
    adapter_factory: Callable[..., Adapter] | None = None,
    status_provider_factory: Callable[..., Callable[[], object]] | None = None,
    cli_executor_factory: Callable[..., CliPhaseExecutor] | None = None,
    loop_runner: Callable[..., object] = run_workflow_loop,
) -> tuple[str, EvalGateResult]:
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    suite = load_suite_file(suite_file)
    dataset = datasets_dir(project_root, suite.name) if suite.dataset_dir is None \
        else Path(suite.dataset_dir)
    samples = load_for_run(dataset, sample_id=sample_id)
    run_id = run_id or _new_run_id(suite.name)
    run_dir = run_dir_for(project_root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = RunManifest(
        run_id=run_id, suite=suite.name, scorer=suite.scorer,
        selected_sample_ids=[
            f"{s.id}#attempt-{i}" for s in samples for i in range(repeat)
        ],
        total_samples=len(samples) * repeat,
        executed_samples=0, target_model=str(suite.executor.get("model", "unknown")),
        started_at=_now(),
    )

    scorer = get_scorer(suite.scorer)
    scores: list[SampleScore] = []
    scope = str(suite.executor.get("scope", "full"))
    for sample in samples:
        for attempt_index in range(repeat):
            attempt = attempt_dir_for(project_root, run_id, sample.id, attempt_index)
            attempt.mkdir(parents=True, exist_ok=True)
            attempt_sut = _copy_attempt_workspace(
                sut_dir,
                attempt,
                extra_memory_dir=extra_memory_dir,
            )
            factory_args = {
                "sample": sample, "sut_dir": attempt_sut, "attempt": attempt_index,
            }
            adapter = adapter_factory(**factory_args) if adapter_factory else None
            status_provider = (
                status_provider_factory(**factory_args) if status_provider_factory else None
            )
            cli_executor = (
                cli_executor_factory(**factory_args) if cli_executor_factory else None
            )
            if adapter is None:
                raise ValueError("adapter_factory required (real adapters wired by CLI)")
            result = execute_attempt(
                sample, attempt, suite=suite.name, sut_dir=attempt_sut, adapter=adapter,
                scope=scope, loop_runner=loop_runner, status_provider=status_provider,
                cli_executor=cli_executor,
                expected_outputs=suite.executor.get("expected_outputs"),
            )
            manifest.executed_samples += 1
            score_key = f"{sample.id}#attempt-{attempt_index}"
            if result.status == "error":
                scores.append(SampleScore(sample_id=score_key, status="error", error=result.error))
                continue
            score = scorer(sample, attempt).model_copy(update={"sample_id": score_key})
            if calibrate:
                judge_model = os.environ.get("AA_JUDGE_MODEL")
                if not judge_model:
                    raise ValueError("--calibrate requires AA_JUDGE_MODEL")
                judged = run_judge(
                    sample, attempt, JudgeConfig(model=judge_model),
                    target_model=manifest.target_model,
                )
                (attempt / "judge.json").write_text(
                    json.dumps(judged.model_dump(mode="json"), indent=2), encoding="utf-8"
                )
                score.notes.update({
                    "judge_label": judged.label,
                    "judge_confidence": judged.confidence,
                    "judge_needs_human_review": float(judged.needs_human_review),
                })
            scores.append(score)

    manifest.completed_at = _now()
    metrics = aggregate_scores(run_id, suite.name, scores)
    gate = compute_gate_result(suite, manifest, metrics)

    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    write_metrics(run_dir, metrics)
    write_gate_result(run_dir, gate)
    write_run_report(run_dir, manifest, metrics, gate)
    return run_id, gate


def run_plan(
    *, plan_path: Path, project_root: Path, sut_dir: Path,
    adapter_factory: Callable[..., Adapter] | None = None,
    status_provider_factory: Callable[..., Callable[[], object]] | None = None,
) -> tuple[str, list[EvalGateResult]]:
    plan = read_plan(plan_path)
    batch_id = _new_run_id("batch")
    results: list[EvalGateResult] = []
    for suite_name in plan.get("suites", []):
        _, suite_file = load_suite(project_root, suite_name)
        _, gate = run_suite(
            suite_file=suite_file, project_root=project_root, sut_dir=sut_dir,
            adapter_factory=adapter_factory,
            status_provider_factory=status_provider_factory,
        )
        results.append(gate)
    return batch_id, results
```

> **说明**：`run_suite` 的 `adapter_factory` / `status_provider_factory` 是 executor 与真实/假 adapter 的接缝。CLI（Task 5）默认注入 headless/opencode adapter 工厂 + 真实 `status_provider`（`AA_EVAL_FAKE_ADAPTER` 时注入拷 golden 的 fake）；单测注入 `FakeAdapter` + 脚本化 provider。runner 不 hardcode 任何 adapter。

- [ ] **Step 5: 跑测试 + 静态检查 + 提交**

```bash
uv run pytest tests/unit/eval/test_executor.py tests/unit/eval/test_runner.py tests/unit/eval/test_report.py -v
uv run ruff check .
uv run pyright
```

预期：executor 2 条 + runner 2 条 + report 1 条通过（覆盖 M6 真循环、outcome audit、repeat workspace 隔离与唯一 score key）；ruff / pyright clean。

```bash
git add assurance_agent/eval tests/unit/eval
git commit -m "feat: eval executor (reuses M6 driver loop) + runner + report + plan (M8 task 4)"
```

---

### Task 5: `aa eval run|plan|report` 命令 + eval 分层契约

> **执行顺序约束**：本 Task 的 `eval_cmd.py` 直接 import baseline API，因此必须先完成下方 **Task 5b**，再回来执行本 Task 的测试与提交；不得提交一个 import 时即失败的 CLI 中间态。

**Files:**
- Create: `assurance_agent/commands/eval_cmd.py`
- Modify: `assurance_agent/cli.py`（挂载 `eval` 命令组）
- Modify: `.importlinter`（layers 增补 `assurance_agent.eval`）
- Test: `tests/integration/test_eval_cli.py`

**Interfaces:**
- Consumes: Task 4 `run_suite`/`run_plan`、Task 3 `read_gate_result`、`report` 模块；M6 headless/opencode adapter 工厂（`AA_EVAL_FAKE_ADAPTER` 走 fake）。
- Produces: `aa eval run` / `aa eval plan` / `aa eval report`。CLI flag 逐一对齐 TS `src/commands/eval.ts`。`--json` run 输出 `{run_id, verdict}`；`--output id` 只打 run_id；`--fail-on-verdict` 非 pass → exit 1。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_eval_cli.py
from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from assurance_agent.cli import main


def _seed(project_root: Path) -> None:
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True)
    (suites / "workflow-case.yaml").write_text(yaml.safe_dump({
        "name": "workflow-case", "scorer": "workflow-case",
        "executor": {"type": "workflow-run", "scope": "full"},
        "thresholds": [
            {"metric": "case_review_gate_pass_rate", "gate": "hard", "op": "gte", "value": 0.99},
        ],
    }), encoding="utf-8")
    ds = project_root / "eval" / "datasets" / "workflow-case"
    ds.mkdir(parents=True)
    (ds / "WC-001.yaml").write_text(yaml.safe_dump({
        "id": "WC-001", "suite": "workflow-case",
        "input": {"change_id": "eval-sample-001"}, "expected": {},
    }), encoding="utf-8")
    # SUT change dir with a passing case-review already seeded → fake adapter no-op OK
    change = project_root / "sut" / "qa" / "changes" / "eval-sample-001" / "review"
    change.mkdir(parents=True)
    (change / "case-review.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")


def test_eval_run_json_shape(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")   # 走 fake adapter，一次即 terminal
        result = runner.invoke(main, [
            "eval", "run", "--suite", "workflow-case",
            "--sut-dir", str(project_root / "sut"), "--json",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["verdict"] == "pass"
        assert payload["run_id"].startswith("eval-")


def test_eval_run_output_id_prints_only_run_id(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        result = runner.invoke(main, [
            "eval", "run", "--suite", "workflow-case",
            "--sut-dir", str(project_root / "sut"), "--output", "id",
        ])
        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("eval-")


def test_eval_run_requires_suite_or_plan() -> None:
    result = CliRunner().invoke(main, ["eval", "run"])
    assert result.exit_code == 1
    assert "--suite" in result.output


def test_eval_run_suite_and_plan_mutually_exclusive() -> None:
    result = CliRunner().invoke(main, ["eval", "run", "--suite", "x", "--plan", "p.json"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_eval_plan_writes_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, [
            "eval", "plan", "--event", "manual", "--suite", "workflow-case",
            "--out", "eval-plan.json",
        ])
        assert result.exit_code == 0, result.output
        plan = json.loads(Path("eval-plan.json").read_text())
        assert plan["suites"] == ["workflow-case"]


def test_eval_report_json(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        run = runner.invoke(main, ["eval", "run", "--suite", "workflow-case",
                                    "--sut-dir", str(project_root / "sut"), "--output", "id"])
        run_id = run.output.strip()
        result = runner.invoke(main, ["eval", "report", "--run", run_id, "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["run_id"] == run_id
        assert report["verdict"] == "pass"
```

```bash
uv run pytest tests/integration/test_eval_cli.py -v
```

预期：`eval` 命令组未挂载 → `No such command 'eval'`。

- [ ] **Step 2: 实现 eval_cmd.py**

```python
# assurance_agent/commands/eval_cmd.py
from __future__ import annotations

import json
import os
from pathlib import Path

import click

from assurance_agent.eval.gate import read_gate_result
from assurance_agent.eval.paths import run_dir as run_dir_for
from assurance_agent.eval.plan import generate_plan, load_suite, write_plan
from assurance_agent.eval.report import generate_trend_report
from assurance_agent.eval.runner import run_plan, run_suite
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.driver.adapter import Adapter, PhaseRequest, PhaseResult

_FAILING = {"fail", "inconclusive", "needs_human_review"}


class _FakeAdapter:
    """AA_EVAL_FAKE_ADAPTER：不调真实 agent，产物由 seed/fixture 提供。"""

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        return PhaseResult(ok=True, output="fake")


def _terminal_status_provider_factory(**_: object):
    from assurance_agent.workflow.orchestration.engine import Terminal, WorkflowStatus

    def provider() -> WorkflowStatus:
        return WorkflowStatus(phases=[], next_dispatch=[],
                              terminal=Terminal(kind="completed", reason="fake"))

    return provider


def _resolve_adapter_factory(*, use_fake: bool, sut: Path):
    """fake 模式 → 桩 adapter + 立即 terminal 的 status；否则复用 M6 HeadlessAdapter。"""
    if use_fake:
        def fake_factory(**_: object) -> Adapter:
            return _FakeAdapter()
        return fake_factory, _terminal_status_provider_factory

    from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter

    agent_cmd = os.environ.get("AA_EVAL_AGENT_CMD", "cursor-agent")

    def real_factory(*, sut_dir: Path | None = None, **_: object) -> Adapter:
        return HeadlessAdapter(agent_cmd=agent_cmd, cwd=sut_dir or sut)

    return real_factory, None    # 真实 run：status_provider 交给 M6 默认 compute_status


@click.group("eval")
def eval_group() -> None:
    """AI Eval Harness — evaluate AI tool quality."""


@eval_group.command("run")
@click.option("--suite", "suite_name", help="Suite name to run")
@click.option("--plan", "plan_path", help="Path to eval-plan.json")
@click.option("--sample", "sample_id", help="Run a single sample only")
@click.option("--repeat", type=int, default=1, help="Repeat runs (stability)")
@click.option("--output", "output_mode", help="Output mode: id")
@click.option("--json", "as_json", is_flag=True, help="Output { run_id, verdict } JSON")
@click.option("--fail-on-verdict", is_flag=True, help="Exit 1 when verdict is not pass")
@click.option("--calibrate", is_flag=True, help="Run judge calibration (records only)")
@click.option("--extra-memory-dir", help="Overlay .aa/memory files into SUT workspaces")
@click.option("--sut-dir", help="Override SUT checkout directory")
def eval_run(suite_name, plan_path, sample_id, repeat, output_mode, as_json,
             fail_on_verdict, calibrate, extra_memory_dir, sut_dir) -> None:
    project_root = Path.cwd()
    if not suite_name and not plan_path:
        click.echo("Error: --suite <name> or --plan <path> required", err=True)
        raise SystemExit(1)
    if suite_name and plan_path:
        click.echo("Error: --suite and --plan are mutually exclusive", err=True)
        raise SystemExit(1)

    use_fake = bool(os.environ.get("AA_EVAL_FAKE_ADAPTER"))
    sut = Path(sut_dir).resolve() if sut_dir else (
        Path(os.environ.get("AA_EVAL_SUT_DIR", str(project_root))))
    adapter_factory, status_factory = _resolve_adapter_factory(use_fake=use_fake, sut=sut)

    try:
        if suite_name:
            _, suite_file = load_suite(project_root, suite_name)
            run_id, gate = run_suite(
                suite_file=suite_file, project_root=project_root, sut_dir=sut,
                sample_id=sample_id, repeat=repeat, calibrate=calibrate,
                extra_memory_dir=(Path(extra_memory_dir).resolve() if extra_memory_dir else None),
                adapter_factory=adapter_factory,
                status_provider_factory=status_factory,
            )
            _print_run(output_mode, as_json, run_id, gate.verdict)
            if fail_on_verdict and gate.verdict in _FAILING:
                raise SystemExit(1)
        else:
            batch_id, gates = run_plan(
                plan_path=Path(plan_path), project_root=project_root, sut_dir=sut,
                adapter_factory=adapter_factory, status_provider_factory=status_factory,
            )
            worst = _worst_verdict([g.verdict for g in gates])
            _print_run(output_mode, as_json, batch_id, worst, key="batch_id")
            if fail_on_verdict and worst in _FAILING:
                raise SystemExit(1)
    except AaError as err:
        click.echo(f"Error: {err}", err=True)
        raise SystemExit(1) from err


def _worst_verdict(verdicts: list[str]) -> str:
    order = ["fail", "inconclusive", "needs_human_review", "pass_with_warnings", "pass"]
    for candidate in order:
        if candidate in verdicts:
            return candidate
    return "pass"


def _print_run(output_mode, as_json, run_id, verdict, key="run_id") -> None:
    if output_mode == "id":
        click.echo(run_id)
        return
    if as_json:
        click.echo(json.dumps({key: run_id, "verdict": verdict}))
        return
    click.echo(f"{key}: {run_id}")
    click.echo(f"verdict: {verdict}")


@eval_group.command("plan")
@click.option("--event", required=True, help="pull_request | manual")
@click.option("--changed-files", help="Path to changed files list")
@click.option("--suite", "suite_name", help="Suite to include (manual)")
@click.option("--out", default="eval-plan.json", help="Output path")
def eval_plan(event, changed_files, suite_name, out) -> None:
    changed = None
    if changed_files:
        changed = Path(changed_files).read_text(encoding="utf-8").split()
    plan = generate_plan(event, changed, suite_name)
    write_plan(plan, Path(out))
    click.echo(f"plan: {out}")


@eval_group.command("report")
@click.option("--run", "run_id", help="Run id")
@click.option("--trend", is_flag=True, help="Trend report")
@click.option("--suite", "suite_name", help="Suite for --trend")
@click.option("--from", "date_from", help="trend filter: started_at >= from")
@click.option("--to", "date_to", help="trend filter: started_at <= to")
@click.option("--html", "as_html", is_flag=True, help="Generate HTML")
@click.option("--output", "output_path", help="Override HTML output path")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def eval_report(run_id, trend, suite_name, date_from, date_to, as_html, output_path,
                as_json) -> None:
    project_root = Path.cwd()
    if trend:
        if not suite_name:
            click.echo("Error: --trend requires --suite", err=True)
            raise SystemExit(1)
        out = generate_trend_report(
            project_root, suite_name, date_from=date_from, date_to=date_to,
            html_out=Path(output_path) if output_path else None,
        )
        click.echo(f"trend: {out}")
        return
    if not run_id:
        click.echo("Error: --run <id> or --trend required", err=True)
        raise SystemExit(1)
    run_dir = run_dir_for(project_root, run_id)
    report_path = run_dir / "report.json"
    if not report_path.exists():
        click.echo(f"Error: run not found: {run_id}", err=True)
        raise SystemExit(1)
    if as_json:
        click.echo(report_path.read_text(encoding="utf-8"))
        return
    gate = read_gate_result(run_dir)
    click.echo(f"run_id: {run_id}")
    click.echo(f"verdict: {gate.verdict}")
    if as_html:
        click.echo(f"html: {run_dir / 'report.html'}")


# ── aa eval gate （只读，不重算；对齐 TS eval.ts gate action）──────────────────
_VERDICT_EXIT = {"pass": 0, "pass_with_warnings": 0, "fail": 1,
                 "inconclusive": 1, "needs_human_review": 30}


@eval_group.command("gate")
@click.option("--run", "run_id", required=True, help="Run id")
def eval_gate(run_id: str) -> None:
    """Read gate result (does NOT recompute)."""
    project_root = Path.cwd()
    run_dir = run_dir_for(project_root, run_id)
    try:
        gate = read_gate_result(run_dir)
    except (FileNotFoundError, AaError) as err:
        click.echo(f"eval gate failed: {err}", err=True)
        raise SystemExit(1) from err
    click.echo(f"suite:   {gate.suite}")
    click.echo(f"verdict: {gate.verdict}")
    if gate.hard_gate_failures:
        click.echo(f"hard_gate_failures: {', '.join(gate.hard_gate_failures)}")
    raise SystemExit(_VERDICT_EXIT.get(gate.verdict, 1))


# ── aa eval compare（相对 baseline 的 per-metric delta；对齐 TS eval.ts compare）──
@eval_group.command("compare")
@click.option("--baseline", "baseline_name", required=True,
              help='Baseline name (currently only "main" supported)')
@click.option("--run", "run_id", required=True, help="Run id")
def eval_compare(baseline_name: str, run_id: str) -> None:
    """Compare a run against the named baseline (read-only)."""
    project_root = Path.cwd()
    try:
        baseline = read_baseline(project_root, baseline_name)
        run_dir = run_dir_for(project_root, run_id)
        manifest = read_run_manifest(run_dir)
        entry = baseline.get(manifest.suite)
        if entry is None:
            click.echo(f"No baseline found for suite: {manifest.suite}")
            raise SystemExit(0)
        delta = compare_with_baseline(run_dir, entry.metrics)
    except (FileNotFoundError, AaError) as err:
        click.echo(f"eval compare failed: {err}", err=True)
        raise SystemExit(1) from err
    click.echo(f"Comparing {run_id} vs baseline ({entry.run_id})")
    for metric, value in delta.items():
        sign = f"+{value:.4f}" if value >= 0 else f"{value:.4f}"
        click.echo(f"  {metric}: {sign}")


# ── aa eval baseline update（需人工确认；对齐 TS eval.ts baseline update）─────────
@eval_group.group("baseline")
def eval_baseline() -> None:
    """Manage eval baselines."""


@eval_baseline.command("update")
@click.option("--suite", "suite_name", required=True, help="Suite name")
@click.option("--run", "run_id", required=True, help="Run id to use as new baseline")
@click.option("--approved-by", default="unknown", help="Approver initials")
@click.option("--yes", is_flag=True, help="Skip interactive confirmation")
def eval_baseline_update(suite_name: str, run_id: str, approved_by: str, yes: bool) -> None:
    """Update baseline for a suite (requires human confirmation)."""
    project_root = Path.cwd()
    if not yes and not click.confirm(
        f"Promote run {run_id} to baseline 'main' for suite {suite_name}?"
    ):
        click.echo("aborted", err=True)
        raise SystemExit(1)
    try:
        update_baseline(project_root, suite_name=suite_name, run_id=run_id,
                        approved_by=approved_by)
    except (FileNotFoundError, AaError) as err:
        click.echo(f"eval baseline update failed: {err}", err=True)
        raise SystemExit(1) from err
    click.echo(f"baseline updated: main.json [{suite_name}] <- {run_id}")
```

> `eval_cmd.py` 顶部新增：`from assurance_agent.eval.baseline import (read_baseline, read_run_manifest, compare_with_baseline, update_baseline)`。`_VERDICT_EXIT` 里 `needs_human_review→30` 与 M4 退出码语义一致。

在 `assurance_agent/cli.py` 挂载（与 M1 `main.add_command(...)` 同风格）：

```python
# assurance_agent/cli.py（新增两行，位置与其它 add_command 一致）
from assurance_agent.commands.eval_cmd import eval_group
main.add_command(eval_group)
```

- [ ] **Step 3: 增量更新并验证累计 `.importlinter` 契约**

只在现有主 `layers` 契约的 `assurance_agent.commands` 与 `assurance_agent.risk` 之间插入 `assurance_agent.eval`。不得整文件替换；M3 的两个 forbidden 契约与 M6 的 `driver-layer` 必须原样保留：

```ini
[importlinter]
root_package = assurance_agent

[importlinter:contract:layers]
name = commands depend on domain, never the reverse
type = layers
layers =
    assurance_agent.cli
    assurance_agent.commands
    assurance_agent.eval
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

[importlinter:contract:driver-layer]
name = workflow.driver sits above orchestration and core
type = layers
layers =
    assurance_agent.workflow.driver
    assurance_agent.workflow.orchestration
    assurance_agent.workflow.core
```

> **说明**：`eval` 位于 `commands` 之下、`risk/workflow` 之上，允许 `eval → workflow.driver/artifacts`，禁止 `workflow → eval`。`retro` 在 Task 8 以独立契约加入（因 `eval` 与 `retro` 互不依赖，用单独 layers 段表达两者与 workflow 的相对关系）。`uv run lint-imports` 必须报告 4 contracts kept；数量减少即失败。

- [ ] **Step 4: 跑测试 + 静态检查（含 lint-imports）+ 提交**

```bash
uv run pytest tests/integration/test_eval_cli.py -v
uv run ruff check .
uv run pyright
uv run lint-imports
```

预期：eval CLI 9 条（run/plan/report/gate/compare/baseline update）通过；ruff / pyright / lint-imports clean。

```bash
git add assurance_agent/commands/eval_cmd.py assurance_agent/cli.py .importlinter tests/integration/test_eval_cli.py
git commit -m "feat: aa eval run|plan|report|gate|compare|baseline CLI + eval import layer (M8 task 5)"
```

---

### Task 5b: eval baseline 管理模块（baseline.py）

> **P1 修订**：`aa eval gate|compare|baseline` 属一一对应迁移，原先误列 deferred。gate 复用 Task 3 的
> `read_gate_result`；本任务补齐 `baseline.py`（对齐 TS `src/eval/baseline.ts` + `report.ts::compareWithBaseline`）。

**Files:**
- Create: `assurance_agent/eval/baseline.py`
- Test: `tests/unit/eval/test_baseline.py`

**Interfaces（对齐 TS `baseline.ts`）：**
- `BaselineSuiteEntry(run_id, suite_version, approved_at, approved_by, metrics: dict[str,float])`；`BaselineFile = dict[str, BaselineSuiteEntry]`。
- `read_baseline(project_root, name="main") -> BaselineFile`——读 `eval/baselines/<name>.json`，缺失/空/`{}` 返回 `{}`。
- `compare_with_baseline(run_dir: Path, baseline_metrics: dict[str,float]) -> dict[str,float]`——`read_metrics(run_dir)` 后对**双方都有**的 metric 计 `run - baseline`（对齐 TS `report.ts` 390-405）。
- `read_run_manifest(run_dir: Path) -> RunManifest`；`update_baseline(project_root, *, suite_name, run_id, approved_by) -> Path` 校验 manifest.suite 与参数一致，再结合 `read_metrics` 写 `eval/baselines/main.json`（原子 replace，保留其它 suite 条目）。**人工确认在 CLI 层**。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/eval/test_baseline.py
import json
from pathlib import Path

from assurance_agent.eval.baseline import compare_with_baseline, read_baseline, update_baseline
from assurance_agent.eval.types import RunManifest, SuiteMetrics


def _run(root: Path, run_id: str, suite: str, metrics: dict[str, float]) -> Path:
    run = root / "eval/out/runs" / run_id
    run.mkdir(parents=True)
    manifest = RunManifest(
        run_id=run_id, suite=suite, scorer=suite, selected_sample_ids=["S-1"],
        total_samples=1, executed_samples=1, target_model="m",
        started_at="2026-07-15T00:00:00Z", completed_at="2026-07-15T00:01:00Z",
    )
    (run / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    value = SuiteMetrics(run_id=run_id, suite=suite, sample_count=1, metrics=metrics)
    (run / "metrics.json").write_text(value.model_dump_json(), encoding="utf-8")
    return run


def test_compare_only_shared_metrics(tmp_path: Path) -> None:
    run = _run(tmp_path, "r1", "s1", {"shared": 0.8, "new": 0.4})
    assert compare_with_baseline(run, {"shared": 0.5, "old": 1.0}) == {
        "shared": pytest.approx(0.3)
    }


def test_read_baseline_missing_returns_empty(tmp_path: Path) -> None:
    assert read_baseline(tmp_path) == {}


def test_update_preserves_other_suites(tmp_path: Path) -> None:
    first = _run(tmp_path, "r1", "s1", {"x": 0.5})
    second = _run(tmp_path, "r2", "s2", {"y": 0.8})
    update_baseline(tmp_path, suite_name="s1", run_id=first.name, approved_by="a")
    update_baseline(tmp_path, suite_name="s2", run_id=second.name, approved_by="b")
    baseline = read_baseline(tmp_path)
    assert set(baseline) == {"s1", "s2"}
    assert baseline["s1"].run_id == "r1"
```

Run: `uv run pytest tests/unit/eval/test_baseline.py -v`
Expected: FAIL（`baseline.py` 不存在）

- [ ] **Step 2: 实现 baseline.py**

```python
# assurance_agent/eval/baseline.py
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from assurance_agent.eval.metrics import read_metrics
from assurance_agent.eval.paths import run_dir as run_dir_for
from assurance_agent.eval.types import RunManifest
from assurance_agent.exceptions import AaError


class BaselineSuiteEntry(BaseModel):
    run_id: str
    suite_version: str = "1"
    approved_at: str
    approved_by: str
    metrics: dict[str, float]


BaselineFile = dict[str, BaselineSuiteEntry]


def read_run_manifest(run_dir: Path) -> RunManifest:
    try:
        return RunManifest.model_validate_json((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise AaError(f"invalid eval run manifest: {run_dir}: {err}") from err


def read_baseline(project_root: Path, name: str = "main") -> BaselineFile:
    if re.fullmatch(r"[A-Za-z0-9._-]+", name) is None:
        raise AaError(f"unsafe baseline name: {name!r}")
    path = project_root / "eval/baselines" / f"{name}.json"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("baseline root must be an object")
    except (OSError, ValueError) as err:
        raise AaError(f"invalid baseline {path}: {err}") from err
    try:
        return {key: BaselineSuiteEntry.model_validate(value) for key, value in raw.items()}
    except ValidationError as err:
        raise AaError(f"invalid baseline {path}: {err}") from err


def compare_with_baseline(run_dir: Path, baseline_metrics: dict[str, float]) -> dict[str, float]:
    current = read_metrics(run_dir).metrics
    return {
        name: current[name] - baseline_metrics[name]
        for name in sorted(current.keys() & baseline_metrics.keys())
    }


def update_baseline(
    project_root: Path, *, suite_name: str, run_id: str, approved_by: str,
) -> Path:
    run_dir = run_dir_for(project_root, run_id)
    manifest = read_run_manifest(run_dir)
    if manifest.suite != suite_name:
        raise AaError(f"run suite {manifest.suite!r} does not match {suite_name!r}")
    baseline = read_baseline(project_root)
    baseline[suite_name] = BaselineSuiteEntry(
        run_id=run_id, approved_at=datetime.now(timezone.utc).isoformat(),
        approved_by=approved_by, metrics=read_metrics(run_dir).metrics,
    )
    path = project_root / "eval/baselines/main.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in baseline.items()}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
```

- [ ] **Step 3: 门禁 + Commit**

Run: `uv run pytest tests/unit/eval/test_baseline.py -v && uv run ruff check . && uv run pyright`
Expected: 3 passed；静态检查通过

```bash
git add assurance_agent/eval/baseline.py tests/unit/eval/test_baseline.py
git commit -m "feat: eval baseline read/compare/update (M8 task 5b)"
```

---

### Task 6: retro 类型 + archive_reader + aggregator + eval_trend + proposals + state

**Files:**
- Create: `assurance_agent/retro/__init__.py`（空）
- Create: `assurance_agent/retro/types.py`
- Create: `assurance_agent/retro/archive_reader.py`
- Create: `assurance_agent/retro/aggregator.py`
- Create: `assurance_agent/retro/eval_trend.py`
- Create: `assurance_agent/retro/proposals.py`
- Create: `assurance_agent/retro/state.py`
- Create: `tests/unit/retro/__init__.py`（空）
- Create: `tests/unit/retro/archive_fixtures.py`
- Test: `tests/unit/retro/test_aggregator.py`
- Test: `tests/unit/retro/test_state.py`

**Interfaces:**
- Consumes: M2 `FailureAnalysis`/`Review`/`ApplySummary`/`WorkflowState`（逐文件 typed validation；缺失或历史坏文件降为 `None`，绝不把同一结构化产物作为裸 dict 传给 aggregator）。
- Produces：
  - `ArchivedChange(change_id, evidence_source, path, events, failure_analysis, reviews, apply_summaries, workflow_state)`；
  - `RetroContext(retro_id, generated_at, window: RetroWindow, signals: RetroSignalSet, signal_count)`（**顶层 `signal_count` 为有意增补**，见设计取舍 3）；
  - `ChangeSource(change_id, evidence_source: Literal["archive","unarchived"], path)`；各 Signal 模型；`RetroProposal`、`RetroPromoteRecord`；
  - `read_archived_change(...)`/`list_archived_changes(...)`；`build_retro_context(project_root, *, since=None, changes=None, retro_id=None) -> RetroContext`；`count_signals(context) -> int`；
  - `read_eval_trend(project_root, *, suites=None) -> list[EvalTrendSignal]`；
  - `read_proposals(retro_dir)`/`validate_retro_proposals(context, proposals) -> list[str]`；
  - `read_state`/`write_state`/`mark_consumed_change`/`complete_retro_stage`（`qa/retro/_state.json`）。Task 7/8 消费。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/retro/archive_fixtures.py
from __future__ import annotations

import json
from pathlib import Path

import yaml


def make_archived_change(project_root: Path, change_id: str, *,
                         failures: list[dict] | None = None,
                         review_decision: str = "pass",
                         gate_pushbacks: int = 0,
                         apply_status: str = "applied") -> Path:
    root = project_root / "qa" / "archive" / change_id
    root.mkdir(parents=True)
    events = [{"type": "workflow_started", "change_id": change_id}]
    for _ in range(gate_pushbacks):
        events.append({"type": "gate_pushback", "gate": "case-review"})
    events.append({
        "source": "progression", "type": "healing_attempt_allocated",
        "episode_id": f"ep-{change_id}", "attempt_id": f"ha-{change_id}-1",
        "attempt_number": 1, "operation_id": f"op-{change_id}-1", "source_batch_id": "b1",
    })
    (root / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (root / "workflow-state.yaml").write_text(
        yaml.safe_dump({"change_id": change_id, "phases": {}}), encoding="utf-8")
    inspect = root / "inspect"
    inspect.mkdir()
    category_map = {
        "assertion": "assertion_failure", "locator": "locator_failure",
        "environment": "environment_failure",
    }
    entries = []
    for index, raw in enumerate(failures or [], start=1):
        category = category_map.get(raw.get("classification", ""), raw.get("category", "test_code_error"))
        entries.append({
            "id": f"F-{index}", "case_id": f"TC-{index}", "target": "api",
            "category": category, "fix_proposal_eligible": False, "severity": "high",
            "evidence": {"result_file": "", "test_file": "", "trace": "", "screenshot": "",
                         "video": "", "raw_log": "", "log_excerpt": "fixture"},
            "diagnosis": "fixture", "recommended_action": "review",
        })
    (inspect / "failure-analysis.json").write_text(
        json.dumps({
            "schema_version": "1.0", "change_id": change_id,
            "source_manifest": "execution/execution-manifest.yaml",
            "inspection_status": "completed", "batch_id": "b1", "source_batch_id": "b1",
            "final_status": "FAIL" if entries else "PASS", "inspect_mode": "primary",
            "classification_performed": True, "status": "analyzed" if entries else "no_failures",
            "failures": entries, "hard_fails": entries, "needs_review": [],
            "known_product_issues": [],
        }), encoding="utf-8")
    review = root / "review"
    review.mkdir()
    (review / "case-review.json").write_text(
        json.dumps({"schema_version": "1.0", "decision": review_decision, "findings": []}),
        encoding="utf-8")
    healing = root / "healing"
    healing.mkdir()
    (healing / "api-apply-summary.json").write_text(
        json.dumps({"schema_version": "1.0", "target": "api",
                    "applied": apply_status == "applied", "status": apply_status}),
        encoding="utf-8")
    return root
```

```python
# tests/unit/retro/test_aggregator.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.retro.aggregator import build_retro_context, count_signals
from tests.unit.retro.archive_fixtures import make_archived_change


def test_build_context_golden_signals(tmp_path: Path) -> None:
    make_archived_change(tmp_path, "CH-1",
                         failures=[{"classification": "assertion"},
                                   {"classification": "assertion"},
                                   {"classification": "locator"}],
                         review_decision="pass", gate_pushbacks=2)
    make_archived_change(tmp_path, "CH-2",
                         failures=[{"classification": "environment"}],
                         review_decision="reject", gate_pushbacks=0)

    context = build_retro_context(tmp_path, changes=["CH-1", "CH-2"], retro_id="retro-test")

    assert context.retro_id == "retro-test"
    assert context.window.change_count == 2
    assert sorted(context.window.change_ids) == ["CH-1", "CH-2"]
    # 失败分布使用 M2 FailureCategory canonical 值。
    dist = {s.category: s.count for s in context.signals.failure_distribution}
    assert dist == {
        "assertion_failure": 2, "locator_failure": 1, "environment_failure": 1,
    }
    # gate pushback 累计 2（均来自 CH-1 的 case-review）
    pushback = {s.gate: s.count for s in context.signals.gate_pushback}
    assert pushback.get("case-review") == 2
    # 顶层 signal_count 与 count_signals 一致（设计取舍 3）
    assert context.signal_count == count_signals(context)
    assert context.signal_count > 0


def test_build_context_no_changes_zero_signals(tmp_path: Path) -> None:
    (tmp_path / "qa" / "archive").mkdir(parents=True)
    context = build_retro_context(tmp_path, changes=[], retro_id="retro-empty")
    assert context.window.change_count == 0
    assert context.signal_count == 0
    assert count_signals(context) == 0


def test_build_context_since_scans_archive(tmp_path: Path) -> None:
    make_archived_change(tmp_path, "CH-A", failures=[{"classification": "assertion"}])
    make_archived_change(tmp_path, "CH-B", failures=[])
    context = build_retro_context(tmp_path, since="2000-01-01T00:00:00Z", retro_id="retro-scan")
    assert context.window.change_count == 2
```

```python
# tests/unit/retro/test_state.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.retro.state import (
    complete_retro_stage, mark_consumed_change, read_state,
)


def test_mark_consumed_change_persists(tmp_path: Path) -> None:
    mark_consumed_change(tmp_path, change_id="CH-1", source="archive",
                         consumed_at="2026-07-15T00:00:00Z", retro_id="retro-1")
    state = read_state(tmp_path)
    assert state["consumed_changes"]["CH-1"]["retro_id"] == "retro-1"


def test_complete_retro_stage_sets_last_retro(tmp_path: Path) -> None:
    complete_retro_stage(tmp_path, "retro-9")
    assert read_state(tmp_path)["last_retro_id"] == "retro-9"
```

```bash
uv run pytest tests/unit/retro/test_aggregator.py tests/unit/retro/test_state.py -v
```

预期：`ModuleNotFoundError: assurance_agent.retro.*`。

- [ ] **Step 2: 实现 types.py**

```python
# assurance_agent/retro/types.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assurance_agent.artifacts.models import ApplySummary, FailureAnalysis, Review, WorkflowState

EvidenceSource = Literal["archive", "unarchived"]


class ArchivedChange(BaseModel):
    change_id: str
    evidence_source: EvidenceSource
    path: str
    events: list[dict] = Field(default_factory=list)
    failure_analysis: FailureAnalysis | None = None
    reviews: dict[str, Review] = Field(default_factory=dict)
    apply_summaries: list[ApplySummary] = Field(default_factory=list)
    workflow_state: WorkflowState | None = None


class ChangeSource(BaseModel):
    change_id: str
    evidence_source: EvidenceSource
    path: str = ""


class FailureDistributionSignal(BaseModel):
    category: str
    count: int


class GatePushbackSignal(BaseModel):
    gate: str
    count: int


class HealingEfficiencySignal(BaseModel):
    attempts: int = 0
    applied: int = 0
    success_rate: float = 0.0


class ReclassificationSignal(BaseModel):
    change_id: str
    from_category: str
    to_category: str


class HumanDecisionSignal(BaseModel):
    change_id: str
    decision: str


class SkillExecutionSignal(BaseModel):
    skill: str
    count: int


class EvalTrendSignal(BaseModel):
    suite: str
    run_id: str
    verdict: str
    started_at: str


class RetroSignalSet(BaseModel):
    failure_distribution: list[FailureDistributionSignal] = Field(default_factory=list)
    gate_pushback: list[GatePushbackSignal] = Field(default_factory=list)
    healing_efficiency: HealingEfficiencySignal = Field(default_factory=HealingEfficiencySignal)
    human_decisions: list[HumanDecisionSignal] = Field(default_factory=list)
    reclassifications: list[ReclassificationSignal] = Field(default_factory=list)
    skill_execution: list[SkillExecutionSignal] = Field(default_factory=list)
    eval_trend: list[EvalTrendSignal] = Field(default_factory=list)


class RetroWindow(BaseModel):
    since: str | None = None
    change_count: int = 0
    change_ids: list[str] = Field(default_factory=list)
    change_sources: list[ChangeSource] = Field(default_factory=list)


class RetroContext(BaseModel):
    retro_id: str
    generated_at: str
    window: RetroWindow
    signals: RetroSignalSet
    signal_count: int = 0    # 有意增补的顶层字段（benchmark nightly 分支消费）


class RetroProposal(BaseModel):
    id: str
    apply_kind: Literal["memory_append", "skill_edit", "other"] = "memory_append"
    eval_suite: str | None = None
    summary: str = ""
    body: str = ""


class RetroPromoteRecord(BaseModel):
    proposal_id: str
    decision: Literal["promoted", "rejected", "needs_rework"]
    decided_by: str
    decided_at: str
    rework_note: str | None = None
    eval_run_id: str | None = None
```

- [ ] **Step 3: 实现 archive_reader.py**

```python
# assurance_agent/retro/archive_reader.py
from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from assurance_agent.artifacts.models import ApplySummary, FailureAnalysis, Review, WorkflowState
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.retro.types import ArchivedChange, EvidenceSource

ModelT = TypeVar("ModelT", bound=BaseModel)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_yaml(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _read_model(path: Path, model: type[ModelT], *, yaml_input: bool = False) -> ModelT | None:
    raw = _read_yaml(path) if yaml_input else _read_json(path)
    if raw is None:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None  # archive tolerance: invalid historical artifact is absent, never a raw dict


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def read_archived_change(change_dir: Path, *, source: EvidenceSource) -> ArchivedChange:
    reviews: dict[str, Review] = {}
    review_dir = change_dir / "review"
    if review_dir.is_dir():
        for file in sorted(review_dir.glob("*.json")):
            data = _read_model(file, Review)
            if data is not None:
                reviews[file.stem] = data
    apply_summaries: list[ApplySummary] = []
    healing_dir = change_dir / "healing"
    if healing_dir.is_dir():
        for file in sorted(healing_dir.glob("*-apply-summary.json")):
            data = _read_model(file, ApplySummary)
            if data is not None:
                apply_summaries.append(data)
    return ArchivedChange(
        change_id=change_dir.name,
        evidence_source=source,
        path=str(change_dir),
        events=_read_events(change_dir / "events.jsonl"),
        failure_analysis=_read_model(
            change_dir / "inspect" / "failure-analysis.json", FailureAnalysis,
        ),
        reviews=reviews,
        apply_summaries=apply_summaries,
        workflow_state=_read_model(
            change_dir / "workflow-state.yaml", WorkflowState, yaml_input=True,
        ),
    )


def list_archived_changes(project_root: Path) -> list[str]:
    archive = project_root / "qa" / "archive"
    if not archive.is_dir():
        return []
    return sorted(p.name for p in archive.iterdir() if p.is_dir())


def resolve_change_dir(project_root: Path, change_id: str) -> tuple[Path, EvidenceSource] | None:
    assert_change_id_safe(change_id)
    archived = project_root / "qa" / "archive" / change_id
    if archived.is_dir():
        return archived, "archive"
    unarchived = project_root / "qa" / "changes" / change_id
    if unarchived.is_dir():
        return unarchived, "unarchived"
    return None
```

- [ ] **Step 4: 实现 aggregator.py + eval_trend.py**

```python
# assurance_agent/retro/eval_trend.py
from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.types import EvalTrendSignal


def read_eval_trend(project_root: Path, *, suites: list[str] | None = None) -> list[EvalTrendSignal]:
    runs = project_root / "eval" / "out" / "runs"
    if not runs.is_dir():
        return []
    signals: list[EvalTrendSignal] = []
    for run in sorted(runs.iterdir()):
        report = run / "report.json"
        if not report.exists():
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        suite = data.get("suite", "")
        if suites and suite not in suites:
            continue
        signals.append(EvalTrendSignal(
            suite=suite, run_id=data.get("run_id", run.name),
            verdict=data.get("verdict", "unknown"),
            started_at=data.get("started_at", ""),
        ))
    return signals
```

```python
# assurance_agent/retro/aggregator.py
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.identifiers import assert_change_id_safe, assert_path_segment_safe
from assurance_agent.retro.archive_reader import (
    list_archived_changes, read_archived_change, resolve_change_dir,
)
from assurance_agent.exceptions import AaError
from assurance_agent.retro.eval_trend import read_eval_trend
from assurance_agent.retro.types import (
    ArchivedChange, ChangeSource, FailureDistributionSignal, GatePushbackSignal,
    HealingEfficiencySignal, HumanDecisionSignal, RetroContext, RetroSignalSet,
    RetroWindow, SkillExecutionSignal,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_retro_id() -> str:
    return "retro-" + datetime.now(timezone.utc).strftime("%Y%m%d")


def _collect_changes(project_root: Path, since: str | None,
                     changes: list[str] | None) -> list[ArchivedChange]:
    cutoff: float | None = None
    if since is not None:
        try:
            cutoff = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        except ValueError as err:
            raise AaError(f"invalid --since timestamp: {since}") from err
    ids = changes if changes is not None else list_archived_changes(project_root)
    result: list[ArchivedChange] = []
    for change_id in ids:
        resolved = resolve_change_dir(project_root, change_id)
        if resolved is None:
            continue
        change_dir, source = resolved
        if cutoff is not None and change_dir.stat().st_mtime < cutoff:
            continue
        result.append(read_archived_change(change_dir, source=source))
    return result


def _failure_distribution(changes: list[ArchivedChange]) -> list[FailureDistributionSignal]:
    counter: Counter[str] = Counter()
    for change in changes:
        if change.failure_analysis is None:
            continue
        for failure in change.failure_analysis.failures:
            counter[str(failure.category)] += 1
    return [FailureDistributionSignal(category=c, count=n)
            for c, n in sorted(counter.items())]


def _gate_pushback(changes: list[ArchivedChange]) -> list[GatePushbackSignal]:
    counter: Counter[str] = Counter()
    for change in changes:
        for event in change.events:
            if event.get("type") == "gate_pushback":
                counter[event.get("gate", "unknown")] += 1
    return [GatePushbackSignal(gate=g, count=n) for g, n in sorted(counter.items())]


def _healing_efficiency(changes: list[ArchivedChange]) -> HealingEfficiencySignal:
    operation_ids: set[str] = set()
    applied = 0
    for change in changes:
        operation_ids.update(
            str(event["operation_id"])
            for event in change.events
            if event.get("type") == "healing_attempt_allocated" and event.get("operation_id")
        )
        for summary in change.apply_summaries:
            if summary.applied:
                applied += 1
    attempts = len(operation_ids)
    rate = applied / attempts if attempts else 0.0
    return HealingEfficiencySignal(attempts=attempts, applied=applied, success_rate=rate)


def _human_decisions(changes: list[ArchivedChange]) -> list[HumanDecisionSignal]:
    decisions: list[HumanDecisionSignal] = []
    for change in changes:
        for event in change.events:
            if event.get("type") == "human_decision":
                decisions.append(HumanDecisionSignal(
                    change_id=change.change_id, decision=str(event.get("action", "unknown"))))
    return decisions


def _skill_execution(changes: list[ArchivedChange]) -> list[SkillExecutionSignal]:
    counter: Counter[str] = Counter()
    for change in changes:
        for event in change.events:
            if event.get("type") == "skill_executed":
                counter[event.get("skill", "unknown")] += 1
    return [SkillExecutionSignal(skill=s, count=n) for s, n in sorted(counter.items())]


def build_retro_context(
    project_root: Path,
    *,
    since: str | None = None,
    changes: list[str] | None = None,
    retro_id: str | None = None,
) -> RetroContext:
    for change_id in changes or []:
        assert_change_id_safe(change_id)
    resolved_retro_id = retro_id or _default_retro_id()
    assert_path_segment_safe(resolved_retro_id, label="retro id")
    collected = _collect_changes(project_root, since, changes)
    signals = RetroSignalSet(
        failure_distribution=_failure_distribution(collected),
        gate_pushback=_gate_pushback(collected),
        healing_efficiency=_healing_efficiency(collected),
        human_decisions=_human_decisions(collected),
        skill_execution=_skill_execution(collected),
        eval_trend=read_eval_trend(project_root),
    )
    window = RetroWindow(
        since=since,
        change_count=len(collected),
        change_ids=[c.change_id for c in collected],
        change_sources=[ChangeSource(change_id=c.change_id,
                                     evidence_source=c.evidence_source, path=c.path)
                        for c in collected],
    )
    context = RetroContext(
        retro_id=resolved_retro_id,
        generated_at=_now(),
        window=window,
        signals=signals,
    )
    context.signal_count = count_signals(context)
    return context


def count_signals(context: RetroContext) -> int:
    signals = context.signals
    total = 0
    total += sum(s.count for s in signals.failure_distribution)
    total += sum(s.count for s in signals.gate_pushback)
    total += signals.healing_efficiency.attempts
    total += len(signals.human_decisions)
    total += len(signals.reclassifications)
    total += sum(s.count for s in signals.skill_execution)
    total += len(signals.eval_trend)
    return total
```

> **`count_signals` 口径**：与 TS `src/retro/nightly/utils.ts` 的 `countSignals` 同构——累计各信号族的"证据条数"，用于 nightly collect 的 no-op 判定（`signal_count==0` → exit 10）。`signal_count` 同时写入 context 顶层供 benchmark 读取。

- [ ] **Step 5: 实现 proposals.py + state.py**

```python
# assurance_agent/retro/proposals.py
from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.retro.types import RetroContext, RetroProposal


def read_proposals(retro_dir: Path) -> list[RetroProposal]:
    path = retro_dir / "proposals.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    entries = raw.get("proposals", raw) if isinstance(raw, dict) else raw
    proposals: list[RetroProposal] = []
    if isinstance(entries, list):
        for entry in entries:
            try:
                proposals.append(RetroProposal.model_validate(entry))
            except ValidationError:
                continue
    return proposals


def validate_retro_proposals(context: RetroContext,
                             proposals: list[RetroProposal]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for proposal in proposals:
        if proposal.id in seen:
            errors.append(f"duplicate proposal id: {proposal.id}")
        seen.add(proposal.id)
        if proposal.apply_kind == "memory_append" and not proposal.body.strip():
            errors.append(f"memory_append proposal {proposal.id} has empty body")
    return errors
```

```python
# assurance_agent/retro/state.py
from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.types import EvidenceSource


def _state_path(project_root: Path) -> Path:
    return project_root / "qa" / "retro" / "_state.json"


def read_state(project_root: Path) -> dict:
    path = _state_path(project_root)
    if not path.exists():
        return {"last_retro_id": None, "consumed_changes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_retro_id": None, "consumed_changes": {}}
    data.setdefault("last_retro_id", None)
    data.setdefault("consumed_changes", {})
    return data


def write_state(project_root: Path, state: dict) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mark_consumed_change(project_root: Path, *, change_id: str, source: EvidenceSource,
                         consumed_at: str, retro_id: str) -> None:
    state = read_state(project_root)
    state["consumed_changes"][change_id] = {
        "source": source, "consumed_at": consumed_at, "retro_id": retro_id,
        "terminal": False,
    }
    write_state(project_root, state)


def complete_retro_stage(project_root: Path, retro_id: str) -> None:
    state = read_state(project_root)
    state["last_retro_id"] = retro_id
    for record in state["consumed_changes"].values():
        if record.get("retro_id") == retro_id:
            record["terminal"] = True
    write_state(project_root, state)
```

- [ ] **Step 6: 跑测试 + 静态检查 + 提交**

```bash
uv run pytest tests/unit/retro/test_aggregator.py tests/unit/retro/test_state.py -v
uv run ruff check .
uv run pyright
```

预期：aggregator 3 条（golden 失败分布/pushback/signal_count 对齐）+ state 2 条通过；ruff / pyright clean。

```bash
git add assurance_agent/retro tests/unit/retro
git commit -m "feat: retro archive reader + signal aggregator + eval trend + state (M8 task 6)"
```

---

### Task 7: retro nightly 驱动（phase A / D / E / F + collect/resume/report + 退出码）

**Files:**
- Create: `assurance_agent/retro/nightly/__init__.py`（空）
- Create: `assurance_agent/retro/nightly/exit_codes.py`
- Create: `assurance_agent/retro/nightly/types.py`
- Create: `assurance_agent/retro/nightly/utils.py`
- Create: `assurance_agent/retro/nightly/agent.py`
- Create: `assurance_agent/retro/nightly/phase_a.py`
- Create: `assurance_agent/retro/nightly/phase_d.py`
- Create: `assurance_agent/retro/nightly/phase_e.py`
- Create: `assurance_agent/retro/nightly/phase_f.py`
- Create: `assurance_agent/retro/nightly/driver.py`
- Create: `tests/unit/retro/nightly/__init__.py`（空）
- Test: `tests/unit/retro/nightly/test_phases.py`
- Test: `tests/unit/retro/nightly/test_collect.py`
- Test: `tests/unit/retro/nightly/test_resume.py`

**Interfaces:**
- Consumes: Task 6 `build_retro_context`/`count_signals`/`read_proposals`/`validate_retro_proposals`/`state`；archive_reader。
- Produces：
  - 退出码常量 `NIGHTLY_OK=0`/`NIGHTLY_NOOP=10`/`NIGHTLY_PENDING_REVIEW=30`/`NIGHTLY_FAILURE=40`；
  - `NightlyOptions`/`ChangeCandidate`；`generate_retro_id`；
  - phase A `enumerate_candidates(sut, state, *, is_terminal) -> tuple[list[ChangeCandidate], list[str]]`、`has_required_evidence(change_dir)`、`snapshot_unarchived_evidence(sut, retro_id, change_id)`；
  - phase D `partition_proposals_for_review(proposals, promotions, *, min_evidence, rework_alert)`、`build_review_queue_markdown(retro_id, partition)`；
  - phase E `stage_memory_proposals(sut, retro_id, suite, proposals) -> Path`（返回仅供 eval overlay 的 memory dir）、`apply_staged_memory(sut, staged_memory) -> list[str]`（只在 phase F 通过后原子复制到 `.aa/memory`）；
  - phase F `compare_suite_regression(baseline, candidate, suite)`、`classify_eval_gate(gate)`、`should_auto_apply(comparison, suite)`；
  - `EvalRunResult(run_id, metrics, gate, suite)` 与 `EvalRunner = Callable[[Path, str, Path | None], EvalRunResult]`；`collect_nightly(...)`、`resume_nightly(options, *, eval_runner: EvalRunner) -> int`、`report_nightly(...) -> int`。`resume_nightly` 必须实际调用 runner 两次/每 suite（baseline 无 overlay、candidate 带 staged memory），不得把 `None` 当作成功路径。Task 8 CLI 提供生产 runner 并消费。

> **phase 命名与职责（对齐 `docs/eval.md`「Nightly 驱动」表 + TS `src/retro/nightly/`）**：**A** 枚举未消费/unarchived 且 terminal 的 change，必要时 snapshot 证据；**B** 进程内直调 `build_retro_context` 写 `context.json`，`signal_count==0` → no-op exit 10；**C** 调 `--agent`（默认 `cursor-agent`）生成 `proposals.json`，schema 校验剔除非法提案；**D** 分流提案（`memory_append`→`for_review`、其余→`pr_only`；累计 `needs_rework` ≥ `rework_alert` 的提案标 stuck；`min_evidence` 本里程碑仅记录、暂不参与分流）+ 写 `review-queue.md` + `complete_retro_stage`；**E**（resume）对已 promote 的 `memory_append` stage apply；**F**（resume）按 `eval_suite` 跑 baseline+candidate、相对 baseline 的 hard-gate 回归判定，仅当 suite 有 hard_gates 且未回归才 auto-apply。collect 覆盖 A→D，退出码 0/10/40。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/retro/nightly/test_phases.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.retro.nightly.phase_a import enumerate_candidates, has_required_evidence
from assurance_agent.retro.nightly.phase_d import (
    build_review_queue_markdown, partition_proposals_for_review,
)
from assurance_agent.retro.nightly.phase_f import (
    classify_eval_gate, compare_suite_regression, should_auto_apply,
)
from assurance_agent.retro.types import RetroProposal, RetroPromoteRecord
from tests.unit.retro.archive_fixtures import make_archived_change


def test_has_required_evidence(tmp_path: Path) -> None:
    root = make_archived_change(tmp_path, "CH-1", failures=[])
    assert has_required_evidence(root) is True
    (root / "events.jsonl").unlink()
    assert has_required_evidence(root) is False


def test_enumerate_candidates_skips_consumed_and_non_terminal(tmp_path: Path) -> None:
    make_archived_change(tmp_path, "CH-A", failures=[])
    make_archived_change(tmp_path, "CH-B", failures=[])
    state = {"consumed_changes": {"CH-A": {"terminal": True}}}
    candidates, incomplete = enumerate_candidates(
        tmp_path, state, is_terminal=lambda root, cid: cid != "CH-B")
    ids = [c.change_id for c in candidates]
    assert "CH-A" not in ids            # 已消费
    assert "CH-B" not in ids            # 非 terminal
    assert incomplete == []


def test_partition_forwards_for_review(tmp_path: Path) -> None:
    proposals = [RetroProposal(id="P-1", apply_kind="memory_append", body="x", eval_suite="s"),
                 RetroProposal(id="P-2", apply_kind="memory_append", body="y", eval_suite="s")]
    promotions: list[RetroPromoteRecord] = []
    partition = partition_proposals_for_review(proposals, promotions, min_evidence=2,
                                               rework_alert=3)
    assert {p.id for p in partition.for_review} == {"P-1", "P-2"}
    md = build_review_queue_markdown("retro-1", partition)
    assert "P-1" in md and "retro-1" in md


def test_partition_flags_stuck_after_rework_alert() -> None:
    proposals = [RetroProposal(id="P-1", apply_kind="memory_append", body="x")]
    promotions = [RetroPromoteRecord(proposal_id="P-1", decision="needs_rework",
                                     decided_by="h", decided_at="t") for _ in range(3)]
    partition = partition_proposals_for_review(proposals, promotions, min_evidence=1,
                                               rework_alert=3)
    assert "P-1" in partition.stuck_tags


def test_phase_f_regression_and_auto_apply() -> None:
    baseline = {"case_review_gate_pass_rate": 1.0}
    candidate = {"case_review_gate_pass_rate": 0.5}
    suite = {"thresholds": [{"metric": "case_review_gate_pass_rate", "gate": "hard",
                             "op": "gte", "value": 0.99}]}
    regressed = compare_suite_regression(baseline, candidate, suite)
    assert regressed.regressed is True
    assert should_auto_apply(regressed, suite) is False

    ok = compare_suite_regression(baseline, {"case_review_gate_pass_rate": 1.0}, suite)
    assert ok.regressed is False
    assert should_auto_apply(ok, suite) is True   # 有 hard_gate 且未回归

    missing = compare_suite_regression(baseline, {}, suite)
    assert missing.regressed is True
    assert should_auto_apply(missing, suite) is False  # 缺 hard evidence 必须 fail closed

    observe_suite = {"thresholds": [{"metric": "x", "gate": "observe", "op": "gte",
                                     "value": 0.0}]}
    ok_observe = compare_suite_regression({}, {}, observe_suite)
    assert should_auto_apply(ok_observe, observe_suite) is False   # observe-only 不 auto-apply


def test_classify_eval_gate() -> None:
    assert classify_eval_gate({"verdict": "pass"}) == "pass"
    assert classify_eval_gate({"verdict": "fail"}) == "fail"
    assert classify_eval_gate({"verdict": "bogus"}) == "inconclusive"
    assert classify_eval_gate({}) == "inconclusive"
```

```python
# tests/unit/retro/nightly/test_collect.py
from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.nightly.driver import collect_nightly
from assurance_agent.retro.nightly.exit_codes import (
    NIGHTLY_FAILURE, NIGHTLY_NOOP, NIGHTLY_OK,
)
from assurance_agent.retro.nightly.types import NightlyOptions
from tests.unit.retro.archive_fixtures import make_archived_change


def _opts(sut: Path, **kw) -> NightlyOptions:
    base = dict(sut=str(sut), retro_id="retro-test", dry_run=False, agent="fake-agent",
                history=5, min_evidence=1, rework_alert=3, skip_eval=False, last=10)
    base.update(kw)
    return NightlyOptions(**base)


def _write_proposals(sut: Path, retro_id: str) -> None:
    retro_dir = sut / "qa" / "retro" / retro_id
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "proposals.json").write_text(json.dumps({"proposals": [
        {"id": "P-1", "apply_kind": "memory_append", "body": "append this", "eval_suite": "s"},
    ]}), encoding="utf-8")
    (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")


def test_collect_success_exit_0(tmp_path: Path) -> None:
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}],
                         gate_pushbacks=1)

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        _write_proposals(sut, "retro-test")
        return 0

    code = collect_nightly(_opts(sut), agent_runner=agent_runner,
                           is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_OK
    retro_dir = sut / "qa" / "retro" / "retro-test"
    assert (retro_dir / "context.json").exists()
    assert (retro_dir / "review-queue.md").exists()
    # context.json 顶层 signal_count > 0（benchmark 依赖）
    ctx = json.loads((retro_dir / "context.json").read_text())
    assert ctx["signal_count"] > 0
    assert ctx["window"]["change_count"] == 1


def test_collect_no_changes_exit_10(tmp_path: Path) -> None:
    (tmp_path / "qa" / "archive").mkdir(parents=True)

    def agent_runner(cmd: str, retro_dir: Path) -> int:  # 不应被调用
        raise AssertionError("agent must not run when there are no candidates")

    code = collect_nightly(_opts(tmp_path), agent_runner=agent_runner,
                           is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_NOOP


def test_collect_zero_signals_exit_10(tmp_path: Path) -> None:
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[], gate_pushbacks=0, apply_status="none")
    # 无失败、无 pushback、无 healing、无 eval → signal_count 可能仍来自 apply_summaries；
    # archive_fixtures 写了一条 apply-summary，故此处显式清掉以制造 0 信号场景。
    (sut / "qa" / "archive" / "CH-1" / "healing" / "api-apply-summary.json").unlink()

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        raise AssertionError("agent must not run on zero-signal no-op")

    code = collect_nightly(_opts(sut), agent_runner=agent_runner,
                           is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_NOOP


def test_collect_agent_failure_exit_40(tmp_path: Path) -> None:
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}])

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        return 7    # 非 0 → agent 失败

    code = collect_nightly(_opts(sut), agent_runner=agent_runner,
                           is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_FAILURE


def test_collect_dry_run_stops_before_agent_exit_0(tmp_path: Path) -> None:
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}])

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        raise AssertionError("dry-run must not invoke agent")

    code = collect_nightly(_opts(sut, dry_run=True), agent_runner=agent_runner,
                           is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_OK
    assert (sut / "qa" / "retro" / "retro-test" / "context.json").exists()
```

```python
# tests/unit/retro/nightly/test_resume.py
import json
from pathlib import Path

from assurance_agent.retro.nightly.driver import resume_nightly
from assurance_agent.retro.nightly.exit_codes import NIGHTLY_OK, NIGHTLY_PENDING_REVIEW
from assurance_agent.retro.nightly.types import EvalRunResult, NightlyOptions


def _seed_resume(sut: Path, *, promoted: bool = True) -> Path:
    retro_dir = sut / "qa/retro/retro-test"
    retro_dir.mkdir(parents=True)
    (retro_dir / "proposals.json").write_text(json.dumps({"proposals": [{
        "id": "P-1", "apply_kind": "memory_append", "body": "remember this",
        "eval_suite": "workflow-case",
    }]}), encoding="utf-8")
    promotions = ([{
        "proposal_id": "P-1", "decision": "promoted", "decided_by": "reviewer",
        "decided_at": "2026-07-15T00:00:00Z",
    }] if promoted else [])
    (retro_dir / "promotions.json").write_text(
        json.dumps({"promotions": promotions}), encoding="utf-8"
    )
    return retro_dir


def _options(sut: Path, *, skip_eval: bool = False) -> NightlyOptions:
    return NightlyOptions(sut=str(sut), retro_id="retro-test", skip_eval=skip_eval)


def _runner(candidate: float, *, gate: str = "pass", hard: bool = True):
    calls: list[tuple[str, bool]] = []

    def run(sut: Path, suite: str, extra_memory: Path | None) -> EvalRunResult:
        calls.append((suite, extra_memory is not None))
        value = candidate if extra_memory is not None else 1.0
        threshold_gate = "hard" if hard else "observe"
        return EvalRunResult(
            run_id="candidate-run" if extra_memory is not None else "baseline-run",
            metrics={"case_review_gate_pass_rate": value},
            gate={"verdict": gate},
            suite={"thresholds": [{
                "metric": "case_review_gate_pass_rate", "gate": threshold_gate,
                "op": "gte", "value": 0.99,
            }]},
        )

    return run, calls


def test_resume_all_pending_returns_30_without_eval(tmp_path: Path) -> None:
    _seed_resume(tmp_path, promoted=False)
    runner, calls = _runner(1.0)
    assert resume_nightly(_options(tmp_path), eval_runner=runner) == NIGHTLY_PENDING_REVIEW
    assert calls == []


def test_resume_skip_eval_stages_but_never_applies(tmp_path: Path) -> None:
    retro_dir = _seed_resume(tmp_path)
    runner, calls = _runner(1.0)
    code = resume_nightly(_options(tmp_path, skip_eval=True), eval_runner=runner)
    assert code == NIGHTLY_PENDING_REVIEW
    assert calls == []
    assert (retro_dir / "staged/workflow-case/memory/retro-test.md").is_file()
    assert not (tmp_path / ".aa/memory/retro-test.md").exists()


def test_resume_regression_marks_needs_rework_and_does_not_apply(tmp_path: Path) -> None:
    retro_dir = _seed_resume(tmp_path)
    runner, calls = _runner(0.5)
    assert resume_nightly(_options(tmp_path), eval_runner=runner) == NIGHTLY_PENDING_REVIEW
    assert calls == [("workflow-case", False), ("workflow-case", True)]
    records = json.loads((retro_dir / "promotions.json").read_text())["promotions"]
    assert records[0]["decision"] == "needs_rework"
    assert records[0]["eval_run_id"] == "candidate-run"
    assert not (tmp_path / ".aa/memory/retro-test.md").exists()


def test_resume_non_regressed_hard_gate_auto_applies(tmp_path: Path) -> None:
    retro_dir = _seed_resume(tmp_path)
    runner, calls = _runner(1.0)
    assert resume_nightly(_options(tmp_path), eval_runner=runner) == NIGHTLY_OK
    assert calls == [("workflow-case", False), ("workflow-case", True)]
    assert (tmp_path / ".aa/memory/retro-test.md").read_text().endswith("remember this\n")
    report = json.loads((retro_dir / "nightly-resume.json").read_text())
    assert report["applied_files"] == [".aa/memory/retro-test.md"]


def test_resume_observe_only_never_auto_applies(tmp_path: Path) -> None:
    _seed_resume(tmp_path)
    runner, _calls = _runner(1.0, hard=False)
    assert resume_nightly(_options(tmp_path), eval_runner=runner) == NIGHTLY_PENDING_REVIEW
    assert not (tmp_path / ".aa/memory/retro-test.md").exists()
```

```bash
uv run pytest tests/unit/retro/nightly/test_phases.py tests/unit/retro/nightly/test_collect.py tests/unit/retro/nightly/test_resume.py -v
```

预期：`ModuleNotFoundError: assurance_agent.retro.nightly.*`。

- [ ] **Step 2: 实现 exit_codes.py / types.py / utils.py / agent.py**

```python
# assurance_agent/retro/nightly/exit_codes.py
NIGHTLY_OK = 0
NIGHTLY_NOOP = 10
NIGHTLY_PENDING_REVIEW = 30
NIGHTLY_FAILURE = 40
```

```python
# assurance_agent/retro/nightly/types.py
from __future__ import annotations

from pydantic import BaseModel, Field

from assurance_agent.retro.types import EvidenceSource


class NightlyOptions(BaseModel):
    sut: str
    retro_id: str | None = None
    dry_run: bool = False
    agent: str = "cursor-agent"
    history: int = 5
    min_evidence: int = 2
    rework_alert: int = 3
    skip_eval: bool = False
    last: int = 10


class ChangeCandidate(BaseModel):
    change_id: str
    evidence_source: EvidenceSource
    path: str


class EvalRunResult(BaseModel):
    run_id: str
    metrics: dict[str, float]
    gate: dict
    suite: dict
```

```python
# assurance_agent/retro/nightly/utils.py
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def generate_retro_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"retro-{stamp}"


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_dir_names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
```

```python
# assurance_agent/retro/nightly/agent.py
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def run_agent(agent_cmd: str, retro_dir: Path) -> int:
    """调用外部提案 agent（写 proposals.json / retro-summary.md）。返回退出码。"""
    argv = shlex.split(agent_cmd)
    try:
        completed = subprocess.run(argv, cwd=str(retro_dir.parent.parent.parent), check=False)
    except (OSError, ValueError):
        return 40
    return completed.returncode
```

> **agent 调用为注入接缝**：`collect_nightly(agent_runner=...)` 缺省绑定 `run_agent`（subprocess）；测试注入写 golden `proposals.json` 的假 runner，确定性验证退出码分支。

- [ ] **Step 3: 实现 phase_a.py / phase_d.py / phase_f.py**

```python
# assurance_agent/retro/nightly/phase_a.py
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from assurance_agent.identifiers import assert_change_id_safe, assert_path_segment_safe
from assurance_agent.retro.archive_reader import list_archived_changes, resolve_change_dir
from assurance_agent.retro.nightly.types import ChangeCandidate
from assurance_agent.retro.nightly.utils import list_dir_names

IsTerminal = Callable[[Path, str], bool]


def has_required_evidence(change_dir: Path) -> bool:
    return (change_dir / "events.jsonl").exists() and \
        (change_dir / "workflow-state.yaml").exists()


def enumerate_candidates(
    sut: Path, state: dict, *, is_terminal: IsTerminal,
) -> tuple[list[ChangeCandidate], list[str]]:
    consumed = {cid for cid, rec in state.get("consumed_changes", {}).items()
                if rec.get("terminal")}
    candidates: list[ChangeCandidate] = []
    incomplete: list[str] = []

    for change_id in list_archived_changes(sut):
        if change_id in consumed:
            continue
        resolved = resolve_change_dir(sut, change_id)
        if resolved is None:
            continue
        change_dir, source = resolved
        if not has_required_evidence(change_dir):
            incomplete.append(change_id)
            continue
        candidates.append(ChangeCandidate(change_id=change_id, evidence_source=source,
                                          path=str(change_dir)))

    changes_root = sut / "qa" / "changes"
    for change_id in list_dir_names(changes_root):
        if change_id in consumed or any(c.change_id == change_id for c in candidates):
            continue
        change_dir = changes_root / change_id
        if not has_required_evidence(change_dir):
            incomplete.append(change_id)
            continue
        if not is_terminal(change_dir, change_id):
            continue
        candidates.append(ChangeCandidate(change_id=change_id, evidence_source="unarchived",
                                          path=str(change_dir)))
    return candidates, incomplete


def snapshot_unarchived_evidence(sut: Path, retro_id: str, change_id: str) -> Path:
    assert_path_segment_safe(retro_id, label="retro id")
    assert_change_id_safe(change_id)
    src = sut / "qa" / "changes" / change_id
    dest = sut / "qa" / "retro" / retro_id / "evidence" / change_id
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "workflow-state.yaml"):
        if (src / name).exists():
            shutil.copy2(src / name, dest / name)
    for sub in ("inspect", "review", "healing"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, dest / sub, dirs_exist_ok=True)
    return dest
```

```python
# assurance_agent/retro/nightly/phase_d.py
from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from assurance_agent.retro.types import RetroProposal, RetroPromoteRecord


class ReviewPartition(BaseModel):
    for_review: list[RetroProposal] = Field(default_factory=list)
    pr_only: list[RetroProposal] = Field(default_factory=list)
    stuck_tags: list[str] = Field(default_factory=list)


def partition_proposals_for_review(
    proposals: list[RetroProposal], promotions: list[RetroPromoteRecord], *,
    min_evidence: int, rework_alert: int,
) -> ReviewPartition:
    rework_counts: Counter[str] = Counter()
    for record in promotions:
        if record.decision == "needs_rework":
            rework_counts[record.proposal_id] += 1
    stuck = sorted(pid for pid, n in rework_counts.items() if n >= rework_alert)

    for_review: list[RetroProposal] = []
    pr_only: list[RetroProposal] = []
    for proposal in proposals:
        if proposal.apply_kind == "memory_append":
            for_review.append(proposal)
        else:
            pr_only.append(proposal)
    return ReviewPartition(for_review=for_review, pr_only=pr_only, stuck_tags=stuck)


def build_review_queue_markdown(retro_id: str, partition: ReviewPartition) -> str:
    lines = [f"# Review Queue — {retro_id}", "", "## For review (memory_append)", ""]
    for proposal in partition.for_review:
        flag = " ⚠️ stuck" if proposal.id in partition.stuck_tags else ""
        lines.append(f"- `{proposal.id}` (suite: {proposal.eval_suite or 'n/a'}){flag}: "
                     f"{proposal.summary}")
    if partition.pr_only:
        lines += ["", "## PR-only (manual)", ""]
        lines += [f"- `{p.id}`: {p.summary}" for p in partition.pr_only]
    return "\n".join(lines) + "\n"
```

```python
# assurance_agent/retro/nightly/phase_e.py
from __future__ import annotations

from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.types import RetroProposal


def stage_memory_proposals(
    sut: Path,
    retro_id: str,
    suite: str,
    proposals: list[RetroProposal],
) -> Path:
    assert_path_segment_safe(retro_id, label="retro id")
    assert_path_segment_safe(suite, label="eval suite")
    memory_dir = sut / "qa/retro" / retro_id / "staged" / suite / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    content = [f"# Retro memory — {retro_id} / {suite}", ""]
    for proposal in sorted(proposals, key=lambda p: p.id):
        content.extend([f"## {proposal.id}", "", proposal.body.rstrip(), ""])
    (memory_dir / f"{retro_id}.md").write_text("\n".join(content), encoding="utf-8")
    return memory_dir


def apply_staged_memory(sut: Path, staged_memory: Path) -> list[str]:
    destination = sut / ".aa/memory"
    destination.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    for source in sorted(staged_memory.iterdir()):
        if not source.is_file():
            continue
        target = destination / source.name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(source.read_bytes())
        temporary.replace(target)
        applied.append(target.relative_to(sut).as_posix())
    return applied
```

```python
# assurance_agent/retro/nightly/phase_f.py
from __future__ import annotations

from pydantic import BaseModel, Field

_OPS = {"gte": lambda v, t: v >= t, "lte": lambda v, t: v <= t, "eq": lambda v, t: v == t}


class RegressionComparison(BaseModel):
    regressed: bool
    has_hard_gate: bool
    details: list[str] = Field(default_factory=list)


def _hard_thresholds(suite: dict) -> list[dict]:
    return [t for t in suite.get("thresholds", []) if t.get("gate") == "hard"]


def compare_suite_regression(baseline: dict, candidate: dict, suite: dict) -> RegressionComparison:
    hard = _hard_thresholds(suite)
    details: list[str] = []
    regressed = False
    for threshold in hard:
        metric = threshold["metric"]
        base_val = baseline.get(metric)
        cand_val = candidate.get(metric)
        if base_val is None or cand_val is None:
            regressed = True  # missing hard-gate evidence is fail-closed
            details.append(f"{metric}: missing baseline or candidate evidence")
            continue
        op = _OPS[threshold["op"]]
        # baseline 达标而 candidate 不达标 → 回归
        if op(base_val, threshold["value"]) and not op(cand_val, threshold["value"]):
            regressed = True
            details.append(f"{metric}: {base_val} → {cand_val}")
    return RegressionComparison(regressed=regressed, has_hard_gate=bool(hard), details=details)


def classify_eval_gate(gate: dict) -> str:
    verdict = gate.get("verdict")
    allowed = {"pass", "pass_with_warnings", "fail", "inconclusive", "needs_human_review"}
    return str(verdict) if verdict in allowed else "inconclusive"


def should_auto_apply(comparison: RegressionComparison, suite: dict) -> bool:
    # 仅当 suite 有 hard_gates 且相对 baseline 未回归时才 auto-apply；observe-only 不 apply。
    return comparison.has_hard_gate and not comparison.regressed
```

- [ ] **Step 4: 实现 driver.py（collect / resume / report）**

```python
# assurance_agent/retro/nightly/driver.py
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.nightly.exit_codes import (
    NIGHTLY_FAILURE, NIGHTLY_NOOP, NIGHTLY_OK, NIGHTLY_PENDING_REVIEW,
)
from assurance_agent.retro.nightly.phase_a import (
    IsTerminal, enumerate_candidates, snapshot_unarchived_evidence,
)
from assurance_agent.retro.nightly.phase_d import (
    build_review_queue_markdown, partition_proposals_for_review,
)
from assurance_agent.retro.nightly.phase_e import apply_staged_memory, stage_memory_proposals
from assurance_agent.retro.nightly.phase_f import (
    classify_eval_gate,
    compare_suite_regression,
    should_auto_apply,
)
from assurance_agent.retro.nightly.types import EvalRunResult, NightlyOptions
from assurance_agent.retro.nightly.utils import generate_retro_id, write_json
from assurance_agent.retro.proposals import read_proposals, validate_retro_proposals
from assurance_agent.retro.state import complete_retro_stage, mark_consumed_change, read_state
from assurance_agent.retro.types import RetroContext, RetroPromoteRecord, RetroProposal
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.state import read_state as read_workflow_state
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.schema import load_workflow_schema

AgentRunner = Callable[[str, Path], int]
ContextBuilder = Callable[..., RetroContext]
EvalRunner = Callable[[Path, str, Path | None], EvalRunResult]


def _default_is_terminal(change_dir: Path, change_id: str) -> bool:
    # Use the same typed projection as `aa status`; mere state-file existence is
    # not terminal and would let an active change leak into retro.
    project_root = change_dir.parents[2]
    try:
        schema = load_workflow_schema(project_root)
        state = read_workflow_state(change_dir)
        status = compute_status(
            schema, change_dir, state, state.params, scope="full",
        )
    except (AaError, OSError, ValueError):
        return False
    return status.terminal is not None


def collect_nightly(
    options: NightlyOptions,
    *,
    agent_runner: AgentRunner,
    context_builder: ContextBuilder = build_retro_context,
    is_terminal: IsTerminal = _default_is_terminal,
    now: datetime | None = None,
) -> int:
    sut = Path(options.sut)
    retro_id = options.retro_id or generate_retro_id(now)
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = sut / "qa" / "retro" / retro_id

    # PHASE A：枚举候选
    state = read_state(sut)
    candidates, _incomplete = enumerate_candidates(sut, state, is_terminal=is_terminal)
    if not candidates:
        return NIGHTLY_NOOP
    for candidate in candidates:
        if candidate.evidence_source == "unarchived":
            snapshot_unarchived_evidence(sut, retro_id, candidate.change_id)

    # PHASE B：进程内直调聚合，写 context.json
    try:
        context = context_builder(sut, changes=[c.change_id for c in candidates],
                                  retro_id=retro_id)
    except Exception:  # noqa: BLE001 - 聚合失败视为基础设施失败
        return NIGHTLY_FAILURE
    write_json(retro_dir / "context.json", context.model_dump())

    consumed_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for candidate in candidates:
        mark_consumed_change(sut, change_id=candidate.change_id,
                             source=candidate.evidence_source,
                             consumed_at=consumed_at, retro_id=retro_id)

    if count_signals(context) == 0:
        complete_retro_stage(sut, retro_id)
        return NIGHTLY_NOOP

    if options.dry_run:
        return NIGHTLY_OK

    # PHASE C：调 agent 生成 proposals.json
    retro_dir.mkdir(parents=True, exist_ok=True)
    agent_exit = agent_runner(options.agent, retro_dir)
    if agent_exit != 0:
        return NIGHTLY_FAILURE
    if not (retro_dir / "proposals.json").exists():
        return NIGHTLY_FAILURE

    proposals = read_proposals(retro_dir)
    proposals = [p for p in proposals
                 if not validate_retro_proposals(context, [p])]
    if not proposals:
        complete_retro_stage(sut, retro_id)
        return NIGHTLY_NOOP

    # PHASE D：分流 + review-queue.md + complete
    partition = partition_proposals_for_review(
        proposals, promotions=[], min_evidence=options.min_evidence,
        rework_alert=options.rework_alert)
    (retro_dir / "review-queue.md").write_text(
        build_review_queue_markdown(retro_id, partition), encoding="utf-8")
    complete_retro_stage(sut, retro_id)
    return NIGHTLY_OK


def _read_promotions(retro_dir: Path) -> list[RetroPromoteRecord]:
    path = retro_dir / "promotions.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("promotions", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []
        return [RetroPromoteRecord.model_validate(entry) for entry in entries]
    except (OSError, ValueError):
        return []


def _write_promotions(retro_dir: Path, records: list[RetroPromoteRecord]) -> None:
    path = retro_dir / "promotions.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"promotions": [r.model_dump(mode="json") for r in records]}, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def resume_nightly(options: NightlyOptions, *, eval_runner: EvalRunner) -> int:
    sut = Path(options.sut)
    retro_id = options.retro_id
    if not retro_id:
        return NIGHTLY_FAILURE
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = sut / "qa" / "retro" / retro_id
    if not (retro_dir / "proposals.json").exists():
        return NIGHTLY_FAILURE

    proposals = read_proposals(retro_dir)
    proposal_by_id = {proposal.id: proposal for proposal in proposals}
    records = _read_promotions(retro_dir)
    record_by_id = {record.proposal_id: record for record in records}
    pending_ids = sorted(set(proposal_by_id) - set(record_by_id))
    promoted = [record for record in records if record.decision == "promoted"]
    if not promoted:
        return NIGHTLY_PENDING_REVIEW if pending_ids else NIGHTLY_NOOP

    grouped: dict[str, list[RetroProposal]] = {}
    manual_promoted = False
    for record in promoted:
        proposal = proposal_by_id.get(record.proposal_id)
        if (
            proposal is None
            or proposal.apply_kind != "memory_append"
            or not proposal.eval_suite
        ):
            manual_promoted = True
            continue
        grouped.setdefault(proposal.eval_suite, []).append(proposal)

    staged = {
        suite: stage_memory_proposals(sut, retro_id, suite, suite_proposals)
        for suite, suite_proposals in sorted(grouped.items())
    }
    report: dict = {
        "retro_id": retro_id,
        "pending_proposal_ids": pending_ids,
        "suites": {},
        "applied_files": [],
    }
    if options.skip_eval:
        report["status"] = "staged_pending_eval"
        write_json(retro_dir / "nightly-resume.json", report)
        return NIGHTLY_PENDING_REVIEW

    pending_review = bool(pending_ids or manual_promoted)
    approved_stages: list[Path] = []
    for suite, staged_memory in staged.items():
        try:
            baseline = eval_runner(sut, suite, None)
            candidate = eval_runner(sut, suite, staged_memory)
        except Exception as err:  # noqa: BLE001 - eval infrastructure failure
            report["status"] = "eval_failure"
            report["error"] = str(err)
            write_json(retro_dir / "nightly-resume.json", report)
            return NIGHTLY_FAILURE

        comparison = compare_suite_regression(
            baseline.metrics,
            candidate.metrics,
            candidate.suite,
        )
        gate = classify_eval_gate(candidate.gate)
        auto_apply = gate in {"pass", "pass_with_warnings"} and should_auto_apply(
            comparison,
            candidate.suite,
        )
        suite_proposals = grouped[suite]
        report["suites"][suite] = {
            "baseline_run_id": baseline.run_id,
            "candidate_run_id": candidate.run_id,
            "gate": gate,
            "regressed": comparison.regressed,
            "has_hard_gate": comparison.has_hard_gate,
            "details": comparison.details,
            "auto_applied": auto_apply,
        }

        if auto_apply:
            # Defer all real SUT writes until every suite has been evaluated, so
            # later baselines cannot observe an earlier suite's newly applied memory.
            approved_stages.append(staged_memory)
        else:
            pending_review = True

        for proposal in suite_proposals:
            current = record_by_id[proposal.id]
            update: dict = {"eval_run_id": candidate.run_id}
            if comparison.regressed or gate not in {"pass", "pass_with_warnings"}:
                update.update({
                    "decision": "needs_rework",
                    "rework_note": "; ".join(comparison.details) or f"eval gate: {gate}",
                })
            replacement = current.model_copy(update=update)
            record_by_id[proposal.id] = replacement
            records[records.index(current)] = replacement

    for staged_memory in approved_stages:
        report["applied_files"].extend(apply_staged_memory(sut, staged_memory))
    _write_promotions(retro_dir, records)
    report["applied_files"] = sorted(set(report["applied_files"]))
    report["status"] = "pending_review" if pending_review else "applied"
    write_json(retro_dir / "nightly-resume.json", report)
    return NIGHTLY_PENDING_REVIEW if pending_review else NIGHTLY_OK


def report_nightly(options: NightlyOptions) -> int:
    sut = Path(options.sut)
    retro_root = sut / "qa" / "retro"
    runs = [p.name for p in retro_root.glob("retro-*")] if retro_root.is_dir() else []
    write_json(retro_root / "cross-run-report.json",
               {"runs": sorted(runs)[-options.last:], "count": len(runs)})
    return NIGHTLY_OK
```

> **退出码分支对拍**（benchmark 只区分 0 vs 10 vs 其它）：无候选 → 10；聚合失败 → 40；`signal_count==0` → 10（先 complete）；dry-run → 0；agent 非 0 或缺 proposals.json → 40；校验后无提案 → 10；正常 → 0。resume 的 30（仍待人工审阅）不被 collect 使用，但常量与 CLI 保留以对齐 `docs/eval.md` 退出码约定。

- [ ] **Step 5: 跑测试 + 静态检查 + 提交**

```bash
uv run pytest tests/unit/retro/nightly/test_phases.py tests/unit/retro/nightly/test_collect.py tests/unit/retro/nightly/test_resume.py -v
uv run ruff check .
uv run pyright
```

预期：phases 6 条 + collect 5 条 + resume 5 条（pending/skip/regression/hard-gate apply/observe-only）通过；ruff / pyright clean。

```bash
git add assurance_agent/retro/nightly tests/unit/retro/nightly
git commit -m "feat: retro nightly driver phases A/D/E/F and resume eval gate (M8 task 7)"
```

---

### Task 8: `aa retro` + `aa retro nightly` 命令 + retro 分层契约 + docs/eval.md + 全量回归

**Files:**
- Create: `assurance_agent/commands/retro_cmd.py`
- Modify: `assurance_agent/cli.py`（挂载 `retro` 命令组）
- Modify: `.importlinter`（增补 `assurance_agent.retro` 独立层契约）
- Create: `docs/eval.md`（迁移后的 eval 规格，aa 重命名）
- Modify: `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`（若落地与契约有出入则回改；见 Step 5）
- Test: `tests/integration/test_retro_cli.py`

**Interfaces:**
- Consumes: Task 6 `build_retro_context`/`count_signals`/`mark_consumed_change`；Task 7 `collect_nightly`/`resume_nightly`/`report_nightly`/`NightlyOptions`/退出码。
- Produces: `aa retro --since|--change|--retro-id|--out|--json`（stdout JSON 含 `retro_id`/`change_count`/`signal_count`）；`aa retro nightly collect|resume|report`（collect 退出码 0/10/40；resume 实际注入 eval runner，退出 0/10/30/40）。CLI flag 逐一对齐 TS `src/commands/retro.ts`。

- [ ] **Step 1: 写失败测试（含 benchmark 消费的 --json 形状 + nightly 退出码）**

```python
# tests/integration/test_retro_cli.py
from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.commands import retro_cmd as retro_cmd_mod
from assurance_agent.retro.nightly.types import EvalRunResult
from tests.unit.retro.archive_fixtures import make_archived_change


def test_retro_json_stdout_shape(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}],
                             gate_pushbacks=1)
        result = runner.invoke(main, ["retro", "--retro-id", "retro-x",
                                      "--change", "CH-1", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        # benchmark legacy 分支解析这三个键（run-workflow-loop.sh L581-583）
        assert payload["retro_id"] == "retro-x"
        assert payload["change_count"] == 1
        assert payload["signal_count"] >= 1
        assert (root / "qa" / "retro" / "retro-x" / "context.json").exists()


def test_retro_since_and_change_mutually_exclusive() -> None:
    result = CliRunner().invoke(main, ["retro", "--since", "2026-01-01", "--change", "CH-1"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_retro_immutable_when_promotions_present(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[])
        retro_dir = root / "qa" / "retro" / "retro-locked"
        retro_dir.mkdir(parents=True)
        (retro_dir / "promotions.json").write_text("[]", encoding="utf-8")
        result = runner.invoke(main, ["retro", "--retro-id", "retro-locked", "--change", "CH-1"])
        assert result.exit_code != 0
        assert "immutable" in result.output


def test_retro_nightly_collect_success_exit_0(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}],
                             gate_pushbacks=1)
        # 用一个写 proposals.json 的假 agent 脚本（echo 到文件）
        agent = root / "fake-agent.sh"
        retro_glob = "qa/retro"
        agent.write_text(
            "#!/usr/bin/env bash\n"
            f'd="$(ls -1d {retro_glob}/retro-* | tail -1)"\n'
            'printf \'{"proposals":[{"id":"P-1","apply_kind":"memory_append",'
            '"body":"x","eval_suite":"s"}]}\' > "$d/proposals.json"\n'
            'printf "# summary\\n" > "$d/retro-summary.md"\n',
            encoding="utf-8")
        os.chmod(agent, 0o755)
        result = runner.invoke(main, ["retro", "nightly", "collect", "--sut", str(root),
                                      "--retro-id", "retro-n", "--agent", f"bash {agent}",
                                      "--min-evidence", "1"])
        assert result.exit_code == 0, result.output
        assert (root / "qa" / "retro" / "retro-n" / "review-queue.md").exists()


def test_retro_nightly_collect_noop_exit_10(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        (root / "qa" / "archive").mkdir(parents=True)
        result = runner.invoke(main, ["retro", "nightly", "collect", "--sut", str(root),
                                      "--retro-id", "retro-empty"])
        assert result.exit_code == 10


def test_retro_nightly_collect_dry_run_exit_0(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}])
        result = runner.invoke(main, ["retro", "nightly", "collect", "--sut", str(root),
                                      "--retro-id", "retro-dry", "--dry-run"])
        assert result.exit_code == 0
        assert (root / "qa" / "retro" / "retro-dry" / "context.json").exists()


def test_retro_nightly_resume_injects_eval_runner(tmp_path: Path, monkeypatch) -> None:
    retro_dir = tmp_path / "qa/retro/retro-test"
    retro_dir.mkdir(parents=True)
    (retro_dir / "proposals.json").write_text(json.dumps({"proposals": [{
        "id": "P-1", "apply_kind": "memory_append", "body": "memory",
        "eval_suite": "workflow-case",
    }]}), encoding="utf-8")
    (retro_dir / "promotions.json").write_text(json.dumps({"promotions": [{
        "proposal_id": "P-1", "decision": "promoted", "decided_by": "r",
        "decided_at": "2026-07-15T00:00:00Z",
    }]}), encoding="utf-8")
    calls: list[bool] = []

    def fake_eval(sut: Path, suite: str, extra: Path | None) -> EvalRunResult:
        calls.append(extra is not None)
        return EvalRunResult(
            run_id="candidate" if extra else "baseline",
            metrics={"m": 1.0},
            gate={"verdict": "pass"},
            suite={"thresholds": [
                {"metric": "m", "gate": "hard", "op": "gte", "value": 1.0}
            ]},
        )

    monkeypatch.setattr(retro_cmd_mod, "_run_nightly_eval", fake_eval)
    result = CliRunner().invoke(main, [
        "retro", "nightly", "resume", "--sut", str(tmp_path),
        "--retro-id", "retro-test",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [False, True]
```

```bash
uv run pytest tests/integration/test_retro_cli.py -v
```

预期：`No such command 'retro'`。

- [ ] **Step 2: 实现 retro_cmd.py**

```python
# assurance_agent/commands/retro_cmd.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from assurance_agent.eval.gate import read_gate_result
from assurance_agent.eval.metrics import read_metrics
from assurance_agent.eval.paths import run_dir as eval_run_dir
from assurance_agent.eval.plan import load_suite
from assurance_agent.exceptions import AaError
from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.nightly.agent import run_agent
from assurance_agent.retro.nightly.driver import (
    collect_nightly, report_nightly, resume_nightly,
)
from assurance_agent.retro.nightly.types import EvalRunResult, NightlyOptions
from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.state import mark_consumed_change
from assurance_agent.workflow.driver.process_runner import resolve_aa_command


def _run_nightly_eval(
    sut: Path,
    suite_name: str,
    extra_memory_dir: Path | None,
) -> EvalRunResult:
    """Production EvalRunner seam; commands may compose eval + retro domains."""
    argv = [
        *resolve_aa_command(),
        "eval", "run",
        "--suite", suite_name,
        "--sut-dir", str(sut),
        "--json",
    ]
    if extra_memory_dir is not None:
        argv += ["--extra-memory-dir", str(extra_memory_dir)]
    completed = subprocess.run(
        argv,
        cwd=sut,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AaError(f"nightly eval failed for {suite_name}: {completed.stderr.strip()}")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        run_id = str(payload["run_id"])
    except (IndexError, KeyError, ValueError) as err:
        raise AaError(f"nightly eval returned invalid JSON for {suite_name}") from err
    run_dir = eval_run_dir(sut, run_id)
    metrics = read_metrics(run_dir)
    gate = read_gate_result(run_dir)
    suite, _suite_file = load_suite(sut, suite_name)
    return EvalRunResult(
        run_id=run_id,
        metrics=metrics.metrics,
        gate=gate.model_dump(mode="json"),
        suite=suite.model_dump(mode="json"),
    )


def _run_retro(since, changes, retro_id, out, as_json) -> None:
    project_root = Path.cwd()
    if since and changes:
        click.echo("Error: --since and --change are mutually exclusive", err=True)
        raise SystemExit(2)
    context = build_retro_context(project_root, since=since,
                                  changes=list(changes) if changes else None,
                                  retro_id=retro_id)
    retro_dir = project_root / "qa" / "retro" / context.retro_id
    out_path = Path(out).resolve() if out else (retro_dir / "context.json")
    inside = retro_dir in out_path.parents or out_path.parent == retro_dir
    if inside and (retro_dir / "promotions.json").exists():
        click.echo(f"Error: retro dir already contains promotions.json and is "
                   f"immutable: {context.retro_id}", err=True)
        raise SystemExit(1)
    write_json(out_path, context.model_dump())

    consumed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for source in context.window.change_sources:
        mark_consumed_change(project_root, change_id=source.change_id,
                             source=source.evidence_source, consumed_at=consumed_at,
                             retro_id=context.retro_id)

    summary = {
        "retro_id": context.retro_id,
        "change_count": context.window.change_count,
        "signal_count": count_signals(context),
    }
    if as_json:
        click.echo(json.dumps(summary))
        return
    click.echo(f"retro_id: {summary['retro_id']}")
    click.echo(f"change_count: {summary['change_count']}")
    click.echo(f"signal_count: {summary['signal_count']}")


# `aa retro`（不带子命令）需消费顶层选项并聚合，同时 `aa retro nightly ...` 是子命令组。
# 用 invoke_without_command=True + ctx.invoked_subcommand is None 判定"group 兼默认命令"。
def register_retro(main_group: click.Group) -> None:
    @click.group("retro", invoke_without_command=True)
    @click.option("--since", help="Scan archived changes archived at/after this ISO date")
    @click.option("--change", "changes", multiple=True, help="Archived change id (repeatable)")
    @click.option("--retro-id", "retro_id", help="Retro id for output dir and context")
    @click.option("--out", help="Output path for context.json")
    @click.option("--json", "as_json", is_flag=True,
                  help="Output { retro_id, change_count, signal_count }")
    @click.pass_context
    def retro(ctx, since, changes, retro_id, out, as_json) -> None:
        """Aggregate archived QA evidence into retro context."""
        if ctx.invoked_subcommand is not None:
            return
        _run_retro(since, changes, retro_id, out, as_json)

    _register_nightly(retro)
    main_group.add_command(retro)


def _register_nightly(retro: click.Group) -> None:
    @retro.group("nightly")
    def nightly() -> None:
        """Run the nightly retro pipeline."""

    @nightly.command("collect")
    @click.option("--sut", required=True, help="SUT project root")
    @click.option("--retro-id", "retro_id", help="Stable retro run id")
    @click.option("--dry-run", is_flag=True, help="Stop before invoking the proposal agent")
    @click.option("--agent", default="cursor-agent", help="Proposal agent command")
    @click.option("--history", type=int, default=5)
    @click.option("--min-evidence", type=int, default=2)
    @click.option("--rework-alert", type=int, default=3)
    def collect(sut, retro_id, dry_run, agent, history, min_evidence, rework_alert) -> None:
        options = NightlyOptions(sut=sut, retro_id=retro_id, dry_run=dry_run, agent=agent,
                                 history=history, min_evidence=min_evidence,
                                 rework_alert=rework_alert)
        code = collect_nightly(options, agent_runner=run_agent)
        raise SystemExit(code)

    @nightly.command("resume")
    @click.option("--sut", required=True)
    @click.option("--retro-id", "retro_id")
    @click.option("--skip-eval", is_flag=True)
    def resume(sut, retro_id, skip_eval) -> None:
        if not retro_id:
            click.echo("error: required option --retro-id <id> not specified", err=True)
            raise SystemExit(2)
        options = NightlyOptions(sut=sut, retro_id=retro_id, skip_eval=skip_eval)
        raise SystemExit(resume_nightly(options, eval_runner=_run_nightly_eval))

    @nightly.command("report")
    @click.option("--sut", required=True)
    @click.option("--last", type=int, default=10)
    @click.option("--rework-alert", type=int, default=3)
    def report(sut, last, rework_alert) -> None:
        options = NightlyOptions(sut=sut, last=last, rework_alert=rework_alert)
        raise SystemExit(report_nightly(options))
```

在 `assurance_agent/cli.py` 挂载：

```python
# assurance_agent/cli.py（新增）
from assurance_agent.commands.retro_cmd import register_retro
register_retro(main)
```

> **实现者注意（click group 带自身选项）**：唯一挂载入口是 `register_retro(main)`——它在内部用闭包定义 `retro` group（`invoke_without_command=True` + 顶层选项）与 `nightly` 子组，避免"先定义装饰器 group 再改造"的中间态。`_run_retro` 为模块级纯函数，被 `retro` 闭包调用。`cli.py` 只需 `from assurance_agent.commands.retro_cmd import register_retro; register_retro(main)`。

- [ ] **Step 3: 只追加 retro 契约并验证最终累计 `.importlinter`**

`eval` 与 `retro` 互不依赖，无法放进同一条线性链；本 Task **只追加** `retro-layers` 与 `eval-retro-independence`，不得覆盖主层、M3 forbidden 或 M6 driver 契约。最终验收视图为：

```ini
[importlinter]
root_package = assurance_agent

[importlinter:contract:layers]
name = commands depend on eval domain, never the reverse
type = layers
layers =
    assurance_agent.cli
    assurance_agent.commands
    assurance_agent.eval
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

[importlinter:contract:driver-layer]
name = workflow.driver sits above orchestration and core
type = layers
layers =
    assurance_agent.workflow.driver
    assurance_agent.workflow.orchestration
    assurance_agent.workflow.core

[importlinter:contract:retro-layers]
name = commands depend on retro domain, never the reverse
type = layers
layers =
    assurance_agent.commands
    assurance_agent.retro
    assurance_agent.workflow

[importlinter:contract:eval-retro-independence]
name = eval and retro must not import each other
type = independence
modules =
    assurance_agent.eval
    assurance_agent.retro
```

Run: `uv run lint-imports`
Expected: **6 contracts kept, 0 broken**；数量减少、既有 contract 改名或层序丢失均失败。

- [ ] **Step 4: 迁移 docs/eval.md（aa 重命名）**

以 TS 源 `/Users/lvqingquan/skills/assurance-workflow-skills/docs/eval.md` 为蓝本，逐节迁移为 `docs/eval.md`，套用重命名并对齐 Python 落地：

- 所有 `aws` → `aa`（`node dist/cli.js eval ...` → `aa eval ...`；`aws retro` → `aa retro`；`aws run` → `aa run`）。
- 环境变量：`EVAL_USE_FAKE_OPENCODE` → `AA_EVAL_FAKE_ADAPTER`、`EVAL_SUT_DIR` → `AA_EVAL_SUT_DIR`、`EVAL_JUDGE_API_URL/API_KEY/MOCK` → `AA_JUDGE_API_URL/API_KEY/MOCK`。
- 产物路径：保留 `eval/out/runs/<run_id>/{manifest.json,metrics.json,gate-result.json,report.json,report.html,report.md}`（本里程碑落地一致）。
- Retro Loop 一节改为描述 `aa retro` / `aa retro nightly collect|resume|report`；nightly 退出码表：`0` 成功 / `10` no-op / `30` 仍待人工审阅（resume）/ `40` 失败；产物 `qa/retro/<retro-id>/{context.json,proposals.json,promotions.json,review-queue.md,retro-summary.md}` + `_state.json` + `cross-run-report.json`。
- 增一段「与 TS 版差异」：（1）nightly phase B 进程内直调聚合而非 shell out `aa retro`；（2）`context.json` 顶层新增 `signal_count`（benchmark nightly 分支消费）；（3）judge 经 `httpx` 直连、`AA_JUDGE_MOCK` 走确定性 mock；（4）observe-only 的 OpenCode 过程可观测性 7 项在 Python 版为**未迁移的 deferred**（scorer 不产出，报告不展示），因 M8 未迁移 OpenCode 事件解析器——在 docs 中明确标注 deferred，避免读者误以为已实现。

> **说明（文档忠实度）**：`eval run`/`plan`/`report`/`gate`/`compare`/`baseline update` 与 retro/nightly 命令均已落地（Task 5/5b），给出可运行示例。`docs/eval.md` 中仍 deferred 的能力仅剩：`eval gate|compare --batch <id>`（批次编排未迁移，`--run` 已支持）、OpenCode 过程指标（M8 未迁移 OpenCode 事件解析器）、judge 校准写基准（`--calibrate` 仅落 sample notes，不改 gate/baseline 数值）——一律明确标 **deferred**，避免读者误以为已实现。指标表（E0/E2a-d/E3/E4）保留，但对未实现的 observe 指标标注 deferred。

- [ ] **Step 5: 契约文档回改核对（仅在有出入时改）**

对照 `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`「M8 Eval + Retro」段（L216-223）。落地后核对：
- `aa eval run|plan|report`：一致（本计划实现 run/plan/report）。
- `aa retro --retro-id <id> --change <id>... --json`，stdout JSON 含 `retro_id/signal_count/change_count`：一致。
- `aa retro nightly collect --sut <dir> --agent <cmd>`，退出码 0/10/其它：一致。
- 产物 `qa/retro/<retro-id>/{context.json,proposals.json,retro-summary.md,review-queue.md}`：一致（本计划另增 `promotions.json`/`_state.json`/`cross-run-report.json`，属超集，不冲突）。

若以上任一不符，**以落地代码为准**并在该段落追加一行「M8 落地补充：…」说明（如"context.json 顶层增 `signal_count`；nightly phase B 进程内聚合"）。预期本里程碑仅需补充这一行增量说明，不需改动被 pin 的命令面。

- [ ] **Step 6: 全量回归 + 静态检查 + 提交**

```bash
uv run pytest tests/unit/eval tests/unit/retro tests/integration/test_eval_cli.py tests/integration/test_retro_cli.py -v
uv run pytest -q                       # 全仓库回归，确认未破坏 M1-M7
uv run ruff check .
uv run pyright
uv run lint-imports
```

预期：M8 新增测试全绿（eval + retro 单测约 40 条 + 两个 CLI 集成文件 12 条）；全仓库既有测试不回归；ruff / pyright / lint-imports clean。

```bash
git add assurance_agent/commands/retro_cmd.py assurance_agent/cli.py .importlinter docs/eval.md docs/superpowers/plans/2026-07-14-python-migration-plan-series.md tests/integration/test_retro_cli.py
git commit -m "feat: aa retro + nightly CLI, retro import layer, migrate docs/eval.md (M8 task 8)"
```

---

## 验收清单（写完即自检）

- [ ] `aa eval run --suite <name> [--sample <id>] [--json|--output id] [--fail-on-verdict] [--sut-dir <dir>]` 可跑；`--json` 输出 `{run_id, verdict}`；`--suite`/`--plan` 互斥且至少一个。
- [ ] `aa eval plan --event <e> [--suite <name>] --out <path>` 写 plan JSON；`aa eval report --run <id> [--json|--html]` / `--trend --suite <s>` 可读。
- [ ] executor 复用 M6 `run_workflow_loop`（不另起循环），测试用 `FakeAdapter` + 脚本化 `status_provider` 真跑一轮并拷贝 raw-output。
- [ ] scorer（workflow_case / codegen / workflow_run / workflow_full）指标口径对齐 `docs/eval.md`，单测含手算期望值。
- [ ] judge 经 `httpx` 直连（`AA_JUDGE_API_URL`/`AA_JUDGE_API_KEY`），golden 请求载荷 + 响应解析 + `AA_JUDGE_MOCK` 确定性 stub + judge≠target fail-closed。
- [ ] `aa retro --retro-id <id> --change <id>... --json` stdout 含 `retro_id`/`change_count`/`signal_count`；`--since`/`--change` 互斥；promotions.json 存在时目录不可变。
- [ ] `aa retro nightly collect --sut <dir> --agent <cmd>` 退出码 0 成功 / 10 no-op / 40 失败；`context.json` 顶层含 `signal_count`、`window.change_count`（benchmark 依赖）。
- [ ] retro 聚合 golden 测试覆盖失败分布 / gate pushback / signal_count；nightly collect 退出码全分支测试。
- [ ] `docs/eval.md` 完成 aa 重命名迁移，deferred 能力明确标注；契约文档已核对（如需仅补一行落地增量）。
- [ ] 每个 Task `ruff` + `pyright`（Task 5/8 加 `lint-imports`）clean 后 conventional-commit 提交。
- [ ] 分层：`.importlinter` 两条 layers 契约 + 一条 eval/retro independence 契约通过。
