"""operation:derive-plan-layer-applicability — cases-only preflight.

When the layer is not applicable, write the not_applicable plan-checks document
so review-gate can skip. When applicable, return ``value.applicable`` only.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_kernel.artifacts.canonical import canonical_json_bytes
from assurance_kernel.verification.applicability import derive_layer_applicability
from assurance_kernel.verification.plan_checks import run_layer_plan_checks
from assurance_kernel.verification.profiles import get_layer_assurance_profile
from assurance_kernel.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_kernel.workflow.graph.task_runner import task_failure, task_with
from assurance_kernel.workflow.graph.workspace import TaskWorkspace


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return raw


def load_sorted_cases(change_dir: Path) -> list[dict[str, object]]:
    """Deterministically load ``change:cases/**/case.yaml`` documents.

    Sorted path order keeps ``derive_layer_applicability`` outcomes stable
    across callers (the preflight and reviewer finalize must never disagree).
    """
    return [_load_yaml_mapping(path) for path in sorted(change_dir.glob("cases/**/case.yaml"))]


def derive_plan_layer_applicability(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """operation:derive-plan-layer-applicability —— cases-only preflight.

    Reads only ``change:cases/**/case.yaml``. Writes the not_applicable
    plan-checks document when the layer has no automated cases; otherwise
    returns the derivation value with no writes.
    """
    del context
    layer = str(task_with(task).get("layer", ""))
    try:
        profile = get_layer_assurance_profile(layer)
    except ValueError as err:
        return task_failure("invalid_input", str(err))

    try:
        cases = load_sorted_cases(workspace.change_dir)
        applicability = derive_layer_applicability(cases, profile)
        if not applicability.applicable:
            document = run_layer_plan_checks(layer=layer, cases=cases)
            path = workspace.change_dir / profile.checks_artifact
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_json_bytes(document))
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as err:
        return task_failure("invalid_output", str(err))

    return TaskResult(status="succeeded", value=applicability.model_dump(mode="json"))
