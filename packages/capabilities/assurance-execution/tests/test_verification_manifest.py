from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.canonical import canonical_digest
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

from assurance_execution.contracts.verification import (
    EvidenceCompletionV1,
    ObservationV1,
    VerificationEvidenceV1,
    VerificationManifestV1,
)
from assurance_execution.operations.verification_manifest import (
    allocate_user_inputs,
    authenticate_verification_manifest,
    build_verification_manifest,
)


SHA = "a" * 64


def _manifest(tmp_path: Path, *, nodeid: str = "tests/api/test_user.py::test_create[admin]"):
    db = tmp_path / "run" / "db.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"sqlite")
    return build_verification_manifest(
        change_id="CH-USER-001",
        case_id="TC_USER_CREATE_001",
        nodeid=nodeid,
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        attempt_key=AttemptKey(digest="b" * 64),
        business_activation=BusinessActivation.for_round(2),
        coverage_epoch=3,
        repair_round=2,
        authorization_scope_digest="3" * 64,
        activity_receipt_digest="4" * 64,
        plan_ref="qa/changes/CH-USER-001/plan/root.json",
        plan_digest="c" * 64,
        case_execution_plan_ref="qa/changes/CH-USER-001/plans/api-case-execution-plan.json",
        case_execution_plan_digest="d" * 64,
        spec_digest="e" * 64,
        mapping_digest="f" * 64,
        sut_digest="1" * 64,
        technical_config_digest="2" * 64,
        validation_profile="api_db.v1",
        sut_base_url="http://127.0.0.1:32123",
        sut_instance_id="sut-1",
        sqlite_path=db,
        username="qa_t1",
        email="qa_t1@example.com",
        evidence_root="qa/changes/CH-USER-001/execution",
    )


