"""operation:verify-plan-mechanical —— 把 plan 的机械判定固化为 evidence 文档。

check 失败不是 task 失败：本节点只负责把事实写进 review/<layer>-plan-checks.json，
是否卡流水线由 gate 消费 policy 决定（spec C2/C3）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.improvements.ledger import atomic_write_json

_LAYERS = ("api", "e2e", "fuzz", "performance")
_REVIEW_NAMES = {
    "api": ("api-plan-review.json",),
    "e2e": ("plan-review.json", "e2e-plan-review.json"),
    "fuzz": ("fuzz-plan-review.json",),
    "performance": ("performance-plan-review.json",),
}


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return raw


def _required_capabilities(change_dir: Path, layer: str) -> tuple[str, ...]:
    review_dir = change_dir / "review"
    for name in _REVIEW_NAMES.get(layer, ()):
        path = review_dir / name
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict):
            continue
        required = raw.get("required_capabilities")
        if isinstance(required, list):
            return tuple(item for item in required if isinstance(item, str) and item.strip())
    return ()


def verify_plan_mechanical(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del context
    layer = str(task_with(task).get("layer", ""))
    if layer not in _LAYERS:
        return task_failure(
            "invalid_input",
            f"operation:verify-plan-mechanical requires with.layer in: {', '.join(_LAYERS)}",
        )
    try:
        plans_dir = workspace.change_dir / "plans"
        plan_texts = {
            f"plans/{path.name}": path.read_text(encoding="utf-8")
            for path in sorted(plans_dir.glob(f"{layer}*.md"))
        }
        cases = [_load_yaml_mapping(path) for path in sorted(workspace.change_dir.glob("cases/**/case.yaml"))]
        data_knowledge = _load_yaml_mapping(workspace.project_root / ".aa" / "data-knowledge.yaml")
        document = run_plan_checks(
            CheckContext(
                plan_texts=plan_texts,
                cases=cases,
                data_knowledge=data_knowledge,
                layer=layer,
                required_capabilities=_required_capabilities(workspace.change_dir, layer),
            )
        )
        atomic_write_json(
            workspace.change_dir / "review" / f"{layer}-plan-checks.json",
            document.model_dump(mode="json"),
        )
    except (OSError, ValueError, yaml.YAMLError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value={"status": document.status})
