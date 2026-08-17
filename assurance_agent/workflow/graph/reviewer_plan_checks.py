"""Write reviewer plan-checks evidence before the write-set freeze."""

from __future__ import annotations

import json

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.exceptions import AaError
from assurance_agent.verification.plan_checks import run_layer_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.handlers.plan_checks import load_sorted_cases
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.precommit import infer_assurance_layer
from assurance_agent.workflow.graph.workspace import TaskWorkspace

_REVIEWER_TARGETS = frozenset(
    {
        "skill:aa-api-plan-reviewer",
        "skill:aa-e2e-plan-reviewer",
        "skill:aa-fuzz-plan-reviewer",
        "skill:aa-performance-plan-reviewer",
    }
)


class PlanChecksCompletionError(AaError):
    """Reviewer finalize could not build the plan-checks evidence document."""


def complete_reviewer_plan_checks(
    *,
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> None:
    """Write ``review/{layer}-plan-checks.json`` before freeze. Does not apply policy."""
    if task.target not in _REVIEWER_TARGETS:
        return
    try:
        layer = infer_assurance_layer(task.target)
        profile = get_layer_assurance_profile(layer)
        cases = load_sorted_cases(workspace.change_dir)
        plan_texts = {
            rel: (workspace.change_dir / rel).read_text(encoding="utf-8") for rel in profile.plan_artifacts
        }
        review_path = workspace.change_dir / profile.review_artifact
        review_payload: dict[str, object] | None = None
        if review_path.is_file():
            raw = json.loads(review_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"{profile.review_artifact} is not a JSON object")
            review_payload = raw
        l1_path = workspace.project_root / ".aa" / "data-knowledge.yaml"
        data_knowledge: dict[str, object] | None = None
        if l1_path.is_file():
            loaded = yaml.safe_load(l1_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("missing repo L1 artifact: .aa/data-knowledge.yaml")
            data_knowledge = loaded
        document = run_layer_plan_checks(
            layer=layer,
            cases=cases,
            plan_texts=plan_texts,
            review_payload=review_payload,
            data_knowledge=data_knowledge,
            change_id=context.change_id,
            require_review=True,
        )
        path = workspace.change_dir / profile.checks_artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(document))
    except (OSError, ValueError, ValidationError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise PlanChecksCompletionError(str(exc)) from exc


__all__ = ["PlanChecksCompletionError", "complete_reviewer_plan_checks"]
