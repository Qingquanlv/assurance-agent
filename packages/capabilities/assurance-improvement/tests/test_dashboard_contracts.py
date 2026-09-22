from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_improvement.contracts.dashboard import (
    DashboardCandidate,
    DashboardDomain,
    DashboardMetric,
    DashboardRun,
    DashboardSignal,
    DashboardSourceRefs,
    DashboardStages,
    DashboardVerification,
    RetroDashboardV1,
)


def _source_refs(**overrides: object) -> DashboardSourceRefs:
    payload: dict[str, object] = {
        "problem_ids": (),
        "occurrence_ids": (),
        "issue_event_ids": (),
        "workflow_evidence_ids": (),
        "eval_run_ids": (),
    }
    payload.update(overrides)
    return DashboardSourceRefs.model_validate(payload)


def test_empty_dashboard_round_trips() -> None:
    doc = RetroDashboardV1(
        stages=DashboardStages(analyses=False, synthesis=False, reconcile=False),
        run=DashboardRun(),
    )
    dumped = doc.model_dump(mode="json")
    assert dumped["schema_version"] == "1"
    assert dumped["change_id"] is None
    assert dumped["stages"] == {"analyses": False, "synthesis": False, "reconcile": False}
    assert dumped["run"]["result"] is None
    assert dumped["run"]["integrity_reasons"] == []
    assert dumped["domains"] == []
    assert dumped["signals"] == []
    assert dumped["candidates"] == []
    assert RetroDashboardV1.model_validate(dumped) == doc


def test_full_dashboard_round_trips() -> None:
    signal = DashboardSignal(
        signal_id="task-failure-1",
        signal_type="task_failure",
        domain="workflow",
        summary="1 technical failure(s) at intake.case-review",
        occurrence_count=1,
        confidence="high",
        recommended_change="Review the node contract.",
        metrics=(DashboardMetric(label="node", value="intake.case-review"),),
        source_refs=_source_refs(workflow_evidence_ids=("attempt-failure-1",)),
        cited_by_candidate_ids=("cand-1",),
    )
    domain = DashboardDomain(
        domain="workflow",
        status="ok",
        failure_reason=None,
        signal_count=1,
        source_count=2,
        source_kinds={"loop_round_history": 1, "workflow_ledger": 1},
        slice_sha256="b" * 64,
    )
    candidate = DashboardCandidate(
        candidate_id="cand-1",
        kind="workflow_improvement",
        delivery="change_draft",
        target="intake.case-review",
        rationale="…",
        proposed_change="…",
        risk="medium",
        confidence="high",
        signal_ids=("task-failure-1",),
        verification=DashboardVerification(suites=("assurance-execution",), success_criteria="…"),
        source_refs=_source_refs(workflow_evidence_ids=("attempt-failure-1",)),
        has_knowledge_delta=False,
        improvement_id="IMP-1",
        improvement_state="proposed",
        improvement_version=1,
    )
    doc = RetroDashboardV1(
        change_id="CH-1",
        retro_id="retro-1",
        generated_at="2026-09-16T12:19:50.482167+00:00",
        dry_run=False,
        stages=DashboardStages(analyses=True, synthesis=True, reconcile=True),
        run=DashboardRun(
            result="completed_with_gaps",
            integrity_status="incomplete",
            integrity_reasons=("issue_evidence_absent",),
            signal_count=2,
            candidate_count=1,
            window_change_ids=("CH-1",),
            selection_mode="change_ids",
            improvement_ids=("IMP-1",),
        ),
        domains=(domain,),
        signals=(signal,),
        candidates=(candidate,),
    )
    dumped = doc.model_dump(mode="json")
    assert RetroDashboardV1.model_validate(dumped) == doc


def test_models_are_frozen_and_reject_unknown_fields() -> None:
    stages = DashboardStages(analyses=True, synthesis=True, reconcile=True)
    with pytest.raises(ValidationError):
        stages.analyses = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DashboardStages.model_validate({"analyses": True, "synthesis": True, "reconcile": True, "extra": 1})


def test_domain_status_absent_is_a_valid_state() -> None:
    domain = DashboardDomain(domain="discovery", status="absent", source_count=0, source_kinds={})
    assert domain.signal_count is None
    assert domain.slice_sha256 is None
