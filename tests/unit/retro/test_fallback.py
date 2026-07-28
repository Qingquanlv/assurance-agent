from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.artifacts.models.retro_batch import (
    RetroBatchScope,
    RetroPipelineFailure,
)
from assurance_agent.artifacts.models.retro_v3 import BatchMemberEvidenceGapSignal
from assurance_agent.retro.candidates import read_candidate_document, validate_candidate_document
from assurance_agent.retro.fallback import (
    candidate_from_evidence_gaps,
    candidate_from_pipeline_failure,
    materialize_pipeline_failure_fallback,
)


def _scope() -> RetroBatchScope:
    return RetroBatchScope.model_validate(
        {
            "batch_id": "batch-1",
            "status": "incomplete",
            "members": [
                {
                    "change_id": "CH-1",
                    "execution_status": "failed",
                    "evidence_availability": "absent",
                }
            ],
        }
    )


def _gap() -> BatchMemberEvidenceGapSignal:
    return BatchMemberEvidenceGapSignal(
        signal_id="BATCH-GAP-1",
        summary="Issue evidence is absent",
        occurrence_count=1,
        recommended_change="Make Issue evidence durable.",
        source_refs=ImprovementSourceRefs(workflow_evidence_ids=("BATCH-GAP-1",)),
        confidence="high",
        change_id="CH-1",
        execution_status="failed",
        domain="issue",
        reason_code="ledger_missing",
    )


def _failure(message_fingerprint: str) -> RetroPipelineFailure:
    return RetroPipelineFailure(
        failure_id="FAIL-1",
        retro_id="retro-1",
        batch_id="batch-1",
        stage="assemble",
        node_id="assemble-retro-context",
        error_kind="invalid_output",
        message_fingerprint=message_fingerprint,
        occurred_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )


def test_evidence_gap_candidate_is_deterministic_process_improvement() -> None:
    first = candidate_from_evidence_gaps(
        retro_id="retro-1",
        batch_scope=_scope(),
        gaps=(_gap(),),
        context_sha256="sha256:context",
    )
    second = candidate_from_evidence_gaps(
        retro_id="retro-1",
        batch_scope=_scope(),
        gaps=(_gap(),),
        context_sha256="sha256:context",
    )
    assert first == second
    assert first.kind == "workflow_improvement"
    assert first.source_refs.workflow_evidence_ids == ("BATCH-GAP-1",)


def test_pipeline_candidate_identity_ignores_free_message_fingerprint() -> None:
    first = candidate_from_pipeline_failure(_failure("sha256:message-a"), context_sha256="sha256:c")
    second = candidate_from_pipeline_failure(_failure("sha256:message-b"), context_sha256="sha256:c")
    assert first.candidate_id == second.candidate_id
    assert first.target == second.target
    assert first.proposed_change == second.proposed_change


def test_pipeline_fallback_materializes_valid_context_and_candidate(tmp_path: Path) -> None:
    context, candidate = materialize_pipeline_failure_fallback(
        tmp_path,
        failure=_failure("sha256:message"),
        batch_scope=_scope(),
    )
    retro_dir = tmp_path / "qa/retro/retro-1"
    for domain in ("issue", "workflow", "eval"):
        assert (retro_dir / f"evidence/{domain}-slice.json").is_file()
    document = read_candidate_document(retro_dir, expected_schema="3")
    validate_candidate_document(context, document)
    assert document.candidates == (candidate,)
    assert context.signal_count == 1
    assert context.integrity.status == "incomplete"