def test_manifest_freezes_runtime_identity_and_full_parameterized_nodeid(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    assert manifest.nodeid == "tests/api/test_user.py::test_create[admin]"
    assert manifest.attempt_key.digest == "b" * 64
    assert manifest.business_activation == BusinessActivation.for_round(2)
    assert manifest.coverage_epoch == 3
    assert manifest.repair_round == 2
    assert manifest.sqlite.path == str((tmp_path / "run" / "db.sqlite3").resolve())
    assert manifest.evidence_root == (f"qa/changes/CH-USER-001/execution/{manifest.execution_id}")
    with pytest.raises(ValidationError):
        VerificationManifestV1.model_validate({**manifest.model_dump(mode="json"), "passed": True})
    with pytest.raises(ValidationError, match="evidence root"):
        VerificationManifestV1.model_validate(
            {
                **manifest.model_dump(mode="json"),
                "evidence_root": (f"qa/changes/CH-USER-001/execution/extra/{manifest.execution_id}"),
            }
        )


def test_real_reruns_receive_new_execution_ids_and_do_not_fold_parameters(tmp_path: Path) -> None:
    first = _manifest(tmp_path / "first", nodeid="tests/api/test_user.py::test_create[one]")
    second = _manifest(tmp_path / "second", nodeid="tests/api/test_user.py::test_create[two]")

    assert first.execution_id != second.execution_id
    assert first.nodeid != second.nodeid


def test_manifest_authentication_rejects_frozen_input_path_and_identity_drift(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    digest = canonical_digest(manifest.model_dump(mode="json"))

    assert (
        authenticate_verification_manifest(
            manifest,
            manifest_digest=digest,
            attempt_key=manifest.attempt_key,
            invocation_id="inv-1",
            nodeid=manifest.nodeid,
            sqlite_path=Path(manifest.sqlite.path),
            username="qa_t1",
            email="qa_t1@example.com",
            authorization_scope_digest="3" * 64,
            activity_receipt_digest="4" * 64,
        )
        is manifest
    )
    with pytest.raises(ValueError, match="username"):
        authenticate_verification_manifest(
            manifest,
            manifest_digest=digest,
            attempt_key=manifest.attempt_key,
            invocation_id="inv-1",
            nodeid=manifest.nodeid,
            sqlite_path=Path(manifest.sqlite.path),
            username="changed",
            email="qa_t1@example.com",
            authorization_scope_digest="3" * 64,
            activity_receipt_digest="4" * 64,
        )
    with pytest.raises(ValueError, match="SQLite"):
        authenticate_verification_manifest(
            manifest,
            manifest_digest=digest,
            attempt_key=manifest.attempt_key,
            invocation_id="inv-1",
            nodeid=manifest.nodeid,
            sqlite_path=tmp_path / "other.sqlite3",
            username="qa_t1",
            email="qa_t1@example.com",
            authorization_scope_digest="3" * 64,
            activity_receipt_digest="4" * 64,
        )


def test_recovery_reuses_only_the_authenticated_manifest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    digest = canonical_digest(manifest.model_dump(mode="json"))

    recovered = authenticate_verification_manifest(
        manifest,
        manifest_digest=digest,
        attempt_key=manifest.attempt_key,
        invocation_id=manifest.invocation_id,
        nodeid=manifest.nodeid,
        sqlite_path=Path(manifest.sqlite.path),
        username=manifest.inputs.username,
        email=manifest.inputs.email,
        authorization_scope_digest="3" * 64,
        activity_receipt_digest="4" * 64,
    )
    assert recovered.execution_id == manifest.execution_id
    with pytest.raises(ValueError, match="digest"):
        authenticate_verification_manifest(
            manifest,
            manifest_digest="0" * 64,
            attempt_key=manifest.attempt_key,
            invocation_id=manifest.invocation_id,
            nodeid=manifest.nodeid,
            sqlite_path=Path(manifest.sqlite.path),
            username=manifest.inputs.username,
            email=manifest.inputs.email,
            authorization_scope_digest="3" * 64,
            activity_receipt_digest="4" * 64,
        )
    with pytest.raises(ValueError, match="scope or activity"):
        authenticate_verification_manifest(
            manifest,
            manifest_digest=digest,
            attempt_key=manifest.attempt_key,
            invocation_id=manifest.invocation_id,
            nodeid=manifest.nodeid,
            sqlite_path=Path(manifest.sqlite.path),
            username=manifest.inputs.username,
            email=manifest.inputs.email,
            authorization_scope_digest="5" * 64,
            activity_receipt_digest="4" * 64,
        )


def test_recovery_rejects_a_replaced_database_file_even_at_the_same_path(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    digest = canonical_digest(manifest.model_dump(mode="json"))
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(b"different sqlite file")
    replacement.replace(Path(manifest.sqlite.path))

    with pytest.raises(ValueError, match="file identity"):
        authenticate_verification_manifest(
            manifest,
            manifest_digest=digest,
            attempt_key=manifest.attempt_key,
            invocation_id=manifest.invocation_id,
            nodeid=manifest.nodeid,
            sqlite_path=Path(manifest.sqlite.path),
            username=manifest.inputs.username,
            email=manifest.inputs.email,
            authorization_scope_digest="3" * 64,
            activity_receipt_digest="4" * 64,
        )


def test_input_allocation_stops_after_three_collisions() -> None:
    seen: list[tuple[str, str]] = []

    def always_exists(username: str, email: str) -> bool:
        seen.append((username, email))
        return True

    with pytest.raises(ValueError, match="three"):
        allocate_user_inputs(always_exists, token_factory=lambda: f"token{len(seen)}")
    assert len(seen) == 3


def test_observation_and_evidence_reject_expected_passed_and_duplicate_obligations() -> None:
    observation = ObservationV1(
        execution_id="01234567-89ab-4def-8123-456789abcdef",
        obligation_id="oracle.executed",
        state="observed",
        actual={"rows": []},
        evidence_ref=EvidenceArtifactRefV1(path="evidence/oracle.json", digest=SHA),
    )
    with pytest.raises(ValidationError):
        ObservationV1.model_validate({**observation.model_dump(mode="json"), "expected": {"rows": 1}})
    with pytest.raises(ValidationError):
        VerificationEvidenceV1(
            execution_id=observation.execution_id,
            manifest_digest=SHA,
            receipt_ref=EvidenceArtifactRefV1(path="evidence/receipt.json", digest=SHA),
            observations=(observation, observation),
            host_completion=EvidenceCompletionV1(state="complete"),
            collector_completion=EvidenceCompletionV1(state="not_required"),
            state="collected",
        )
    stale = observation.model_copy(update={"execution_id": "11234567-89ab-4def-8123-456789abcdef"})
    with pytest.raises(ValidationError, match="execution identity"):
        VerificationEvidenceV1(
            execution_id=observation.execution_id,
            manifest_digest=SHA,
            receipt_ref=EvidenceArtifactRefV1(path="evidence/receipt.json", digest=SHA),
            observations=(stale,),
            host_completion=EvidenceCompletionV1(state="complete"),
            collector_completion=EvidenceCompletionV1(state="not_required"),
            state="collected",
        )
