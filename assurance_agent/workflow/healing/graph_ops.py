"""Healing-domain graph operations (registry check + attempt allocate/record)."""

from __future__ import annotations

import hashlib
import json

import yaml

from assurance_agent.artifacts.paths import EXECUTION_MANIFEST_REL, existing_with_alias
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.healing.operations import (
    enhance_allocate_result_with_authority,
    operation_combine_fixer_safety,
    operation_fixer_authority_ready,
    operation_fixer_dispatch,
    operation_record_codegen_fix_apply,
    operation_record_fixer_approval,
)
from assurance_agent.workflow.orchestration.operations import BASELINE_REL, HEAL_STATUSES

_REQUIRED_HEALING_SKILLS = ("aa-fix-proposal", "aa-api-codegen-fixer", "aa-e2e-codegen-fixer")


def skill_registry_check(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """healing 可用性判定，并写出 ``registry/skill-registry-check.json``。"""
    del task  # contract signature
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


def operation_allocate_healing_attempt(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """写 entry-baseline artifact 并返回 allocation payload（ids 按 v1 规则结构化派生）。

    Legacy host-ledger allocation events are appended by the scheduler
    compatibility hook in ``_persist_success``; this function only produces
    workspace artifacts, optional fixer-authority output, and allocation value.
    """
    manifest_path = existing_with_alias(workspace.change_dir / "execution" / "execution-manifest.json")
    batch_id: str | None = None
    if manifest_path is not None:
        try:
            doc = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("batch_id"), str):
            batch_id = doc["batch_id"]
    if not batch_id:
        return task_failure(
            "invalid_input",
            f"operation:allocate-healing-attempt requires {EXECUTION_MANIFEST_REL} batch_id",
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
    # Activated path: always write fixer-authority and emit healing_allocation/v2.
    # Callers may still force-disable via with.emit_durable_effect=false for tests.
    params = task_with(task)
    emit_effect = True if "emit_durable_effect" not in params else bool(params.get("emit_durable_effect"))
    return enhance_allocate_result_with_authority(
        task=task,
        workspace=workspace,
        context=context,
        allocation=allocation,
        attempt_id=attempt_id,
        emit_durable_effect=emit_effect,
    )


def operation_record_healing_status(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del context  # contract signature
    status = task_with(task).get("status")
    if not isinstance(status, str) or status not in HEAL_STATUSES:
        allowed = ", ".join(sorted(HEAL_STATUSES))
        return task_failure(
            "invalid_input",
            f"operation:record-healing-status requires with.status in: {allowed}",
        )
    status_path = workspace.change_dir / "healing" / "status.json"
    previous: dict[str, object] = {}
    attempts_used = 0
    if status_path.is_file():
        try:
            prev = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        if isinstance(prev, dict):
            previous = prev
            raw_attempts = prev.get("attempts_used")
            if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool) and raw_attempts >= 0:
                attempts_used = raw_attempts
    doc: dict[str, object] = {
        **previous,
        "schema_version": "1.0",
        "status": status,
        "attempts_used": attempts_used,
        "all_fixers_no_op": status == "exhausted",
    }
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return TaskResult(status="succeeded", value={"healing_status": status})


__all__ = [
    "operation_allocate_healing_attempt",
    "operation_combine_fixer_safety",
    "operation_fixer_authority_ready",
    "operation_fixer_dispatch",
    "operation_record_codegen_fix_apply",
    "operation_record_fixer_approval",
    "operation_record_healing_status",
    "skill_registry_check",
]
