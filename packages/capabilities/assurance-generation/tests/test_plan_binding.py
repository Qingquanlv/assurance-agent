from assurance_generation.contracts.agent import (
    AgentFinalizeInputV1,
    CodegenInputV1,
    PlanInputV1,
)
from assurance_generation.contracts.workflow import (
    CompleteGenerationInputV1,
    GenerationCycleResultV1,
    PublishCycleInputV1,
    ResolveGenerationInputV1,
)
from assurance_generation.graphs.factory import GenerationFlowInput, LaneFlowInput


def test_lane_input_keeps_the_plan_binding() -> None:
    for model in (GenerationFlowInput, LaneFlowInput, CodegenInputV1):
        assert model.model_fields["plan_digest"].is_required()
        assert model.model_fields["plan_ref"].is_required()


def test_generation_contracts_require_plan_binding() -> None:
    for model in (
        ResolveGenerationInputV1,
        CompleteGenerationInputV1,
        PublishCycleInputV1,
        GenerationCycleResultV1,
        PlanInputV1,
        CodegenInputV1,
        AgentFinalizeInputV1,
    ):
        assert model.model_fields["plan_digest"].is_required()
        assert model.model_fields["plan_ref"].is_required()
