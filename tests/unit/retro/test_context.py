"""Schema-v2 RetroContext aggregation through injected typed readers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.artifacts.models.issues import (
    IssueAnalysisStatus,
    IssueOccurrence,
    OccurrenceAnalysis,
    Observation,
    ObservationSource,
    ProblemFingerprint,
    ProvisionalAssessment,
)
from assurance_agent.retro.context import (
    RetroContextImmutableError,
    assert_signal_refs_resolve,
    build_retro_context,
    count_signals,
)
from assurance_agent.retro.eval_history import (
    EvalEvidenceSlice,
    EvalReportRecord,
    InMemoryEvalHistoryReader,
)
from assurance_agent.retro.types import RetroIntegrity, RetroSignal, RetroSignalSet
from assurance_agent.retro.window import RetroWindowSelection
from assurance_agent.retro.workflow_history import (
    GateVerdictRecord,
    HealingAllocationRecord,
    HealingApplyRecord,
    InMemoryWorkflowHistoryReader,
    SkillLoadedFalseRecord,
    TerminalChangeRef,
    WorkflowEvidenceSlice,
    WorkflowTaskFailureRecord,
)
from assurance_agent.workflow.issues.events import (
    CHANGE_ISSUE_EVENT_ADAPTER,
    PROBLEM_EVENT_ADAPTER,
    ChangeIssueEvent,
    ProblemEvent,
)
from assurance_agent.workflow.issues.history import InMemoryIssueHistoryReader
from assurance_agent.workflow.issues.history_models import IssueTypedEvents
from assurance_agent.workflow.issues.ledger import ChangeIssueStore, ProjectProblemStore
from tests.helpers_aa import write_aa_config

PROB_1 = "PROB-1"
PROB_2 = "PROB-2"
OCC_1 = "OCC-1"
OCC_2 = "OCC-2"
OBS_1 = "OBS-1"
OBS_2 = "OBS-2"
FINGERPRINT = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
FINGERPRINT_B = ProblemFingerprint(version="1", digest="sha256:" + "b" * 64)


def _ce(
    *,
    seq: int,
    event_id: str,
    type_: str,
    change_id: str,
    ts: str = "2026-07-25T10:00:00Z",
    batch_id: str = "B-001",
    **extra: object,
) -> ChangeIssueEvent:
    return CHANGE_ISSUE_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id,
            "idempotency_key": f"IDEM-{event_id}",
            "ts": ts,
            "evidence_digest": "sha256:aabbccdd",
            "type": type_,
            "change_id": change_id,
            "batch_id": batch_id,
            **extra,
        }
    )


def _pe(
    *,
    seq: int,
    event_id: str,
    type_: str,
    problem_id: str = PROB_1,
    expected_problem_version: int = 0,
    ts: str = "2026-07-25T10:05:00Z",
    **extra: object,
) -> ProblemEvent:
    return PROBLEM_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id,
            "idempotency_key": f"IDEM-{event_id}",
            "ts": ts,
            "evidence_digest": "sha256:aabbccdd",
            "type": type_,
            "problem_id": problem_id,
            "expected_problem_version": expected_problem_version,
            **extra,
        }
    )


def _observation(observation_id: str, change_id: str, *, kind: str = "test_failure") -> Observation:
    return Observation(
        observation_id=observation_id,
        change_id=change_id,
        batch_id="B-001",
        kind=kind,  # type: ignore[arg-type]
        target="api",
        case_id=None,
        source=ObservationSource(artifact="execution/runs/B-001/api-result.json", json_pointer="/cases/0"),
        evidence_refs=["execution/runs/B-001/api-result.json"],
        signature=f"sig-{observation_id}",
        observed_at="2026-07-25T10:00:00Z",
    )


def _occurrence(
    occurrence_id: str,
    change_id: str,
    problem_id: str,
    observation_id: str,
    *,
    classification: str = "product_bug",
) -> IssueOccurrence:
    return IssueOccurrence(
        occurrence_id=occurrence_id,
        change_id=change_id,
        batch_id="B-001",
        observation_ids=[observation_id],
        problem_id=problem_id,
        provisional_assessment=ProvisionalAssessment(
            classification=classification,  # type: ignore[arg-type]
            severity="high",
            authority="llm_provisional",
            root_cause_hypothesis="null pointer",
        ),
        analysis=OccurrenceAnalysis(
            evidence_bundle_digest="sha256:aabbccdd",
            analyzer="aa-issue-analyzer",
            prompt_version="v1",
            candidate_digest="sha256:11223344",
        ),
    )


def _change_events(
    change_id: str,
    *,
    observation_id: str,
    occurrence_id: str,
    problem_id: str,
    classification: str = "product_bug",
    analysis_failed: bool = False,
) -> list[ChangeIssueEvent]:
    obs = _observation(observation_id, change_id)
    occ = _occurrence(occurrence_id, change_id, problem_id, observation_id, classification=classification)
    analysis = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=change_id,
        batch_id="B-001",
        status="failed" if analysis_failed else "completed",
        evidence_bundle_digest="sha256:aabbccdd",
        candidate_count=0 if analysis_failed else 1,
        candidate_digest=None if analysis_failed else "sha256:11223344",
        reason="timeout" if analysis_failed else None,
        retryable=True if analysis_failed else None,
    )
    events = [
        _ce(
            seq=1,
            event_id=f"CEVT-{change_id}-1",
            type_="observation_recorded",
            change_id=change_id,
            observation=obs.model_dump(mode="json"),
        ),
        _ce(
            seq=2,
            event_id=f"CEVT-{change_id}-2",
            type_="issue_analysis_failed" if analysis_failed else "issue_analysis_completed",
            change_id=change_id,
            analysis_status=analysis.model_dump(mode="json"),
            ts="2026-07-25T10:01:00Z",
        ),
    ]
    if not analysis_failed:
        events.append(
            _ce(
                seq=3,
                event_id=f"CEVT-{change_id}-3",
                type_="occurrence_detected",
                change_id=change_id,
                occurrence=occ.model_dump(mode="json"),
                ts="2026-07-25T10:02:00Z",
            )
        )
    return events


def _rich_problem_events() -> list[ProblemEvent]:
    return [
        _pe(
            seq=1,
            event_id="PEVT-1",
            type_="problem_detected",
            occurrence_id=OCC_1,
            change_id="RET-1",
            batch_id="B-001",
            fingerprint=FINGERPRINT.model_dump(mode="json"),
            title="HTTP 500",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="null pointer",
            ts="2026-07-25T10:03:00Z",
        ),
        _pe(
            seq=2,
            event_id="PEVT-2",
            type_="problem_assessment_confirmed",
            expected_problem_version=1,
            classification="test_bug",
            severity="medium",
            root_cause_hypothesis="fixture drift",
            reason="human corrected classification",
            evidence_refs=["review/note-1"],
            ts="2026-07-25T11:00:00Z",
        ),
        _pe(
            seq=3,
            event_id="PEVT-3",
            type_="problem_work_started",
            expected_problem_version=2,
            reason="fix started",
            evidence_refs=["ticket-1"],
            ts="2026-07-25T12:00:00Z",
        ),
        _pe(
            seq=4,
            event_id="PEVT-4",
            type_="problem_resolved",
            expected_problem_version=3,
            resolved_at="2026-07-25T14:00:00Z",
            change_id="RET-1",
            batch_id="B-001",
            disposition="fixed",
            verification_scope=["api"],
            ts="2026-07-25T14:00:00Z",
        ),
        _pe(
            seq=5,
            event_id="PEVT-5",
            type_="problem_detected",
            problem_id=PROB_2,
            expected_problem_version=0,
            occurrence_id=OCC_2,
            change_id="RET-2",
            batch_id="B-001",
            fingerprint=FINGERPRINT_B.model_dump(mode="json"),
            title="flaky locator",
            classification="test_bug",
            severity="low",
            ts="2026-07-25T15:00:00Z",
        ),
        _pe(
            seq=6,
            event_id="PEVT-6",
            type_="problem_marked_not_an_issue",
            problem_id=PROB_2,
            expected_problem_version=1,
            reason="expected product behavior",
            evidence_refs=["review/note-2"],
            ts="2026-07-25T16:00:00Z",
        ),
        _pe(
            seq=7,
            event_id="PEVT-7",
            type_="problem_regressed",
            expected_problem_version=4,
            occurrence_id=OCC_1,
            change_id="RET-2",
            ts="2026-07-25T17:00:00Z",
        ),
    ]


def _typed_events(*, analysis_failed: bool = False) -> IssueTypedEvents:
    change_events = {
        "RET-1": _change_events(
            "RET-1",
            observation_id=OBS_1,
            occurrence_id=OCC_1,
            problem_id=PROB_1,
            analysis_failed=analysis_failed,
        ),
        "RET-2": _change_events(
            "RET-2",
            observation_id=OBS_2,
            occurrence_id=OCC_2,
            problem_id=PROB_2,
            classification="test_bug",
        ),
    }
    problem_events = [] if analysis_failed else _rich_problem_events()
    # Serialize via stores for digest-stable ledger bytes without writing disk in callers.
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_aa_config(root)
        change_bytes: dict[str, bytes] = {}
        for change_id, events in change_events.items():
            store_root = root / "qa" / "archive" / change_id
            store_root.mkdir(parents=True)
            ChangeIssueStore(store_root).append_and_rebuild(events)
            change_bytes[change_id] = (store_root / "issues" / "events.jsonl").read_bytes()
        if problem_events:
            ProjectProblemStore(root).append_and_rebuild(problem_events)
            problem_bytes = (root / "qa" / "issues" / "events.jsonl").read_bytes()
        else:
            problem_bytes = b""
    return IssueTypedEvents(
        change_events={cid: tuple(evs) for cid, evs in change_events.items()},
        problem_events=tuple(problem_events),
        change_ledger_bytes=change_bytes,
        problem_ledger_bytes=problem_bytes,
    )


def _workflow_slice() -> WorkflowEvidenceSlice:
    return WorkflowEvidenceSlice(
        change_ids=("RET-1", "RET-2"),
        sources=(),
        integrity=RetroIntegrity(status="complete"),
        gate_verdicts=(
            GateVerdictRecord(
                change_id="RET-1",
                seq=2,
                ts="2026-07-25T09:00:00Z",
                gate="case-review",
                verdict="needs_fix",
                reason="unresolved findings",
                evidence_id="RET-1#seq2",
            ),
        ),
        healing_allocations=(
            HealingAllocationRecord(
                change_id="RET-1",
                seq=3,
                ts="2026-07-25T09:01:00Z",
                operation_id="op-1",
                evidence_id="RET-1#seq3",
            ),
        ),
        healing_applies=(
            HealingApplyRecord(
                change_id="RET-1",
                target="api",
                applied=True,
                evidence_id="RET-1#healing:api-apply-summary.json",
            ),
        ),
        skill_loaded_false=(
            SkillLoadedFalseRecord(
                change_id="RET-2",
                phase="inspect",
                evidence_id="RET-2#workflow-state:inspect",
            ),
        ),
        task_failures=(
            WorkflowTaskFailureRecord(
                change_id="RET-1",
                node_id="report",
                error_kind="internal",
                message="report renderer crashed",
                recovered=False,
                evidence_id="RET-1#seq4",
            ),
        ),
    )


def _eval_slice() -> EvalEvidenceSlice:
    return EvalEvidenceSlice(
        reports=(
            EvalReportRecord(
                run_id="eval-run-1",
                suite="workflow-full",
                verdict="pass",
                started_at="2026-07-25T08:00:00Z",
                sha256="sha256:" + "c" * 64,
                source_change_ids=("RET-1",),
            ),
        ),
        sources=(),
        integrity=RetroIntegrity(status="complete"),
    )


@dataclass(frozen=True)
class _Readers:
    issues: InMemoryIssueHistoryReader
    workflow: InMemoryWorkflowHistoryReader
    eval: InMemoryEvalHistoryReader


@pytest.fixture
def readers() -> _Readers:
    typed = _typed_events()
    wf = _workflow_slice()
    # Populate workflow sources with evidence ids so manifest resolvability works.
    from assurance_agent.retro.types import RetroSourceDescriptor

    wf = wf.model_copy(
        update={
            "sources": (
                RetroSourceDescriptor(
                    kind="workflow_ledger",
                    change_id="RET-1",
                    head_event_id="RET-1#seq3",
                    sha256="sha256:wf1",
                    evidence_ids=(
                        "RET-1#seq2",
                        "RET-1#seq3",
                        "RET-1#seq4",
                        "RET-1#healing:api-apply-summary.json",
                    ),
                ),
                RetroSourceDescriptor(
                    kind="workflow_ledger",
                    change_id="RET-2",
                    head_event_id="RET-2#seq1",
                    sha256="sha256:wf2",
                    evidence_ids=("RET-2#workflow-state:inspect",),
                ),
            )
        }
    )
    ev = _eval_slice()
    from assurance_agent.retro.types import RetroSourceDescriptor as RSD

    ev = ev.model_copy(
        update={
            "sources": (
                RSD(
                    kind="eval_run",
                    head_event_id="eval-run-1",
                    sha256="sha256:" + "c" * 64,
                    evidence_ids=("eval-run-1",),
                ),
            )
        }
    )
    return _Readers(
        issues=InMemoryIssueHistoryReader.from_events(typed),
        workflow=InMemoryWorkflowHistoryReader(
            terminals=(
                TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-25T18:00:00Z"),
                TerminalChangeRef(change_id="RET-2", terminal_ts="2026-07-25T19:00:00Z"),
            ),
            slice_=wf,
            known_ids=frozenset({"RET-1", "RET-2"}),
        ),
        eval=InMemoryEvalHistoryReader.from_slice(ev),
    )


def test_context_aggregates_issue_signals_without_problem_copies(readers: _Readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.schema_version == "2"
    assert context.signals.issue.occurrence_trends[0].source_refs.problem_ids == (PROB_1,)
    assert "severity" not in context.signals.issue.occurrence_trends[0].model_dump()


def test_context_aggregates_assessment_corrections(readers: _Readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    corrections = context.signals.issue.assessment_corrections
    assert corrections
    assert corrections[0].source_refs.problem_ids == (PROB_1,)
    assert corrections[0].source_refs.issue_event_ids == ("PEVT-2",)
    assert "severity" not in corrections[0].model_dump()


def test_context_aggregates_regressions_and_review_and_resolution(readers: _Readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.signals.issue.regressions
    assert context.signals.issue.regressions[0].source_refs.problem_ids == (PROB_1,)
    assert context.signals.issue.review_decision_patterns
    assert context.signals.issue.resolution_outcomes
    outcomes = {s.metrics.get("outcome") for s in context.signals.issue.resolution_outcomes}
    assert "resolved" in outcomes


def test_context_aggregates_repeated_not_an_issue(readers: _Readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    patterns = context.signals.issue.repeated_not_an_issue
    assert patterns
    assert PROB_2 in patterns[0].source_refs.problem_ids


def test_context_aggregates_workflow_and_eval_signals(readers: _Readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.signals.workflow.gate_pushback
    assert context.signals.workflow.gate_pushback[0].source_refs.workflow_evidence_ids == ("RET-1#seq2",)
    assert context.signals.workflow.healing_efficiency
    he = context.signals.workflow.healing_efficiency[0]
    assert he.metrics["attempts"] == 1
    assert he.metrics["applied"] == 1
    assert context.signals.workflow.skill_execution_drift
    assert len(context.signals.workflow.task_failures) == 1
    failure = context.signals.workflow.task_failures[0]
    assert failure.source_refs.workflow_evidence_ids == ("RET-1#seq4",)
    assert failure.metrics == {
        "node_id": "report",
        "error_kind": "internal",
        "recovered": "false",
        "count": 1,
        "sample_message": "report renderer crashed",
    }
    assert context.signals.eval.trends
    assert context.signals.eval.trends[0].source_refs.eval_run_ids == ("eval-run-1",)


def test_signal_source_refs_resolve_against_manifest(readers: _Readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert_signal_refs_resolve(context.signals, context.source_manifest)
    resolvable = context.source_manifest.resolvable_ids()
    for signal in (
        *context.signals.issue.occurrence_trends,
        *context.signals.issue.assessment_corrections,
        *context.signals.issue.regressions,
        *context.signals.workflow.gate_pushback,
        *context.signals.eval.trends,
    ):
        assert set(signal.source_refs.all_ids()).issubset(resolvable)


def test_context_is_byte_identical_for_same_inputs(readers: _Readers) -> None:
    selection = RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None)
    a = build_retro_context(
        selection,
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    b = build_retro_context(
        selection,
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


def test_zero_signal_window_succeeds() -> None:
    empty_wf = InMemoryWorkflowHistoryReader.from_terminals(())
    empty_eval = InMemoryEvalHistoryReader.from_slice(EvalEvidenceSlice())
    empty_issues = InMemoryIssueHistoryReader.from_events(
        IssueTypedEvents(
            change_events={},
            problem_events=(),
            change_ledger_bytes={},
            problem_ledger_bytes=b"",
        )
    )
    context = build_retro_context(
        RetroWindowSelection(last=1),
        issue_history=empty_issues,
        workflow_history=empty_wf,
        eval_history=empty_eval,
        retro_id="retro-empty",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.signal_count == 0
    assert count_signals(context.signals) == 0
    assert context.schema_version == "2"


def test_incomplete_issue_integrity_forbids_only_domain_knowledge(readers: _Readers) -> None:
    typed = _typed_events(analysis_failed=True)
    incomplete_readers = _Readers(
        issues=InMemoryIssueHistoryReader.from_events(typed),
        workflow=readers.workflow,
        eval=readers.eval,
    )
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1",), last=None),
        issue_history=incomplete_readers.issues,
        workflow_history=incomplete_readers.workflow,
        eval_history=incomplete_readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.integrity.status == "incomplete"
    assert "analysis_failed" in context.integrity.reasons
    assert context.allows_domain_knowledge is False


def test_complete_issue_integrity_allows_domain_knowledge(readers: _Readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.integrity.status == "complete"
    assert context.allows_domain_knowledge is True


def test_missing_workflow_source_is_degraded_not_fabricated(readers: _Readers) -> None:
    degraded = WorkflowEvidenceSlice(
        change_ids=("RET-1", "RET-2"),
        integrity=RetroIntegrity(
            status="incomplete",
            reasons=("workflow_source_missing:RET-2",),
        ),
    )
    wf = InMemoryWorkflowHistoryReader(
        terminals=(
            TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-25T18:00:00Z"),
            TerminalChangeRef(change_id="RET-2", terminal_ts="2026-07-25T19:00:00Z"),
        ),
        slice_=degraded,
        known_ids=frozenset({"RET-1", "RET-2"}),
    )
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=wf,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.integrity.status == "incomplete"
    assert "workflow_source_missing:RET-2" in context.integrity.reasons
    # No fabricated zero-valued workflow signals when sources are degraded/empty.
    assert context.signals.workflow.healing_efficiency == ()
    assert context.signals.workflow.gate_pushback == ()
    # v2 replay preserves its historical issue-only eligibility rule: a
    # degraded workflow source does not block domain knowledge.
    assert context.allows_domain_knowledge is True


def test_unrecovered_issue_pipeline_failure_blocks_domain_knowledge(readers: _Readers) -> None:
    failed_issue_pipeline = WorkflowEvidenceSlice(
        change_ids=("RET-1", "RET-2"),
        integrity=RetroIntegrity(
            status="incomplete",
            reasons=("issue_pipeline_failed:RET-1",),
        ),
    )
    workflow = InMemoryWorkflowHistoryReader(
        terminals=(
            TerminalChangeRef(change_id="RET-1", terminal_ts="2026-07-25T18:00:00Z"),
            TerminalChangeRef(change_id="RET-2", terminal_ts="2026-07-25T19:00:00Z"),
        ),
        slice_=failed_issue_pipeline,
        known_ids=frozenset({"RET-1", "RET-2"}),
    )

    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=workflow,
        eval_history=readers.eval,
        retro_id="retro-issue-pipeline-failed",
        generated_at="2026-07-25T00:00:00Z",
    )

    assert context.integrity.status == "incomplete"
    assert context.allows_domain_knowledge is False


def test_assert_signal_refs_resolve_rejects_unknown_id() -> None:
    from assurance_agent.retro.types import (
        EvalRetroSignals,
        IssueRetroSignals,
        RetroSourceManifest,
        WorkflowRetroSignals,
    )

    signals = RetroSignalSet(
        issue=IssueRetroSignals(
            occurrence_trends=(
                RetroSignal(
                    signal_id="bad",
                    source_refs=ImprovementSourceRefs(problem_ids=("PROB-MISSING",)),
                    metrics={"count": 1},
                ),
            )
        ),
        workflow=WorkflowRetroSignals(),
        eval=EvalRetroSignals(),
    )
    manifest = RetroSourceManifest(
        issue_slice_sha256="sha256:x",
        issue_sources=(),
        workflow_sources=(),
        eval_sources=(),
    )
    with pytest.raises(ValueError, match="PROB-MISSING"):
        assert_signal_refs_resolve(signals, manifest)


def test_retro_context_immutable_error_is_aa_error() -> None:
    from assurance_agent.exceptions import AaError

    assert issubclass(RetroContextImmutableError, AaError)
