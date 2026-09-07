from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from assurance_execution.contracts.verification import (
    FrozenUserInputsV1,
    ManagedSutV1,
    SqliteFileIdentityV1,
    VerificationEvidenceV1,
    VerificationManifestV1,
    VerifiedExecutionAuthorityV1,
    VerifiedExecutionResultV1,
)
from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import InspectionOutcomeV1, ReportOutcomeV1
from assurance_quality.contracts.verification import (
    VerificationObligationV1,
    VerificationVerdictV1,
)
from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import TaskOutcome
from tests.product.test_achieved_terminal import CHANGE_ID, _ready_change
from tests.verified_generation_fixture import accepted_verified_execution_input

INVOCATION_ID = "inv-achieved-001"
EXECUTION_ID = "12345678-1234-4123-8123-123456789abc"


def _write_ref(project: Path, relative: str, payload: object) -> EvidenceArtifactRefV1:
    data = canonical_json_bytes(cast(JSONValue, payload)) + b"\n"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def _verified_terminal(
    tmp_path: Path,
    *,
    omit_obligation: str | None = None,
    verdict_execution_id: str = EXECUTION_ID,
    repair_round: int = 0,
    include_verification: bool = True,
    failed_verdict: bool = False,
) -> tuple[Path, object]:
    project = _ready_change(tmp_path)
    prepared = accepted_verified_execution_input(project, change_id=CHANGE_ID)
    legacy_generated = project / f"qa/changes/{CHANGE_ID}/generated/api/files/tests/api/test_users.py"
    legacy_generated.unlink()
    generation = prepared.generation_result
    assert generation is not None and generation.case_execution_plan_ref is not None
    plan_set = CaseExecutionPlanSetV1.model_validate_json(
        (project / generation.case_execution_plan_ref.path).read_bytes()
    )
    plan = plan_set.cases[0]
    sqlite_path = project / ".aa" / "verified.sqlite3"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.write_bytes(b"sqlite")
    sqlite_stat = sqlite_path.stat()
    attempt_key = AttemptKey(digest="4" * 64)
    evidence_root = f"qa/changes/{CHANGE_ID}/execution/{EXECUTION_ID}"
    manifest = VerificationManifestV1(
        execution_id=EXECUTION_ID,
        change_id=CHANGE_ID,
        case_id=plan.case_id,
        nodeid="tests/api/test_users.py::test_create_user",
        invocation_id=INVOCATION_ID,
        task_id="execution-task",
        graph_instance_id="execution-graph",
        attempt_key=attempt_key,
        business_activation=BusinessActivation.for_trigger(
            f"coverage.2.{'execute' if repair_round == 0 else f'repair.{repair_round}.rerun'}"
        ),
        coverage_epoch=plan.coverage_epoch,
        repair_round=repair_round,
        authorization_scope_digest="a" * 64,
        activity_receipt_digest="b" * 64,
        plan_ref=plan.plan_ref.path,
        plan_digest=plan.plan_digest,
        case_execution_plan_ref=generation.case_execution_plan_ref.path,
        case_execution_plan_digest=generation.case_execution_plan_ref.digest,
        spec_digest=plan.spec_digest,
        mapping_digest=generation.mapping_ref.digest,
        sut_digest=plan.sut_digest,
        technical_config_digest=plan.technical_config_digest,
        validation_profile="api_db.v1",
        sut=ManagedSutV1(
            instance_id="sut-1",
            base_url="http://127.0.0.1:41001",
            sqlite_path=str(sqlite_path),
        ),
        sqlite=SqliteFileIdentityV1(
            path=str(sqlite_path),
            device=sqlite_stat.st_dev,
            inode=sqlite_stat.st_ino,
        ),
        inputs=FrozenUserInputsV1.model_validate(plan.inputs),
        evidence_root=evidence_root,
    )
    manifest_ref = _write_ref(project, f"{evidence_root}/manifest.json", manifest.model_dump(mode="json"))
    manifest_binding = canonical_digest(cast(JSONValue, manifest.model_dump(mode="json")))
    process_ref = _write_ref(
        project,
        f"{evidence_root}/process_terminal.json",
        {
            "manifest_digest": manifest_binding,
            "record": "process_terminal",
            "payload": {"execution_id": EXECUTION_ID, "exit_code": 0},
            "seal": "f" * 64,
        },
    )
    required = tuple(item for item in plan.required if item != omit_obligation)
    observations = tuple(
        {
            "execution_id": EXECUTION_ID,
            "obligation_id": obligation,
            "state": "observed",
            "actual": True,
            "evidence_ref": process_ref.model_dump(mode="json"),
        }
        for obligation in required
    )
    evidence = VerificationEvidenceV1.model_validate(
        {
            "execution_id": EXECUTION_ID,
            "manifest_digest": manifest_binding,
            "receipt_ref": process_ref.model_dump(mode="json"),
            "observations": observations,
            "host_completion": {"state": "complete"},
            "collector_completion": {"state": "not_required"},
            "state": "collected",
        }
    )
    outcome_ref = _write_ref(
        project,
        f"{evidence_root}/outcome.json",
        {
            "manifest_digest": manifest_binding,
            "record": "outcome",
            "payload": TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json"))).model_dump(
                mode="json"
            ),
            "seal": "f" * 64,
        },
    )
    authority = VerifiedExecutionAuthorityV1(
        validation_profile="api_db.v1",
        change_id=CHANGE_ID,
        case_id=plan.case_id,
        reviewed_case=generation.reviewed_case,
        coverage_epoch=plan.coverage_epoch,
        repair_round=repair_round,
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        case_execution_plan_ref=generation.case_execution_plan_ref,
        case_execution_plan_digest=generation.case_execution_plan_ref.digest,
        spec_digest=plan.spec_digest,
        execution_id=EXECUTION_ID,
        attempt_key=attempt_key,
        batch_id="verified-batch",
        mapping_digest=generation.mapping_ref.digest,
        manifest_ref=manifest_ref,
        evidence_ref=outcome_ref,
        raw_evidence_refs=(process_ref,),
        executed_at=datetime(2026, 9, 7, tzinfo=UTC),
        completion_status="collected",
        evidence=evidence,
    )
    authority_ref = _write_ref(
        project,
        f"{evidence_root}/execution_terminal.json",
        {
            "manifest_digest": manifest_binding,
            "record": "execution_terminal",
            "payload": authority.model_dump(mode="json"),
            "seal": "f" * 64,
        },
    )
    result = VerifiedExecutionResultV1(
        **authority.model_dump(mode="python"),
        execution_authority_ref=authority_ref,
    )
    result_document = result.model_dump(mode="json")
    execution_name = "execute-result.json" if repair_round == 0 else "run-result.json"
    execution_path = f"qa/changes/{CHANGE_ID}/execution/{execution_name}"
    encoded_result = (json.dumps(result_document, indent=2) + "\n").encode()
    path = project / execution_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded_result)
    execution_ref = EvidenceArtifactRefV1(
        path=execution_path,
        digest=hashlib.sha256(encoded_result).hexdigest(),
    )
    execution_receipt = ReceiptRef(receipt_id="verified-execution", receipt_digest="c" * 64)
    cycle = VerifiedExecutionCycleResultV1(
        validation_profile="api_db.v1",
        change_id=CHANGE_ID,
        case_id=plan.case_id,
        reviewed_case=generation.reviewed_case,
        coverage_epoch=plan.coverage_epoch,
        repair_round=repair_round,
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        case_execution_plan_ref=generation.case_execution_plan_ref,
        case_execution_plan_digest=generation.case_execution_plan_ref.digest,
        spec_digest=plan.spec_digest,
        execution_id=EXECUTION_ID,
        attempt_key=attempt_key,
        batch_id=result.batch_id,
        executed_at=result.executed_at,
        completion_status="collected",
        mapping_ref=generation.mapping_ref,
        mapping_digest=generation.mapping_ref.digest,
        manifest_ref=manifest_ref,
        evidence_ref=outcome_ref,
        execution_index_ref=execution_ref,
        execution_authority_ref=authority_ref,
        raw_evidence_refs=(process_ref,),
        source_refs=generation.source_refs,
        receipt=execution_receipt,
    )
    violated = "user.email" if failed_verdict else None
    obligations = tuple(
        VerificationObligationV1(
            obligation_id=obligation,
            kind="completion" if obligation in {"action.finished", "oracle.executed"} else "business",
            evidence_status="observed",
            business_status="violated" if obligation == violated else "satisfied",
            actual=False if obligation == violated else True,
        )
        for obligation in required
    )
    verdict = VerificationVerdictV1(
        validation_profile="api_db.v1",
        execution_id=verdict_execution_id,
        case_id=plan.case_id,
        verdict="FAILED" if failed_verdict else "PASSED",
        required=len(required),
        executed=len(required),
        evaluated=len(required),
        satisfied=len(required) - int(failed_verdict),
        obligations=obligations,
        reason_codes=("verification.business_violation",) if failed_verdict else (),
    )
    verdict_ref = _write_ref(
        project,
        f"qa/changes/{CHANGE_ID}/inspect/epochs/{plan.coverage_epoch}/batches/{result.batch_id}/verification.json",
        verdict.model_dump(mode="json"),
    )
    inspection_receipt = ReceiptRef(receipt_id="inspection", receipt_digest="d" * 64)
    assessment_refs = tuple(
        sorted(
            (execution_ref, verdict_ref) if include_verification else (execution_ref,),
            key=lambda item: item.path,
        )
    )
    inspection = InspectionOutcomeV1(
        change_id=CHANGE_ID,
        coverage_epoch=plan.coverage_epoch,
        batch_id=result.batch_id,
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        disposition="satisfied",
        inspection_receipt=inspection_receipt,
        reviewed_case=generation.reviewed_case,
        mapping_ref=generation.mapping_ref,
        assessment_refs=assessment_refs,
        reason_codes=(),
        coverage_state="satisfied",
        verification_ref=verdict_ref if include_verification else None,
        verification_status="PASSED" if include_verification else None,
    )
    report_ref = _write_ref(project, f"qa/changes/{CHANGE_ID}/report/report.md", {"ok": True})
    report = ReportOutcomeV1(
        change_id=CHANGE_ID,
        coverage_epoch=plan.coverage_epoch,
        batch_id=result.batch_id,
        inspection_receipt=inspection_receipt,
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        report_refs=(report_ref,),
        report_receipt=ReceiptRef(receipt_id="report", receipt_digest="e" * 64),
    )
    snapshot = SimpleNamespace(
        next=(),
        interrupts=(),
        values={
            "terminal": {"status": "completed", "reason": "achieved"},
            "selected_test_families": ["api"],
            "validation_profile": "api_db.v1",
            "coverage_epoch": plan.coverage_epoch,
            "batch_id": result.batch_id,
            "execution_semantic_node_id": ("execution.execute" if repair_round == 0 else "execution.run"),
            "execution_evidence": result_document,
            "execution_digest": canonical_digest(cast(JSONValue, result_document)),
            "execution_result": cycle.model_dump(mode="json"),
            "inspection_outcome": inspection.model_dump(mode="json"),
            "report_outcome": report.model_dump(mode="json"),
        },
    )
    return project, snapshot


