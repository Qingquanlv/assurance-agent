from assurance_generation.contracts.agent import CodegenInputV1
from assurance_generation.graphs.factory import LaneFlowInput

_SHA = "a" * 64
_UI_REF = {"path": "qa/results/surface/ui-exploration.json", "digest": _SHA}
_API_REF = {"path": "qa/results/surface/api-discovery.json", "digest": _SHA}


def test_lane_and_codegen_inputs_carry_exploration_refs() -> None:
    assert "ui_exploration_ref" in LaneFlowInput.model_fields
    assert "api_discovery_ref" in LaneFlowInput.model_fields
    selected = CodegenInputV1.model_validate(
        {
            "change_id": "CH-1",
            "plan_digest": _SHA,
            "plan_ref": {"path": "qa/results/plan/plan.json", "digest": _SHA},
            "capability_leafs": ["entities.item.create"],
            "ui_exploration_ref": _UI_REF,
            "api_discovery_ref": _API_REF,
        }
    )
    assert selected.ui_exploration_ref is not None
    assert selected.ui_exploration_ref.path == _UI_REF["path"]
    assert selected.api_discovery_ref is not None
    assert selected.api_discovery_ref.path == _API_REF["path"]


def test_codegen_input_omits_exploration_refs_when_absent() -> None:
    selected = CodegenInputV1.model_validate(
        {
            "change_id": "CH-1",
            "plan_digest": _SHA,
            "plan_ref": {"path": "qa/results/plan/plan.json", "digest": _SHA},
            "capability_leafs": ["entities.item.create"],
        }
    )
    assert selected.ui_exploration_ref is None
    assert selected.api_discovery_ref is None
