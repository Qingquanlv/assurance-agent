"""operation:verify-plan-mechanical —— 把 plan 的机械判定固化为 evidence 文档。

check 失败不是 task 失败：本节点只负责把事实写进 profile 声明的 checks_artifact 路径，
是否卡流水线由 gate 消费 policy 决定（spec C2/C3）。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.improvements.ledger import atomic_write_json


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return raw


def load_sorted_cases(change_dir: Path) -> list[dict[str, object]]:
    """Deterministically load ``change:cases/**/case.yaml`` documents.

    Sorted path order keeps ``derive_layer_applicability`` outcomes stable
    across callers (the preflight and mechanical execution must never disagree).
    """
    return [_load_yaml_mapping(path) for path in sorted(change_dir.glob("cases/**/case.yaml"))]


def _required_capabilities(review_path: Path) -> tuple[str, ...]:
    """Missing review is valid on the first mechanical pass; a present but
    malformed review must raise instead of being silently ignored."""
    if not review_path.is_file():
        return ()
    raw = _load_yaml_mapping(review_path)
    required = raw.get("required_capabilities", [])
    if not isinstance(required, list) or not all(isinstance(item, str) and item.strip() for item in required):
        raise ValueError(f"{review_path} required_capabilities must be a list of non-empty strings")
    return tuple(required)


def verify_plan_mechanical(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del context
    layer = str(task_with(task).get("layer", ""))
    try:
        profile = get_layer_assurance_profile(layer)
    except ValueError as err:
        return task_failure("invalid_input", str(err))

    try:
        cases = load_sorted_cases(workspace.change_dir)
        applicability = derive_layer_applicability(cases, profile)

        if applicability.applicable:
            plan_texts = {
                rel: (workspace.change_dir / rel).read_text(encoding="utf-8")
                for rel in profile.plan_artifacts
            }
            data_knowledge = _load_yaml_mapping(workspace.project_root / ".aa" / "data-knowledge.yaml")
        else:
            plan_texts = {}
            data_knowledge = {}

        required_capabilities = _required_capabilities(workspace.change_dir / profile.review_artifact)

        document = run_plan_checks(
            CheckContext(
                plan_texts=plan_texts,
                cases=cases,
                data_knowledge=data_knowledge,
                layer=profile.layer,
                required_capabilities=required_capabilities,
            ),
            applicability=applicability,
        )
        atomic_write_json(
            workspace.change_dir / profile.checks_artifact,
            document.model_dump(mode="json"),
        )
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value={"status": document.status})


def derive_plan_layer_applicability(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """operation:derive-plan-layer-applicability —— cases-only preflight.

    Reads only ``change:cases/**/case.yaml`` and returns the pure
    ``LayerApplicability`` derivation; no plans/review/L1 access, no writes
    (spec: cheap gating before the mechanical plan-check pipeline runs).
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
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as err:
        return task_failure("invalid_output", str(err))

    return TaskResult(status="succeeded", value=applicability.model_dump(mode="json"))
