from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
import hashlib
import json

import pytest

from assurance_execution.contracts.verification import (
    ObservationV1,
    VerificationEvidenceV1,
    VerifiedProcessLimitsV1,
)
from assurance_execution.operations.verified_execution import ActionJournal
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1, CaseExecutionPlanV1
from assurance_quality.contracts.verification import VerificationVerdictV1
from assurance_quality.operations.verification import evaluate_verification
from assurance_quality.operations.verification import verification_failure_facts
from assurance_quality.contracts.decisions import classify_inspection_disposition
from assurance_quality.contracts.assessment import MaterializeAssessmentInputV1
from assurance_quality.operations.assessment import AssessmentInputError, materialize_assessment_inputs
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from tests.verified_generation_fixture import accepted_verified_execution_input
from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.plugin_api import TaskOutcome
from graph_engine.canonical import JSONValue, canonical_json_bytes
from assurance_execution.contracts.verification import (
    VerificationManifestV1,
    VerifiedExecutionAuthorityV1,
    VerifiedExecutionResultV1,
)
from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from assurance_execution.contracts.workflow import VerifiedIncompleteExecutionV1
from assurance_generation.contracts.admission import diagnose_verified_bridge_defect
from graph_engine.attempts.resolutions import ReceiptRef
from typing import cast

EXECUTION_ID = "12345678-1234-4123-8123-123456789abc"


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


def _actuals(plan) -> dict[str, object]:
    values = {
        "initial.user_absent": 0,
        "action.finished": True,
        "oracle.executed": True,
        "api.http_status": 200,
        "api.code": 200,
        "user.row_count": 1,
        "user.username": plan.inputs["username"],
        "user.email": plan.inputs["email"],
        "user.is_active": plan.inputs["is_active"],
        "user.is_superuser": plan.inputs["is_superuser"],
        "user.dept_id": plan.inputs["dept_id"],
        "trace.http": True,
        "trace.user_write": True,
        "trace.user_completed": True,
        "trace.drained": True,
    }
    return {key: values[key] for key in plan.required}


def _evidence(plan, *, actuals=None, states=None, host="complete", collector="not_required"):
    actuals = _actuals(plan) if actuals is None else actuals
    states = states or {}
    observations = []
    for obligation in plan.required:
        state = states.get(obligation, "observed")
        observations.append(
            ObservationV1.model_validate(
                {
                    "execution_id": EXECUTION_ID,
                    "obligation_id": obligation,
                    "state": state,
                    "actual": actuals.get(obligation),
                    "evidence_ref": (
                        {"path": f"qa/changes/{plan.change_id}/execution/fact.json", "digest": "a" * 64}
                        if state == "observed"
                        else None
                    ),
                    "reason": None if state == "observed" else "not_available",
                }
            )
        )
    return VerificationEvidenceV1.model_validate(
        {
            "execution_id": EXECUTION_ID,
            "manifest_digest": "b" * 64,
            "receipt_ref": {
                "path": f"qa/changes/{plan.change_id}/execution/process.json",
                "digest": "c" * 64,
            },
            "observations": tuple(observations),
            "host_completion": {
                "state": host,
                "reason": None if host in {"complete", "not_required"} else "host_failed",
            },
            "collector_completion": {
                "state": collector,
                "reason": None if collector in {"complete", "not_required"} else "collector_failed",
            },
            "state": (
                "collected"
                if host in {"complete", "not_required"} and collector in {"complete", "not_required"}
                else "incomplete"
            ),
        }
    )


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


def _write(root: Path, relative: str, payload: bytes):
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(payload).hexdigest())


