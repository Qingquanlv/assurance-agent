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
from assurance_generation.graphs.factory import route_families


def test_all_authored_sends_preserve_plan_binding() -> None:
    state = {
        "change_id": "CH-1",
        "selected_test_families": ["api"],
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": "qa/results/plan/plan.json",
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
