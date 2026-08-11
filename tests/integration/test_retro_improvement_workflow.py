"""End-to-end Retro v3 → Improvement closed-loop assertions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.issues.history import IssueHistoryIntegrityError
from tests.unit.workflow.graph.test_retro_workflow import (
    EmptyAnalysisInvoker,
    FakeRetroAgent,
    _build_runtime,
    _compile_canonical,
    _fake_retro_collect_with_signal,
    _make_project,
    _retro_context,
    _v2_context,
)


class _PassingImprovementReviewer(FakeRetroAgent):
    """Runs the normal Retro agents and emits one bound Auto Review assessment."""

    def invoke(self, request: AgentRequest) -> AgentResult:
        if request.target != "skill:aa-improvement-reviewer":
            return super().invoke(request)
        subjects = tuple((request.workspace_root / "qa/improvements/review-subjects").glob("*.json"))
        if not subjects:
            raise AssertionError(
                f"review subject missing; writes={request.allowed_writes}; "
                f"json={tuple(request.workspace_root.rglob('*.json'))}"
            )
        subject_path = subjects[0]
        subject = json.loads(subject_path.read_text(encoding="utf-8"))
        identity = f"{subject['improvement_id']}:{subject_path.stem}:1:1"
        review_id = "AUTO-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        assessment_path = (
            request.workspace_root / "qa" / "improvements" / "reviews" / review_id / "assessment.json"
        )
        assessment_path.parent.mkdir(parents=True, exist_ok=True)
        assessment_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "review_type": "improvement",
                    "decision": "pass",
                    "findings": [],
                    "evidence_traceability": "complete",
                    "scope_readiness": "ready",
                    "verification_readiness": "ready",
                    "delivery_safety": "ready",
                    "human_review_required": False,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (assessment_path.parent / "summary.md").write_text(
            "# Improvement review\n\nEligible for bounded automatic approval.\n",
            encoding="utf-8",
        )
        return AgentResult(ok=True)


def test_zero_signal_runs_all_analyzers_and_writes_noop_receipt(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, EmptyAnalysisInvoker())

    result = runtime.run(compiled, "retro", _retro_context(project, "retro-zero"))

    assert result.exit_code == 0, result.reason
    retro_dir = project / "qa/retro/retro-zero"
    assert json.loads((retro_dir / "context.json").read_text())["signal_count"] == 0
    assert json.loads((retro_dir / "proposal-candidates.json").read_text())["candidates"] == []
    assert "no_actionable_signals" in (retro_dir / "retro-summary.md").read_text()


def test_corrupt_issue_ledger_recovers_to_pipeline_improvement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise IssueHistoryIntegrityError("corrupt Issue Ledger")

    monkeypatch.setattr(
        "assurance_agent.workflow.retro_ops.materialize_slices",
        _boom,
    )
    project = _make_project(tmp_path)
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, EmptyAnalysisInvoker())

    result = runtime.run(compiled, "retro", _retro_context(project, "retro-corrupt"))

    assert result.status.status == "completed"
    retro_dir = project / "qa/retro/retro-corrupt"
    assert (retro_dir / "pipeline-failure.json").is_file()
    assert json.loads((retro_dir / "retro-status.json").read_text())["result"] == "completed_with_gaps"
    assert (project / "qa/improvements/events.jsonl").is_file()


def test_successful_v3_run_proposes_and_reconciles_improvement(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    retro_id = "retro-success"
    compiled, contracts = _compile_canonical()
    invoker = FakeRetroAgent(retro_id, context=_v2_context(retro_id))
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops={"operation:retro-collect-v3": _fake_retro_collect_with_signal},
    )

    result = runtime.run(compiled, "retro", _retro_context(project, retro_id))

    assert result.exit_code == 0, result.reason
    retro_dir = project / "qa/retro" / retro_id
    for name in (
        "context.json",
        "proposal-candidates.json",
        "retro-summary.md",
        "accept-status.json",
        "review-queue.md",
    ):
        assert (retro_dir / name).is_file(), name
    assert json.loads((retro_dir / "context.json").read_text())["schema_version"] == "3"
    assert json.loads((retro_dir / "proposal-candidates.json").read_text())["schema_version"] == "3"
    assert json.loads((retro_dir / "accept-status.json").read_text())["result"] == "accepted"
    assert (project / "qa/improvements/events.jsonl").is_file()


def test_eligible_current_retro_improvement_is_automatically_approved(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    retro_id = "retro-auto-approved"
    compiled, contracts = _compile_canonical()
    invoker = _PassingImprovementReviewer(retro_id, context=_v2_context(retro_id))
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops={"operation:retro-collect-v3": _fake_retro_collect_with_signal},
    )

    result = runtime.run(compiled, "retro", _retro_context(project, retro_id))

    assert result.exit_code == 0, result.reason
    projection = json.loads((project / "qa/improvements/improvements.json").read_text())
    improvement = next(iter(projection["improvements"].values()))
    assert improvement["state"] == "approved"
    assert improvement["approval_source"] == "automatic"
    summary = json.loads((project / "qa/retro" / retro_id / "auto-review-summary.json").read_text())
    assert summary["approved"] == 1
    assert summary["review_ids"]


class _InvalidProposer(FakeRetroAgent):
    def invoke(self, request: AgentRequest) -> AgentResult:
        if request.target != "skill:aa-retro":
            return super().invoke(request)
        retro_dir = request.workspace_root / "qa/retro" / self._retro_id  # noqa: SLF001
        (retro_dir / "proposal-candidates.json").write_text("{not-json\n", encoding="utf-8")
        (retro_dir / "retro-summary.md").write_text("# invalid\n", encoding="utf-8")
        return AgentResult(ok=True)


def test_invalid_v3_candidate_recovers_to_pipeline_improvement(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    retro_id = "retro-invalid"
    compiled, contracts = _compile_canonical()
    invoker = _InvalidProposer(retro_id, context=_v2_context(retro_id))
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops={"operation:retro-collect-v3": _fake_retro_collect_with_signal},
    )

    result = runtime.run(compiled, "retro", _retro_context(project, retro_id))

    assert result.status.status == "completed"
    retro_dir = project / "qa/retro" / retro_id
    assert (retro_dir / "pipeline-failure.json").is_file()
    assert json.loads((retro_dir / "retro-status.json").read_text())["result"] == "completed_with_gaps"
    assert (project / "qa/improvements/events.jsonl").is_file()


def test_temporary_reconcile_failure_leaves_durable_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path)
    retro_id = "retro-pending"
    compiled, contracts = _compile_canonical()
    invoker = FakeRetroAgent(retro_id, context=_v2_context(retro_id))
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops={"operation:retro-collect-v3": _fake_retro_collect_with_signal},
    )
    calls = 0

    def fail_second_drain(_root: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ()
        raise OSError("registry temporarily unavailable")

    monkeypatch.setattr(
        "assurance_agent.workflow.retro_ops.drain_reconcile_outbox",
        fail_second_drain,
    )

    result = runtime.run(compiled, "retro", _retro_context(project, retro_id))

    assert result.exit_code == 0, result.reason
    pending = tuple((project / "qa/improvements/outbox/pending").glob("*.json"))
    assert len(pending) == 1
    assert not (project / "qa/improvements/events.jsonl").exists()
