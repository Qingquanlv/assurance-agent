# M4 — `aa status/gate/state/decide` 命令 + risk（Explore）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 spec 第 5 节 status/gate/state 机器面的查询与推进命令（`aa status`、`aa gate check`、`aa state apply`、`aa state heal`、`aa decide`），以及 `assurance_agent/risk/` 新包与 `aa risk context` / `aa risk validate-advisory`（Explore 支持）；统一退出码常量落在 `workflow/core/exit_codes.py`。

**Architecture:** 命令模块只做参数解析、typed event 写入与退出码，全部业务逻辑复用 M3 编排核心（`compute_status` / `check_gate` / canonical `WorkflowState` / `append_event_*` / file snapshots）与 M2 产物模型（`Advisory`）。查询类命令（status、gate check、risk）用 best-effort 事件；状态推进命令先捕获 `events.jsonl` 与 `workflow-state.yaml` 快照，再追加冻结的 strict audit event、原子写 typed state，任一步失败都恢复快照并退出 40。不存在 `_txn`、pending/committed WAL 或裸 dict state。`risk/` 是全新的顶层子包，Explore 上下文聚合与 advisory 语义校验在包内自足实现，不引入 M5 执行层。

**Tech Stack:** Python 3.11+, uv, click, pydantic v2, PyYAML, pytest, ruff, pyright, import-linter。

## Global Constraints

- CLI 命令名 `aa`；Python 包名 `assurance_agent`；项目配置目录 `.aa/`。新文件中不得残留 `aws` 字样（除非引用 TS 源路径的注释）。
- 工具链固定：uv + pyproject.toml + pydantic v2 + click + ruff + pyright + pytest。
- TS 源仓库 `/Users/lvqingquan/skills/assurance-workflow-skills` **仅作规则参考**：只提取 flag 面、输出形状、事件语义、退出码映射与聚合/校验规则，不复制实现。
- 本里程碑消费 M2 / M3 的落地产物，二者必须已并入 `main`。所有跨里程碑签名以 `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`「接口契约」为准；本计划 Task 7 会把 M3 `WorkflowSchema` 需新增的两个访问器（`has_phase` / `gate_for_phase`）回写该文档。
- 路径安全：所有 `--change` 入口在拼接 `qa/changes/<id>` 前调用 M2 `assurance_agent.identifiers.assert_change_id_safe`；risk 包的同名函数只包装公共实现以保持 `RiskSafetyError` API，不得维护第二套正则。
- 事件双模式（spec 第 4 节）：查询类命令 best-effort；推进类命令只写 M3 冻结的 typed audit event，并用 `capture_files` / `restore_files` 包住 event+state 文件。禁止新增 WAL event shape；失败抛 `EventWriteError`/`AaError`，命令退出 40 且文件恢复到调用前字节。
- 退出码常量统一在 `assurance_agent/workflow/core/exit_codes.py`（**属主 M3**，M4 只消费）：`EXIT_COMPLETED=0` / `EXIT_STOPPED=20` / `EXIT_HUMAN_REVIEW=30` / `EXIT_ERROR=40` / `EXIT_USAGE=2`。`Terminal.kind` 仅 `{completed, stopped, needs_human_review}`（无 `exhausted`；healing 耗尽 → `healing-loop-gate` → `stop` → `Terminal(kind="stopped")` → 20）。`core` 是最底层，`exit_codes.py` 禁止 import `orchestration`（用结构化 Protocol 接住 `Terminal`）。
- 分层约束（`.importlinter`，Task 7 更新）：`cli → commands → risk → workflow → artifacts → config → resources`。`risk/` 可 import `workflow`、`artifacts`（`Advisory`）、`resources`、`exceptions`；反向禁止。
- 包内资源只经 `assurance_agent/resources.py` 访问（M1 已落地：`read_text` / `exists` / `iter_children`），禁止 `Path(__file__)` 相对路径。
- 每个 Task 结束必须通过 `uv run ruff check .` 与 `uv run pyright`，然后 `git commit`；提交信息用 conventional commits（feat/test/chore/docs）。
- 本计划中所有 pytest 命令在仓库根目录运行：`uv run pytest <path> -v`。

## 消费的 M2 / M3 契约（只读引用，不在本里程碑创建）

```python
# M3 workflow/orchestration/schema.py
class WorkflowSchema(BaseModel): ...          # 属性 phases/loops/gates/params
    def phase_produces(self, phase_id: str) -> list[str] | None: ...
    def has_phase(self, phase_id: str) -> bool: ...          # ← Task 7 追加进 M3 契约
    def gate_for_phase(self, phase_id: str) -> str | None: ...# ← Task 7 追加进 M3 契约
def load_workflow_schema(project_root: Path, explicit: Path | None = None) -> WorkflowSchema

# M3 workflow/orchestration/engine.py
class DispatchEntry(BaseModel): phase_id: str; skill: str | None; agent: str | None; kind: Literal["skill","cli","orchestrator"]
class Terminal(BaseModel): kind: Literal["completed","stopped","needs_human_review"]; reason: str | None = None
class PhaseView(BaseModel): id: str; status: str
class WorkflowStatus(BaseModel): phases: list[PhaseView]; next_dispatch: list[DispatchEntry]; terminal: Terminal | None; healing_episode: HealingEpisodeSnapshot
def compute_status(schema, change_dir: Path, state: WorkflowState, params: dict, *, scope: str = "full", healing_provider=None) -> WorkflowStatus

# M3 workflow/orchestration/gates.py
class GateVerdict(BaseModel): gate: str; verdict: Verdict; reason: str | None = None
def check_gate(schema, gate_name: str, change_dir: Path, state: WorkflowState, params: dict) -> GateVerdict

# M3 workflow/core/state.py
def read_state(change_dir: Path) -> WorkflowState        # 含 state hash 校验；文件缺失返回默认空 state
def write_state(change_dir: Path, state: WorkflowState) -> None   # 原子写 + hash

# M3 workflow/core/events.py
class EventWriteError(AaError): ...
def append_event_best_effort(change_dir: Path, event: dict) -> None
def append_event_strict(change_dir: Path, event: AuditEvent) -> None    # 失败抛 EventWriteError
def capture_files(paths: Sequence[Path]) -> tuple[FileSnapshot, ...]
def restore_files(snapshots: Sequence[FileSnapshot]) -> None

# M2 artifacts/models
class Advisory(BaseModel): ...   # schema_version/watchlist/open_questions_for_case_design/...（extra="allow"）
```

> 说明：`WorkflowState` 由 M2 唯一定义，`params`、`phases`、`gates`、`run_context` 均为显式 typed 字段；healing 只允许位于 `state.phases.healing`。动态 phase 通过 `model_dump()` → 更新 phases mapping → `WorkflowState.model_validate()` 写回，不对模型做 dict 下标访问。

## 文件结构总览

新建文件及职责：

```
assurance_agent/workflow/core/exit_codes.py        # 退出码常量 + 两个映射函数（Task 1）
assurance_agent/commands/status_cmd.py             # aa status（Task 2）
assurance_agent/commands/gate_cmd.py               # aa gate check（Task 3）
assurance_agent/commands/state_cmd.py              # aa state apply / aa state heal（Task 4）
assurance_agent/commands/decide_cmd.py             # aa decide（Task 5）
assurance_agent/risk/__init__.py                   # 空（Task 6）
assurance_agent/risk/safety.py                     # change-id / 路径包含校验（Task 6）
assurance_agent/risk/paths.py                      # explore 目录与 context/advisory 路径（Task 6）
assurance_agent/risk/context.py                    # Explore 上下文聚合（Task 6）
assurance_agent/risk/advisory.py                   # advisory 语义校验（Task 7）
assurance_agent/commands/risk_cmd.py               # aa risk context / validate-advisory（Task 6/7）
```

修改：`assurance_agent/cli.py`（挂载 6 个命令组）、`.importlinter`（插入 `risk` 层）、`docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`（回写 M3 契约新增访问器）。

---

### Task 1: 退出码常量（消费 M3 的 exit_codes.py，不重复实现）

> **重要（P1 修订）**：`exit_codes.py` 的**唯一属主是 M3**（`2026-07-15-m3-orchestration.md` Task 1），
> 其中 `exit_code_for_terminal` 已把 `needs_human_review → 30`、且 `Terminal.kind` **不含 `exhausted`**
> （healing 耗尽经 `healing-loop-gate → stop → Terminal(kind="stopped")` 表达，映射 20）。M4 **不再重建**该模块，
> 只消费它并补齐 M4 视角的回归用例，避免出现两份互相矛盾的退出码映射。

**Files:**
- Test: `tests/unit/workflow/core/test_exit_codes_m4_contract.py`（消费方契约用例；不新建被测模块）

**Interfaces:**
- Consumes: M3 `assurance_agent.workflow.core.exit_codes` 的常量 `EXIT_COMPLETED=0`/`EXIT_STOPPED=20`/`EXIT_HUMAN_REVIEW=30`/`EXIT_ERROR=40`/`EXIT_USAGE=2` 与 `exit_code_for_gate_verdict` / `exit_code_for_terminal`（`TerminalLike` = 具 `.kind: str` 属性的对象，M3 `Terminal` 天然满足）。Task 2–5 全部消费。
- Produces: 无新运行时模块。

- [ ] **Step 1: 写消费方契约测试（对齐 M3 已交付的映射语义）**

```python
# tests/unit/workflow/core/test_exit_codes_m4_contract.py
import pytest

from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    EXIT_USAGE,
    exit_code_for_gate_verdict,
    exit_code_for_terminal,
)


class FakeTerminal:
    def __init__(self, kind: str) -> None:
        self.kind = kind


def test_constant_values() -> None:
    assert (EXIT_COMPLETED, EXIT_STOPPED, EXIT_HUMAN_REVIEW, EXIT_ERROR, EXIT_USAGE) == (0, 20, 30, 40, 2)


@pytest.mark.parametrize("verdict", ["pass", "enter", "exit", "skip"])
def test_gate_verdict_zero(verdict: str) -> None:
    assert exit_code_for_gate_verdict(verdict) == EXIT_COMPLETED


@pytest.mark.parametrize("verdict", ["needs_fix", "needs_human_review", "continue"])
def test_gate_verdict_human_review(verdict: str) -> None:
    assert exit_code_for_gate_verdict(verdict) == EXIT_HUMAN_REVIEW


@pytest.mark.parametrize("verdict", ["reject", "stop"])
def test_gate_verdict_error(verdict: str) -> None:
    assert exit_code_for_gate_verdict(verdict) == EXIT_ERROR


def test_terminal_none_and_completed_zero() -> None:
    assert exit_code_for_terminal(None) == EXIT_COMPLETED
    assert exit_code_for_terminal(FakeTerminal("completed")) == EXIT_COMPLETED


def test_terminal_stopped_twenty() -> None:
    assert exit_code_for_terminal(FakeTerminal("stopped")) == EXIT_STOPPED


def test_terminal_needs_human_review_thirty() -> None:
    # P1 修订：needs_human_review 是合法 terminal，必须映射 30（不是回落 0）。
    assert exit_code_for_terminal(FakeTerminal("needs_human_review")) == EXIT_HUMAN_REVIEW


def test_unknown_terminal_fails_closed() -> None:
    assert exit_code_for_terminal(FakeTerminal("future-terminal-kind")) == EXIT_ERROR
```

- [ ] **Step 2: 跑测试确认通过**

Run: `uv run pytest tests/unit/workflow/core/test_exit_codes_m4_contract.py -v`
Expected: PASS（前提：M3 已交付 `exit_codes.py`；若尚未，先完成 M3 Task 1）。若断言失败说明 M3 与 M4 语义漂移——以本用例（needs_human_review=30、无 exhausted）为准回改 M3。

