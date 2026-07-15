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
    DispatchEntry,
    PhaseView,
    Terminal,
    WorkflowStatus,
)


def _seed_suite(project_root: Path) -> Path:
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True)
    suite_file = suites / "workflow-case.yaml"
    suite_file.write_text(
        yaml.safe_dump(
            {
                "name": "workflow-case",
                "scorer": "workflow-case",
                "executor": {"type": "workflow-run", "scope": "full"},
                "thresholds": [
                    {"metric": "case_review_gate_pass_rate", "gate": "hard", "op": "gte", "value": 0.99},
                    {"metric": "secret_leak_count", "gate": "hard", "op": "eq", "value": 0.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    ds = project_root / "eval" / "datasets" / "workflow-case"
    ds.mkdir(parents=True)
    (ds / "WC-001.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "WC-001",
                "suite": "workflow-case",
                "input": {"change_id": "eval-sample-001", "run_mode": "case-only"},
                "expected": {},
            }
        ),
        encoding="utf-8",
    )
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

    def status_provider() -> WorkflowStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            return WorkflowStatus(
                phases=[PhaseView(id="case-design", status="ready")],
                next_dispatch=[
                    DispatchEntry(phase_id="case-design", skill="aa-case-design", agent=None, kind="skill")
                ],
                terminal=None,
            )
        return WorkflowStatus(
            phases=[PhaseView(id="case-design", status="done")],
            next_dispatch=[],
            terminal=Terminal(kind="completed", reason="ok"),
        )

    monkeypatch.setenv("AA_JUDGE_MODEL", "judge-model")
    monkeypatch.setattr(
        runner_mod,
        "run_judge",
        lambda *a, **k: JudgeOutput(label="covered", confidence=0.9),
    )

    run_id, gate = run_suite(
        suite_file=suite_file,
        project_root=project_root,
        sut_dir=sut,
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

    class IsolatedAdapter:
        def __init__(self, workspace: Path, attempt: int) -> None:
            self.change = workspace / "qa/changes/eval-sample-001"
            marker = self.change / "attempt-marker"
            assert not marker.exists()
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
            return PhaseResult(ok=True)

    def status_factory(**_):
        calls = {"n": 0}

        def status() -> WorkflowStatus:
            calls["n"] += 1
            if calls["n"] == 1:
                return WorkflowStatus(
                    phases=[PhaseView(id="case-design", status="ready")],
                    next_dispatch=[
                        DispatchEntry(
                            phase_id="case-design",
                            skill="aa-case-design",
                            agent=None,
                            kind="skill",
                        )
                    ],
                    terminal=None,
                )
            return WorkflowStatus(
                phases=[PhaseView(id="case-design", status="done")],
                next_dispatch=[],
                terminal=Terminal(kind="completed"),
            )

        return status

    run_id, gate = run_suite(
        suite_file=suite_file,
        project_root=project_root,
        sut_dir=sut,
        repeat=2,
        adapter_factory=lambda sut_dir, attempt, **_: IsolatedAdapter(sut_dir, attempt),
        status_provider_factory=status_factory,
        cli_executor_factory=lambda **_: AuditOutcome(),
    )
    assert gate.verdict == "pass"
    metrics = json.loads((project_root / "eval/out/runs" / run_id / "metrics.json").read_text())
    assert set(metrics["per_sample"]) == {"WC-001#attempt-0", "WC-001#attempt-1"}
