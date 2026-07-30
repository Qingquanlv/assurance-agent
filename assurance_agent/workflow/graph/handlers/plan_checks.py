"""operation:verify-plan-mechanical —— 把 plan 的机械判定固化为 evidence 文档。

check 失败不是 task 失败：本节点只负责把事实写进 profile 声明的 checks_artifact 路径，
是否卡流水线由 gate 消费 policy 决定（spec C2/C3）。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.review import PlanReview
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks, validate_plan_check_document
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


def _load_review_payload(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a JSON object")
    return raw


def load_sorted_cases(change_dir: Path) -> list[dict[str, object]]:
    """Deterministically load ``change:cases/**/case.yaml`` documents.

    Sorted path order keeps ``derive_layer_applicability`` outcomes stable
    across callers (the preflight and mechanical execution must never disagree).
    """
    return [_load_yaml_mapping(path) for path in sorted(change_dir.glob("cases/**/case.yaml"))]


def _optional_required_capabilities(review_path: Path) -> tuple[str, ...]:
    """Missing review is valid on the first mechanical pass; a present but
    malformed review must raise instead of being silently ignored."""
    if not review_path.is_file():
        return ()
    raw = _load_review_payload(review_path)
    required = raw.get("required_capabilities", [])
    if not isinstance(required, list) or not all(isinstance(item, str) and item.strip() for item in required):
        raise ValueError(f"{review_path} required_capabilities must be a list of non-empty strings")
    return tuple(required)


def _load_applicable_plan_texts(change_dir: Path, plan_artifacts: tuple[str, ...]) -> dict[str, str]:
    return {
        rel: (change_dir / rel).read_text(encoding="utf-8") for rel in plan_artifacts
    }


def verify_plan_mechanical(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    layer = str(task_with(task).get("layer", ""))
    raw_require_review = task_with(task).get("require_review", False)
    if not isinstance(raw_require_review, bool):
        return task_failure("invalid_input", "require_review must be a boolean")

    try:
        profile = get_layer_assurance_profile(layer)
    except ValueError as err:
        return task_failure("invalid_input", str(err))

    try:
        cases = load_sorted_cases(workspace.change_dir)
        applicability = derive_layer_applicability(cases, profile)

        if not applicability.applicable:
            document = run_plan_checks(
                CheckContext(
                    plan_texts={},
                    cases=cases,
                    data_knowledge={},
                    layer=profile.layer,
                ),
                applicability=applicability,
            )
            atomic_write_json(
                workspace.change_dir / profile.checks_artifact,
                document.model_dump(mode="json"),
            )
            return TaskResult(status="succeeded", value={"status": document.status})

        plan_texts = _load_applicable_plan_texts(workspace.change_dir, profile.plan_artifacts)
        review_path = workspace.change_dir / profile.review_artifact

        if raw_require_review:
            if not review_path.is_file():
                raise ValueError(f"missing review artifact: {profile.review_artifact}")

            review = profile.review_model.model_validate(_load_review_payload(review_path))
            if not isinstance(review, PlanReview):
                return task_failure("invalid_output", "review-required mode requires PlanReview")
            if review.review_type != f"{profile.layer}-plan":
                return task_failure("invalid_output", "review_type does not match layer")
            if review.change_id != context.change_id:
                return task_failure("invalid_output", "review change_id does not match runtime change")

            l1_path = workspace.project_root / ".aa" / "data-knowledge.yaml"
            if not l1_path.is_file():
                raise ValueError("missing repo L1 artifact: .aa/data-knowledge.yaml")
            knowledge_payload = _load_yaml_mapping(l1_path)
            knowledge = DataKnowledge.model_validate(knowledge_payload)
            required_capabilities = tuple(review.required_capabilities or ())

            document = run_plan_checks(
                CheckContext(
                    plan_texts=plan_texts,
                    cases=cases,
                    data_knowledge=knowledge.model_dump(mode="json"),
                    layer=profile.layer,
                    required_capabilities=required_capabilities,
                ),
                applicability=applicability,
            )
            document = validate_plan_check_document(document, profile)
        else:
            if review_path.is_file():
                profile.review_model.model_validate(_load_review_payload(review_path))
            data_knowledge = _load_yaml_mapping(workspace.project_root / ".aa" / "data-knowledge.yaml")
            required_capabilities = _optional_required_capabilities(review_path)

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
    except (OSError, ValueError, ValidationError, yaml.YAMLError, json.JSONDecodeError) as err:
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
