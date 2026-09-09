from __future__ import annotations

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.verification import VerificationObligationV1, VerificationVerdictV1
from assurance_quality.graphs.nodes import _optional_artifact_ref
from assurance_quality.operations.assessment import _verification_issue_observations


def test_optional_artifact_ref_treats_cleared_cycle_placeholder_as_absent() -> None:
    assert _optional_artifact_ref(None) is None
    assert _optional_artifact_ref({}) is None
    ref = EvidenceArtifactRefV1(path="qa/changes/CH-1/report/report.md", digest="a" * 64)
    assert _optional_artifact_ref(ref.model_dump(mode="json")) == ref


def test_incomplete_verification_projects_owned_observation() -> None:
    verification = VerificationVerdictV1(
        validation_profile="api_db.v1",
        execution_id="95099045-c0ec-482d-a727-7c73758ffc2a",
        case_id="TC_USER_CREATE_001",
        verdict="INCOMPLETE",
        required=1,
        executed=0,
        evaluated=0,
        satisfied=0,
        obligations=(
            VerificationObligationV1(
                obligation_id="oracle.executed",
                kind="completion",
                evidence_status="error",
                business_status="not_evaluated",
                reason="database_unavailable",
            ),
        ),
        reason_codes=("verification.required_evidence_missing",),
    )
    ref = EvidenceArtifactRefV1(
        path="qa/changes/CH-USER-DB-UNAVAILABLE/inspect/epochs/0/batches/batch/verification.json",
        digest="a" * 64,
    )
    observations = _verification_issue_observations(
        verification=verification,
        verification_ref=ref,
        change_id="CH-USER-DB-UNAVAILABLE",
        batch_id="batch",
        observed_at="2026-09-09T00:00:00+00:00",
    )
    assert len(observations) == 1
    assert observations[0].kind == "anomaly"
    assert observations[0].case_id == "TC_USER_CREATE_001"
    assert observations[0].signature == "verification_incomplete"
    assert observations[0].source.artifact == ref.path
