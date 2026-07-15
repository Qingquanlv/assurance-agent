"""Resolve which test layers to run for a change (aligned with TS resolveSelectedTargets).

Precedence: explicit workflow-state (selected_targets or layers) → codegen plan
presence → all four targets.
"""

from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models import SelectedTargets

_KEYS = ("api", "e2e", "fuzz", "performance")


def resolve_selected_targets(change_dir: Path) -> SelectedTargets:
    from_state = _from_workflow_state(change_dir)
    if from_state is not None:
        return from_state

    plans_dir = change_dir / "plans"
    plan_selection = SelectedTargets(
        api=(plans_dir / "api-codegen-plan.md").is_file(),
        e2e=(plans_dir / "e2e-codegen-plan.md").is_file(),
        fuzz=(plans_dir / "fuzz-codegen-plan.md").is_file(),
        performance=(plans_dir / "performance-codegen-plan.md").is_file(),
    )
    if any(getattr(plan_selection, k) for k in _KEYS):
        return plan_selection

    return SelectedTargets(api=True, e2e=True, fuzz=True, performance=True)


def _from_workflow_state(change_dir: Path) -> SelectedTargets | None:
    state_path = change_dir / "workflow-state.yaml"
    if not state_path.is_file():
        return None
    try:
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    return _normalise(doc.get("selected_targets")) or _normalise(doc.get("layers"))


def _normalise(value: Any) -> SelectedTargets | None:
    if not isinstance(value, dict):
        return None
    if not any(isinstance(value.get(k), bool) for k in _KEYS):
        return None
    return SelectedTargets(
        api=value.get("api") is True,
        e2e=value.get("e2e") is True,
        fuzz=value.get("fuzz") is True,
        performance=value.get("performance") is True,
    )