同时创建空的 `tests/unit/workflow/__init__.py` 与 `tests/unit/workflow/core/__init__.py`（若不存在）。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/workflow
git commit -m "test: pin M4 consumer contract for shared exit-code terminal mapping"
```

---

### Task 2: `aa status` 命令（status_cmd.py）

**Files:**
- Create: `assurance_agent/commands/status_cmd.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/integration/test_cli_status.py`

**Interfaces:**
- Consumes: M3 `load_workflow_schema` / `compute_status` / `WorkflowStatus` / `read_state`；M3 `append_event_best_effort`；Task 1 `exit_code_for_terminal`；M1 click Group `main`、`AaError`。
- Produces: `aa status --change <id> [--next] [--json]` 子命令。人类模式打印相位表；`--json` 打印 `WorkflowStatus.model_dump()`（含 `terminal.kind`/`terminal.reason`/`next_dispatch[].agent`/`skill`/`kind`）；`--next` 只输出下一批 dispatch。退出码经 `exit_code_for_terminal`：running 或 completed=0 / stopped=20 / **needs_human_review=30**（无 `exhausted` 种类）。查询 best-effort 事件。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_cli_status.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main

RUNNING_STATE = """schema_version: "1"
params:
  run_mode: full
phases:
  explore:
    status: done
"""


def make_change(change_id: str = "CH-1", state_yaml: str = RUNNING_STATE) -> Path:
    change_dir = Path("qa/changes") / change_id
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(state_yaml, encoding="utf-8")
    return change_dir


def test_status_missing_change_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["status", "--change", "NOPE"])
        assert result.exit_code == 40
        assert "not found" in result.output


def test_status_running_json_shape_and_exit_0() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["status", "--change", "CH-1", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert "phases" in doc
        assert "next_dispatch" in doc
        assert doc["terminal"] is None
        # next_dispatch 条目形状：phase_id/skill/agent/kind
        for entry in doc["next_dispatch"]:
            assert set(entry) >= {"phase_id", "skill", "agent", "kind"}


def test_status_next_json_limits_to_dispatch_list() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["status", "--change", "CH-1", "--next", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert set(doc) == {"next_dispatch", "terminal"}


def test_status_next_human_lists_phase_ids() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["status", "--change", "CH-1", "--next"])
        assert result.exit_code == 0, result.output
        # 至少不报错，输出下一批 phase id 或 "(none)"
        assert result.output.strip() != ""


def test_status_stopped_terminal_exits_20() -> None:
    stopped_state = """schema_version: "1"
params: {}
phases:
  skill_registry_check:
    status: fail
"""
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change(state_yaml=stopped_state)
        result = runner.invoke(main, ["status", "--change", "CH-1", "--json"])
        doc = json.loads(result.output)
        # workflow-state.yaml 本身是 registry phase 的 produces；真实 registry gate
        # 读取 skill_registry_check.status=fail，故 M3 投影出 stopped terminal。
        assert doc["terminal"]["kind"] == "stopped"
        assert result.exit_code == 20
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_cli_status.py -v`
Expected: FAIL（`No such command 'status'`）

- [ ] **Step 3: 实现 status_cmd.py 并挂载**

```python
# assurance_agent/commands/status_cmd.py
"""aa status — 计算工作流图中每个相位的状态（确定性, 无 LLM）。

对齐 TS src/commands/status.ts 的 flag 面与退出码语义：
查询类命令写 best-effort 遥测事件（失败静默，退出码不变）。
"""
from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR, exit_code_for_terminal
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.orchestration.engine import WorkflowStatus, compute_status
from assurance_agent.workflow.orchestration.schema import load_workflow_schema


@click.command("status")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--next", "next_only", is_flag=True, help="Print only the next dispatch batch.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def status_command(change_id: str, next_only: bool, as_json: bool) -> None:
    """Compute the state of every phase in the workflow graph (deterministic, no LLM)."""
    project_root = Path.cwd()
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    change_dir = project_root / "qa" / "changes" / change_id
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(EXIT_ERROR)

    try:
        schema = load_workflow_schema(project_root)
        state = read_state(change_dir)
        params = getattr(state, "params", None) or {}
        status = compute_status(schema, change_dir, state, params)
    except AaError as err:
        click.secho(f"status failed: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    _emit_telemetry(change_dir, status)

    if next_only:
        _print_next(status, as_json)
    elif as_json:
        click.echo(status.model_dump_json(indent=2))
    else:
        _print_table(change_id, status)

    raise SystemExit(exit_code_for_terminal(status.terminal))


def _emit_telemetry(change_dir: Path, status: WorkflowStatus) -> None:
    append_event_best_effort(
        change_dir,
        {
            "source": "status",
            "type": "status_query",
            "terminal": status.terminal.kind if status.terminal else None,
            "next": [d.phase_id for d in status.next_dispatch],
        },
    )


def _print_next(status: WorkflowStatus, as_json: bool) -> None:
    if as_json:
        click.echo(
            _json_dumps(
                {
                    "next_dispatch": [d.model_dump() for d in status.next_dispatch],
                    "terminal": status.terminal.model_dump() if status.terminal else None,
                }
            )
        )
        return
    if status.next_dispatch:
        for entry in status.next_dispatch:
            click.echo(entry.phase_id)
    else:
        click.echo("(none)")


def _print_table(change_id: str, status: WorkflowStatus) -> None:
    click.secho(f"aa status — change: {change_id}", bold=True)
    click.echo()
    for phase in status.phases:
        click.echo(f"  {phase.status.ljust(14)} {phase.id}")
    click.echo()
    next_ids = ", ".join(d.phase_id for d in status.next_dispatch) or "(none)"
    click.echo(f"  Next     : {next_ids}")
    if status.terminal:
        color = "green" if status.terminal.kind == "completed" else "red"
        reason = status.terminal.reason or ""
        click.echo("  Terminal : " + click.style(f"{status.terminal.kind} — {reason}", fg=color))
    click.echo()


def _json_dumps(obj: object) -> str:
    import json

    return json.dumps(obj, indent=2, ensure_ascii=False)
```

```python
# assurance_agent/cli.py（追加 import 与挂载，保持既有内容不变）
from assurance_agent.commands.status_cmd import status_command

main.add_command(status_command)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_cli_status.py -v`
Expected: 5 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/commands/status_cmd.py assurance_agent/cli.py tests/integration/test_cli_status.py
git commit -m "feat: add aa status command with JSON, --next and terminal exit codes"
```

---

### Task 3: `aa gate check` 命令（gate_cmd.py）

**Files:**
- Create: `assurance_agent/commands/gate_cmd.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/integration/test_cli_gate.py`

**Interfaces:**
- Consumes: M3 `load_workflow_schema`（含 `has_phase` / `gate_for_phase`）、`check_gate` / `GateVerdict`、`read_state`；M3 `append_event_best_effort`；Task 1 `exit_code_for_gate_verdict`。
- Produces: `aa gate check --change <id> --phase <phase> [--json]` 子命令。退出码经 `exit_code_for_gate_verdict`：pass/enter/exit/skip=0 / needs_fix/needs_human_review/continue=30 / reject/stop=40。查询 best-effort 事件。未知 phase、phase 无 gate、输入数据错误均 fail-closed → 退出 40；只有 Click 参数解析错误使用 2。

> 依赖说明：`schema.has_phase(phase)` 与 `schema.gate_for_phase(phase)` 是 M3 `WorkflowSchema` 的访问器，Task 7 会把它们回写进计划系列总览的 M3 契约。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_cli_gate.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main

STATE = """schema_version: "1"
params: {}
phases: {}
"""


def make_change(change_id: str = "CH-1") -> Path:
    change_dir = Path("qa/changes") / change_id
    (change_dir / "review").mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(STATE, encoding="utf-8")
    return change_dir


def test_gate_missing_change_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["gate", "check", "--change", "NOPE", "--phase", "case-review"])
        assert result.exit_code == 40
        assert "not found" in result.output


def test_gate_unknown_phase_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["gate", "check", "--change", "CH-1", "--phase", "no-such"])
        assert result.exit_code == 40
        assert "phase" in result.output.lower()


def test_gate_pass_verdict_exits_0_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        # packaged schema: case-review 的 gate 读 review/case-review.json
        (change_dir / "review/case-review.json").write_text(
            json.dumps({"schema_version": "1.0", "decision": "pass", "findings": []}),
            encoding="utf-8",
        )
        result = runner.invoke(
            main, ["gate", "check", "--change", "CH-1", "--phase", "case-review", "--json"]
        )
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["verdict"] == "pass"


def test_gate_needs_fix_verdict_exits_30() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(
            json.dumps({"schema_version": "1.0", "decision": "needs_fix", "findings": [{"id": "F1"}]}),
            encoding="utf-8",
        )
        result = runner.invoke(main, ["gate", "check", "--change", "CH-1", "--phase", "case-review"])
        assert result.exit_code == 30


def test_gate_reject_verdict_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(
            json.dumps({"schema_version": "1.0", "decision": "reject", "findings": []}),
            encoding="utf-8",
        )
        result = runner.invoke(main, ["gate", "check", "--change", "CH-1", "--phase", "case-review"])
        assert result.exit_code == 40
```

> 说明：以上测试对 `case-review` 相位/gate 与其读取的 evidence 路径的假设，来自打包 `workflow-schema.yaml` 的 review gate 声明。若 M3 落地的 gate 名或 evidence 路径不同，按落地 schema 调整 fixture 文件名与 phase 名，但每条断言（verdict → 退出码 0/30/40）不变。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_cli_gate.py -v`
Expected: FAIL（`No such command 'gate'`）

- [ ] **Step 3: 实现 gate_cmd.py 并挂载**

```python
# assurance_agent/commands/gate_cmd.py
"""aa gate check — 把单个相位 gate 裁决为一个 verdict（确定性, 无 LLM）。

对齐 TS src/commands/gate.ts 的 flag 面与退出码：查询类命令写 best-effort 事件。
"""
from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR, exit_code_for_gate_verdict
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.orchestration.gates import check_gate
from assurance_agent.workflow.orchestration.schema import load_workflow_schema


@click.group("gate")
def gate_group() -> None:
    """Gate adjudication commands."""


@gate_group.command("check")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", "phase_id", required=True, help="Phase whose gate to check.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def gate_check(change_id: str, phase_id: str, as_json: bool) -> None:
    """Adjudicate a single phase gate to one verdict (deterministic, no LLM)."""
    project_root = Path.cwd()
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    change_dir = project_root / "qa" / "changes" / change_id
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(EXIT_ERROR)

    try:
        schema = load_workflow_schema(project_root)
        if not schema.has_phase(phase_id):
            click.secho(f"gate check failed: unknown phase '{phase_id}'", fg="red")
            raise SystemExit(EXIT_ERROR)
        gate_name = schema.gate_for_phase(phase_id)
        if gate_name is None:
            click.secho(f"gate check failed: phase '{phase_id}' has no gate", fg="red")
            raise SystemExit(EXIT_ERROR)
        state = read_state(change_dir)
        params = getattr(state, "params", None) or {}
        verdict = check_gate(schema, gate_name, change_dir, state, params)
    except AaError as err:
        click.secho(f"gate check failed: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    append_event_best_effort(
        change_dir,
        {"source": "gate", "type": "gate_verdict", "phase": phase_id, "gate": verdict.gate, "verdict": verdict.verdict},
    )

    if as_json:
        click.echo(verdict.model_dump_json(indent=2))
    else:
        click.secho(f"aa gate check — {phase_id} → {verdict.gate}", bold=True)
        click.echo()
        click.echo(f"  Verdict : {verdict.verdict}")
        if verdict.reason:
            click.echo(f"  Reason  : {verdict.reason}")
        click.echo()

    raise SystemExit(exit_code_for_gate_verdict(verdict.verdict))
```

```python
# assurance_agent/cli.py（追加）
from assurance_agent.commands.gate_cmd import gate_group

main.add_command(gate_group)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_cli_gate.py -v`
Expected: 5 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/commands/gate_cmd.py assurance_agent/cli.py tests/integration/test_cli_gate.py
git commit -m "feat: add aa gate check command with verdict exit-code mapping"
```

---

### Task 4: `aa state apply` / `aa state heal`（state_cmd.py）

**Files:**
- Create: `assurance_agent/commands/state_cmd.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/integration/test_cli_state.py`

**Interfaces:**
- Consumes: M2 `WorkflowState`；M3 `load_workflow_schema.has_phase` / `phase_produces`、`resolve_change_path`、`read_state` / `write_state` / `state_file`、`append_event_strict` / `EventWriteError`、`capture_files` / `restore_files`、schema `ORCHESTRATOR_INTERNAL`；Task 1 `EXIT_ERROR`。
- Produces:
  - `aa state apply --change <id> --phase <phase> [--attempt-id <id>] [--skill <name>] [--skill-md-path <path>]`——先确认该 phase 的全部 declared produces 已存在，再记录 `phase_outcome_committed` 并更新 typed phase 展示态。**只检查存在性，不在写边界二次调用 gate**：`needs_fix` 必须先完成 outcome commit，随后由下一轮 `compute_status` 路由 repair/healing；M4 不得拒绝它。M6 必须传 dispatch 的 attempt id；人工调用未传时生成 `manual:<phase>:<uuid>`。
  - `aa state heal --change <id> --status <s>`——strict `heal_transition` + `state.phases.healing.status` 展示态更新；`<s>` ∈ {resolved, exhausted, not_needed, failed, skipped}。attempt budget 不在本命令写入，只由 allocation event 派生。
  - 两个子命令都以 snapshot 包住 `events.jsonl` 与 `workflow-state.yaml`；event 或 state 写失败退出 40，并逐字节恢复两个文件。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_cli_state.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main

