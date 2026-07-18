import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    DefaultCliPhaseExecutor,
    run_workflow_loop,
)
from assurance_agent.workflow.orchestration.schema import load_workflow_schema

SCHEMA = """\
schema_version: "1"
name: t
phases:
  - id: p1
    skill: aa-p1
    agent: aa-doc-author
    requires: []
    produces: [p1/p1-out.json]
  - id: p2
    skill: aa-p2
    agent: aa-doc-author
    requires: [p1]
    produces: [p2/p2-out.json]
"""


class WritingAdapter:
    def __init__(self, change_dir: Path) -> None:
        self.change_dir = change_dir
        self.requests: list[PhaseRequest] = []

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.requests.append(request)
        out = self.change_dir / request.phase_id / f"{request.phase_id}-out.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"phase": request.phase_id}), encoding="utf-8")
        return PhaseResult(ok=True, output="ok")


def _bootstrap(tmp_path: Path) -> Path:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "workflow-schema.yaml").write_text(SCHEMA, encoding="utf-8")
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    return change_dir


def test_real_provider_advances_skill_phases_to_completed(tmp_path: Path) -> None:
    change_dir = _bootstrap(tmp_path)
    adapter = WritingAdapter(change_dir)
    schema = load_workflow_schema(tmp_path)
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=adapter,
        schema=schema,
        cli_executor=DefaultCliPhaseExecutor(schema=schema),
        max_iterations=10,
    )
    assert result.exit_code == EXIT_COMPLETED
    assert [r.phase_id for r in adapter.requests] == ["p1", "p2"]
    events = read_events(change_dir)
    signed = [(e["phase"], e["attempt_id"]) for e in events if e.get("type") == "dispatch_signed"]
    committed = [(e["phase"], e["attempt_id"]) for e in events if e.get("type") == "phase_outcome_committed"]
    assert committed == signed


def test_outcome_commit_failure_is_driver_fatal(tmp_path: Path) -> None:
    change_dir = _bootstrap(tmp_path)
    adapter = WritingAdapter(change_dir)
    schema = load_workflow_schema(tmp_path)

    class FailedApply(DefaultCliPhaseExecutor):
        def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001
            return PhaseResult(ok=False, error="strict outcome commit failed")

    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=adapter,
        schema=schema,
        cli_executor=FailedApply(schema=schema),
        max_iterations=4,
    )
    assert result.exit_code == EXIT_ERROR
    assert "strict outcome commit failed" in result.reason
    assert [r.phase_id for r in adapter.requests] == ["p1"]


class CrashingAdapter(WritingAdapter):
    """Raises (hard crash) on a chosen phase; artifacts of earlier phases persist."""

    def __init__(self, change_dir: Path, crash_on: str) -> None:
        super().__init__(change_dir)
        self._crash_on = crash_on

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        if request.phase_id == self._crash_on:
            raise RuntimeError("simulated driver crash")
        return super().run_phase(request)


def test_crash_resume_reprojects_from_last_checkpoint(tmp_path: Path) -> None:
    """恢复不变量：迭代边界即 checkpoint——driver 在 p2 崩溃后重跑，
    p1 不重派发、不重复提交，终局与无崩溃运行一致。"""
    change_dir = _bootstrap(tmp_path)
    schema = load_workflow_schema(tmp_path)

    crashed = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=CrashingAdapter(change_dir, crash_on="p2"),
        schema=schema,
        cli_executor=DefaultCliPhaseExecutor(schema=schema),
        max_iterations=10,
    )
    assert crashed.exit_code == EXIT_ERROR
    from assurance_agent.workflow.driver.driver_state import read_driver_state

    driver_after_crash = read_driver_state(change_dir)
    assert driver_after_crash is not None
    assert driver_after_crash.status == "failed"
    assert driver_after_crash.iteration == 1  # p1 提交后过了 1 个迭代边界
    assert driver_after_crash.last_checkpoint_at is not None

    resumed_adapter = WritingAdapter(change_dir)
    resumed = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=resumed_adapter,
        schema=schema,
        cli_executor=DefaultCliPhaseExecutor(schema=schema),
        max_iterations=10,
    )
    assert resumed.exit_code == EXIT_COMPLETED
    assert [r.phase_id for r in resumed_adapter.requests] == ["p2"]  # p1 已 done，不重跑

    driver_after_resume = read_driver_state(change_dir)
    assert driver_after_resume is not None and driver_after_resume.iteration == 2

    events = read_events(change_dir)
    committed = [e["phase"] for e in events if e.get("type") == "phase_outcome_committed"]
    assert committed == ["p1", "p2"]  # 无重复提交（幂等重放不变量）
