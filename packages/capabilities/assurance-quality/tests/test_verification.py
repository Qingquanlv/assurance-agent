from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
import hashlib
import json

import pytest

from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1, CaseExecutionPlanV1
from assurance_quality.contracts.verification import VerificationVerdictV1
from assurance_quality.operations.verification import evaluate_verification
from assurance_quality.operations.verification import verification_failure_facts
from assurance_quality.contracts.decisions import classify_inspection_disposition
from assurance_quality.contracts.assessment import MaterializeAssessmentInputV1
from assurance_quality.operations.assessment import AssessmentInputError, materialize_assessment_inputs
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from tests.verified_generation_fixture import accepted_verified_execution_input
from graph_engine.attempts import AttemptKey
from assurance_execution.contracts.verification import (
    VerifiedExecutionResultV1,
)
from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from assurance_execution.contracts.workflow import VerifiedIncompleteExecutionV1
from assurance_generation.contracts.admission import diagnose_verified_bridge_defect
from graph_engine.attempts.resolutions import ReceiptRef
from typing import cast

from tests.verified_assessment_fixture import EXECUTION_ID, _actuals, _evidence, _materialization_request


def _plan(root: Path, *, trace: bool = False):
    prepared = accepted_verified_execution_input(root)
    ref = prepared.verification.case_execution_plan_ref  # type: ignore[union-attr]
    plan = CaseExecutionPlanSetV1.model_validate_json((root / ref.path).read_bytes()).cases[0]
    if trace:
        required = tuple(
            sorted(
                (*plan.required, "trace.drained", "trace.http", "trace.user_completed", "trace.user_write")
            )
        )
        document = plan.model_dump(mode="json")
        document.update(
            {
                "validation_profile": "api_db_trace.v1",
                "trace": {
                    "status": "required",
                    "http_obligation": "trace.http",
                    "user_write_obligation": "trace.user_write",
                    "checkpoint_obligation": "trace.user_completed",
                    "checkpoint_id": "user.create.completed",
                    "checkpoint_version": "1",
                    "drain_obligation": "trace.drained",
                    "require_same_action_and_sut": True,
                },
                "required": required,
                "completion": {**plan.completion.model_dump(mode="json"), "obligations": required},
                "bindings": tuple(
                    sorted(
                        (
                            *(item.model_dump(mode="json") for item in plan.bindings),
                            {
                                "obligation_id": "trace.http",
                                "actual": {"kind": "trace_http", "action_key": "create_user"},
                            },
                            {
                                "obligation_id": "trace.user_write",
                                "actual": {
                                    "kind": "trace_user_write",
                                    "binding_id": "assurance.execution.trace.sqlite-user-write.v1",
                                    "binding_version": "1",
                                },
                            },
                            {
                                "obligation_id": "trace.user_completed",
                                "actual": {
                                    "kind": "trace_checkpoint",
                                    "checkpoint_id": "user.create.completed",
                                    "checkpoint_version": "1",
                                },
                            },
                            {
                                "obligation_id": "trace.drained",
                                "actual": {
                                    "kind": "trace_drain",
                                    "binding_id": "assurance.execution.trace.drain.v1",
                                    "binding_version": "1",
                                },
                            },
                        ),
                        key=lambda item: item["obligation_id"],
                    )
                ),
            }
        )
        plan = CaseExecutionPlanV1.model_validate(document)
    return plan