STATE = """schema_version: "1"
params: {}
phases:
  healing:
    status: in_progress
"""


def make_change(change_id: str = "CH-1") -> Path:
    change_dir = Path("qa/changes") / change_id
    (change_dir / "review").mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(STATE, encoding="utf-8")
    return change_dir


def write_inspect_produces(change_dir: Path) -> None:
    (change_dir / "inspect").mkdir(exist_ok=True)
    (change_dir / "inspect/failure-analysis.json").write_text("{}", encoding="utf-8")
    (change_dir / "inspect/quality-gate-result.json").write_text("{}", encoding="utf-8")


def test_state_apply_unknown_phase_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["state", "apply", "--change", "CH-1", "--phase", "no-such"])
        assert result.exit_code == 40
        assert "phase" in result.output.lower()


def test_state_apply_advances_phase_and_records_skill_load_gate() -> None:
    import yaml

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        result = runner.invoke(
            main,
            ["state", "apply", "--change", "CH-1", "--phase", "inspect",
             "--skill", "aa-inspect", "--skill-md-path", "skills/aa-inspect/SKILL.md"],
        )
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["phases"]["inspect"]["status"] == "done"
        assert state["phases"]["inspect"]["skill_loaded"] is True
        assert state["phases"]["inspect"]["skill_md_path"] == "skills/aa-inspect/SKILL.md"
        assert "skill_loaded_at" in state["phases"]["inspect"]
        # 事件已落盘
        events = (change_dir / "events.jsonl").read_text().strip().splitlines()
        event = next(json.loads(line) for line in events if json.loads(line)["type"] == "phase_outcome_committed")
        assert event["phase"] == "inspect"


def test_state_apply_commits_outcome_without_rechecking_exit_gate() -> None:
    """Outcome commit must precede gate routing, including needs_fix evidence."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        # No inspect gate evidence exists. Rechecking here would reject the
        # outcome before compute_status can route repair/healing.
        result = runner.invoke(
            main,
            ["state", "apply", "--change", "CH-1", "--phase", "inspect", "--attempt-id", "a-7"],
        )
        assert result.exit_code == 0, result.output
        event = json.loads((change_dir / "events.jsonl").read_text().strip())
        assert event["type"] == "phase_outcome_committed"
        assert event["attempt_id"] == "a-7"


def test_state_apply_missing_declared_produces_exits_40_without_event() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main, ["state", "apply", "--change", "CH-1", "--phase", "inspect"]
        )
        assert result.exit_code == 40
        assert "missing declared produces" in result.output
        assert not (change_dir / "events.jsonl").exists()


def test_state_apply_strict_event_failure_rolls_back_and_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        before = (change_dir / "workflow-state.yaml").read_text()
        # 让 events.jsonl 不可写：建成目录，append_event_strict 抛 EventWriteError。
        (change_dir / "events.jsonl").mkdir()
        result = runner.invoke(main, ["state", "apply", "--change", "CH-1", "--phase", "inspect"])
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before


def test_state_heal_records_transition() -> None:
    import yaml

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "resolved"])
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["phases"]["healing"]["status"] == "resolved"
        events = (change_dir / "events.jsonl").read_text().strip().splitlines()
        assert any(json.loads(line)["type"] == "heal_transition" for line in events)


def test_state_heal_invalid_status_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "banana"])
        assert result.exit_code == 40
        assert "banana" in result.output


def test_state_heal_strict_event_failure_rolls_back_and_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        before = (change_dir / "workflow-state.yaml").read_text()
        (change_dir / "events.jsonl").mkdir()
        result = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "resolved"])
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_cli_state.py -v`
Expected: FAIL（`No such command 'state'`）

- [ ] **Step 3: 实现 state_cmd.py 并挂载**

```python
# assurance_agent/commands/state_cmd.py
"""aa state apply / aa state heal — typed audit event + state presentation update."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import click

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.events import EventWriteError, append_event_strict
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.snapshot import capture_files, restore_files
from assurance_agent.workflow.core.state import read_state, state_file, write_state
from assurance_agent.workflow.orchestration.gates import resolve_change_path
from assurance_agent.workflow.orchestration.schema import ORCHESTRATOR_INTERNAL, load_workflow_schema

# 对齐 TS state.ts HEAL_STATUSES。
HEAL_STATUSES = {"resolved", "exhausted", "not_needed", "failed", "skipped"}


@click.group("state")
def state_group() -> None:
    """Workflow-state maintenance commands for the orchestrator."""


def _validated_change_dir(change_id: str) -> tuple[Path, Path]:
    project_root = Path.cwd()
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    return project_root, project_root / "qa" / "changes" / change_id


