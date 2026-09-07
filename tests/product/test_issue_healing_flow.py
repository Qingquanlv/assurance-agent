from __future__ import annotations

from typing import cast, Any

from langgraph.graph import END, START, StateGraph
import pytest

from assurance_execution.contracts.workflow import ExecutionCycleResultV1, VerifiedExecutionCycleResultV1
from assurance_generation.contracts.workflow import (
    GenerationCycleResultV1,
    VerifiedGenerationDefectV1,
)
from assurance_healing.contracts.application import AppliedTestRepairV1
from assurance_healing.graphs.factory import HealingGraphs
from graph_engine.attempts import AttemptKey
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_product.graphs.execute import adapt_repair_failure
from assurance_product.graphs.factory import invoke_product_root
from assurance_product.generation_defect_authority import GenerationDefectRouteAuthenticator
from assurance_product.graphs.routes import execute_named_matches, route_applied_repair, route_execute
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
from tests.verified_generation_fixture import install_verified_generation_defect_cycle


def _route_defect_state(
    project,
    *,
    invocation_id: str,
    repair_round: int = 0,
):
    verified = _verified_state(project)
    generation = GenerationCycleResultV1.model_validate(verified["generation_result"])
    initial = VerifiedExecutionCycleResultV1.model_validate(verified["execution_result"])
    state = {
        **_public_input(
            "full",
            validation_profile="api_db.v1",
            verification_config_digest="5" * 64,
            verification_policy={
                "resource_id": "assurance.product.configuration.verification-policy",
                "sha256": "6" * 64,
            },
        ),
        "change_id": generation.change_id,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "reviewed_case": generation.reviewed_case.model_dump(mode="json"),
        "selected_test_families": ["api"],
        "coverage_epoch": generation.coverage_epoch,
        "repair_round": repair_round,
        "rounds_budget": 1,
        "rounds_used": repair_round,
        "healing_rounds_used": 0,
        "generation_result": generation.model_dump(mode="json"),
    }
    authenticator = GenerationDefectRouteAuthenticator(
        project_root=project,
        invocation_id=invocation_id,
        public_entrypoint="full",
        graph_revision="3" * 64,
    )
    binding = authenticator.expected_binding(state)
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    defect = VerifiedGenerationDefectV1(
        defect_kind="invalid_bridge",
        generation=generation,
        validation_profile="api_db.v1",
        attempt_key=binding.attempt_key,
        case_id=initial.case_id,
        bridge_symbol="test_tc_user_create_001__create",
        bridge_ref=bridge,
        observed_digest="8" * 64,
        expected_digest="9" * 64,
    )
    cycle = install_verified_generation_defect_cycle(
        project,
        defect,
        execution_binding=binding,
    )
    state.update(
        {
            "execution_result": cycle.model_dump(mode="json"),
            "generation_defect": cycle.model_dump(mode="json"),
            "generation_defect_authority_ref": cycle.attempt.authority_receipt.model_dump(mode="json"),
            "generation_defect_execution_binding": binding.model_dump(mode="json"),
            "status": "generation_defect",
        }
    )
    return state, cycle, authenticator


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
    product_input = _public_input(
        "full",
        validation_profile="api_db.v1",
        verification_config_digest="5" * 64,
        verification_policy={
            "resource_id": "assurance.product.configuration.verification-policy",
            "sha256": "6" * 64,
        },
    )
    authenticator = GenerationDefectRouteAuthenticator(
        project_root=tmp_path,
        invocation_id="inv-current-full",
        public_entrypoint="full",
        graph_revision="3" * 64,
    )
    binding_state = {
        **product_input,
        "change_id": generation.change_id,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "selected_test_families": ["api"],
        "coverage_epoch": generation.coverage_epoch,
        "repair_round": 0,
        "rounds_budget": 1,
        "rounds_used": 0,
        "generation_result": generation.model_dump(mode="json"),
    }
    binding = authenticator.expected_binding(binding_state)
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    defect = VerifiedGenerationDefectV1(
        defect_kind="invalid_bridge",
        generation=generation,
        validation_profile="api_db.v1",
        attempt_key=binding.attempt_key,
        case_id=initial.case_id,
        bridge_symbol="test_tc_user_create_001__create",
        bridge_ref=bridge,
        observed_digest="8" * 64,
        expected_digest="9" * 64,
    )
    defect_cycle = install_verified_generation_defect_cycle(
        tmp_path,
        defect,
        execution_binding=binding,
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
            "execution_result": defect_cycle.model_dump(mode="json"),
            "generation_defect": defect_cycle.model_dump(mode="json"),
            "generation_defect_authority_ref": defect_cycle.attempt.authority_receipt.model_dump(mode="json"),
            "generation_defect_execution_binding": binding.model_dump(mode="json"),
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
    result = invoke_product_root(
        _product_graphs(features, authenticate_generation_defect=authenticator),
        "full",
        product_input,
    )

    final = VerifiedExecutionCycleResultV1.model_validate(result["execution_result"])
    assert final.execution_id == rerun.execution_id
    assert final.execution_id != initial.execution_id
    assert final.repair_round == 1
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "reported"
    assert result["terminal"] == {"status": "completed", "reason": "achieved"}


def test_verified_generation_defect_checkpoint_round_trip_keeps_exclusive_repair_route(
    tmp_path,
) -> None:
    from graph_engine.persistence.journal import strict_checkpoint_serializer

    verified = _verified_state(tmp_path)
    generation = GenerationCycleResultV1.model_validate(verified["generation_result"])
    initial = VerifiedExecutionCycleResultV1.model_validate(verified["execution_result"])
    state = {
        **verified,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "selected_test_families": ["api"],
        "repair_round": 0,
    }
    authenticator = GenerationDefectRouteAuthenticator(
        project_root=tmp_path,
        invocation_id="inv-checkpoint",
        public_entrypoint="full",
        graph_revision="3" * 64,
    )
    binding = authenticator.expected_binding(state)
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    defect = VerifiedGenerationDefectV1(
        defect_kind="invalid_bridge",
        generation=generation,
        validation_profile="api_db.v1",
        attempt_key=binding.attempt_key,
        case_id=initial.case_id,
        bridge_symbol="test_tc_user_create_001__create",
        bridge_ref=bridge,
        observed_digest="8" * 64,
        expected_digest="9" * 64,
    )
    cycle = install_verified_generation_defect_cycle(
        tmp_path,
        defect,
        execution_binding=binding,
    )
    state = {
        **verified,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "selected_test_families": ["api"],
        "repair_round": 0,
        "execution_result": cycle.model_dump(mode="json"),
        "generation_defect": cycle.model_dump(mode="json"),
        "generation_defect_authority_ref": cycle.attempt.authority_receipt.model_dump(mode="json"),
        "generation_defect_execution_binding": binding.model_dump(mode="json"),
        "status": "generation_defect",
    }
    serializer = strict_checkpoint_serializer()
    encoded = serializer.dumps_typed(state)
    recovered = serializer.loads_typed(encoded)

    assert recovered == state
    assert execute_named_matches(
        recovered,
        authenticate_generation_defect=authenticator,
    ) == {"quality": None, "repair": "repair"}


def test_self_consistent_generation_defect_without_route_authority_never_reaches_repair(
    tmp_path,
) -> None:
    project = tmp_path / "generation"
    verified = _verified_state(project)
    generation = GenerationCycleResultV1.model_validate(verified["generation_result"])
    initial = VerifiedExecutionCycleResultV1.model_validate(verified["execution_result"])
    product_input = _public_input(
        "execute",
        validation_profile="api_db.v1",
        verification_config_digest="5" * 64,
        verification_policy={
            "resource_id": "assurance.product.configuration.verification-policy",
            "sha256": "6" * 64,
        },
    )
    authenticator = GenerationDefectRouteAuthenticator(
        project_root=project,
        invocation_id="inv-current-no-receipts",
        public_entrypoint="execute",
        graph_revision="3" * 64,
    )
    binding_state = {
        **product_input,
        "change_id": generation.change_id,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "selected_test_families": ["api"],
        "coverage_epoch": generation.coverage_epoch,
        "repair_round": 0,
        "generation_result": generation.model_dump(mode="json"),
    }
    binding = authenticator.expected_binding(binding_state)
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    defect = VerifiedGenerationDefectV1(
        defect_kind="invalid_bridge",
        generation=generation,
        validation_profile="api_db.v1",
        attempt_key=binding.attempt_key,
        case_id=initial.case_id,
        bridge_symbol="test_tc_user_create_001__create",
        bridge_ref=bridge,
        observed_digest="8" * 64,
        expected_digest="9" * 64,
    )
    cycle = install_verified_generation_defect_cycle(
        tmp_path / "unrelated-authority",
        defect,
        execution_binding=binding,
    )
    repair_calls = 0

    def count_repair(state: object) -> dict[str, object]:
        nonlocal repair_calls
        del state
        repair_calls += 1
        return {"status": "failed"}

    repair_builder = StateGraph(cast(Any, dict))
    repair_builder.add_node("count", cast(Any, count_repair))
    repair_builder.add_edge(START, "count")
    repair_builder.add_edge("count", END)
    features = _flow_features(
        generation={
            "generation_result": generation.model_dump(mode="json"),
            "coverage_epoch": generation.coverage_epoch,
            "status": "passed",
        },
        execute={
            "execution_result": cycle.model_dump(mode="json"),
            "generation_defect": cycle.model_dump(mode="json"),
            "generation_defect_authority_ref": cycle.attempt.authority_receipt.model_dump(mode="json"),
            "generation_defect_execution_binding": binding.model_dump(mode="json"),
            "status": "generation_defect",
        },
    )
    healing = cast(HealingGraphs, features["assurance.healing"])
    features["assurance.healing"] = HealingGraphs(
        repair_failure=repair_builder.compile(checkpointer=None),
        repair_coverage=healing.repair_coverage,
    )
    result = invoke_product_root(
        _product_graphs(features, authenticate_generation_defect=authenticator),
        "execute",
        product_input,
    )

    assert repair_calls == 0
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"


def test_complete_old_generation_defect_tuple_cannot_replay_into_current_invocation(
    tmp_path,
) -> None:
    state, _cycle, _old_authenticator = _route_defect_state(
        tmp_path,
        invocation_id="inv-old-complete-tuple",
    )
    current = GenerationDefectRouteAuthenticator(
        project_root=tmp_path,
        invocation_id="inv-current-complete-tuple",
        public_entrypoint="full",
        graph_revision="3" * 64,
    )

    assert route_execute(state, authenticate_generation_defect=current) == "blocked"
    with pytest.raises(ValueError, match="current|expected"):
        adapt_repair_failure(
            cast(Any, state),
            authenticate_generation_defect=current,
        )


def test_complete_same_invocation_defect_tuple_cannot_replay_across_attempts(
    tmp_path,
) -> None:
    state, _cycle, current = _route_defect_state(
        tmp_path,
        invocation_id="inv-same-cross-attempt",
        repair_round=1,
    )
    state["repair_round"] = 0
    state["rounds_used"] = 0

    assert route_execute(state, authenticate_generation_defect=current) == "blocked"
    with pytest.raises(ValueError, match="current"):
        adapt_repair_failure(
            cast(Any, state),
            authenticate_generation_defect=current,
        )


@pytest.mark.parametrize(
    "missing",
    [
        "generation_defect",
        "generation_defect_authority_ref",
        "generation_defect_execution_binding",
    ],
)
def test_generation_defect_route_requires_complete_checkpoint_authority(
    tmp_path,
    missing: str,
) -> None:
    state, _cycle, current = _route_defect_state(
        tmp_path,
        invocation_id="inv-complete-current-authority",
    )
    state.pop(missing)

    assert route_execute(state, authenticate_generation_defect=current) == "blocked"
    with pytest.raises(ValueError, match="provenance"):
        adapt_repair_failure(
            cast(Any, state),
            authenticate_generation_defect=current,
        )