def test_all_required_facts_are_evaluated_and_pass(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    verdict = evaluate_verification(plan, _evidence(plan), completion_status="collected")

    assert verdict.verdict == "PASSED"
    assert (verdict.required, verdict.executed, verdict.evaluated, verdict.satisfied) == (11, 11, 11, 11)


def test_wrong_business_value_fails_even_with_complete_evidence(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    actuals = _actuals(plan)
    actuals["user.email"] = "wrong@example.test"

    verdict = evaluate_verification(plan, _evidence(plan, actuals=actuals), completion_status="collected")

    assert verdict.verdict == "FAILED"
    assert verdict.by_id("user.email").business_status == "violated"


@pytest.mark.parametrize(
    ("obligation_id", "confused_value"),
    (("user.row_count", True), ("api.code", 200.0)),
)
def test_json_type_confusion_is_a_business_failure(
    tmp_path: Path,
    obligation_id: str,
    confused_value: object,
) -> None:
    plan = _plan(tmp_path)
    actuals = _actuals(plan)
    actuals[obligation_id] = confused_value

    verdict = evaluate_verification(
        plan,
        _evidence(plan, actuals=actuals),
        completion_status="collected",
    )

    assert verdict.verdict == "FAILED"
    assert verdict.by_id(obligation_id).business_status == "violated"


def test_observed_incomplete_action_is_incomplete_not_a_business_failure(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    actuals = _actuals(plan)
    actuals["action.finished"] = False

    verdict = evaluate_verification(
        plan,
        _evidence(plan, actuals=actuals),
        completion_status="collected",
    )

    assert verdict.verdict == "INCOMPLETE"
    assert verdict.by_id("action.finished").reason == "completion_obligation_not_satisfied"
    assert verdict.reason_codes == ("verification.completion_unsatisfied",)


def test_host_completion_cannot_be_waived_as_not_required(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    verdict = evaluate_verification(
        plan,
        _evidence(plan, host="not_required"),
        completion_status="collected",
    )

    assert verdict.verdict == "INCOMPLETE"
    assert verdict.reason_codes == ("verification.runner_incomplete",)


def test_trace_profile_requires_collector_completion(tmp_path: Path) -> None:
    plan = _plan(tmp_path, trace=True)

    incomplete = evaluate_verification(
        plan,
        _evidence(plan, collector="not_required"),
        completion_status="collected",
    )
    passed = evaluate_verification(
        plan,
        _evidence(plan, collector="complete"),
        completion_status="collected",
    )

    assert incomplete.verdict == "INCOMPLETE"
    assert passed.verdict == "PASSED"


@pytest.mark.parametrize("state", ["missing", "error", "skipped"])
def test_unavailable_oracle_fact_is_incomplete(tmp_path: Path, state: str) -> None:
    plan = _plan(tmp_path)
    verdict = evaluate_verification(
        plan,
        _evidence(plan, states={"oracle.executed": state}),
        completion_status="incomplete",
    )

    assert verdict.verdict == "INCOMPLETE"
    assert verdict.by_id("oracle.executed").evidence_status == state


def test_business_violation_dominates_missing_telemetry(tmp_path: Path) -> None:
    plan = _plan(tmp_path, trace=True)
    actuals = _actuals(plan)
    actuals["user.row_count"] = 0
    verdict = evaluate_verification(
        plan,
        _evidence(
            plan,
            actuals=actuals,
            states={"trace.user_completed": "missing"},
            collector="error",
        ),
        completion_status="incomplete",
    )

    assert verdict.verdict == "FAILED"
    assert verdict.by_id("user.row_count").business_status == "violated"
    assert verdict.by_id("trace.user_completed").evidence_status == "missing"


def test_missing_required_observation_cannot_shrink_denominator(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    evidence = _evidence(plan).model_copy(
        update={
            "observations": tuple(
                item for item in _evidence(plan).observations if item.obligation_id != "user.email"
            )
        }
    )

    verdict = evaluate_verification(plan, evidence, completion_status="collected")

    assert verdict.verdict == "INCOMPLETE"
    assert (verdict.required, verdict.executed, verdict.evaluated, verdict.satisfied) == (11, 10, 10, 10)


def test_wrong_execution_identity_is_rejected_before_evaluation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    evidence = _evidence(plan).model_copy(update={"execution_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"})

    with pytest.raises(ValueError, match="execution identity"):
        evaluate_verification(
            plan,
            evidence,
            completion_status="collected",
            execution_id=EXECUTION_ID,
        )


def test_empty_obligations_cannot_report_a_pass() -> None:
    with pytest.raises(ValueError, match="required obligations"):
        VerificationVerdictV1(
            validation_profile="api_db.v1",
            execution_id=EXECUTION_ID,
            case_id="case",
            verdict="PASSED",
            required=0,
            executed=0,
            evaluated=0,
            satisfied=0,
            obligations=(),
            reason_codes=(),
        )


def test_typed_verdict_rejects_a_pass_over_a_business_violation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    actuals = _actuals(plan)
    actuals["user.row_count"] = 0
    failed = evaluate_verification(
        plan,
        _evidence(plan, actuals=actuals),
        completion_status="collected",
    )

    with pytest.raises(ValueError, match="verdict contradicts"):
        VerificationVerdictV1.model_validate({**failed.model_dump(mode="json"), "verdict": "PASSED"})


def test_only_verification_verdict_controls_failure_disposition(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    passed = evaluate_verification(plan, _evidence(plan), completion_status="collected")
    wrong = _actuals(plan)
    wrong["user.row_count"] = 0
    failed = evaluate_verification(plan, _evidence(plan, actuals=wrong), completion_status="collected")
    incomplete = evaluate_verification(
        plan,
        _evidence(plan, states={"oracle.executed": "missing"}),
        completion_status="incomplete",
    )

    assert (
        classify_inspection_disposition(facts=verification_failure_facts(passed), coverage_state="satisfied")
        == "satisfied"
    )
    assert (
        classify_inspection_disposition(facts=verification_failure_facts(failed), coverage_state=None)
        == "needs_human"
    )
    assert (
        classify_inspection_disposition(facts=verification_failure_facts(incomplete), coverage_state=None)
        == "blocked"
    )
    assert not verification_failure_facts(incomplete).repairable_failure


def test_untyped_bridge_reason_cannot_make_verified_incomplete_repairable(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    incomplete = evaluate_verification(
        plan,
        _evidence(plan, states={"action.finished": "missing"}),
        completion_status="incomplete",
    )
    bridge = VerificationVerdictV1.model_validate(
        {
            **incomplete.model_dump(mode="json"),
            "reason_codes": sorted({*incomplete.reason_codes, "verification.generated_bridge_missing"}),
        }
    )

    assert (
        classify_inspection_disposition(facts=verification_failure_facts(bridge), coverage_state=None)
        == "blocked"
    )
    for reason in (
        "verification.runner_incomplete",
        "verification.required_evidence_missing",
    ):
        ordinary = incomplete.model_copy(update={"reason_codes": (reason,)})
        assert (
            classify_inspection_disposition(facts=verification_failure_facts(ordinary), coverage_state=None)
            == "blocked"
        )


def test_authenticated_generated_bridge_defect_is_the_only_repairable_incomplete(
    tmp_path: Path,
) -> None:
    prepared = accepted_verified_execution_input(tmp_path)
    generation = prepared.generation_result
    assert generation is not None
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    bridge_path = tmp_path / bridge.path
    bridge_path.write_text("def test_tc_user_create_001__create():\n    pass\n")
    replacement_ref = bridge.model_copy(
        update={"digest": hashlib.sha256(bridge_path.read_bytes()).hexdigest()}
    )
    generation = generation.model_copy(
        update={
            "source_refs": tuple(
                replacement_ref if ref.path == bridge.path else ref for ref in generation.source_refs
            )
        }
    )
    attempt_key = AttemptKey(digest="4" * 64)
    defect = diagnose_verified_bridge_defect(
        tmp_path,
        generation=generation,
        validation_profile="api_db.v1",
        selected_test_families=prepared.selected_test_families,
        capability_leafs=prepared.capability_leafs,
        attempt_key=attempt_key,
    )
    cycle = VerifiedIncompleteExecutionV1(
        defect=defect,
        batch_id=attempt_key.digest,
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
        receipt=ReceiptRef(receipt_id="kernel", receipt_digest="9" * 64),
    )
    from assurance_intake.contracts.plan import decode_plan

    frozen_plan = decode_plan((tmp_path / prepared.plan_ref.path).read_bytes(), prepared.plan_ref)
    request = MaterializeAssessmentInputV1(
        plan_digest=generation.plan_digest,
        plan_ref=generation.plan_ref,
        reviewed_case=generation.reviewed_case,
        generation=generation,
        execution=cycle,
        policy_resource_id="assurance.product.configuration.product-policy",
        policy_sha256=frozen_plan.policy_digest,
        execution_at=cycle.executed_at,
    )

    assessment = materialize_assessment_inputs(
        request,
        project_root=tmp_path,
        write_root=tmp_path,
    )

    assert assessment.execution_ref is None
    assert assessment.incomplete_execution == cycle
    assert assessment.verification_ref is not None
    verdict = VerificationVerdictV1.model_validate_json(
        (tmp_path / assessment.verification_ref.path).read_bytes()
    )
    assert verdict.verdict == "INCOMPLETE"
    assert (verdict.required, verdict.executed, verdict.evaluated, verdict.satisfied) == (
        len(verdict.obligations),
        0,
        0,
        0,
    )
    assert all(
        obligation.evidence_status == "missing" and obligation.business_status == "not_evaluated"
        for obligation in verdict.obligations
    )
    assert verdict.repairable_bridge_defect
    trace = json.loads((tmp_path / assessment.trace_ref.path).read_bytes())
    assert all(
        not row["covering_tests"]
        and row["latest_execution"] is None
        and row["presence_in_current_batch"] == "not_in_current_batch"
        for row in trace["rows"]
    )
    facts = verification_failure_facts(verdict)
    assert facts.repairable_failure and not facts.blocking_failure
    assert classify_inspection_disposition(facts=facts, coverage_state=None) == (
        "repairable_execution_failure"
    )


def test_self_consistent_public_execution_without_host_journal_cannot_pass(tmp_path: Path) -> None:
    request, _, _ = _materialization_request(tmp_path, authenticated=False)

    with pytest.raises(AssessmentInputError, match="host authority"):
        materialize_assessment_inputs(request, project_root=tmp_path, write_root=tmp_path)


def test_authenticated_host_journal_is_replayed_before_business_pass(tmp_path: Path) -> None:
    request, authority, authority_handle = _materialization_request(tmp_path, authenticated=True)

    assessment = materialize_assessment_inputs(
        request,
        project_root=tmp_path,
        write_root=tmp_path,
        secret_port=authority,  # type: ignore[arg-type]
        authority_handle=authority_handle,
    )

    assert assessment.verification_ref is not None
    verdict = VerificationVerdictV1.model_validate_json(
        (tmp_path / assessment.verification_ref.path).read_bytes()
    )
    assert verdict.verdict == "PASSED"


@pytest.mark.parametrize(
    ("http_unknown", "row_count", "http_status", "wrong_email", "process_reason", "expected"),
    [
        (True, 0, 200, False, None, "INCOMPLETE"),
        (True, 1, 200, False, None, "INCOMPLETE"),
        (False, 0, 200, False, None, "FAILED"),
        (False, 0, 500, False, None, "FAILED"),
        (False, 1, 200, True, None, "FAILED"),
        (False, 1, 200, True, "telemetry_unavailable", "FAILED"),
    ],
)
def test_authenticated_materializer_preserves_terminal_dependencies(
    tmp_path: Path,
    http_unknown: bool,
    row_count: int,
    http_status: int,
    wrong_email: bool,
    process_reason: str | None,
    expected: str,
) -> None:
    request, authority, handle = _materialization_request(
        tmp_path,
        authenticated=True,
        http_unknown=http_unknown,
        row_count=row_count,
        http_status=http_status,
        wrong_email=wrong_email,
        process_reason=process_reason,
    )
    assessment = materialize_assessment_inputs(
        request,
        project_root=tmp_path,
        write_root=tmp_path,
        secret_port=authority,  # type: ignore[arg-type]
        authority_handle=handle,
    )
    assert assessment.verification_ref is not None
    verdict = VerificationVerdictV1.model_validate_json(
        (tmp_path / assessment.verification_ref.path).read_bytes()
    )
    assert verdict.verdict == expected
    assert verdict.by_id("oracle.executed").evidence_status == "observed"
    if http_unknown:
        for obligation in verdict.obligations:
            if obligation.obligation_id == "action.finished" or obligation.obligation_id.startswith("user."):
                assert obligation.evidence_status == "missing"
                assert obligation.business_status == "not_evaluated"
                assert obligation.reason == "http_terminal_unknown"
    if process_reason:
        assert "verification.runner_incomplete" in verdict.reason_codes
        assert verdict.by_id("user.email").business_status == "violated"
    if not http_unknown and row_count == 0:
        assert verdict.by_id("user.row_count").business_status == "violated"
        assert verdict.by_id("user.email").evidence_status == "missing"
        assert verdict.by_id("user.email").business_status == "not_evaluated"
        assert "verification.required_evidence_missing" in verdict.reason_codes


def test_authenticated_journal_rejects_forged_index_batch_and_arbitrary_receipt(
    tmp_path: Path,
) -> None:
    from graph_engine.attempts.resolutions import ReceiptRef

    request, authority, authority_handle = _materialization_request(tmp_path, authenticated=True)
    cycle = cast(VerifiedExecutionCycleResultV1, request.execution)
    forged = VerifiedExecutionResultV1.model_validate_json(
        (tmp_path / cycle.execution_index_ref.path).read_bytes()
    ).model_copy(update={"batch_id": "forged-batch"})
    encoded = (json.dumps(forged.model_dump(mode="json"), indent=2) + "\n").encode()
    (tmp_path / cycle.execution_index_ref.path).write_bytes(encoded)
    forged_cycle = cycle.model_copy(
        update={
            "batch_id": "forged-batch",
            "execution_index_ref": cycle.execution_index_ref.model_copy(
                update={"digest": hashlib.sha256(encoded).hexdigest()}
            ),
            "receipt": ReceiptRef(receipt_id="forged", receipt_digest="f" * 64),
        }
    )

    with pytest.raises(AssessmentInputError, match="authenticated host execution result"):
        materialize_assessment_inputs(
            request.model_copy(update={"execution": forged_cycle}),
            project_root=tmp_path,
            write_root=tmp_path,
            secret_port=authority,  # type: ignore[arg-type]
            authority_handle=authority_handle,
        )


@pytest.mark.parametrize(
    "field",
    ("execution_id", "attempt_key", "executed_at", "completion_status", "manifest_ref", "evidence_ref"),
)
def test_authenticated_journal_rejects_other_forged_execution_identity_fields(
    tmp_path: Path,
    field: str,
) -> None:
    from datetime import timedelta

    request, authority, authority_handle = _materialization_request(tmp_path, authenticated=True)
    cycle = cast(VerifiedExecutionCycleResultV1, request.execution)
    indexed = VerifiedExecutionResultV1.model_validate_json(
        (tmp_path / cycle.execution_index_ref.path).read_bytes()
    )
    if field == "execution_id":
        execution_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        authority_ref = indexed.execution_authority_ref.model_copy(
            update={
                "path": (f"qa/changes/{indexed.change_id}/execution/{execution_id}/execution_terminal.json")
            }
        )
        forged_authority_path = tmp_path / authority_ref.path
        forged_authority_path.parent.mkdir(parents=True, exist_ok=True)
        forged_authority_path.write_bytes((tmp_path / indexed.execution_authority_ref.path).read_bytes())
        evidence = indexed.evidence.model_copy(
            update={
                "execution_id": execution_id,
                "observations": tuple(
                    item.model_copy(update={"execution_id": execution_id})
                    for item in indexed.evidence.observations
                ),
            }
        )
        update = {
            field: execution_id,
            "evidence": evidence,
            "execution_authority_ref": authority_ref,
        }
    elif field == "attempt_key":
        update = {field: AttemptKey(digest="e" * 64)}
    elif field == "executed_at":
        update = {field: indexed.executed_at + timedelta(seconds=1)}
    elif field == "completion_status":
        update = {
            field: "incomplete",
            "evidence": indexed.evidence.model_copy(update={"state": "incomplete"}),
        }
    else:
        original = cast(EvidenceArtifactRefV1, getattr(indexed, field))
        copied = original.model_copy(update={"path": f"qa/changes/{indexed.change_id}/{field}.json"})
        copied_path = tmp_path / copied.path
        copied_path.parent.mkdir(parents=True, exist_ok=True)
        copied_path.write_bytes((tmp_path / original.path).read_bytes())
        update = {field: copied}
    forged = VerifiedExecutionResultV1.model_validate({**indexed.model_dump(mode="python"), **update})
    encoded = (json.dumps(forged.model_dump(mode="json"), indent=2) + "\n").encode()
    (tmp_path / cycle.execution_index_ref.path).write_bytes(encoded)
    cycle_update = {
        field: getattr(forged, field),
        "execution_index_ref": cycle.execution_index_ref.model_copy(
            update={"digest": hashlib.sha256(encoded).hexdigest()}
        ),
    }
    if field == "execution_id":
        cycle_update["execution_authority_ref"] = forged.execution_authority_ref
    forged_cycle = cycle.model_copy(update=cycle_update)

    with pytest.raises(AssessmentInputError):
        materialize_assessment_inputs(
            request.model_copy(
                update={
                    "execution": forged_cycle,
                    "execution_at": forged.executed_at,
                }
            ),
            project_root=tmp_path,
            write_root=tmp_path,
            secret_port=authority,  # type: ignore[arg-type]
            authority_handle=authority_handle,
        )
