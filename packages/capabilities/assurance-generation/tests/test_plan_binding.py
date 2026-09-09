from assurance_generation.contracts.agent import (
    AgentFinalizeInputV1,
    CodegenInputV1,
    PlanInputV1,
)
from assurance_generation.contracts.workflow import (
    CompleteGenerationInputV1,
    GenerationCycleResultV1,
    ResolveGenerationInputV1,
)
from assurance_generation.graphs.nodes import select_plan, select_plan_review
from assurance_generation.graphs.routes import route_families
from planning_fixtures import valid_plan_result  # pyright: ignore[reportMissingImports]


def test_all_authored_sends_preserve_plan_binding() -> None:
    state = {
        "change_id": "CH-1",
        "selected_test_families": ["api"],
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": "qa/changes/CH-1/plan/plan.json",
            "digest": "b" * 64,
        },
    }
    sends = route_families(state)
    assert [send.node for send in sends] == ["api", "e2e", "fuzz", "performance"]
    assert [send.arg["lane_selected"] for send in sends] == [True, False, False, False]
    assert all(send.arg["plan_digest"] == state["plan_digest"] for send in sends)
    assert all(send.arg["plan_ref"] == state["plan_ref"] for send in sends)


def test_generation_contracts_require_plan_binding() -> None:
    for model in (
        ResolveGenerationInputV1,
        CompleteGenerationInputV1,
        GenerationCycleResultV1,
        PlanInputV1,
        CodegenInputV1,
        AgentFinalizeInputV1,
    ):
        assert model.model_fields["plan_digest"].is_required()
        assert model.model_fields["plan_ref"].is_required()


def test_verified_generation_input_accepts_stage_available_references() -> None:
    payload = {
        "change_id": "CH-1",
        "coverage_epoch": 0,
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": "qa/changes/CH-1/plan/plan.json",
            "digest": "b" * 64,
        },
        "validation_profile": "api_db.v1",
    }

    parsed = ResolveGenerationInputV1.model_validate(payload)
    assert parsed.validation_profile == "api_db.v1"


def test_plan_review_selection_carries_published_machine_plan_identity() -> None:
    state = {
        "change_id": "CH-1",
        "family": "api",
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": "qa/changes/CH-1/plan/plan.json",
            "digest": "b" * 64,
        },
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "case_execution_plan_ref": {
            "path": "qa/changes/CH-1/plans/api-case-execution-plan.json",
            "digest": "c" * 64,
        },
        "case_execution_plan_digest": "c" * 64,
        "reviewed_plan": {**valid_plan_result("api"), "change_id": "CH-1"},
    }

    selected = select_plan_review(state)

    assert selected.case_execution_plan_ref is not None
    assert selected.case_execution_plan_ref.digest == "c" * 64
    assert selected.case_execution_plan_digest == "c" * 64


def test_non_api_plan_selection_does_not_compile_api_machine_plan() -> None:
    state = {
        "change_id": "CH-1",
        "family": "e2e",
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": "qa/changes/CH-1/plan/plan.json",
            "digest": "b" * 64,
        },
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "case_plan_context": {"api": "only"},
        "assertion_sources": {"api": "only"},
        "validation_profile": "api_db.v1",
    }

    selected = select_plan(state)

    assert selected.assertion_sources is None
    assert selected.validation_profile is None
