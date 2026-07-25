"""AA domain operation target handler（``operation:<name>``）。

operation 是进程内函数调用，绝不 spawn ``aa`` 子进程（无 subprocess 递归）：

- ``operation:no-op``：空操作；
- ``operation:skill-registry-check``：healing 所需 skill 与参数的注册表检查；
  写出 ``registry/skill-registry-check.json``（供 attached ``registry-gate``），
  value 含 ``healing_available`` / ``status``；output 与 gate 冻结由 runner finalize；
- ``operation:run-tests``：直接调用 ``workflow.execution.runner.run_change``，
  project/change 路径 remap 到 task 私有 workspace；
- ``operation:inspect``：直接调用 ``workflow.report.inspector.inspect_change``，
  写出 ``inspect/failure-analysis.json`` 与 ``inspect/quality-gate-result.json``；
- ``operation:allocate-healing-attempt``：把 entry-baseline artifact 写进 task
  workspace 并返回 state updates；``budget_consumed`` strict 事件归 scheduler；
- ``operation:record-healing-status``：返回 healing 终局判定；
- ``operation:stop``：返回 ``stopped`` 与声明的 reason。

未注册的 operation target 是 ``contract`` 失败。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from typing import Iterator

import yaml

from assurance_agent.config import load_config
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.execution.runner import run_change
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.orchestration.operations import BASELINE_REL, HEAL_STATUSES
from assurance_agent.workflow.graph.handlers.retro_ops import retro_accept, retro_collect
from assurance_agent.workflow.issues.operations import (
    collect_observations_operation,
    record_empty_issue_analysis_operation,
    record_issue_analysis_failure_operation,
    record_project_sync_pending_operation,
)

OperationResult = TaskResult
OperationFn = Callable[[ExecutableTask, TaskWorkspace, RuntimeContext], OperationResult]

_REQUIRED_HEALING_SKILLS = ("aa-fix-proposal", "aa-api-codegen-fixer", "aa-e2e-codegen-fixer")
# Captured trees omit these (size / absolute interpreter links). run-tests must
# reattach them so ``uv run pytest`` does not rebuild a broken task-local venv.
_HOST_RUNTIME_DIRS = (".venv", "node_modules")
# Coordinator files excluded from content trees but required by in-workspace CLI
# (e.g. ``aa heal record-apply`` reads the strict allocation ledger).
_HOST_CHANGE_COORDINATOR_FILES = ("events.jsonl",)


def _link_host_runtime_dirs(workspace: TaskWorkspace, context: RuntimeContext) -> None:
    """Symlink host ``.venv`` / ``node_modules`` into the task-private project root."""
    host_root = context.project_root.resolve()
    task_root = workspace.project_root.resolve()
    if host_root == task_root:
        return
    for name in _HOST_RUNTIME_DIRS:
        host = host_root / name
        task = task_root / name
        if not host.exists():
            continue
        if task.is_symlink() and task.resolve() == host.resolve():
            continue
        if task.exists() or task.is_symlink():
            if task.is_dir() and not task.is_symlink():
                shutil.rmtree(task)
            else:
                task.unlink(missing_ok=True)
        try:
            task.symlink_to(host, target_is_directory=True)
        except OSError:
            continue


def _link_host_change_coordinator_files(workspace: TaskWorkspace, context: RuntimeContext) -> None:
    """Symlink canonical change-dir coordinator files into the task-private change root."""
    host_change = context.change_dir.resolve()
    task_change = workspace.change_dir.resolve()
    if host_change == task_change:
        return
    for name in _HOST_CHANGE_COORDINATOR_FILES:
        host = host_change / name
        task = task_change / name
        if not host.is_file():
            continue
        if task.is_symlink() and task.resolve() == host.resolve():
            continue
        if task.exists() or task.is_symlink():
            task.unlink(missing_ok=True)
        try:
            task.symlink_to(host)
        except OSError:
            continue


def link_host_task_paths(workspace: TaskWorkspace, context: RuntimeContext) -> None:
    """Reattach host runtime dirs and coordinator files the tree capture omits."""
    _link_host_runtime_dirs(workspace, context)
    _link_host_change_coordinator_files(workspace, context)


@contextmanager
def _host_uv_environment(context: RuntimeContext) -> Iterator[None]:
    """Force ``uv run`` to reuse the host project venv (not a task-local rebuild)."""
    host_venv = context.project_root.resolve() / ".venv"
    host_python = host_venv / "bin" / "python"
    keys = ("UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "VIRTUAL_ENV")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        if host_venv.is_dir():
            os.environ["UV_PROJECT_ENVIRONMENT"] = str(host_venv)
            os.environ["VIRTUAL_ENV"] = str(host_venv)
        if host_python.is_file():
            os.environ["UV_PYTHON"] = str(host_python)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def no_op(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> OperationResult:
    return TaskResult(status="succeeded")


def skill_registry_check(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> OperationResult:
    """healing 可用性判定，并写出 ``registry/skill-registry-check.json``。"""
    max_attempts = context.params.get("max_healing_attempts")
    try:
        attempts_ok = int(max_attempts) > 0 if max_attempts is not None else True  # type: ignore[arg-type]
    except (TypeError, ValueError):
        attempts_ok = True
    skills_root = workspace.project_root / "skills"
    skills_ok = all((skills_root / name / "SKILL.md").is_file() for name in _REQUIRED_HEALING_SKILLS)
    healing_available = bool(attempts_ok and skills_ok)
    status = "pass" if healing_available else "fail"
    reason = (
        "healing skills registered and max_healing_attempts > 0"
        if healing_available
        else "healing unavailable: missing skills or max_healing_attempts <= 0"
    )
    payload = {
        "schema_version": "1.0",
        "status": status,
        "healing_available": healing_available,
        "reason": reason,
        "required_skills": list(_REQUIRED_HEALING_SKILLS),
    }
    out = workspace.change_dir / "registry" / "skill-registry-check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return TaskResult(
        status="succeeded",
        value={"healing_available": healing_available, "status": status},
    )


def inspect_operation(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> OperationResult:
    """Classify execution evidence and publish inspect artifacts (no spawn ``aa report inspect``)."""
    try:
        result = inspect_change(workspace.project_root, context.change_id)
    except EvidenceError as err:
        return task_failure("invalid_input", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "batch_id": result.analysis.batch_id,
            "final_status": result.analysis.final_status,
            "status": result.analysis.status,
        },
    )


def run_tests(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> OperationResult:
    """在 task 私有 workspace 中执行测试并发布 execution evidence（不 spawn ``aa run``）。

    ``run_change`` 内部经 ``uv run pytest`` 在 workspace project root 下跑测试；
    测试失败不是 task 失败——final_status 进 value，由 gate/route 裁决。
    """
    link_host_task_paths(workspace, context)
    with _host_uv_environment(context):
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
    """写 entry-baseline artifact 并返回 allocation payload（ids 按 v1 规则结构化派生）。

    strict ``healing_attempt_allocated``/``healing_entry_baseline_pinned`` 事件在
    scheduler ``_persist_success`` 中写入 canonical ledger；本函数只产出 workspace
    artifact 与 allocation value。
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
    baseline_sha256 = hashlib.sha256(baseline_data).hexdigest()
    baseline_path = workspace.change_dir / BASELINE_REL
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_bytes(baseline_data)
    status_path = workspace.change_dir / "healing" / "status.json"
    previous_attempts = 0
    if status_path.is_file():
        try:
            prev = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        if isinstance(prev, dict):
            raw_prev = prev.get("attempts_used")
            if isinstance(raw_prev, int) and not isinstance(raw_prev, bool) and raw_prev >= 0:
                previous_attempts = raw_prev
    attempts_used = max(previous_attempts + 1, attempt_number)
    status_doc = {
        "schema_version": "1.0",
        "status": "pending",
        "attempts_used": attempts_used,
        "all_fixers_no_op": False,
        "latest_attempt_id": attempt_id,
        "source_batch_id": batch_id,
    }
    status_path.write_text(json.dumps(status_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    allocation = {
        "episode_id": episode_id,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "operation_id": operation_id,
        "source_batch_id": batch_id,
        "baseline_sha256": baseline_sha256,
        "entry_batch_id": batch_id,
    }
    return TaskResult(
        status="succeeded",
        value=allocation,
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
    status_path = workspace.change_dir / "healing" / "status.json"
    doc: dict[str, object] = {
        "schema_version": "1.0",
        "status": status,
        "attempts_used": 0,
        "all_fixers_no_op": status == "exhausted",
    }
    if status_path.is_file():
        try:
            prev = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        if isinstance(prev, dict):
            doc = {**prev, **doc, "status": status}
            if status == "exhausted":
                doc["all_fixers_no_op"] = True
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
        "operation:inspect": inspect_operation,
        "operation:allocate-healing-attempt": operation_allocate_healing_attempt,
        "operation:record-healing-status": operation_record_healing_status,
        "operation:stop": stop_operation,
        "operation:retro-collect": retro_collect,
        "operation:retro-accept": retro_accept,
        "operation:collect-observations": collect_observations_operation,
        "operation:record-empty-issue-analysis": record_empty_issue_analysis_operation,
        "operation:record-issue-analysis-failure": record_issue_analysis_failure_operation,
        "operation:record-project-sync-pending": record_project_sync_pending_operation,
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
    "link_host_task_paths",
]
