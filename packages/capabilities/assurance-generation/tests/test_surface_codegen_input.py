from __future__ import annotations

from assurance_generation.contracts.agent import CodegenInputV1
from planning_fixtures import PLAN_DIGEST, PLAN_REF, VALID_LEAFS  # pyright: ignore[reportMissingImports]

_SHA = "a" * 64
_UI_REF = {"path": "qa/results/facts/ui-exploration.json", "digest": _SHA}
_API_REF = {"path": "qa/results/facts/api-discovery.json", "digest": _SHA}


def _base_state() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": PLAN_DIGEST,
        "plan_ref": PLAN_REF,
        "capability_leafs": list(VALID_LEAFS),
        "artifact_paths": [],
        "coverage_epoch": 0,
        "local_round": 0,
    }


def test_select_codegen_copies_surface_refs() -> None:
    state = {
        **_base_state(),
        "ui_exploration_ref": _UI_REF,
        "api_discovery_ref": _API_REF,
    }
    selected = CodegenInputV1.model_validate(state)
    assert selected.ui_exploration_ref is not None
    assert selected.ui_exploration_ref.path == _UI_REF["path"]
    assert selected.api_discovery_ref is not None
    assert selected.api_discovery_ref.path == _API_REF["path"]


def test_select_codegen_without_surface_refs_keeps_none() -> None:
    selected = CodegenInputV1.model_validate(_base_state())
    assert selected.ui_exploration_ref is None
    assert selected.api_discovery_ref is None
