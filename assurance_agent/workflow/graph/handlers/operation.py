"""AA domain operation target handler（``operation:<name>``）。

operation 是进程内函数调用，绝不 spawn ``aa`` 子进程（无 subprocess 递归）：

- ``operation:no-op``：空操作；
- ``operation:skill-registry-check``：healing 所需 skill 与参数的注册表检查
  （v1 ``_ensure_healing_available`` 的 graph 形态，只读判定不写 state 文件）；
- ``operation:run-tests``：直接调用 ``workflow.execution.runner.run_change``，
  project/change 路径 remap 到 task 私有 workspace；
- ``operation:allocate-healing-attempt``：把 entry-baseline artifact 写进 task
  workspace 并返回 state updates；``budget_consumed`` strict 事件归 scheduler；
- ``operation:record-healing-status``：返回 healing 终局判定；
- ``operation:stop``：返回 ``stopped`` 与声明的 reason。

未注册的 operation target 是 ``contract`` 失败。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping

import yaml

from assurance_agent.config import load_config
from assurance_agent.workflow.execution.runner import run_change
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.orchestration.operations import BASELINE_REL, HEAL_STATUSES

OperationResult = TaskResult
OperationFn = Callable[[ExecutableTask, TaskWorkspace, RuntimeContext], OperationResult]

_REQUIRED_HEALING_SKILLS = ("aa-fix-proposal", "aa-api-codegen-fixer", "aa-e2e-codegen-fixer")


def no_op(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> OperationResult:
    return TaskResult(status="succeeded")


def skill_registry_check(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> OperationResult:
    """healing 可用性判定：``params.max_healing_attempts`` > 0 且所需 skill 齐全。"""
    max_attempts = context.params.get("max_healing_attempts")
    try:
        attempts_ok = int(max_attempts) > 0 if max_attempts is not None else True  # type: ignore[arg-type]
    except (TypeError, ValueError):
        attempts_ok = True
    skills_root = workspace.project_root / "skills"
    skills_ok = all((skills_root / name / "SKILL.md").is_file() for name in _REQUIRED_HEALING_SKILLS)
    return TaskResult(status="succeeded", value={"healing_available": bool(attempts_ok and skills_ok)})


def run_tests(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> OperationResult:
    """在 task 私有 workspace 中执行测试并发布 execution evidence（不 spawn ``aa run``）。

    ``run_change`` 内部经 ``uv run pytest`` 在 workspace project root 下跑测试；
    测试失败不是 task 失败——final_status 进 value，由 gate/route 裁决。
    """
    config = load_config(workspace.project_root)
    manifest = run_change(workspace.project_root, workspace.change_dir, config)
    final_status = getattr(manifest.final_status, "value", manifest.final_status)
    return TaskResult(
        status="succeeded",
        value={"batch_id": manifest.batch_id, "final_status": final_status},
    )


def operation_allocate_healing_attempt(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> OperationResult:
    """写 entry-baseline artifact 并返回 allocation state updates（ids 按 v1 规则结构化派生）。

    strict ``healing_attempt_allocated``/``budget_consumed`` 事件不在此处写——
    ledger 事务归 scheduler/runtime；本函数只产出 workspace artifact 与 state。
    """
    manifest_path = workspace.change_dir / "execution" / "execution-manifest.yaml"
    batch_id: str | None = None
    if manifest_path.is_file():
        try:
            doc = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("batch_id"), str):
            batch_id = doc["batch_id"]
    if not batch_id:
        return task_failure(
            "invalid_input",
            "operation:allocate-healing-attempt requires execution/execution-manifest.yaml batch_id",
        )
    proposal_path = workspace.change_dir / "healing" / "fix-proposal.json"
    if not proposal_path.is_file():
        return task_failure(
            "invalid_input",
            "operation:allocate-healing-attempt requires healing/fix-proposal.json",
        )
    proposal_sha = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
    raw_number = task_with(task).get("attempt_number")
    attempt_number = (
        raw_number
        if isinstance(raw_number, int) and not isinstance(raw_number, bool) and raw_number >= 1
        else 1
    )
    # 与 v1 _allocation_intent 相同的派生规则：episode/operation/attempt id 确定性。
    episode_id = hashlib.sha256(f"{context.change_id}:{batch_id}:{proposal_sha}".encode()).hexdigest()
    operation_id = hashlib.sha256(f"{batch_id}:{proposal_sha}:{attempt_number}".encode()).hexdigest()
    attempt_id = f"ha-{episode_id[:12]}-{attempt_number}"
    payload = {"schema_version": "1.0", "episode_id": episode_id, "entry_batch_id": batch_id}
    baseline_data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    baseline_path = workspace.change_dir / BASELINE_REL
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_bytes(baseline_data)
    allocation = {
        "episode_id": episode_id,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "operation_id": operation_id,
        "source_batch_id": batch_id,
    }
    return TaskResult(
        status="succeeded",
        value=allocation,
        state_updates={"healing_attempt": allocation},
    )


def operation_record_healing_status(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> OperationResult:
    status = task_with(task).get("status")
    if not isinstance(status, str) or status not in HEAL_STATUSES:
        allowed = ", ".join(sorted(HEAL_STATUSES))
        return task_failure(
            "invalid_input",
            f"operation:record-healing-status requires with.status in: {allowed}",
        )
    return TaskResult(status="succeeded", value={"healing_status": status})


def stop_operation(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> OperationResult:
    reason = task_with(task).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return task_failure("invalid_input", "operation:stop requires a non-empty with.reason")
    return TaskResult(status="stopped", value={"reason": reason})


def default_operations() -> dict[str, OperationFn]:
    """canonical operation registry：精确 target → 进程内 callable。"""
    return {
        "operation:no-op": no_op,
        "operation:skill-registry-check": skill_registry_check,
        "operation:run-tests": run_tests,
        "operation:allocate-healing-attempt": operation_allocate_healing_attempt,
        "operation:record-healing-status": operation_record_healing_status,
        "operation:stop": stop_operation,
    }


class OperationHandler:
    """按精确 target 分发到注册的 operation callable。"""

    def __init__(self, operations: Mapping[str, OperationFn]) -> None:
        self._operations = dict(operations)

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        fn = self._operations.get(task.target)
        if fn is None:
            return task_failure("contract", f"unknown operation target: {task.target}")
        return fn(task, workspace, context)


__all__ = [
    "OperationFn",
    "OperationHandler",
    "OperationResult",
    "default_operations",
]
