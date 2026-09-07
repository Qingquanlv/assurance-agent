from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from graph_engine.attempts import AttemptKey
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_product.graphs.execute import adapt_quality_assess
from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.routes import route_execute, route_run
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from tests.product.test_product_stategraph_flow import (
    _flow_features,
    _inspection,
    _product_graphs,
    _public_input,
)
from tests.verified_generation_fixture import accepted_verified_execution_input


def test_passed_execution_reaches_inspect_and_committed_report() -> None:
    result = invoke_product_root(_product_graphs(), "execute", _public_input("execute"))
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "reported"
    assert tail.inspection is not None
    assert tail.report is not None


def test_coverage_insufficient_returns_without_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="coverage_insufficient"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "coverage_insufficient"
    assert result["terminal"] == {"status": "stopped", "reason": "coverage_insufficient"}
    assert tail.report is None
    assert tail.report_refs == ()


def test_needs_human_returns_without_test_repair_or_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="needs_human"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "needs_human"
    assert result["terminal"] == {"status": "stopped", "reason": "needs_human"}
    assert tail.report is None
    assert "repair_result" not in result


def test_invalid_execution_result_blocks_before_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(execute={"status": "failed", "attempt_failure": {"kind": "runtime"}})),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "blocked"
    assert "inspection_outcome" not in result
    assert result.get("execution_result") in (None, {})


def _verified_state(root: Path, completion: str = "collected") -> dict[str, Any]:
    prepared = accepted_verified_execution_input(root, change_id="CH-DEMO-001")
    generation_model = prepared.generation_result
    assert generation_model is not None
    machine_ref = generation_model.case_execution_plan_ref
    assert machine_ref is not None
    execution_id = "12345678-1234-4123-8123-123456789abc"
    process_ref = generation_model.mapping_ref.model_copy(
        update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/process_terminal.json"}
    )
    cycle = VerifiedExecutionCycleResultV1(
        validation_profile="api_db.v1",
        change_id=prepared.change_id,
        case_id="TC_USER_CREATE_001",
        reviewed_case=generation_model.reviewed_case,
        coverage_epoch=prepared.coverage_epoch,
        repair_round=0,
        plan_digest=prepared.plan_digest,
        plan_ref=prepared.plan_ref,
        case_execution_plan_ref=machine_ref,
        case_execution_plan_digest=machine_ref.digest,
        spec_digest=generation_model.reviewed_case.preparation_refs[0].digest,
        execution_id=execution_id,
        attempt_key=AttemptKey(digest="4" * 64),
        batch_id="verified-batch",
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
        completion_status=cast(Any, completion),
        mapping_ref=generation_model.mapping_ref,
        mapping_digest=generation_model.mapping_ref.digest,
        manifest_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/manifest.json"}
        ),
        evidence_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/outcome.json"}
        ),
        execution_index_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/execute-result.json"}
        ),
        raw_evidence_refs=(process_ref,),
        source_refs=generation_model.source_refs,
        receipt=ReceiptRef(receipt_id="verified-execution", receipt_digest="f" * 64),
    )
    # adapt_quality only needs coherent generation/cycle identities; use the
    # cycle's ReviewedCase and machine-plan bindings in the generation DTO.
    generation = {
        "change_id": cycle.change_id,
        "coverage_epoch": cycle.coverage_epoch,
        "reviewed_case": cycle.reviewed_case.model_dump(mode="json"),
        "plan_digest": cycle.plan_digest,
        "plan_ref": cycle.plan_ref.model_dump(mode="json"),
        "mapping_ref": cycle.mapping_ref.model_dump(mode="json"),
        "source_refs": [item.model_dump(mode="json") for item in cycle.source_refs],
        "plan_refs": [cycle.case_execution_plan_ref.model_dump(mode="json")],
        "case_execution_plan_ref": cycle.case_execution_plan_ref.model_dump(mode="json"),
        "case_execution_plan_digest": cycle.case_execution_plan_digest,
    }
    state = cast(dict[str, Any], _public_input("full"))
    state.update(
        {
            "change_id": cycle.change_id,
            "coverage_epoch": cycle.coverage_epoch,
            "validation_profile": cycle.validation_profile,
            "generation_result": generation,
            "execution_result": cycle.model_dump(mode="json"),
            "reviewed_case": cycle.reviewed_case.model_dump(mode="json"),
        }
    )
    return state


def test_collected_and_incomplete_verified_cycles_both_reach_quality(tmp_path: Path) -> None:
    for completion in ("collected", "incomplete"):
        state = _verified_state(tmp_path / completion, completion)
        assert route_execute(state) == "quality"
        assert route_run(state) == "quality"
        adapted = adapt_quality_assess(cast(Any, state))
        assert adapted["execution_status"] == completion
        feature_input = cast(dict[str, Any], adapted["feature_input"])
        execution_result = cast(dict[str, Any], feature_input["execution_result"])
        assert execution_result["completion_status"] == completion
