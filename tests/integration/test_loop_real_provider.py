import contextlib
import json
import os
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.loop import EXIT_COMPLETED, EXIT_ERROR, run_workflow_loop

# Minimal but real schema (valid per M3 parse_schema static checks): two skill
# phases in sequence. Gateless on purpose — the P0 regression is about the driver
# persisting phase state, not gate adjudication (covered by M3 tests). Gates
# default to `stop`, so a gated-but-unproduced ready phase would wrongly terminate;
# keeping this schema gateless isolates the advancement behaviour under test.
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


@contextlib.contextmanager
def _chdir(path: Path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


class WritingAdapter:
    """Simulates a real agent: writes each phase's `produces` artifact."""

    def __init__(self, change_dir: Path) -> None:
        self.change_dir = change_dir
        self.requests: list[PhaseRequest] = []

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.requests.append(request)
        out = self.change_dir / request.phase_id / f"{request.phase_id}-out.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"phase": request.phase_id}), encoding="utf-8")
        return PhaseResult(ok=True, output="ok")


class InProcessAa:
    """cli_executor whose apply_phase_state runs the REAL `aa state apply`."""

    def run_cli_phase(self, entry, ctx):  # noqa: ANN001, ANN201
        raise AssertionError("no cli phases in this schema")

    def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
        args = [
            "state",
            "apply",
            "--change",
            ctx.change_id,
            "--phase",
            entry.phase_id,
            "--attempt-id",
            attempt_id,
        ]
        if entry.skill:
            args += ["--skill", entry.skill]
        with _chdir(ctx.project_root):
            res = CliRunner().invoke(main, args, catch_exceptions=False)
        exit_code = 0 if res.exit_code is None else res.exit_code
        if exit_code != 0:
            return PhaseResult(ok=False, output=res.output, error=res.output[:500])
        return PhaseResult(ok=True, output=res.output)


def _bootstrap(tmp_path: Path) -> Path:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "workflow-schema.yaml").write_text(SCHEMA, encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    return change_dir


def test_real_provider_advances_skill_phases_to_completed(tmp_path: Path) -> None:
    change_dir = _bootstrap(tmp_path)
    adapter = WritingAdapter(change_dir)
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=adapter,
        cli_executor=InProcessAa(),
        max_iterations=10,
    )
    assert result.exit_code == EXIT_COMPLETED
    assert [r.phase_id for r in adapter.requests] == ["p1", "p2"]
    events = read_events(change_dir)
    signed = [(e["phase"], e["attempt_id"]) for e in events if e.get("type") == "dispatch_signed"]
    committed = [(e["phase"], e["attempt_id"]) for e in events if e.get("type") == "phase_outcome_committed"]
    assert committed == signed


def test_outcome_commit_failure_is_driver_fatal(tmp_path: Path) -> None:
    # Produces may already exist, but the driver must not continue after its
    # audit/state commit boundary reports failure.
    change_dir = _bootstrap(tmp_path)
    adapter = WritingAdapter(change_dir)

    class FailedApply(InProcessAa):
        def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
            return PhaseResult(ok=False, error="strict outcome commit failed")

    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=adapter,
        cli_executor=FailedApply(),
        max_iterations=4,
    )
    assert result.exit_code == EXIT_ERROR
    assert "strict outcome commit failed" in result.reason
    assert [r.phase_id for r in adapter.requests] == ["p1"]