def _materialization_request(
    tmp_path: Path, *, authenticated: bool
) -> tuple[MaterializeAssessmentInputV1, object | None, str | None]:
    prepared = accepted_verified_execution_input(tmp_path)
    generation = prepared.generation_result
    assert generation is not None and generation.case_execution_plan_ref is not None
    plan = CaseExecutionPlanSetV1.model_validate_json(
        (tmp_path / generation.case_execution_plan_ref.path).read_bytes()
    ).cases[0]
    execution_id = EXECUTION_ID
    run_root = tmp_path / "run"
    run_root.mkdir()
    sqlite = tmp_path / "verified.sqlite3"
    identity = sqlite.stat()
    manifest = VerificationManifestV1.model_validate(
        {
            "execution_id": execution_id,
            "change_id": plan.change_id,
            "case_id": plan.case_id,
            "nodeid": "tests/api/test_user_create.py::test_tc_user_create_001__create",
            "invocation_id": "invocation",
            "task_id": "task",
            "graph_instance_id": "graph",
            "attempt_key": AttemptKey(digest="4" * 64),
            "business_activation": BusinessActivation.for_trigger("coverage.2.execute"),
            "coverage_epoch": plan.coverage_epoch,
            "repair_round": 0,
            "authorization_scope_digest": "5" * 64,
            "activity_receipt_digest": "6" * 64,
            "plan_ref": plan.plan_ref.path,
            "plan_digest": plan.plan_digest,
            "case_execution_plan_ref": generation.case_execution_plan_ref.path,
            "case_execution_plan_digest": generation.case_execution_plan_ref.digest,
            "spec_digest": plan.spec_digest,
            "mapping_digest": generation.mapping_ref.digest,
            "sut_digest": plan.sut_digest,
            "technical_config_digest": plan.technical_config_digest,
            "validation_profile": plan.validation_profile,
            "sut": {
                "instance_id": "sut",
                "base_url": "http://127.0.0.1:1234",
                "sqlite_path": str(sqlite),
            },
            "sqlite": {"path": str(sqlite), "device": identity.st_dev, "inode": identity.st_ino},
            "inputs": plan.inputs,
            "evidence_root": f"qa/changes/{plan.change_id}/execution/{execution_id}",
        }
    )
    manifest_ref = _write(
        tmp_path,
        f"qa/changes/{plan.change_id}/execution/{execution_id}/manifest.json",
        canonical_json_bytes(cast(JSONValue, manifest.model_dump(mode="json"))),
    )
    authority = None
    authority_handle = None
    journal: ActionJournal | None = None
    if authenticated:
        from assurance_execution.contracts.verification import (
            ManagedSutAuthorityV1,
            VerifiedProcessReceiptV1,
        )

        token = bytes(range(32))
        token_path = run_root / ".ownership-token"
        token_path.write_bytes(token)
        token_path.chmod(0o600)
        token_stat = token_path.stat()
        authority_document = ManagedSutAuthorityV1.model_validate(
            {
                "run_root": str(run_root),
                "ownership_token": {
                    "path": str(token_path),
                    "device": token_stat.st_dev,
                    "inode": token_stat.st_ino,
                    "digest": f"sha256:{hashlib.sha256(token).hexdigest()}",
                },
                "prepare_receipt_digest": "7" * 64,
                "start_receipt_digest": "8" * 64,
                "authorization_scope_digest": manifest.authorization_scope_digest,
                "activity_receipt_digest": manifest.activity_receipt_digest,
            }
        )

        class _Secrets:
            def resolve(self, handle: str) -> bytes:
                assert handle == "sut.authority"
                return canonical_json_bytes(cast(JSONValue, authority_document.model_dump(mode="json")))

        authority = _Secrets()
        authority_handle = "sut.authority"
        journal = ActionJournal(tmp_path / manifest.evidence_root, manifest, token)
        journal.write("action_started", {"execution_id": execution_id, "state": "started"})
        database_identity = {
            "path": str(sqlite),
            "device": identity.st_dev,
            "inode": identity.st_ino,
        }
        database_metadata = {"size": identity.st_size, "mtime_ns": identity.st_mtime_ns}
        journal.write(
            "action_terminal",
            {
                "initial": {
                    "state": "observed",
                    "rows": [],
                    "reason": None,
                    "database_identity": database_identity,
                    "database_metadata": database_metadata,
                },
                "http": {"state": "observed", "status": 200, "code": 200},
                "oracle": {
                    "state": "observed",
                    "rows": [
                        {
                            "username": plan.inputs["username"],
                            "email": plan.inputs["email"],
                            "is_active": plan.inputs["is_active"],
                            "is_superuser": plan.inputs["is_superuser"],
                            "dept_id": plan.inputs["dept_id"],
                        }
                    ],
                    "reason": None,
                    "database_identity": database_identity,
                    "database_metadata": database_metadata,
                },
            },
        )
        journal.write(
            "process_terminal",
            VerifiedProcessReceiptV1(
                command=("pytest",),
                limits=VerifiedProcessLimitsV1(),
                exit_code=0,
                report={},
                reason=None,
                request_count=1,
                stderr="",
                cleanup_confirmed=True,
            ).model_dump(mode="json"),
        )
        action_ref = next(
            EvidenceArtifactRefV1(
                path=f"{manifest.evidence_root}/{name}.json",
                digest=hashlib.sha256((journal.root / f"{name}.json").read_bytes()).hexdigest(),
            )
            for name in ("action_terminal",)
        )
        process_ref = EvidenceArtifactRefV1(
            path=f"{manifest.evidence_root}/process_terminal.json",
            digest=hashlib.sha256((journal.root / "process_terminal.json").read_bytes()).hexdigest(),
        )
        raw_refs = tuple(
            sorted(
                (
                    EvidenceArtifactRefV1(
                        path=f"{manifest.evidence_root}/{name}.json",
                        digest=hashlib.sha256((journal.root / f"{name}.json").read_bytes()).hexdigest(),
                    )
                    for name in ("action_started", "action_terminal", "process_terminal")
                ),
                key=lambda item: (item.path, item.digest),
            )
        )
    else:
        process_ref = _write(
            tmp_path,
            f"qa/changes/{plan.change_id}/execution/{execution_id}/process_terminal.json",
            b"process\n",
        )
        action_ref = process_ref
        raw_refs = (process_ref,)
    evidence = _evidence(plan).model_copy(
        update={
            "manifest_digest": manifest_ref.digest,
            "receipt_ref": process_ref,
            "observations": tuple(
                item.model_copy(update={"evidence_ref": action_ref}) for item in _evidence(plan).observations
            ),
        }
    )
    outcome = TaskOutcome.succeeded(evidence.model_dump(mode="json"))
    if authenticated:
        assert journal is not None
        journal.write("outcome", outcome.model_dump(mode="json"))
        outcome_ref = EvidenceArtifactRefV1(
            path=f"{manifest.evidence_root}/outcome.json",
            digest=hashlib.sha256((journal.root / "outcome.json").read_bytes()).hexdigest(),
        )
    else:
        outcome_ref = _write(
            tmp_path,
            f"qa/changes/{plan.change_id}/execution/{execution_id}/outcome.json",
            canonical_json_bytes(cast(JSONValue, outcome.model_dump(mode="json"))),
        )
    authority_result = VerifiedExecutionAuthorityV1(
        validation_profile=plan.validation_profile,
        change_id=plan.change_id,
        case_id=plan.case_id,
        reviewed_case=plan.reviewed_case,
        coverage_epoch=plan.coverage_epoch,
        repair_round=0,
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        case_execution_plan_ref=generation.case_execution_plan_ref,
        case_execution_plan_digest=generation.case_execution_plan_ref.digest,
        spec_digest=plan.spec_digest,
        execution_id=execution_id,
        attempt_key=manifest.attempt_key,
        batch_id="verified-batch",
        mapping_digest=generation.mapping_ref.digest,
        manifest_ref=manifest_ref,
        evidence_ref=outcome_ref,
        raw_evidence_refs=raw_refs,
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
        completion_status="collected",
        evidence=evidence,
    )
    if authenticated:
        assert journal is not None
        journal.write("execution_terminal", authority_result.model_dump(mode="json"))
        execution_authority_ref = EvidenceArtifactRefV1(
            path=f"{manifest.evidence_root}/execution_terminal.json",
            digest=hashlib.sha256((journal.root / "execution_terminal.json").read_bytes()).hexdigest(),
        )
    else:
        execution_authority_ref = _write(
            tmp_path,
            f"{manifest.evidence_root}/execution_terminal.json",
            b"authority\n",
        )
    verified = VerifiedExecutionResultV1(
        **authority_result.model_dump(mode="python"),
        execution_authority_ref=execution_authority_ref,
    )
    encoded = (json.dumps(verified.model_dump(mode="json"), indent=2) + "\n").encode()
    index_ref = _write(tmp_path, f"qa/changes/{plan.change_id}/execution/execute-result.json", encoded)
    cycle = VerifiedExecutionCycleResultV1.model_validate(
        {
            **verified.model_dump(mode="json", exclude={"evidence"}),
            "mapping_ref": generation.mapping_ref.model_dump(mode="json"),
            "execution_index_ref": index_ref.model_dump(mode="json"),
            "source_refs": [item.model_dump(mode="json") for item in generation.source_refs],
            "receipt": {"receipt_id": "kernel", "receipt_digest": "9" * 64},
        }
    )
    request = MaterializeAssessmentInputV1(
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        reviewed_case=plan.reviewed_case,
        generation=generation,
        execution=cycle,
        policy_resource_id="assurance.product.configuration.product-policy",
        policy_sha256=prepared.plan_ref.digest,  # replaced below from the frozen plan
        execution_at=cycle.executed_at,
    )
    from assurance_intake.contracts.plan import decode_plan

    root_plan = decode_plan((tmp_path / prepared.plan_ref.path).read_bytes(), prepared.plan_ref)
    request = request.model_copy(update={"policy_sha256": root_plan.policy_digest})

    return request, authority, authority_handle


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