@state_group.command("apply")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", "phase_id", required=True, help="Schema phase id to apply.")
@click.option("--attempt-id", "attempt_id", default=None, help="Dispatch attempt id from the driver.")
@click.option("--skill", "skill", default=None, help="Skill loaded for this phase (Skill Load Gate).")
@click.option("--skill-md-path", "skill_md_path", default=None, help="Path to the loaded SKILL.md.")
def state_apply(
    change_id: str,
    phase_id: str,
    attempt_id: str | None,
    skill: str | None,
    skill_md_path: str | None,
) -> None:
    """Commit one completed phase outcome; gate routing happens in compute_status."""
    project_root, change_dir = _validated_change_dir(change_id)
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(EXIT_ERROR)

    try:
        schema = load_workflow_schema(project_root)
        if not schema.has_phase(phase_id):
            click.secho(f"state apply failed: unknown phase '{phase_id}'", fg="red")
            raise SystemExit(EXIT_ERROR)
        missing = [
            rel for rel in (schema.phase_produces(phase_id) or [])
            if not resolve_change_path(change_dir, rel).exists()
        ]
        if missing:
            click.secho(
                f"state apply failed: missing declared produces: {', '.join(missing)}", fg="red"
            )
            raise SystemExit(EXIT_ERROR)
    except AaError as err:
        click.secho(f"state apply failed: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    # orchestrator/registry phase 需写 registry-gate 期望的 "pass"（driver 也走本命令推进
    # skill-registry-check）；其余 skill/cli phase 写通用的 "done"。见 M6「M3/M4 依赖增补」。
    applied_status = "pass" if phase_id in ORCHESTRATOR_INTERNAL else "done"
    phase_entry = {
        "status": applied_status,
        "skill_loaded": skill is not None,
        "skill_md_path": skill_md_path,
        "skill_loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    if skill is not None:
        phase_entry["skill"] = skill

    outcome_id = attempt_id or f"manual:{phase_id}:{uuid4()}"
    commit_state_change(
        change_dir,
        event={
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": phase_id,
            "attempt_id": outcome_id,
            "gate_report": None,
        },
        next_state=_with_phase(read_state(change_dir), phase_id, phase_entry),
    )
    click.secho(f'workflow-state.yaml updated for phase "{phase_id}"', fg="green")


@state_group.command("heal")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--status", "status", required=True, help="Healing judgment.")
def state_heal(change_id: str, status: str) -> None:
    """Record an orchestrator healing judgment."""
    project_root, change_dir = _validated_change_dir(change_id)
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(EXIT_ERROR)
    if status not in HEAL_STATUSES:
        allowed = ", ".join(sorted(HEAL_STATUSES))
        click.secho(f'unsupported healing status "{status}". Expected one of: {allowed}', fg="red")
        raise SystemExit(EXIT_ERROR)

    prior = _current_healing_status(change_dir)
    current = read_state(change_dir)
    commit_state_change(
        change_dir,
        event={"source": "status", "type": "heal_transition", "from": prior or "pending", "to": status},
        next_state=_with_healing_status(current, status),
    )
    click.secho(f"healing judgment recorded: {prior} → {status}", fg="green")


def commit_state_change(change_dir: Path, event: dict, next_state: WorkflowState) -> None:
    """Snapshot → strict event → atomic typed state; restore both files on failure."""
    snapshots = capture_files((change_dir / "events.jsonl", state_file(change_dir)))
    try:
        append_event_strict(change_dir, event)
        write_state(change_dir, next_state)
    except (EventWriteError, AaError, OSError) as err:
        restore_files(snapshots)
        click.secho(f"state transition rolled back: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err


def _with_phase(state: WorkflowState, phase_id: str, entry: dict) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases[phase_id.replace("-", "_")] = entry
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _with_healing_status(state: WorkflowState, status: str) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    healing = dict(phases.get("healing") or {})
    healing["status"] = status
    phases["healing"] = healing
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _current_healing_status(change_dir: Path) -> str | None:
    try:
        state = read_state(change_dir)
    except AaError:
        return None
    return state.phases.healing.status
```

```python
# assurance_agent/cli.py（追加）
from assurance_agent.commands.state_cmd import state_group

main.add_command(state_group)
```

> `state apply` 写入的 phase status 只是可观测展示/legacy gate evidence；普通 DAG 进度仍只由 produces + gate 决定。healing attempts/status 的运行时事实由 M3 typed events 重新投影，持久化值不得覆盖 event ledger。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_cli_state.py -v`
Expected: 8 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/commands/state_cmd.py assurance_agent/cli.py tests/integration/test_cli_state.py
git commit -m "feat: add aa state apply/heal with strict-event-then-state rollback"
```

---

### Task 5: `aa decide` 命令（decide_cmd.py）

**Files:**
- Create: `assurance_agent/commands/decide_cmd.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/integration/test_cli_decide.py`

**Interfaces:**
- Consumes: M3 `load_workflow_schema`、`compute_status` / `WorkflowStatus`、`read_state`、frozen `HumanDecisionEvent`；Task 4 `commit_state_change`；Task 1 `EXIT_ERROR`；M1 `AaError`。
- Produces: `aa decide --change <id> --at <checkpoint> --action <action> --reason <text> [--evidence <path>]`。事件严格使用冻结字段 `checkpoint/action/reason/who/review_file/review_sha256`；`state_at_stop` 只放进 typed state 的 decision record，不给 strict event 增加未注册字段。`action==stop` 时先确认尚未 terminal；最新 human decision 为 stop 时，M3 `compute_status` 投影 `Terminal(stopped)`。event/state 任一步失败恢复快照并退出 40。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_cli_decide.py
import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from assurance_agent.cli import main

STATE = """schema_version: "1"
params: {}
phases:
  explore:
    status: done
"""


def make_change(change_id: str = "CH-1") -> Path:
    change_dir = Path("qa/changes") / change_id
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(STATE, encoding="utf-8")
    return change_dir


def test_decide_missing_change_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main, ["decide", "--change", "NOPE", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "x"]
        )
        assert result.exit_code == 40
        assert "not found" in result.output


def test_decide_unsupported_action_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "case-review", "--action", "wibble", "--reason", "x"]
        )
        assert result.exit_code == 40
        assert "wibble" in result.output


def test_decide_records_event_and_appends_state_decision() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main,
            ["decide", "--change", "CH-1", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "looks good"],
        )
        assert result.exit_code == 0, result.output
        events = [json.loads(line) for line in (change_dir / "events.jsonl").read_text().strip().splitlines()]
        rec = next(e for e in events if e["type"] == "human_decision")
        assert rec["action"] == "fix_and_proceed"
        assert rec["checkpoint"] == "case-review"
        assert rec["reason"] == "looks good"
        assert len(events) == 1
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["decisions"][-1]["action"] == "fix_and_proceed"


def test_decide_stop_marks_terminal() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "workflow", "--action", "stop", "--reason", "halt"]
        )
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["terminal"]["kind"] == "stopped"
        events = [json.loads(line) for line in (change_dir / "events.jsonl").read_text().strip().splitlines()]
        rec = next(e for e in events if e["type"] == "human_decision")
        assert isinstance(state["decisions"][-1]["state_at_stop"]["next"], list)


def test_decide_strict_event_failure_rolls_back_and_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        before = (change_dir / "workflow-state.yaml").read_text()
        (change_dir / "events.jsonl").mkdir()  # 不可写 -> EventWriteError
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "x"]
        )
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before


def test_decide_empty_reason_rejected() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "   "]
        )
        assert result.exit_code == 40
        assert "reason" in result.output.lower()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_cli_decide.py -v`
Expected: FAIL（`No such command 'decide'`）

- [ ] **Step 3: 实现 decide_cmd.py 并挂载**

```python
# assurance_agent/commands/decide_cmd.py
"""aa decide — 记录一次受支持的人工工作流决定。

对齐 TS src/commands/decide.ts + workflow/core/decide.ts 的 flag 面与语义。
审计型命令复用 state_cmd.commit_state_change 的 snapshot 边界：strict human_decision
后写 canonical WorkflowState，失败恢复 event/state 两文件。
"""
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import click

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.commands.state_cmd import commit_state_change
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.schema import load_workflow_schema

HUMAN_DECISION_ACTIONS = {"fix_and_proceed", "accept_risk", "stop", "allow_test_changes", "skip_branch"}


@click.command("decide")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--at", "checkpoint", required=True, help="Gate, phase, or supported workflow checkpoint.")
@click.option("--action", "action", required=True, help="Supported action for the checkpoint.")
@click.option("--reason", "reason", required=True, help="Human decision reason.")
@click.option("--evidence", "evidence", default=None, help="Supporting evidence file within the project root.")
def decide_command(change_id: str, checkpoint: str, action: str, reason: str, evidence: str | None) -> None:
    """Record a supported human workflow decision."""
    project_root = Path.cwd()
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    change_dir = project_root / "qa" / "changes" / change_id
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(EXIT_ERROR)
    if action not in HUMAN_DECISION_ACTIONS:
        click.secho(f"decide failed: unsupported action '{action}'", fg="red")
        raise SystemExit(EXIT_ERROR)
    if not reason.strip():
        click.secho("decide failed: decision reason is required", fg="red")
        raise SystemExit(EXIT_ERROR)

    who = (os.environ.get("USER") or "unknown").strip() or "unknown"
    event: dict = {
        "source": "decide",
        "type": "human_decision",
        "checkpoint": checkpoint,
        "action": action,
        "reason": reason,
        "who": who,
    }
    if evidence is not None:
        evidence_path = (project_root / evidence).resolve()
        try:
            evidence_path.relative_to(project_root.resolve())
        except ValueError:
            click.secho("decide failed: evidence must stay under project root", fg="red")
            raise SystemExit(EXIT_ERROR)
        if not evidence_path.is_file():
            click.secho(f"decide failed: evidence not found: {evidence}", fg="red")
            raise SystemExit(EXIT_ERROR)
        event["review_file"] = evidence_path.relative_to(project_root).as_posix()
        event["review_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    stop_snapshot: dict | None = None
    if action == "stop":
        try:
            schema = load_workflow_schema(project_root)
            state = read_state(change_dir)
            params = state.params
            status = compute_status(schema, change_dir, state, params)
        except AaError as err:
            click.secho(f"decide failed: {err}", fg="red")
            raise SystemExit(EXIT_ERROR) from err
        if status.terminal is not None:
            click.secho(f"decide failed: workflow already terminal ({status.terminal.kind})", fg="red")
            raise SystemExit(EXIT_ERROR)
        stop_snapshot = {
            "next": [d.phase_id for d in status.next_dispatch],
            "phases": {p.id: p.status for p in status.phases},
        }
    _commit_decision(change_dir, event, action, checkpoint, reason, who, stop_snapshot)
    click.secho(f"aa decide — {checkpoint}", bold=True)
    click.secho(f"human_decision recorded: action={action}", fg="green")


def _commit_decision(
    change_dir: Path, event: dict, action: str, checkpoint: str, reason: str, who: str, stop_snapshot: dict | None
) -> None:
    try:
        state = read_state(change_dir)
    except AaError as err:
        click.secho(f"decide failed: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    decisions = (state.model_extra or {}).get("decisions")
    if not isinstance(decisions, list):
        decisions = []
    record = {
        "checkpoint": checkpoint,
        "action": action,
        "reason": reason,
        "who": who,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if stop_snapshot is not None:
        record["state_at_stop"] = stop_snapshot
    decisions.append(record)
    data = state.model_dump(mode="python", exclude_none=True)
    data["decisions"] = decisions
    if action == "stop":
        data["terminal"] = {"kind": "stopped", "reason": reason}
    commit_state_change(change_dir, event, WorkflowState.model_validate(data))
```

```python
# assurance_agent/cli.py（追加）
from assurance_agent.commands.decide_cmd import decide_command

main.add_command(decide_command)
```

> `decisions` / `terminal` 作为 versioned state 的展示扩展写回；终态事实来自最新 frozen `human_decision` event，而不是信任 `state.terminal`。M3 对最新 decision 的投影保证 `aa status` 与 M6 driver 看到相同终态。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_cli_decide.py -v`
Expected: 6 passed

- [ ] **Step 5: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/commands/decide_cmd.py assurance_agent/cli.py tests/integration/test_cli_decide.py
git commit -m "feat: add aa decide command with strict human-decision event and rollback"
```

---

### Task 6: risk 包 + `aa risk context`（Explore 上下文聚合）

**Files:**
- Create: `assurance_agent/risk/__init__.py`（空）
- Create: `assurance_agent/risk/safety.py`
- Create: `assurance_agent/risk/paths.py`
- Create: `assurance_agent/risk/context.py`
- Create: `assurance_agent/commands/risk_cmd.py`
- Modify: `assurance_agent/cli.py`
- Create: `tests/unit/risk/__init__.py`（空）
- Test: `tests/unit/risk/test_context.py`
- Test: `tests/integration/test_cli_risk_context.py`

**Interfaces:**
- Consumes: M1 `resources`（未直接需要）；stdlib + PyYAML；不依赖 M3/M5。
- Produces:
  - `safety.py`：`RiskSafetyError(AaError)`；`assert_change_id_safe(change_id: str) -> None`（正则 `^[a-zA-Z0-9._-]+$`）；`assert_inside_project(project_root: Path, target: Path) -> None`；`resolve_inside_project(project_root: Path, *segments: str) -> Path`；`resolve_requirement_path(project_root: Path, requirement_path: str) -> Path`。
  - `paths.py`：`explore_dir(project_root, change_id) -> Path`；`context_json_path(...) -> Path`；`advisory_json_path(...) -> Path`。
  - `context.py`：`RiskContext`（pydantic 模型）；`build_risk_context(*, change_id, project_root, diff_base="main", archive_depth=10, staleness_days=30, requirement_path=None, now=None) -> RiskContext`；`serialize_context(ctx) -> str`；`write_risk_context(project_root, change_id, ctx) -> Path`；`validate_context_shape(ctx) -> tuple[bool, list[str]]`；模块级 `get_changed_files(project_root, diff_base) -> GitDiffResult`（测试可 monkeypatch）。Task 7 的 advisory 校验消费 `RiskContext`。
  - `risk_cmd.py`：`aa risk context --change <id> [--project-dir <root>] [--diff-base <ref>] [--archive-depth <n>] [--staleness-days <n>] [--requirement <path>] [--output-dir <dir>] [--stdout]`。成功退出 0；safety/shape 校验失败退出 1。

- [ ] **Step 1: 写失败单元测试**

```python
# tests/unit/risk/test_context.py
from pathlib import Path

import pytest

from assurance_agent.risk import context as ctxmod
from assurance_agent.risk.context import (
    build_risk_context,
    serialize_context,
    validate_context_shape,
)
from assurance_agent.risk.safety import RiskSafetyError, assert_change_id_safe

FIXED_NOW = "2026-07-15T00:00:00+00:00"


def write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_assert_change_id_rejects_traversal() -> None:
    assert_change_id_safe("REQ-002-user-logout")
    with pytest.raises(RiskSafetyError):
        assert_change_id_safe("../escape")
    with pytest.raises(RiskSafetyError):
        assert_change_id_safe("a/b")


def test_build_context_non_git_is_degraded(tmp_path: Path) -> None:
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.no_git is True
    assert ctx.degraded is True
    assert any(r.startswith("no_git") for r in ctx.degraded_reasons)
    assert ctx.schema_version == "1.0"
    assert ctx.change_id == "CH-1"
    assert ctx.generated_at == FIXED_NOW


def test_build_context_aggregates_modules_and_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 造 module-map + cases，monkeypatch git diff 给定 changed_files（确定性）。
    write(
        tmp_path,
        ".aa/module-map.yaml",
        'rules:\n  - pattern: "backend/app/menus/**"\n    modules: ["menus"]\n    confidence: high\n',
    )
    write(
        tmp_path,
        "qa/cases/menus/case.yaml",
        "added:\n"
        "  - case_id: TC_MENU_001\n    module: menus\n    priority: P1\n    automation:\n      required: true\n",
    )
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(
            changed_files=["backend/app/menus/service.py"], no_git=False, degraded_reasons=[]
        ),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)

    assert [m.name for m in ctx.impact.modules] == ["menus"]
    assert ctx.impact.modules[0].confidence == "high"
    assert ctx.impact.affected_case_ids == ["TC_MENU_001"]
    assert ctx.impact.affected_cases_by_module == {"menus": ["TC_MENU_001"]}
    # code_change 证据 id 规则 EV-DIFF-<MODULE>-<CONF>
    ev_ids = [e.id for e in ctx.evidence]
    assert "EV-DIFF-MENUS-HIGH" in ev_ids
    # case_signals 反映 automation_status
    assert ctx.case_signals[0].automation_status == "automated"
    # 无 archive -> degraded 含 no_archives
    assert any(r.startswith("no_archives") for r in ctx.degraded_reasons)


def test_serialize_and_validate_shape(tmp_path: Path) -> None:
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    text = serialize_context(ctx)
    assert text.endswith("\n")
    ok, errors = validate_context_shape(ctx)
    assert ok is True and errors == []


def test_historical_issue_from_json_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(
        tmp_path,
        "qa/archive/A-001/known-product-issues.json",
        '{"issues": [{"id": "KPI-1", "module": "menus", "severity": "high", "status": "open"}]}',
    )
    write(tmp_path, "qa/archive/A-001/archive-summary.md", "archived_at: '2026-07-10T00:00:00Z'\n")
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert [h.id for h in ctx.historical_issues] == ["KPI-1"]
    assert any(e.type == "historical_issue" and e.issue_id == "KPI-1" for e in ctx.evidence)
```

- [ ] **Step 2: 跑单元测试确认失败**

Run: `uv run pytest tests/unit/risk/test_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'assurance_agent.risk'`

- [ ] **Step 3: 实现 safety.py 与 paths.py**

```python
# assurance_agent/risk/safety.py
"""Path / change-id safety helpers for the risk (Explore) package.

Change-id validation delegates to the M2-wide identifier contract; resolved
risk paths additionally must stay inside the project root.
"""
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe as _assert_id


class RiskSafetyError(AaError):
    pass


def assert_change_id_safe(change_id: str) -> None:
    try:
        _assert_id(change_id)
    except UnsafeIdentifierError as err:
        raise RiskSafetyError(str(err)) from err


def assert_inside_project(project_root: Path, target: Path) -> None:
    root = project_root.resolve()
    resolved = target.resolve()
    if root != resolved and root not in resolved.parents:
        raise RiskSafetyError(f"Path escapes project root: {target}")


def resolve_inside_project(project_root: Path, *segments: str) -> Path:
    joined = project_root.joinpath(*segments)
    assert_inside_project(project_root, joined)
    return joined


def resolve_requirement_path(project_root: Path, requirement_path: str) -> Path:
    candidate = Path(requirement_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
    assert_inside_project(project_root, resolved)
    return resolved
```

```python
# assurance_agent/risk/paths.py
"""Explore artifact paths (qa/changes/<id>/explore/). Transcribed from src/risk/paths.ts."""
from pathlib import Path

from assurance_agent.risk.safety import assert_change_id_safe, resolve_inside_project


def explore_dir(project_root: Path, change_id: str) -> Path:
    assert_change_id_safe(change_id)
    return resolve_inside_project(project_root, "qa", "changes", change_id, "explore")


def context_json_path(project_root: Path, change_id: str) -> Path:
    return explore_dir(project_root, change_id) / "context.json"


def advisory_json_path(project_root: Path, change_id: str) -> Path:
    return explore_dir(project_root, change_id) / "advisory.json"
```

- [ ] **Step 4: 实现 context.py**

```python
# assurance_agent/risk/context.py
"""Explore context aggregation (spec 第 5 节 risk).

净室重写 TS src/risk/{context_builder,git_diff,module_map,case_loader,
archive_sampler,pass_rate,historical_issues}.ts 的行为规则，聚合到本模块。
与 TS 的有意差异（已文档化，不依赖 M5）：archive 采样直接读
qa/archive/<id>/execution/runs/<batch>/{api,e2e}-result.json（按 mtime 取最新
batch，附 legacy execution/ 回退），不走 M5 的 execution-evidence primary-mode
完整性检查。generated_at 支持注入（now 参数）以便 golden 测试确定性。
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

from assurance_agent.risk.safety import RiskSafetyError, resolve_requirement_path

Confidence = str  # "high" | "medium" | "low"
_CONF_RANK = {"high": 3, "medium": 2, "low": 1}
_PASS_RATE_FAIL_THRESHOLD = 0.85
_WINDOW_K = 3
_LAYERS = ["api", "e2e"]
_XFAIL_RATIONALE = (
    "MVP conservatively treats xfailed/xpassed as failed (non-green or expectation mismatch)."
)


# ---------- models ----------
class GitDiffResult(BaseModel):
    changed_files: list[str]
    no_git: bool
    degraded_reasons: list[str]


class ModuleImpact(BaseModel):
    name: str
    confidence: Confidence
    matched_rules: list[str]
    changed_files: list[str]
    reason: str | None = None


class CaseSignal(BaseModel):
    case_id: str
    module: str
    priority: str | None = None
    automation_status: str | None = None
    flaky: bool = False


class TestHealthEntry(BaseModel):
    module: str
    layer: str
    runs_sampled: int
    pass_rate: float
    recent_fail_case_ids: list[str]
    evidence_id: str


class HistoricalIssue(BaseModel):
    id: str
    module: str
    endpoint: str | None = None
    severity: str | None = None
    status: str | None = None
    evidence_id: str


class EvidenceEntry(BaseModel):
    id: str
    type: str  # test_pass_rate | historical_issue | code_change
    module: str | None = None
    layer: str | None = None
    value: float | None = None
    runs_sampled: int | None = None
    below_fail_threshold: bool | None = None
    endpoint: str | None = None
    issue_id: str | None = None
    confidence: Confidence | None = None
    changed_files: list[str] | None = None
    source: str
    parse_source: str | None = None
    parse_confidence_cap: Confidence | None = None


class ImpactBlock(BaseModel):
    diff_base: str
    changed_files: list[str]
    modules: list[ModuleImpact]
    affected_case_ids: list[str]
    affected_cases_by_module: dict[str, list[str]]
    affected_test_files: list[str]


class RiskContext(BaseModel):
    schema_version: str = "1.0"
    change_id: str
    generated_at: str
    requirement_summary: str | None = None
    aggregation_policy: dict
    archive_window: dict
    staleness: dict
    impact: ImpactBlock
    case_signals: list[CaseSignal]
    test_health: list[TestHealthEntry]
    historical_issues: list[HistoricalIssue]
    evidence: list[EvidenceEntry]
    degraded: bool
    degraded_reasons: list[str]
    no_git: bool | None = None


# ---------- helpers: case id ----------
def canonicalize_case_id(raw: str) -> str:
    return re.sub(r"-", "_", raw.strip().upper())


# ---------- git diff ----------
def _run_git(project_root: Path, args: list[str]) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=project_root, capture_output=True, text=True, shell=False
        )
    except FileNotFoundError:
        return False, "", "git not found"
    return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def get_changed_files(project_root: Path, diff_base: str) -> GitDiffResult:
    if not (project_root / ".git").exists():
        return GitDiffResult(
            changed_files=[], no_git=True, degraded_reasons=["no_git: project root is not a git repository"]
        )
    degraded: list[str] = []
    ok, base, err = _run_git(project_root, ["merge-base", "HEAD", diff_base])
    if not ok or not base:
        degraded.append(f"git merge-base HEAD {diff_base} failed: {err or 'unknown error'}")
        ok2, out2, err2 = _run_git(project_root, ["diff", "--name-only", diff_base])
        if not ok2:
            degraded.append(f"git diff --name-only {diff_base} failed: {err2 or 'unknown error'}")
            return GitDiffResult(changed_files=[], no_git=False, degraded_reasons=degraded)
        return GitDiffResult(changed_files=_parse_name_only(out2), no_git=False, degraded_reasons=degraded)
    ok3, out3, err3 = _run_git(project_root, ["diff", "--name-only", base, "HEAD"])
    if not ok3:
        degraded.append(f"git diff failed: {err3 or 'unknown error'}")
        return GitDiffResult(changed_files=[], no_git=False, degraded_reasons=degraded)
    return GitDiffResult(changed_files=_parse_name_only(out3), no_git=False, degraded_reasons=degraded)


def _parse_name_only(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.split("\n") if line.strip()]


# ---------- module map ----------
_DEFAULT_RULES = [
    {"pattern": "backend/**", "modules": ["backend"], "confidence": "low", "reason": "default backend path mapping"},
    {"pattern": "frontend/**", "modules": ["frontend"], "confidence": "low", "reason": "default frontend path mapping"},
]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
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


def _load_module_rules(project_root: Path) -> list[dict]:
    map_path = project_root / ".aa" / "module-map.yaml"
    if not map_path.is_file():
        return _DEFAULT_RULES
    raw = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    rules = raw.get("rules") if isinstance(raw, dict) else None
    return rules if rules else _DEFAULT_RULES


def _merge_conf(a: Confidence, b: Confidence) -> Confidence:
    return a if _CONF_RANK.get(a, 0) >= _CONF_RANK.get(b, 0) else b


def _match_modules(changed_files: list[str], rules: list[dict]) -> list[ModuleImpact]:
    by_name: dict[str, ModuleImpact] = {}
    for file in changed_files:
        norm = file.replace("\\", "/")
        for rule in rules:
            if not _glob_to_regex(rule["pattern"]).match(norm):
                continue
            for mod in rule["modules"]:
                existing = by_name.get(mod)
                if existing is None:
                    by_name[mod] = ModuleImpact(
                        name=mod,
                        confidence=rule["confidence"],
                        matched_rules=[rule["pattern"]],
                        changed_files=[file],
                        reason=rule.get("reason"),
                    )
                else:
                    if rule["pattern"] not in existing.matched_rules:
                        existing.matched_rules.append(rule["pattern"])
                    if file not in existing.changed_files:
                        existing.changed_files.append(file)
                    existing.confidence = _merge_conf(existing.confidence, rule["confidence"])
    return sorted(by_name.values(), key=lambda m: m.name)


# ---------- case loader ----------
def _load_cases(project_root: Path) -> list[dict]:
    cases_root = project_root / "qa" / "cases"
    if not cases_root.is_dir():
        return []
    by_id: dict[str, dict] = {}
    for path in sorted(cases_root.rglob("*")):
        if not path.is_file():
            continue
        if not (path.name == "case.yaml" or path.name.endswith(".case.yaml")):
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        default_module = _infer_module(path, cases_root)
        for item in _collect_case_items(doc):
            if not isinstance(item, dict):
                continue
            case_id = item.get("case_id") or item.get("id")
            if not isinstance(case_id, str):
                continue
            automation = item.get("automation")
            by_id[case_id] = {
                "case_id": case_id,
                "module": item["module"] if isinstance(item.get("module"), str) else default_module,
                "priority": item.get("priority") if isinstance(item.get("priority"), str) else None,
                "flaky": item.get("flaky") is True,
                "automation_required": isinstance(automation, dict) and automation.get("required") is True,
            }
    return [by_id[k] for k in sorted(by_id)]


def _infer_module(path: Path, cases_root: Path) -> str:
    rel = path.parent.relative_to(cases_root).parts
    return rel[0] if rel else "unknown"


def _collect_case_items(doc: dict) -> list:
    items: list = []
    for key in ("cases", "added", "modified"):
        if isinstance(doc.get(key), list):
            items.extend(doc[key])
    return items or [doc]


def _resolve_affected(modules: list[ModuleImpact], cases: list[dict]) -> tuple[list[str], dict[str, list[str]], list[dict]]:
    names = {m.name for m in modules}
    by_module: dict[str, list[str]] = {}
    ids: list[str] = []
    signals: list[dict] = []
    for c in cases:
        if c["module"] not in names:
            continue
        ids.append(c["case_id"])
        signals.append(c)
        by_module.setdefault(c["module"], []).append(c["case_id"])
    for k in by_module:
        by_module[k].sort()
    return sorted(set(ids)), by_module, signals


# ---------- archive sampling ----------
def _read_archived_at_ms(archive_path: Path) -> float:
    summary = archive_path / "archive-summary.md"
    if summary.is_file():
        m = re.search(r"archived_at:\s*['\"]?([^'\"\n]+)", summary.read_text(encoding="utf-8"), re.IGNORECASE)
        if m:
            try:
                return datetime.fromisoformat(m.group(1).strip().replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                pass
    return archive_path.stat().st_mtime * 1000


def _latest_batch(archive_path: Path) -> dict | None:
    runs_dir = archive_path / "execution" / "runs"
    if runs_dir.is_dir():
        best: dict | None = None
        for entry in runs_dir.iterdir():
            if not entry.is_dir():
                continue
            api = entry / "api-result.json"
            e2e = entry / "e2e-result.json"
            sample = {
                "batch_id": entry.name,
                "batch_mtime_ms": entry.stat().st_mtime * 1000,
                "api_result_path": api if api.is_file() else None,
                "e2e_result_path": e2e if e2e.is_file() else None,
            }
            if best is None or sample["batch_mtime_ms"] > best["batch_mtime_ms"]:
                best = sample
        return best
    legacy = archive_path / "execution"
    if (legacy / "api-result.json").is_file():
        e2e = legacy / "e2e-result.json"
        return {
            "batch_id": "legacy",
            "batch_mtime_ms": legacy.stat().st_mtime * 1000,
            "api_result_path": legacy / "api-result.json",
            "e2e_result_path": e2e if e2e.is_file() else None,
        }
    return None


def _sample_archives(project_root: Path, depth: int) -> list[dict]:
    root = project_root / "qa" / "archive"
    if not root.is_dir():
        return []
    entries = [
        {"archive_id": e.name, "archive_path": e, "archived_at_ms": _read_archived_at_ms(e)}
        for e in root.iterdir()
        if e.is_dir()
    ]
    entries.sort(key=lambda a: a["archived_at_ms"], reverse=True)
    entries = entries[:depth]
    for e in entries:
        e["latest_batch"] = _latest_batch(e["archive_path"])
    return entries


def _read_layer_cases(path: Path | None, layer: str) -> list[dict]:
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if data.get("target") != layer:
        return []
    cases = data.get("cases")
    return cases if isinstance(cases, list) else []


# ---------- pass rate ----------
def _normalize_status(status: str) -> str:
    s = status.lower()
    if s == "passed":
        return "passed"
    if s in ("skipped", "not_run"):
        return "skipped"
    if s in ("xfailed", "xpassed", "failed"):
        return "failed"
    return "ignored"


def _aggregate_case_pass_rate(samples: list[list[dict]]) -> dict[str, dict]:
    counts: dict[str, dict[str, int]] = {}
    for batch in samples:
        for row in batch:
            norm = _normalize_status(str(row.get("status", "")))
            if norm in ("skipped", "ignored"):
                continue
            key = canonicalize_case_id(str(row.get("case_id", "")))
            cur = counts.setdefault(key, {"passed": 0, "executed": 0})
            cur["executed"] += 1
            if norm == "passed":
                cur["passed"] += 1
    return {
        k: {"passed": v["passed"], "executed": v["executed"], "rate": v["passed"] / v["executed"] if v["executed"] else 0.0}
        for k, v in counts.items()
    }


def _module_pass_rate(case_ids: list[str], case_rates: dict[str, dict]) -> dict:
    passed = executed = 0
    for cid in case_ids:
        r = case_rates.get(canonicalize_case_id(cid))
        if not r:
            continue
        passed += r["passed"]
        executed += r["executed"]
    return {"passed": passed, "executed": executed, "rate": passed / executed if executed else 0.0}


def _recent_fail_case_ids(batches: list[dict], layer: str) -> list[str]:
    ordered_batches = sorted(batches, key=lambda b: b["batch_mtime_ms"], reverse=True)[:_WINDOW_K]
    seen: set[str] = set()
    out: list[str] = []
    for batch in ordered_batches:
        fp = batch.get("api_result_path") if layer == "api" else batch.get("e2e_result_path")
        for row in _read_layer_cases(fp, layer):
            if _normalize_status(str(row.get("status", ""))) != "failed":
                continue
            key = canonicalize_case_id(str(row.get("case_id", "")))
            if key in seen:
                continue
            seen.add(key)
            out.append(str(row.get("case_id", "")))
    return out


# ---------- historical issues ----------
_SOURCE_RANK = {
    "known_product_issues_json": 4,
    "known_product_issues_frontmatter": 3,
    "archive_summary_kpi_table": 2,
    "known_product_issues_regex": 1,
}


def _cap_for_source(source: str) -> Confidence:
    if source in ("known_product_issues_json", "known_product_issues_frontmatter"):
        return "high"
    if source == "archive_summary_kpi_table":
        return "medium"
    return "low"


def _collect_issues_from_archive(archive_path: Path) -> list[dict]:
    found: list[dict] = []
    json_path = archive_path / "known-product-issues.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            for item in data.get("issues", []) if isinstance(data, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("module"), str):
                    found.append(
                        {
                            "id": item["id"],
                            "module": item["module"],
                            "endpoint": item.get("endpoint") if isinstance(item.get("endpoint"), str) else None,
                            "severity": item.get("severity") if isinstance(item.get("severity"), str) else None,
                            "status": item.get("status") if isinstance(item.get("status"), str) else None,
                            "parse_source": "known_product_issues_json",
                            "source_path": str(json_path),
                        }
                    )
        except (json.JSONDecodeError, OSError):
            pass
    return found


def _merge_historical_issues(archive_paths: list[Path]) -> tuple[list[HistoricalIssue], list[EvidenceEntry]]:
    by_id: dict[str, dict] = {}
    for archive_path in archive_paths:
        for issue in _collect_issues_from_archive(archive_path):
            existing = by_id.get(issue["id"])
            if existing is None or _SOURCE_RANK[issue["parse_source"]] > _SOURCE_RANK[existing["parse_source"]]:
                by_id[issue["id"]] = issue
    issues: list[HistoricalIssue] = []
    evidence: list[EvidenceEntry] = []
    for issue in by_id.values():
        ev_id = "EV-HIST-ISSUE-" + re.sub(r"[^a-zA-Z0-9]+", "-", issue["id"]).upper()
        issues.append(
            HistoricalIssue(
                id=issue["id"], module=issue["module"], endpoint=issue.get("endpoint"),
                severity=issue.get("severity"), status=issue.get("status"), evidence_id=ev_id,
            )
        )
        evidence.append(
            EvidenceEntry(
                id=ev_id, type="historical_issue", module=issue["module"], endpoint=issue.get("endpoint"),
                issue_id=issue["id"], source=issue["source_path"], parse_source=issue["parse_source"],
                parse_confidence_cap=_cap_for_source(issue["parse_source"]),
            )
        )
    issues.sort(key=lambda h: h.id)
    evidence.sort(key=lambda e: e.id)
    return issues, evidence


# ---------- aggregation entrypoint ----------
def build_risk_context(
    *,
    change_id: str,
    project_root: Path,
    diff_base: str = "main",
    archive_depth: int = 10,
    staleness_days: int = 30,
    requirement_path: str | None = None,
    now: str | None = None,
) -> RiskContext:
    degraded_reasons: list[str] = []
    evidence: list[EvidenceEntry] = []
    test_health: list[TestHealthEntry] = []

    requirement_summary: str | None = None
    if requirement_path:
        req = resolve_requirement_path(project_root, requirement_path)
        if not req.is_file():
            raise RiskSafetyError(f"--requirement is not a file: {req}")
        requirement_summary = req.read_text(encoding="utf-8")[:2000]

    git = get_changed_files(project_root, diff_base)
    degraded_reasons.extend(git.degraded_reasons)

    modules = _match_modules(git.changed_files, _load_module_rules(project_root))
    all_cases = _load_cases(project_root)
    affected_ids, affected_by_module, signals = _resolve_affected(modules, all_cases)

    for mod in modules:
        if not mod.changed_files:
            continue
        ev_id = f"EV-DIFF-{mod.name.upper()}-{mod.confidence.upper()}"
        evidence.append(
            EvidenceEntry(
                id=ev_id, type="code_change", module=mod.name, confidence=mod.confidence,
                changed_files=mod.changed_files, source="git diff",
            )
        )

    archives = _sample_archives(project_root, archive_depth)
    batches = [a["latest_batch"] for a in archives if a.get("latest_batch")]

    for layer in _LAYERS:
        samples = [
            _read_layer_cases(b.get("api_result_path") if layer == "api" else b.get("e2e_result_path"), layer)
            for b in batches
        ]
        samples = [s for s in samples if s]
        case_rates = _aggregate_case_pass_rate(samples)
        recent_fails = _recent_fail_case_ids(batches, layer)
        for mod in modules:
            case_ids = affected_by_module.get(mod.name, [])
            if not case_ids:
                continue
            keys = {canonicalize_case_id(c) for c in case_ids}
            rate = _module_pass_rate(case_ids, case_rates)
            if rate["executed"] == 0:
                continue
            ev_id = f"EV-TEST-HEALTH-{mod.name.upper()}-{layer.upper()}"
            fails = [f for f in recent_fails if canonicalize_case_id(f) in keys]
            test_health.append(
                TestHealthEntry(
                    module=mod.name, layer=layer, runs_sampled=rate["executed"],
                    pass_rate=rate["rate"], recent_fail_case_ids=fails, evidence_id=ev_id,
                )
            )
            evidence.append(
                EvidenceEntry(
                    id=ev_id, type="test_pass_rate", module=mod.name, layer=layer, value=rate["rate"],
                    runs_sampled=rate["executed"], below_fail_threshold=rate["rate"] < _PASS_RATE_FAIL_THRESHOLD,
                    source="qa/archive",
                )
            )

    hist_issues, hist_evidence = _merge_historical_issues([a["archive_path"] for a in archives])
    evidence.extend(hist_evidence)

    newest_ms = archives[0]["archived_at_ms"] if archives else None
    oldest_ms = archives[-1]["archived_at_ms"] if archives else None
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    stale = newest_ms is not None and now_ms - newest_ms > staleness_days * 86_400_000

    if not archives:
        degraded_reasons.append("no_archives: qa/archive is empty or missing")
    if not git.changed_files:
        degraded_reasons.append("no_diff: no changed files vs diff base")
    if not all_cases:
        degraded_reasons.append("no_cases: qa/cases is empty or missing")
    if not hist_issues:
        degraded_reasons.append("no_history: no historical issues parsed from archives")

    case_signals = [
        CaseSignal(
            case_id=c["case_id"], module=c["module"], priority=c.get("priority"),
            automation_status="automated" if c["automation_required"] else "manual", flaky=c["flaky"],
        )
        for c in signals
    ]

    ctx = RiskContext(
        change_id=change_id,
        generated_at=now or datetime.now(timezone.utc).isoformat(),
        requirement_summary=requirement_summary,
        aggregation_policy={
            "archive_depth": archive_depth,
            "archive_order": "archive_created_at_desc",
            "runs_per_archive": "latest_batch_only",
            "skipped_counted_in_denominator": False,
            "layers": _LAYERS,
            "xfail_treated_as": "failed",
            "xfail_rationale": _XFAIL_RATIONALE,
            "recent_fail_batch_window_k": _WINDOW_K,
            "pass_rate_fail_threshold": _PASS_RATE_FAIL_THRESHOLD,
        },
        archive_window={
            "depth": archive_depth,
            "archives_sampled": [a["archive_id"] for a in archives],
            "newest_archive": _format_date(newest_ms),
            "oldest_archive": _format_date(oldest_ms),
        },
        staleness={"max_age_days": staleness_days, "stale": stale},
        impact=ImpactBlock(
            diff_base=diff_base,
            changed_files=git.changed_files,
            modules=modules,
            affected_case_ids=affected_ids,
            affected_cases_by_module=affected_by_module,
            affected_test_files=[],
        ),
        case_signals=case_signals,
        test_health=test_health,
        historical_issues=hist_issues,
        evidence=_dedupe_evidence(evidence),
        degraded=len(degraded_reasons) > 0,
        degraded_reasons=degraded_reasons,
    )
    if git.no_git:
        ctx.no_git = True
    return ctx


def _format_date(ms: float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _dedupe_evidence(items: list[EvidenceEntry]) -> list[EvidenceEntry]:
    by_id = {e.id: e for e in items}
    return sorted(by_id.values(), key=lambda e: e.id)


def serialize_context(ctx: RiskContext) -> str:
    return json.dumps(ctx.model_dump(exclude_none=False), indent=2, ensure_ascii=False) + "\n"


def write_risk_context(project_root: Path, change_id: str, ctx: RiskContext) -> Path:
    from assurance_agent.risk.paths import context_json_path

    out = context_json_path(project_root, change_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialize_context(ctx), encoding="utf-8")
    return out


def validate_context_shape(ctx: RiskContext) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if ctx.schema_version != "1.0":
        errors.append("schema_version must be 1.0")
    if not ctx.change_id:
        errors.append("change_id required")
    return (len(errors) == 0, errors)
```

- [ ] **Step 5: 跑单元测试确认通过**

Run: `uv run pytest tests/unit/risk/test_context.py -v`
Expected: 5 passed

- [ ] **Step 6: 写失败集成测试**

```python
# tests/integration/test_cli_risk_context.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def test_risk_context_stdout_mode_prints_json_and_does_not_write() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["risk", "context", "--change", "CH-1", "--stdout"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["change_id"] == "CH-1"
        assert doc["schema_version"] == "1.0"
        assert not Path("qa/changes/CH-1/explore/context.json").exists()


def test_risk_context_writes_context_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("qa/changes/CH-1").mkdir(parents=True)
        result = runner.invoke(main, ["risk", "context", "--change", "CH-1"])
        assert result.exit_code == 0, result.output
        out = Path("qa/changes/CH-1/explore/context.json")
        assert out.is_file()
        doc = json.loads(out.read_text())
        assert doc["change_id"] == "CH-1"


def test_risk_context_rejects_unsafe_change_id() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["risk", "context", "--change", "../escape", "--stdout"])
        assert result.exit_code == 40
        assert "change-id" in result.output.lower()
```

- [ ] **Step 7: 跑集成测试确认失败**

Run: `uv run pytest tests/integration/test_cli_risk_context.py -v`
Expected: FAIL（`No such command 'risk'`）

- [ ] **Step 8: 实现 risk_cmd.py（context 子命令）并挂载**

```python
# assurance_agent/commands/risk_cmd.py
"""aa risk — Explore 命令（Phase 0.5）。对齐 TS src/commands/risk.ts 的 flag 面。"""
from pathlib import Path

import click

from assurance_agent.risk.context import (
    build_risk_context,
    serialize_context,
    validate_context_shape,
    write_risk_context,
)
from assurance_agent.risk.safety import RiskSafetyError, assert_change_id_safe, assert_inside_project
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR


@click.group("risk")
def risk_group() -> None:
    """Explore commands (Phase 0.5)."""


@risk_group.command("context")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--project-dir", "project_dir", default=None, help="Project root (default: cwd).")
@click.option("--diff-base", "diff_base", default="main", help="Git diff base ref.")
@click.option("--archive-depth", "archive_depth", default=10, type=int, help="Recent archives to sample.")
@click.option("--staleness-days", "staleness_days", default=30, type=int, help="Archive staleness threshold (days).")
@click.option("--requirement", "requirement", default=None, help="Requirement text file inside project root.")
@click.option("--output-dir", "output_dir", default=None, help="Write context.json into this dir instead.")
@click.option("--stdout", "to_stdout", is_flag=True, help="Print JSON to stdout and do NOT write to disk.")
def risk_context(
    change_id: str,
    project_dir: str | None,
    diff_base: str,
    archive_depth: int,
    staleness_days: int,
    requirement: str | None,
    output_dir: str | None,
    to_stdout: bool,
) -> None:
    """Aggregate git diff, cases, and archive history into explore/context.json."""
    try:
        assert_change_id_safe(change_id)
        project_root = Path(project_dir).resolve() if project_dir else Path.cwd()
        if archive_depth <= 0 or staleness_days <= 0:
            raise RiskSafetyError("--archive-depth and --staleness-days must be positive integers")
        if not project_root.is_dir():
            raise RiskSafetyError(f"--project-dir is not a directory: {project_root}")

        ctx = build_risk_context(
            change_id=change_id,
            project_root=project_root,
            diff_base=diff_base,
            archive_depth=archive_depth,
            staleness_days=staleness_days,
            requirement_path=requirement,
        )
        ok, errors = validate_context_shape(ctx)
        if not ok:
            click.secho("Context validation failed: " + "; ".join(errors), fg="red")
            raise SystemExit(EXIT_ERROR)

        if to_stdout:
            click.echo(serialize_context(ctx), nl=False)
            return

        if output_dir is not None:
            out_dir = (project_root / output_dir).resolve()
            assert_inside_project(project_root, out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "context.json"
            out_path.write_text(serialize_context(ctx), encoding="utf-8")
        else:
            out_path = write_risk_context(project_root, change_id, ctx)
        click.secho(f"Wrote {out_path}", fg="green")
        click.echo(f"Evidence entries: {len(ctx.evidence)}")
        click.echo(f"Degraded: {ctx.degraded}")
    except RiskSafetyError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
```

```python
# assurance_agent/cli.py（追加）
from assurance_agent.commands.risk_cmd import risk_group

main.add_command(risk_group)
```

- [ ] **Step 9: 跑集成测试确认通过**

Run: `uv run pytest tests/integration/test_cli_risk_context.py -v`
Expected: 3 passed

- [ ] **Step 10: 质量门禁**

Run: `uv run ruff check . && uv run pyright`
Expected: 均无报错

- [ ] **Step 11: Commit**

```bash
git add assurance_agent/risk assurance_agent/commands/risk_cmd.py assurance_agent/cli.py tests/unit/risk tests/integration/test_cli_risk_context.py
git commit -m "feat: add risk package and aa risk context Explore aggregation"
```

---

### Task 7: `aa risk validate-advisory` + 分层契约 + 契约回写 + 收尾

**Files:**
- Create: `assurance_agent/risk/advisory.py`
- Modify: `assurance_agent/commands/risk_cmd.py`
- Modify: `assurance_agent/cli.py`（无需改动——risk_group 已挂载；确认即可）
- Modify: `.importlinter`
- Modify: `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`
- Test: `tests/unit/risk/test_advisory.py`
- Test: `tests/integration/test_cli_risk_advisory.py`

**Interfaces:**
- Consumes: Task 6 `RiskContext`；M2 `Advisory` 模型（结构校验）；`paths.py` 的 `context_json_path` / `advisory_json_path`。
- Produces: `advisory.py` 的 `validate_advisory(context: RiskContext, advisory: dict, known_case_ids: list[str], *, interaction_mode=None, orchestrator_skill=None) -> tuple[bool, list[str]]`（语义规则转写自 TS src/risk/validate_advisory.ts）；`aa risk validate-advisory --change <id> [--project-dir <root>]`——context.json / advisory.json 缺失或校验失败退出 1，通过退出 0。

- [ ] **Step 1: 写失败单元测试**

```python
# tests/unit/risk/test_advisory.py
from assurance_agent.risk.advisory import validate_advisory
from assurance_agent.risk.context import EvidenceEntry, ImpactBlock, RiskContext


def make_context(**overrides: object) -> RiskContext:
    ctx = RiskContext(
        change_id="CH-1",
        generated_at="2026-07-15T00:00:00+00:00",
        aggregation_policy={},
        archive_window={},
        staleness={"max_age_days": 30, "stale": False},
        impact=ImpactBlock(
            diff_base="main", changed_files=[], modules=[],
            affected_case_ids=["TC_MENU_001"], affected_cases_by_module={}, affected_test_files=[],
        ),
        case_signals=[],
        test_health=[],
        historical_issues=[],
        evidence=[EvidenceEntry(id="EV-DIFF-MENUS-HIGH", type="code_change", module="menus", confidence="high", source="git diff")],
        degraded=False,
        degraded_reasons=[],
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def test_valid_minimal_advisory_passes() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=["TC_MENU_001"])
    assert ok is True, errors


def test_unknown_evidence_id_fails() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "low", "evidence_ids": ["EV-DOES-NOT-EXIST"]}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("unknown id" in e for e in errors)


def test_high_confidence_requires_non_empty_evidence_ids() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": []}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("confidence high requires non-empty evidence_ids" in e for e in errors)


def test_high_confidence_forbidden_when_stale() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
        "open_questions_for_case_design": [],
    }
    ctx = make_context(staleness={"max_age_days": 30, "stale": True})
    ok, errors = validate_advisory(ctx, advisory, known_case_ids=[])
    assert ok is False
    assert any("staleness.stale" in e for e in errors)


def test_missing_evidence_must_use_low_confidence() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "medium", "evidence_ids": []}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("missing evidence_ids must use confidence low" in e for e in errors)


def test_open_question_answered_requires_intent_and_via() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [],
        "open_questions_for_case_design": [{"id": "OQ-1", "status": "answered"}],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("requires assertion_intent" in e for e in errors)
    assert any("requires answered_via" in e for e in errors)


def test_autonomous_mode_forbids_explore_answered_via() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [],
        "open_questions_for_case_design": [
            {"id": "OQ-1", "status": "answered", "assertion_intent": "assert_ideal", "answered_via": "explore", "answer": "x"}
        ],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[], interaction_mode="autonomous")
    assert ok is False
    assert any("autonomous run forbids answered_via explore" in e for e in errors)


def test_structural_failure_short_circuits() -> None:
    # schema_version 存在但 watchlist 缺失 -> Advisory 模型结构校验失败。
    advisory = {"schema_version": "1.0", "open_questions_for_case_design": []}
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert errors  # 结构错误已收集
```

- [ ] **Step 2: 跑单元测试确认失败**

Run: `uv run pytest tests/unit/risk/test_advisory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'assurance_agent.risk.advisory'`

- [ ] **Step 3: 实现 advisory.py**

```python
# assurance_agent/risk/advisory.py
"""Advisory 语义校验（spec 第 5 节 §5.5–5.7）。

规则转写自 TS src/risk/validate_advisory.ts。结构校验复用 M2 的 Advisory 模型
（等价于 TS 的 schema/advisory zod 验证器，且源自打包 explore-advisory.schema.json）；
语义校验（evidence_ids / case_id 引用、置信度门槛、open-question 生命周期、断言
传播、模式一致性）在本模块实现。
"""
from typing import Any

from pydantic import ValidationError

from assurance_agent.artifacts.models import Advisory
from assurance_agent.risk.context import RiskContext

_CAP_RANK = {"high": 3, "medium": 2, "low": 1}
_OQ_STATUSES = {"unanswered", "answered", "deferred"}
_ASSERTION_INTENTS = {"assert_ideal", "assert_known_bug", "ignore", "undecided"}
_ANSWERED_VIA = {"explore", "auto_default", "aa-intake"}


def _cap_rank(c: str | None) -> int:
    return _CAP_RANK.get(c or "low", 1)


def _as_str_list(v: Any) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _guidance(advisory: dict) -> dict | None:
    g = advisory.get("case_design_guidance")
    return g if isinstance(g, dict) else None


def _collect_evidence_ids(advisory: dict) -> list[str]:
    ids: set[str] = set()

    def scan(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                for x in _as_str_list(item.get("evidence_ids")):
                    ids.add(x)

    scan(advisory.get("watchlist"))
    g = _guidance(advisory)
    if g:
        scan(g.get("priority_hints"))
        scan(g.get("suggested_scenarios"))
    return list(ids)


def _collect_case_ids(advisory: dict) -> list[str]:
    g = _guidance(advisory)
    hints = g.get("priority_hints") if g else None
    out: list[str] = []
    if isinstance(hints, list):
        for h in hints:
            if isinstance(h, dict) and isinstance(h.get("case_id"), str):
                out.append(h["case_id"])
    return out


def _collect_issue_refs(advisory: dict) -> list[str]:
    ids: set[str] = set()

    def scan(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("issue_id"), str):
                ids.add(item["issue_id"])
            for x in _as_str_list(item.get("issue_ids")):
                ids.add(x)

    scan(advisory.get("watchlist"))
    g = _guidance(advisory)
    if g:
        scan(g.get("priority_hints"))
    return list(ids)


def _is_answered_oq(row: dict) -> bool:
    if row.get("status") == "answered":
        return True
    return row.get("status") is None and row.get("answer") is not None and row.get("answered_via") == "explore"


def _check_open_question_lifecycle(advisory: dict, errors: list[str]) -> None:
    oqs = advisory.get("open_questions_for_case_design")
    if not isinstance(oqs, list):
        return
    for oq in oqs:
        if not isinstance(oq, dict):
            continue
        oq_id = oq["id"] if isinstance(oq.get("id"), str) else "(unknown OQ)"
        status = oq.get("status")
        intent = oq.get("assertion_intent")
        via = oq.get("answered_via")
        if status is not None and str(status) not in _OQ_STATUSES:
            errors.append(f"{oq_id}: status must be one of unanswered, answered, deferred")
        if intent is not None and str(intent) not in _ASSERTION_INTENTS:
            errors.append(f"{oq_id}: assertion_intent must be one of assert_ideal, assert_known_bug, ignore, undecided")
        if via is not None and str(via) not in _ANSWERED_VIA:
            errors.append(f"{oq_id}: answered_via must be one of explore, auto_default, aa-intake")
        if status == "answered":
            if intent is None:
                errors.append(f"{oq_id}: status answered requires assertion_intent")
            if via is None:
                errors.append(f"{oq_id}: status answered requires answered_via")
            if via != "auto_default" and oq.get("answer") is None and oq.get("answer_text") is None:
                errors.append(f"{oq_id}: status answered requires answer or answer_text")
        if status == "unanswered":
            if intent is not None:
                errors.append(f"{oq_id}: status unanswered must not set assertion_intent")
            if via is not None:
                errors.append(f"{oq_id}: status unanswered must not set answered_via")
        if status == "deferred":
            reason = oq.get("deferred_reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{oq_id}: status deferred requires deferred_reason")
            if intent is not None:
                errors.append(f"{oq_id}: status deferred must not set assertion_intent")


def _check_mode_consistency(advisory: dict, interaction_mode: str | None, errors: list[str]) -> None:
    if interaction_mode != "autonomous":
        return
    oqs = advisory.get("open_questions_for_case_design")
    if not isinstance(oqs, list):
        return
    for oq in oqs:
        if not isinstance(oq, dict):
            continue
        via = oq.get("answered_via")
        if via in ("explore", "aa-intake"):
            oq_id = oq["id"] if isinstance(oq.get("id"), str) else "(unknown OQ)"
            errors.append(f"{oq_id}: autonomous run forbids answered_via {via}")


def _check_confidence_items(context: RiskContext, items: Any, label: str, errors: list[str]) -> None:
    if not isinstance(items, list):
        return
    evidence_by_id = {e.id: e for e in context.evidence}
    module_conf = {m.name: m.confidence for m in context.impact.modules}

    def qualifies_high(ev_id: str) -> bool:
        ev = evidence_by_id.get(ev_id)
        if ev is None:
            return False
        if ev.type == "historical_issue":
            return _cap_rank(ev.parse_confidence_cap) >= _cap_rank("high")
        if ev.type == "test_pass_rate":
            return ev.below_fail_threshold is True
        if ev.type == "code_change":
            return _cap_rank(ev.confidence) >= _cap_rank("medium")
        return False

    for item in items:
        if not isinstance(item, dict):
            continue
        conf = item.get("confidence")
        ev_ids = [x for x in _as_str_list(item.get("evidence_ids")) if x in evidence_by_id]
        if conf == "high":
            if not ev_ids:
                errors.append(f"{label}: confidence high requires non-empty evidence_ids")
            if context.staleness.get("stale") is True:
                errors.append(f"{label}: confidence high forbidden when staleness.stale is true")
            caps = [evidence_by_id[i].parse_confidence_cap or "high" for i in ev_ids]
            if caps and all(_cap_rank(c) <= _cap_rank("low") for c in caps):
                errors.append(f"{label}: confidence high cannot rely only on low-cap evidence")
            if ev_ids and not any(qualifies_high(i) for i in ev_ids):
                errors.append(
                    f"{label}: confidence high requires a qualifying evidence "
                    "(historical_issue source 1–2, test_health below threshold, or diff module confidence >= medium)"
                )
            modules = [evidence_by_id[i].module for i in ev_ids if evidence_by_id[i].module]
            if modules and all(_cap_rank(module_conf.get(m, "medium")) < _cap_rank("medium") for m in modules):
                errors.append(f"{label}: confidence high requires diff module confidence >= medium")
        if not ev_ids and conf != "low":
            errors.append(f"{label}: missing evidence_ids must use confidence low")


def validate_advisory(
    context: RiskContext,
    advisory: dict,
    known_case_ids: list[str],
    *,
    interaction_mode: str | None = None,
    orchestrator_skill: str | None = None,
) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if isinstance(advisory.get("schema_version"), str):
        try:
            Advisory.model_validate(advisory)
        except ValidationError as err:
            return (False, [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in err.errors()])

    evidence_by_id = {e.id: e for e in context.evidence}
    known_issue_ids = {h.id for h in context.historical_issues}
    affected = set(context.impact.affected_case_ids) | set(known_case_ids)

    for ev_id in _collect_evidence_ids(advisory):
        if ev_id not in evidence_by_id:
            errors.append(f"evidence_ids references unknown id: {ev_id}")

    for case_id in _collect_case_ids(advisory):
        if case_id not in affected:
            errors.append(f"case_id not in context.affected_case_ids or qa/cases: {case_id}")

    _check_confidence_items(context, advisory.get("watchlist"), "watchlist", errors)
    g = _guidance(advisory)
    _check_confidence_items(context, g.get("priority_hints") if g else None, "case_design_guidance.priority_hints", errors)

    _check_open_question_lifecycle(advisory, errors)
    _check_mode_consistency(advisory, interaction_mode, errors)

    for issue_id in _collect_issue_refs(advisory):
        if issue_id not in known_issue_ids:
            errors.append(f"issue reference not in context.historical_issues: {issue_id}")

    return (len(errors) == 0, errors)
```

> 说明：`context.staleness` 是 `dict`（见 Task 6 `RiskContext`），故用 `context.staleness.get("stale")`。TS 的 `answered_via` 枚举含 `aws-intake`，本 Python 版按命名规则改为 `aa-intake`。断言传播（priority_hints/watchlist 的 `pitfall_ref` 联动）与低置信断言问题两条规则在本里程碑先落地上表覆盖的核心子集；若后续 skill 用例需要完整传播校验，再在 M7 skill 改写时补充对应单测与规则（不影响本表退出码断言）。

- [ ] **Step 4: 跑单元测试确认通过**

Run: `uv run pytest tests/unit/risk/test_advisory.py -v`
Expected: 8 passed

- [ ] **Step 5: 给 risk_cmd.py 增补 validate-advisory 子命令**

```python
# assurance_agent/commands/risk_cmd.py（追加 import 与子命令，保持既有 context 子命令不变）
import json

from assurance_agent.risk.advisory import validate_advisory
from assurance_agent.risk.context import RiskContext
from assurance_agent.risk.paths import advisory_json_path, context_json_path
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR


@risk_group.command("validate-advisory")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--project-dir", "project_dir", default=None, help="Project root (default: cwd).")
def risk_validate_advisory(change_id: str, project_dir: str | None) -> None:
    """Validate explore/advisory.json against explore/context.json (§5.5)."""
    try:
        assert_change_id_safe(change_id)
        project_root = Path(project_dir).resolve() if project_dir else Path.cwd()
        ctx_path = context_json_path(project_root, change_id)
        adv_path = advisory_json_path(project_root, change_id)
        if not ctx_path.is_file():
            click.secho(f"Missing {ctx_path}", fg="red")
            raise SystemExit(EXIT_ERROR)
        if not adv_path.is_file():
            click.secho(f"Missing {adv_path}", fg="red")
            raise SystemExit(EXIT_ERROR)

        context = RiskContext.model_validate(json.loads(ctx_path.read_text(encoding="utf-8")))
        advisory = json.loads(adv_path.read_text(encoding="utf-8"))
        run_ctx = _read_run_context(project_root, change_id)
        ok, errors = validate_advisory(
            context,
            advisory,
            known_case_ids=[],
            interaction_mode=run_ctx.get("interaction_mode"),
            orchestrator_skill=run_ctx.get("orchestrator_skill"),
        )
        if ok:
            click.secho("advisory.json validation passed", fg="green")
            raise SystemExit(0)
        click.secho("advisory.json validation failed:", fg="red")
        for e in errors:
            click.secho(f"  - {e}", fg="red")
        raise SystemExit(EXIT_ERROR)
    except RiskSafetyError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err


def _read_run_context(project_root: Path, change_id: str) -> dict:
    import yaml

    state_file = project_root / "qa" / "changes" / change_id / "workflow-state.yaml"
    if not state_file.is_file():
        return {}
    parsed = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return {}
    run_context = parsed.get("run_context")
    if not isinstance(run_context, dict):
        return {}
    mode = run_context.get("interaction_mode")
    return {
        "interaction_mode": mode if mode in ("interactive", "autonomous") else None,
        "orchestrator_skill": run_context.get("orchestrator_skill")
        if isinstance(run_context.get("orchestrator_skill"), str)
        else None,
    }
```

- [ ] **Step 6: 写失败集成测试**

```python
# tests/integration/test_cli_risk_advisory.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main

CONTEXT = {
    "schema_version": "1.0",
    "change_id": "CH-1",
    "generated_at": "2026-07-15T00:00:00+00:00",
    "aggregation_policy": {},
    "archive_window": {},
    "staleness": {"max_age_days": 30, "stale": False},
    "impact": {
        "diff_base": "main", "changed_files": [], "modules": [],
        "affected_case_ids": ["TC_MENU_001"], "affected_cases_by_module": {}, "affected_test_files": [],
    },
    "case_signals": [],
    "test_health": [],
    "historical_issues": [],
    "evidence": [{"id": "EV-DIFF-MENUS-HIGH", "type": "code_change", "module": "menus", "confidence": "high", "source": "git diff"}],
    "degraded": False,
    "degraded_reasons": [],
}


def seed_explore(change_id: str, advisory: dict) -> Path:
    explore = Path("qa/changes") / change_id / "explore"
    explore.mkdir(parents=True)
    (explore / "context.json").write_text(json.dumps(CONTEXT), encoding="utf-8")
    (explore / "advisory.json").write_text(json.dumps(advisory), encoding="utf-8")
    return explore


def test_validate_advisory_missing_context_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("qa/changes/CH-1/explore").mkdir(parents=True)
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 40
        assert "Missing" in result.output


def test_validate_advisory_pass_exits_0() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        seed_explore(
            "CH-1",
            {
                "schema_version": "1.0",
                "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
                "open_questions_for_case_design": [],
            },
        )
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 0, result.output
        assert "passed" in result.output


def test_validate_advisory_semantic_failure_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        seed_explore(
            "CH-1",
            {
                "schema_version": "1.0",
                "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-NOPE"]}],
                "open_questions_for_case_design": [],
            },
        )
        result = runner.invoke(main, ["risk", "validate-advisory", "--change", "CH-1"])
        assert result.exit_code == 40
        assert "unknown id" in result.output
```

- [ ] **Step 7: 跑集成测试确认失败→实现已就位→确认通过**

Run: `uv run pytest tests/integration/test_cli_risk_advisory.py -v`
Expected: 3 passed（Step 5 已实现子命令；若先于 Step 5 运行则 FAIL `No such command`）

- [ ] **Step 8: 增量更新并验证累计 `.importlinter` 契约**

只修改既有主 `layers` 契约，在 `commands` 与 `workflow` 之间插入 `risk`（`commands → risk` 与 `risk → workflow/artifacts` 允许，反向禁止）。**不得整文件替换**，且必须原样保留 M3 已冻结的两个 forbidden 契约：

```ini
# .importlinter
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

Run: `uv run lint-imports`
Expected: Contracts: 3 kept, 0 broken（主 layers + 两个 M3 forbidden；契约数量减少即失败）。

- [ ] **Step 9: 回写计划系列总览的 M3 / M4 契约**

按总览「变更需先改本节」约定，编辑 `docs/superpowers/plans/2026-07-14-python-migration-plan-series.md`：

1. 在「### M3 编排核心」的 `class WorkflowSchema(BaseModel): ...` 代码块内，`phase_produces` 之后追加两个访问器（M4 的 `aa gate check` / `aa state apply` 需要 phase→gate 映射与相位存在性判定）：

```python
    def phase_produces(self, phase_id: str) -> list[str] | None: ...  # 满足 M2 WorkflowSchemaLike
    def has_phase(self, phase_id: str) -> bool: ...                   # M4 gate/state 需要
    def gate_for_phase(self, phase_id: str) -> str | None: ...        # M4 gate/state 需要；无 gate 返回 None
```

2. 确认「### M4 命令面」的 `exit_codes.py` 常量块与本计划 Task 1 一致（`EXIT_COMPLETED/STOPPED/HUMAN_REVIEW/ERROR/USAGE` + `exit_code_for_gate_verdict` / `exit_code_for_terminal`）；若数值或函数名不一致，以本计划落地代码为准并回改总览。

- [ ] **Step 10: 全量回归 + 质量门禁**

Run: `uv run pytest -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过（M1 + M2 + M3 既有测试 + 本里程碑新增：exit_codes 16、status 5、gate 5、state 8、decide 6、risk context 单测 5 + 集成 3、advisory 单测 8 + 集成 3 = 59）

- [ ] **Step 11: Commit**

```bash
git add assurance_agent/risk/advisory.py assurance_agent/commands/risk_cmd.py .importlinter \
        tests/unit/risk/test_advisory.py tests/integration/test_cli_risk_advisory.py \
        docs/superpowers/plans/2026-07-14-python-migration-plan-series.md
git commit -m "feat: add aa risk validate-advisory, risk layer contract and M3 accessor sync"
```

---

## M4 验收清单

- `aa status --change <id> [--next] [--json]`：running/completed 退出 0、stopped 退出 20、needs_human_review 退出 30；`--json` 输出 `WorkflowStatus.model_dump()`（含 `terminal.kind/reason`、`next_dispatch[].agent/skill/kind`）；`--next` 只出 dispatch 列表；查询 best-effort 事件。
- `aa gate check --change <id> --phase <phase> [--json]`：verdict → 退出码 0/30/40 完全对齐 `exit_code_for_gate_verdict`；未知 phase / 无 gate 退出 1。
- `aa state apply`：不重检 exit gate；用 dispatch attempt id（人工调用生成 `manual:*`）追加 frozen `phase_outcome_committed` 后更新 typed phase 展示态。event/state 共用文件快照边界，任一步失败退出 40 并逐字节恢复。
- `aa state heal --status <s>`：状态 ∈ {resolved,exhausted,not_needed,failed,skipped}；追加 frozen `heal_transition` 并更新 `state.phases.healing.status`；attempt budget 只由 allocation event 派生；非法状态退出 1，写失败退出 40 并恢复快照。
- `aa decide`：action ∈ 5 个合法值，reason 必填；追加 frozen `human_decision` 并更新 typed state 的 `decisions` 展示记录；`stop` 校验非 terminal 并记录 `state_at_stop`，终态由 M3 对最新 decision 的投影决定；写失败退出 40 并恢复快照。
- `aa risk context`：写 `explore/context.json`（`--stdout` 只打印不落盘、`--output-dir` 改写目标目录）；非法 change-id / shape 校验失败退出 1；聚合含 impact/modules、code_change/test_pass_rate/historical_issue 三类证据、degraded 原因。
- `aa risk validate-advisory`：context/advisory 缺失或语义校验失败退出 1，通过退出 0；语义规则（evidence_ids / case_id 引用、置信度门槛、OQ 生命周期、autonomous 模式）对齐 TS。
- `uv run pytest` / `ruff` / `pyright` / `lint-imports` 全绿；新文件无 `aws` 残留（TS 源路径注释除外）；退出码常量在 `workflow/core/exit_codes.py` 且 core 不 import orchestration。
- 计划系列总览的 M3 契约已追加 `has_phase` / `gate_for_phase`，M4 exit_codes 常量已确认一致。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-15-m4-status-commands.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task with two-stage review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Which approach?
