from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.eval.executor import execute_attempt
from assurance_agent.eval.types import DatasetSample
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.orchestration.engine import (
    DispatchEntry,
    PhaseView,
    Terminal,
    WorkflowStatus,
)


def _git_init(repo: Path) -> None:
    # Fresh throwaway repo inside the pytest tmp dir — write-scan requires the
    # SUT workspace to be a git repo (same precondition as the TS executor).
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)


class FakeAdapter:
    """FakeAdapter：run_phase 落一份 golden 产物到 change 目录，再返回成功。"""

    def __init__(self, change_dir: Path) -> None:
        self.change_dir = change_dir
        self.requests: list[PhaseRequest] = []

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.requests.append(request)
        review = self.change_dir / "review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "case-review.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
        return PhaseResult(ok=True, output="done")


class AuditOutcomeExecutor:
    def run_cli_phase(self, entry, ctx):  # noqa: ANN001, ANN201
        raise AssertionError("skill-only eval must not run a cli phase")

    def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
        append_event_strict(
            ctx.change_dir,
            {
                "source": "progression",
                "type": "phase_outcome_committed",
                "phase": entry.phase_id,
                "attempt_id": attempt_id,
                "gate_report": None,
            },
        )
        return PhaseResult(ok=True, output="committed")


def _scripted_status(steps: list[WorkflowStatus]):
    seq = iter(steps)

    def provider() -> WorkflowStatus:
        return next(seq)

    return provider


def test_execute_in_process_writes_evidence_without_change_id(tmp_path: Path) -> None:
    sample = DatasetSample(
        id="FC-001",
        suite="classification-unit",
        input={"message": "x", "log_excerpt": "y", "target": "e2e"},
        expected={"category": "locator_failure"},
    )
    attempt = tmp_path / "attempt-0"
    result = execute_attempt(
        sample,
        attempt,
        suite="classification-unit",
        sut_dir=tmp_path / "sut",
        adapter=object(),  # type: ignore[arg-type]
        executor_type="in_process",
    )
    assert result.status == "ok"
    assert result.executor == "in_process"
    assert (attempt / "stdout.log").is_file()
    assert (attempt / "stderr.log").is_file()
    assert (attempt / "execution.json").is_file()
    assert (attempt / "raw-output" / ".in-process").is_file()


def test_execute_attempt_copies_sut_tests_into_raw_output(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    change_dir = sut / "qa" / "changes" / "eval-sample-001"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    tests = sut / "tests" / "api"
    tests.mkdir(parents=True)
    (tests / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    for required in ("config.py", "conftest.py", "schema_validation.py"):
        (sut / "tests" / required).write_text("# bootstrap\n", encoding="utf-8")
    _git_init(sut)

    status = _scripted_status(
        [
            WorkflowStatus(phases=[], next_dispatch=[], terminal=Terminal(kind="completed", reason="ok")),
        ]
    )
    sample = DatasetSample(
        id="WAC-001",
        suite="workflow-api-codegen",
        input={"change_id": "eval-sample-001"},
        expected={},
    )
    attempt = tmp_path / "run" / "WAC-001" / "attempt-0"

    class NoopAdapter:
        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            return PhaseResult(ok=True, output="")

    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-api-codegen",
        sut_dir=sut,
        adapter=NoopAdapter(),
        status_provider=status,
    )
    assert result.status == "ok"
    assert (attempt / "raw-output" / "tests" / "api" / "test_a.py").is_file()


def test_execute_attempt_runs_m6_loop_and_copies_raw_output(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    change_dir = sut / "qa" / "changes" / "eval-sample-001"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    adapter = FakeAdapter(change_dir)
    _git_init(sut)
    status = _scripted_status(
        [
            WorkflowStatus(
                phases=[PhaseView(id="case-design", status="ready")],
                next_dispatch=[
                    DispatchEntry(phase_id="case-design", skill="aa-case-design", agent=None, kind="skill")
                ],
                terminal=None,
            ),
            WorkflowStatus(
                phases=[PhaseView(id="case-design", status="done")],
                next_dispatch=[],
                terminal=Terminal(kind="completed", reason="ok"),
            ),
        ]
    )
    sample = DatasetSample(
        id="WC-001",
        suite="workflow-case",
        input={"change_id": "eval-sample-001", "run_mode": "case-only"},
        expected={},
    )
    attempt = tmp_path / "run" / "samples" / "WC-001" / "attempt-0"

    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-case",
        sut_dir=sut,
        adapter=adapter,
        scope="case",
        status_provider=status,
        cli_executor=AuditOutcomeExecutor(),
    )

    assert result.status == "ok"
    assert result.exit_code == 0
    assert [r.phase_id for r in adapter.requests] == ["case-design"]
    copied = attempt / "raw-output" / "review" / "case-review.json"
    assert json.loads(copied.read_text())["decision"] == "pass"
    assert (attempt / "execution.json").exists()
    assert (attempt / "stdout.log").exists()


def test_execute_attempt_error_exit_recorded(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    (sut / "qa" / "changes" / "eval-sample-002").mkdir(parents=True)
    _git_init(sut)
    status = _scripted_status(
        [
            WorkflowStatus(
                phases=[], next_dispatch=[], terminal=Terminal(kind="stopped", reason="gate reject")
            ),
        ]
    )
    sample = DatasetSample(
        id="WC-002", suite="workflow-case", input={"change_id": "eval-sample-002"}, expected={}
    )
    attempt = tmp_path / "run" / "s" / "WC-002" / "attempt-0"

    class NoopAdapter:
        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            return PhaseResult(ok=True, output="")

    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-case",
        sut_dir=sut,
        adapter=NoopAdapter(),
        scope="case",
        status_provider=status,
    )
    assert result.status == "error"
    assert result.exit_code == 20  # EXIT_STOPPED