def _render(snapshot: object):
    from assurance_product.status import render_status_from_langgraph

    return render_status_from_langgraph(
        invocation_id=INVOCATION_ID,
        lock_digest="a" * 64,
        root_input_digest="b" * 64,
        entrypoint="full",
        change_id=CHANGE_ID,
        status="completed",
        snapshot=snapshot,
    )


def test_verified_delivery_reaches_achieved_and_export(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path)
    rendered = _render(snapshot)
    assert rendered.execution_gate is not None
    assert rendered.execution_gate.validation_profile == "api_db.v1"

    achieved = finalize_achieved(project, CHANGE_ID, ("api",), invocation=rendered)
    receipt = publish_achieved(project, CHANGE_ID)

    assert achieved.change.state == "achieved"
    assert receipt.change_id == CHANGE_ID


def test_verified_rerun_can_achieve_without_legacy_failed_pytest_evidence(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path, repair_round=1)
    rendered = _render(snapshot)
    assert rendered.execution_gate is not None
    assert rendered.execution_gate.semantic_node_id == "execution.run"

    achieved = finalize_achieved(project, CHANGE_ID, ("api",), invocation=rendered)

    assert achieved.change.state == "achieved"


def test_verified_delivery_rejects_omitted_required_db_obligation(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path, omit_obligation="user.email")
    with pytest.raises(ValueError, match="required obligations"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=_render(snapshot))
    assert not (project / f"qa/changes/{CHANGE_ID}/status.json").exists()
    assert not (project / f"qa/changes/{CHANGE_ID}/apply-manifest.json").exists()


