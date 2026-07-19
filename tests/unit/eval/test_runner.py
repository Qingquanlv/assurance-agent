from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

from tests.helpers_aa import write_aa_config

import yaml
import pytest

from assurance_agent.eval import runner as runner_mod
from assurance_agent.eval.runner import run_suite
from assurance_agent.eval.types import JudgeOutput
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult


class _UnusedStatusStub:
    """Legacy LoopRunner stubs retained only so dead local helpers still type-check."""


class WorkflowStatus(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class PhaseView(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Terminal(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class DispatchEntry(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _completed_runtime_factory(sut_dir: Path | None = None):
    from assurance_agent.workflow.core.exit_codes import EXIT_COMPLETED
    from assurance_agent.workflow.graph.models import GraphStatus, RunResult

    class _RT:
        def run(self, compiled, entrypoint, context):  # noqa: ANN001, ANN201
            # Ensure scorer sees a pass review artifact.
            change = context.change_dir
            review = change / "review"
            review.mkdir(parents=True, exist_ok=True)
            (review / "case-review.json").write_text('{"decision":"pass"}', encoding="utf-8")
            status = GraphStatus(
                invocation_id="i",
                entrypoint=entrypoint,
                status="completed",
                checkpoint_id="c",
                event_seq=1,
                superstep=1,
                running_tasks=(),
                pending_tasks=(),
                pending_write_sets=(),
                pending_interrupts=(),
                next_retry_at=None,
                budgets={},
                terminal_reason="ok",
            )
            return RunResult(invocation_id="i", status=status, exit_code=EXIT_COMPLETED, reason="ok")

        def import_checkpoint(self, *a, **k):  # noqa: ANN001, ANN201
            return self.run(None, "case", a[2] if len(a) > 2 else k.get("context"))

        def status(self, invocation_id: str):  # noqa: ANN201
            from assurance_agent.workflow.graph.models import GraphStatus

            return GraphStatus(
                invocation_id=invocation_id,
                entrypoint="case",
                status="completed",
                checkpoint_id="c",
                event_seq=1,
                superstep=1,
                running_tasks=(),
                pending_tasks=(),
                pending_write_sets=(),
                pending_interrupts=(),
                next_retry_at=None,
                budgets={},
                terminal_reason="ok",
            )

    class _Bundle:
        runtime = _RT()
        compiled = object()

    def factory(**_: object):
        return _Bundle()

    return factory


def _git_init(repo: Path) -> None:
    # Fresh throwaway repo inside the pytest tmp dir — write-scan requires the
    # SUT workspace to be a git repo (same precondition as the TS executor).
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)


def _seed_suite(project_root: Path) -> Path:
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True)
    suite_file = suites / "workflow-case.yaml"
    suite_file.write_text(
        yaml.safe_dump(
            {
                "name": "workflow-case",
                "scorer": "workflow-case",
                "executor": {"type": "workflow-run", "entrypoint": "case"},
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
    write_aa_config(tmp_path)
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_suite(project_root)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    _git_init(sut)
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
        runtime_factory=_completed_runtime_factory(),
    )
    assert gate.verdict == "pass"
    run_dir = sut / "eval" / "out" / "runs" / run_id
    assert (run_dir / "metrics.json").exists()
    assert (run_dir / "gate-result.json").exists()
    assert (run_dir / "report.json").exists()
    judge = run_dir / "samples/WC-001/attempt-0/judge-result.json"
    assert json.loads(judge.read_text())["label"] == "covered"


def test_run_suite_overlays_extra_memory_into_attempt_sandbox(tmp_path: Path) -> None:
    """extra_memory_dir is merged into the sandbox .aa/memory the agent loads."""
    write_aa_config(tmp_path)
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_suite(project_root)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    _git_init(sut)
    seed_change = sut / "qa/changes/eval-sample-001"
    seed_change.mkdir(parents=True)
    (seed_change / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")

    overlay = tmp_path / "overlay"
    (overlay / ".aa" / "memory").mkdir(parents=True)
    (overlay / ".aa" / "memory" / "RETRO-001.md").write_text("# candidate memory\n", encoding="utf-8")

    seen = {"memory_present": False}

    class MemoryProbeAdapter:
        def __init__(self, workspace: Path) -> None:
            self.workspace = workspace
            self.change = workspace / "qa/changes/eval-sample-001"

        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            seen["memory_present"] = (self.workspace / ".aa" / "memory" / "RETRO-001.md").is_file()
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
                            phase_id="case-design", skill="aa-case-design", agent=None, kind="skill"
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
        adapter_factory=lambda sut_dir, **_: MemoryProbeAdapter(sut_dir),
        runtime_factory=_completed_runtime_factory(),
        extra_memory_dir=overlay,
    )
    assert gate.verdict == "pass"
    # Overlay is copied before runtime; assert on attempt sandbox files.
    attempt_mem = sut / "eval/out/runs" / run_id / "samples/WC-001/attempt-0/sut/.aa/memory/RETRO-001.md"
    assert attempt_mem.is_file()
    manifest = json.loads((sut / "eval/out/runs" / run_id / "manifest.json").read_text())
    file_hash = hashlib.sha256(b"# candidate memory\n").hexdigest()
    canonical = f".aa/memory/RETRO-001.md:{file_hash}"
    assert manifest["memory_overlay_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()


def test_run_suite_rejects_symlinked_memory_overlay(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_case_generation_suite(project_root, judge=None)
    sut = tmp_path / "sut"
    sut.mkdir()
    overlay = tmp_path / "overlay"
    memory = overlay / ".aa" / "memory"
    memory.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("secret\n", encoding="utf-8")
    (memory / "escape.md").symlink_to(secret)

    with pytest.raises(AaError, match="symlink"):
        run_suite(
            suite_file=suite_file,
            project_root=project_root,
            sut_dir=sut,
            extra_memory_dir=overlay,
        )


def test_run_suite_repeat_uses_isolated_workspaces_and_unique_score_keys(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_suite(project_root)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    _git_init(sut)
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
        runtime_factory=_completed_runtime_factory(),
    )
    assert gate.verdict == "pass"
    metrics = json.loads((sut / "eval/out/runs" / run_id / "metrics.json").read_text())
    assert set(metrics["per_sample"]) == {"WC-001#attempt-0", "WC-001#attempt-1"}
    manifest = json.loads((sut / "eval/out/runs" / run_id / "manifest.json").read_text())
    assert manifest["repeat"] == 2


def _seed_case_generation_suite(project_root: Path, *, judge: dict | None) -> Path:
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True)
    suite_file = suites / "case-generation.yaml"
    suite_def: dict = {
        "name": "case-generation",
        "scorer": "case-generation",
        "executor": {"type": "in_process"},
        "thresholds": [
            {"metric": "requirement_f1", "gate": "advisory", "op": "gte", "value": 0.85},
        ],
    }
    if judge is not None:
        suite_def["judge"] = judge
    suite_file.write_text(yaml.safe_dump(suite_def), encoding="utf-8")
    ds = project_root / "eval" / "datasets" / "case-generation"
    ds.mkdir(parents=True)
    (ds / "CG-001.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "CG-001",
                "suite": "case-generation",
                "input": {},
                "expected": {"human_label": "covered"},
                "mock_judge_label": "covered",
            }
        ),
        encoding="utf-8",
    )
    return suite_file


def test_run_suite_with_judge_config_writes_judge_result_before_scoring(tmp_path: Path, monkeypatch) -> None:
    """Regression (user-reported): judge runs before scoring, so the scorer's P/R/F1 is non-zero."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_case_generation_suite(
        project_root,
        judge={"model": "judge-x", "temperature": 0.0, "confidence_threshold": 0.8},
    )
    sut = tmp_path / "sut"
    sut.mkdir()
    monkeypatch.setenv("AA_JUDGE_MOCK", "1")

    run_id, gate = run_suite(suite_file=suite_file, project_root=project_root, sut_dir=sut)

    assert gate.verdict == "pass"
    run_dir = sut / "eval" / "out" / "runs" / run_id
    judge_path = run_dir / "samples/CG-001/attempt-0/judge-result.json"
    assert json.loads(judge_path.read_text())["label"] == "covered"
    metrics = json.loads((run_dir / "metrics.json").read_text())
    per_sample = metrics["per_sample"]["CG-001#attempt-0"]
    assert per_sample["requirement_precision"] == 1.0
    assert per_sample["requirement_recall"] == 1.0
    assert per_sample["requirement_f1"] == 1.0


def test_run_suite_without_judge_config_skips_judge(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_case_generation_suite(project_root, judge=None)
    sut = tmp_path / "sut"
    sut.mkdir()
    monkeypatch.setenv("AA_JUDGE_MOCK", "1")

    run_id, _ = run_suite(suite_file=suite_file, project_root=project_root, sut_dir=sut)

    run_dir = sut / "eval" / "out" / "runs" / run_id
    assert not (run_dir / "samples/CG-001/attempt-0/judge-result.json").exists()
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["per_sample"]["CG-001#attempt-0"]["requirement_f1"] == 0.0


def test_run_suite_judge_failure_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """Judge errors (here: no API url configured) become per-sample error scores, not crashes."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    suite_file = _seed_case_generation_suite(project_root, judge={"model": "judge-x"})
    sut = tmp_path / "sut"
    sut.mkdir()
    monkeypatch.delenv("AA_JUDGE_MOCK", raising=False)
    monkeypatch.delenv("AA_JUDGE_API_URL", raising=False)

    run_id, gate = run_suite(suite_file=suite_file, project_root=project_root, sut_dir=sut)

    run_dir = sut / "eval" / "out" / "runs" / run_id
    assert not (run_dir / "samples/CG-001/attempt-0/judge-result.json").exists()
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["sample_count"] == 1
    assert metrics["per_sample"]["CG-001#attempt-0"] == {}
    assert gate.verdict == "inconclusive"
