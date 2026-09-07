from __future__ import annotations

from assurance_execution.contracts.workflow import ExecutionCycleResultV1, VerifiedExecutionCycleResultV1
from assurance_generation.contracts.workflow import (
    GenerationCycleResultV1,
    VerifiedGenerationDefectV1,
)
from assurance_healing.contracts.application import AppliedTestRepairV1
from graph_engine.attempts import AttemptKey
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.routes import route_applied_repair
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1

from tests.product.test_product_stategraph_flow import (
    _applied,
    _flow_features,
    _inspection,
    _report,
    _product_graphs,
    _public_input,
)
from tests.product.test_execution_quality_flow import _verified_state


def test_applied_test_repair_is_the_only_path_to_rerun() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=(_inspection(disposition="repairable_execution_failure"), _inspection()),
                repair_failure=_applied(),
            )
        ),
        "execute",
        _public_input("execute"),
    )
    execution = ExecutionCycleResultV1.model_validate(result["execution_result"])
    assert execution.repair_round == 1
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "reported"


def test_fix_proposal_output_cannot_parse_as_applied_repair() -> None:
    proposal_only = {
        "proposal_result": {"schema_version": "1", "change_id": "CH-DEMO-001"},
        "status": "passed",
    }
    assert route_applied_repair(proposal_only) == "blocked"
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_inspection(disposition="repairable_execution_failure"),
                repair_failure=proposal_only,
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"
    assert ExecutionCycleResultV1.model_validate(result["execution_result"]).repair_round == 0


def test_non_applied_repair_status_is_blocked() -> None:
    repair = {
        "repair_result": {
            "change_id": "CH-DEMO-001",
            "coverage_epoch": 0,
            "repair_round": 1,
            "status": "exhausted",
        }
    }
    assert route_applied_repair(repair) == "blocked"


def test_full_verified_pre_dispatch_defect_runs_apply_repair_then_fresh_execution(tmp_path) -> None:
    verified = _verified_state(tmp_path)
    generation = GenerationCycleResultV1.model_validate(verified["generation_result"])
    initial = VerifiedExecutionCycleResultV1.model_validate(verified["execution_result"])
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    defect = VerifiedGenerationDefectV1(
        defect_kind="invalid_bridge",
        generation=generation,
        validation_profile="api_db.v1",
        attempt_key=AttemptKey(digest="4" * 64),
        case_id=initial.case_id,
        bridge_symbol="test_tc_user_create_001__create",
        bridge_ref=bridge,
        observed_digest="8" * 64,
        expected_digest="9" * 64,
    )
    repair = AppliedTestRepairV1(
        change_id=generation.change_id,
        plan_digest=generation.plan_digest,
        plan_ref=generation.plan_ref,
        coverage_epoch=generation.coverage_epoch,
        repair_round=1,
        status="applied",
        changed_test_refs=(bridge.model_copy(update={"digest": defect.expected_digest}),),
        mapping_ref=generation.mapping_ref,
        receipt=ReceiptRef(receipt_id="verified-repair", receipt_digest="7" * 64),
    )
    rerun = initial.model_copy(
        update={
            "repair_round": 1,
            "execution_id": "87654321-4321-4321-8321-cba987654321",
            "batch_id": "verified-rerun-batch",
            "attempt_key": AttemptKey(digest="6" * 64),
            "source_refs": repair.changed_test_refs
            + tuple(ref for ref in generation.source_refs if ref.path != bridge.path),
        }
    )
    features = _flow_features(
        prepare={
            "plan_digest": generation.plan_digest,
            "plan_ref": generation.plan_ref.model_dump(mode="json"),
            "selected_test_families": ["api"],
            "preparation_refs": [
                ref.model_dump(mode="json") for ref in generation.reviewed_case.preparation_refs
            ],
            "status": "prepared",
        },
        case={
            "reviewed_case": generation.reviewed_case.model_dump(mode="json"),
            "case_receipt": ReceiptRef(receipt_id="verified-case", receipt_digest="3" * 64).model_dump(
                mode="json"
            ),
            "coverage_epoch": generation.coverage_epoch,
            "status": "reviewed",
        },
        generation={
            "generation_result": generation.model_dump(mode="json"),
            "coverage_epoch": generation.coverage_epoch,
            "status": "passed",
        },
        execute={
            "execution_result": defect.model_dump(mode="json"),
            "status": "generation_defect",
        },
        repair_failure={
            "repair_result": repair.model_dump(mode="json"),
            "rounds_used": 1,
            "healing_rounds_used": 1,
            "status": "applied",
        },
        run={"execution_result": rerun.model_dump(mode="json"), "status": "collected"},
        assess=_inspection(epoch=generation.coverage_epoch),
        report=_report(epoch=generation.coverage_epoch),
    )
    product_input = _public_input(
        "full",
        validation_profile="api_db.v1",
        verification_config_digest="5" * 64,
        verification_policy={
            "resource_id": "assurance.product.configuration.verification-policy",
            "sha256": "6" * 64,
        },
    )

    result = invoke_product_root(_product_graphs(features), "full", product_input)

    final = VerifiedExecutionCycleResultV1.model_validate(result["execution_result"])
    assert final.execution_id == rerun.execution_id
    assert final.execution_id != initial.execution_id
    assert final.repair_round == 1
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "reported"
    assert result["terminal"] == {"status": "completed", "reason": "achieved"}