def test_verified_delivery_rejects_missing_verification_gate(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path, include_verification=False)
    with pytest.raises(ValueError, match="quality identity"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=_render(snapshot))


def test_verified_delivery_rejects_failed_verdict(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path, failed_verdict=True)
    with pytest.raises(ValueError, match="verdict is not passed"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=_render(snapshot))


def test_export_rechecks_verified_evidence_after_achieved(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path)
    rendered = _render(snapshot)
    finalize_achieved(project, CHANGE_ID, ("api",), invocation=rendered)
    assert rendered.quality_gate is not None
    verdict_ref = rendered.quality_gate.inspection.verification_ref
    assert verdict_ref is not None
    (project / verdict_ref.path).write_bytes(b"drifted")

    with pytest.raises(PublishError, match="verified delivery"):
        publish_achieved(project, CHANGE_ID)


def test_export_rejects_verified_profile_downgrade(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path)
    finalize_achieved(project, CHANGE_ID, ("api",), invocation=_render(snapshot))
    status_path = project / f"qa/changes/{CHANGE_ID}/status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["execution_gate"].update(
        {
            "validation_profile": None,
            "execution_receipt_id": None,
            "execution_receipt_digest": None,
        }
    )
    status["quality_gate"]["inspection"].update({"verification_ref": None, "verification_status": None})
    status_path.write_text(json.dumps(status), encoding="utf-8")

    with pytest.raises(PublishError, match="profile was removed"):
        publish_achieved(project, CHANGE_ID)


def test_verified_delivery_rejects_stale_execution_id(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(
        tmp_path,
        verdict_execution_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    with pytest.raises(ValueError, match="verdict is not passed"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=_render(snapshot))


def test_application_status_rechecks_persisted_verified_delivery(tmp_path: Path, monkeypatch) -> None:
    from assurance_product.application import AssuranceProductApplication, RuntimeSelectionError
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.invocation_identity import InvocationIdentityRecord
    from assurance_product.status import finalize_achieved

    project, snapshot = _verified_terminal(tmp_path)
    rendered = _render(snapshot)
    finalize_achieved(project, CHANGE_ID, ("api",), invocation=rendered)
    workspace = ChangeWorkspace.open(project.resolve(), CHANGE_ID)
    workspace.initialize()
    identity = InvocationIdentityRecord(
        schema_version="1",
        phase="initialized",
        invocation_id=INVOCATION_ID,
        entrypoint="full",
        root_input_digest="b" * 64,
        product_lock_digest="a" * 64,
        revision_id="c" * 64,
    )
    assert rendered.quality_gate is not None
    verdict_ref = rendered.quality_gate.inspection.verification_ref
    assert verdict_ref is not None
    (project / verdict_ref.path).write_bytes(b"drifted")
    application = AssuranceProductApplication()
    monkeypatch.setattr(application, "_resolve_existing", lambda *_args, **_kwargs: identity)

    async def terminal_status(*_args: object, **_kwargs: object):
        return "completed", snapshot, ()

    monkeypatch.setattr(application, "_status_langgraph", terminal_status)
    with pytest.raises(RuntimeSelectionError, match="persisted verified delivery"):
        application.status(
            workspace=workspace,
            composition=object(),
            authorization=object(),  # type: ignore[arg-type]
            invocation_id=INVOCATION_ID,
            change_id=CHANGE_ID,
        )
