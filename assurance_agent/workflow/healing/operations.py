"""Graph-owned healing operations (dark-shipped until Task 15 activation)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.healing_codegen import (
    ApiCodegenFixApplyIntentV1,
    ApiCodegenFixApplySummaryV1,
    ApiCodegenFixerSafetyCheckV1,
    CodegenFixApplyIntentV1,
    E2eCodegenFixApplyIntentV1,
    E2eCodegenFixApplySummaryV1,
    E2eCodegenFixerSafetyCheckV1,
    FixerAuthorityPathV1,
    FixerAuthorityTargetV1,
    FixerAuthorityV1,
    FixerProposalApprovalReceiptV1,
    FixerSafetyCheckV1,
)
from assurance_agent.workflow.graph.durable_effects import (
    FIXER_PROPOSAL_APPROVED_V1,
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
    DurableEffectIntentV1,
    build_intent,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.healing.effects import (
    FixerProposalApprovedEffectV1,
    HealRecordApplyEffectV2,
    HealingAllocationEffectV2,
)
from assurance_agent.workflow.healing.projection import project_healing_episode

BASELINE_REL = "healing/entry-baseline.json"
AUTHORITY_REL = "healing/fixer-authority.json"
APPROVAL_REL = "healing/fixer-proposal-approval.json"
AGGREGATE_SAFETY_REL = "healing/fixer-safety-check.json"


def _write_json(path: Path, payload: Mapping[str, object] | object) -> str:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")  # type: ignore[union-attr]
    else:
        data = dict(payload)  # type: ignore[arg-type]
    raw = canonical_json_bytes(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw + b"\n")
    return sha256_bytes(raw)


def _intent_wire(intent: DurableEffectIntentV1) -> dict[str, object]:
    return intent.model_dump(mode="json")


def operation_fixer_authority_ready(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Gate: pass when every active authority target is ready; else stop/unverified."""
    del task, context
    authority_path = workspace.change_dir / AUTHORITY_REL
    if not authority_path.is_file():
        return TaskResult(status="succeeded", value={"route": "stop", "reason": "missing_fixer_authority"})
    try:
        authority = FixerAuthorityV1.model_validate(json.loads(authority_path.read_text(encoding="utf-8")))
    except Exception as exc:
        return TaskResult(
            status="succeeded",
            value={"route": "stop", "reason": f"malformed_fixer_authority:{exc}"},
        )
    if any(target.status != "ready" for target in authority.targets):
        return TaskResult(
            status="succeeded",
            value={"route": "stop", "reason": "unverified_imported_codegen"},
        )
    if not authority.targets:
        return TaskResult(status="succeeded", value={"route": "stop", "reason": "no_active_targets"})
    return TaskResult(status="succeeded", value={"route": "pass"})


def operation_record_fixer_approval(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Write approval receipt hard output and emit fixer_proposal_approved/v1 effect."""
    params = task_with(task)
    try:
        receipt = FixerProposalApprovalReceiptV1.model_validate(params.get("receipt") or params)
    except Exception as exc:
        return task_failure("invalid_input", str(exc))
    digest = _write_json(workspace.change_dir / APPROVAL_REL, receipt)
    target_tree_id = str(params.get("target_tree_id") or task.invocation_id)
    effect = FixerProposalApprovedEffectV1(
        schema_version="1",
        approval_id=receipt.approval_id,
        root_invocation_id=receipt.root_invocation_id,
        interrupt_task_id=receipt.interrupt_task_id,
        source_gate_attempt_id=receipt.source_gate_attempt_id,
        source_tree_id=receipt.source_tree_id,
        proposal_sha256=receipt.proposal_sha256,
        fixer_authority_sha256=receipt.fixer_authority_sha256,
        entry_baseline_sha256=receipt.entry_baseline_sha256,
        policy_sha256=receipt.policy_sha256,
        targets=list(receipt.targets),
        paths=list(receipt.paths),
        target_tree_id=target_tree_id,
    )
    intent = build_intent(
        kind=FIXER_PROPOSAL_APPROVED_V1,
        payload=effect,
        invocation_id=task.invocation_id,
        task_id=task.task_id,
        attempt_id=str(params.get("attempt_id") or f"{task.task_id}:1"),
    )
    return TaskResult(
        status="succeeded",
        value={"approval_id": receipt.approval_id, "receipt_sha256": digest},
        outputs_sha256={f"change:{APPROVAL_REL}": digest.removeprefix("sha256:")},
        durable_effects=(_intent_wire(intent),),
    )


def operation_fixer_dispatch(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Side-effect-free fan-out helper; eligibility is enforced by edge guards."""
    del task, context
    proposal_path = workspace.change_dir / "healing" / "fix-proposal.json"
    if not proposal_path.is_file():
        return task_failure("invalid_input", "fixer-dispatch requires healing/fix-proposal.json")
    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return task_failure("invalid_input", str(exc))
    items = proposal.get("proposals") if isinstance(proposal, dict) else None
    active: list[str] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or not item.get("eligible"):
                continue
            target = item.get("target")
            if target in {"api", "e2e"} and target not in active:
                active.append(str(target))
    return TaskResult(status="succeeded", value={"active_targets": sorted(active)})


def operation_record_codegen_fix_apply(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Record one target's apply summary + safety fragment and emit heal_record_apply/v2."""
    del context
    params = task_with(task)
    target = params.get("target")
    if target not in {"api", "e2e"}:
        return task_failure(
            "invalid_input",
            "record-codegen-fix-apply requires with.target in {api,e2e}",
        )
    intent_rel = f"healing/{target}-apply-intent.json"
    intent_path = workspace.change_dir / intent_rel
    if not intent_path.is_file():
        return task_failure("invalid_input", f"missing intent hard input: {intent_rel}")
    model = ApiCodegenFixApplyIntentV1 if target == "api" else E2eCodegenFixApplyIntentV1
    try:
        intent_doc = model.model_validate(json.loads(intent_path.read_text(encoding="utf-8")))
    except Exception as exc:
        return task_failure("invalid_output", str(exc))
    intent_digest = sha256_bytes(canonical_json_bytes(intent_doc))
    write_set_id = str(params.get("write_set_id") or "")
    if not write_set_id:
        return task_failure("invalid_input", "record-codegen-fix-apply requires with.write_set_id")
    summary_model = ApiCodegenFixApplySummaryV1 if target == "api" else E2eCodegenFixApplySummaryV1
    summary = summary_model(
        schema_version="1",
        target=target,  # type: ignore[arg-type]
        outcome=intent_doc.outcome,
        proposal_ids=list(intent_doc.proposal_ids),
        claimed_modified_paths=list(intent_doc.claimed_modified_paths),
        intent_sha256=intent_digest,
        write_set_id=write_set_id,
    )
    safety_flags = _safety_flags_from_params(params, intent_doc)
    safety_model = ApiCodegenFixerSafetyCheckV1 if target == "api" else E2eCodegenFixerSafetyCheckV1
    safety = safety_model(schema_version="1", target=target, **safety_flags)  # type: ignore[arg-type]
    summary_rel = f"healing/{target}-apply-summary.json"
    safety_rel = f"healing/{target}-fixer-safety-check.json"
    summary_digest = _write_json(workspace.change_dir / summary_rel, summary)
    safety_digest = _write_json(workspace.change_dir / safety_rel, safety)
    projection = project_healing_episode(workspace.change_dir)
    entry_batch = (
        projection.baseline.entry_batch_id
        if projection.baseline is not None
        else str(params.get("entry_batch_id") or "unknown")
    )
    fixer_attempt_id = str(params.get("fixer_attempt_id") or "")
    record_key = sha256_bytes(
        canonical_json_bytes(
            {
                "fixer_attempt_id": fixer_attempt_id,
                "intent_sha256": intent_digest,
                "record_task_id": task.task_id,
                "root_invocation_id": task.invocation_id,
                "write_set_id": write_set_id,
            }
        )
    ).removeprefix("sha256:")
    effect = HealRecordApplyEffectV2(
        schema_version="2",
        record_key=record_key,
        root_invocation_id=task.invocation_id,
        record_task_id=task.task_id,
        fixer_attempt_id=fixer_attempt_id or task.task_id,
        target=target,  # type: ignore[arg-type]
        entry_batch_id=entry_batch,
        intent_sha256=intent_digest,
        write_set_id=write_set_id,
        outcome=intent_doc.outcome,
        proposal_ids=list(intent_doc.proposal_ids),
        claimed_modified_paths=list(intent_doc.claimed_modified_paths),
        safety_payload_sha256=safety_digest,
    )
    durable = build_intent(
        kind=HEAL_RECORD_APPLY_V2,
        payload=effect,
        invocation_id=task.invocation_id,
        task_id=task.task_id,
        attempt_id=str(params.get("attempt_id") or f"{task.task_id}:1"),
    )
    return TaskResult(
        status="succeeded",
        value={"target": target, "record_key": record_key},
        outputs_sha256={
            f"change:{summary_rel}": summary_digest.removeprefix("sha256:"),
            f"change:{safety_rel}": safety_digest.removeprefix("sha256:"),
        },
        durable_effects=(_intent_wire(durable),),
    )


def operation_combine_fixer_safety(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Combine exactly the active target safety fragments into one aggregate."""
    params = task_with(task)
    active_raw = params.get("active_targets")
    if not isinstance(active_raw, list) or not active_raw:
        dispatch = operation_fixer_dispatch(task, workspace, context)
        value = dispatch.value if isinstance(dispatch.value, Mapping) else {}
        active_raw = value.get("active_targets") if dispatch.status == "succeeded" else []
    active: list[Literal["api", "e2e"]] = []
    for item in active_raw or []:
        if item in {"api", "e2e"} and item not in active:
            active.append(item)  # type: ignore[arg-type]
    if not active:
        return task_failure("invalid_input", "combine-fixer-safety requires at least one active target")
    digests: list[str] = []
    passed = True
    needs_review = False
    for target in active:
        path = workspace.change_dir / f"healing/{target}-fixer-safety-check.json"
        if not path.is_file():
            return task_failure("invalid_input", f"missing active safety fragment for {target}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            model = ApiCodegenFixerSafetyCheckV1 if target == "api" else E2eCodegenFixerSafetyCheckV1
            fragment = model.model_validate(payload)
        except Exception as exc:
            return task_failure("invalid_output", str(exc))
        digests.append(sha256_bytes(canonical_json_bytes(fragment)))
        passed = passed and fragment.passed
        needs_review = needs_review or fragment.needs_review
    for target in ("api", "e2e"):
        if target in active:
            continue
        unexpected = workspace.change_dir / f"healing/{target}-fixer-safety-check.json"
        if unexpected.is_file() and bool(params.get("reject_inactive_fragments")):
            return task_failure("invalid_output", f"unexpected inactive safety fragment for {target}")
    aggregate = FixerSafetyCheckV1(
        schema_version="1",
        passed=passed and not needs_review,
        needs_review=needs_review,
        active_targets=active,
        target_safety_sha256=digests,
    )
    digest = _write_json(workspace.change_dir / AGGREGATE_SAFETY_REL, aggregate)
    return TaskResult(
        status="succeeded",
        value={"active_targets": active, "passed": aggregate.passed},
        outputs_sha256={f"change:{AGGREGATE_SAFETY_REL}": digest.removeprefix("sha256:")},
    )


def build_fixer_authority_for_allocate(
    *,
    change_id: str,
    active_targets: list[Literal["api", "e2e"]],
    target_bindings: Mapping[str, Mapping[str, Any]],
) -> FixerAuthorityV1:
    targets: list[FixerAuthorityTargetV1] = []
    for target in active_targets:
        binding = target_bindings.get(target, {})
        status = str(binding.get("status") or "unverified")
        paths_raw = binding.get("paths") or []
        paths = [
            FixerAuthorityPathV1.model_validate(item) if not isinstance(item, FixerAuthorityPathV1) else item
            for item in paths_raw
        ]
        targets.append(
            FixerAuthorityTargetV1(
                target=target,
                status=status,  # type: ignore[arg-type]
                codegen_attempt_id=binding.get("codegen_attempt_id"),
                generated_files_sha256=binding.get("generated_files_sha256"),
                summary_sha256=binding.get("summary_sha256"),
                write_set_id=binding.get("write_set_id"),
                execution_batch_id=binding.get("execution_batch_id"),
                paths=paths,
            )
        )
    return FixerAuthorityV1(schema_version="1", change_id=change_id, targets=targets)


def enhance_allocate_result_with_authority(
    *,
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
    allocation: Mapping[str, object],
    attempt_id: str,
    emit_durable_effect: bool,
) -> TaskResult:
    """Write fixer-authority hard output and optionally attach allocation effect."""
    params = task_with(task)
    active_raw = params.get("active_targets") or ["api"]
    active: list[Literal["api", "e2e"]] = [item for item in active_raw if item in {"api", "e2e"}]  # type: ignore[misc]
    bindings = params.get("authority_bindings")
    if not isinstance(bindings, Mapping):
        bindings = {}
    authority = build_fixer_authority_for_allocate(
        change_id=context.change_id,
        active_targets=active or ["api"],
        target_bindings=bindings,  # type: ignore[arg-type]
    )
    authority_digest = _write_json(workspace.change_dir / AUTHORITY_REL, authority)
    outputs = {
        f"change:{BASELINE_REL}": str(allocation.get("baseline_sha256") or "").removeprefix("sha256:"),
        f"change:{AUTHORITY_REL}": authority_digest.removeprefix("sha256:"),
    }
    durable: tuple[dict[str, object], ...] = ()
    if emit_durable_effect:
        projection = project_healing_episode(workspace.change_dir)
        baseline_sha = str(allocation["baseline_sha256"])
        if not baseline_sha.startswith("sha256:"):
            baseline_sha = f"sha256:{baseline_sha}"
        effect = HealingAllocationEffectV2(
            schema_version="2",
            episode_id=str(allocation["episode_id"]),
            attempt_id=str(allocation["attempt_id"]),
            attempt_number=int(allocation["attempt_number"]),  # type: ignore[arg-type]
            operation_id=str(allocation["operation_id"]),
            source_batch_id=str(allocation["source_batch_id"]),
            entry_batch_id=str(allocation["entry_batch_id"]),
            baseline_sha256=baseline_sha,
            baseline_embedded=projection.baseline is None,
        )
        durable = (
            _intent_wire(
                build_intent(
                    kind=HEALING_ALLOCATION_V2,
                    payload=effect,
                    invocation_id=task.invocation_id,
                    task_id=task.task_id,
                    attempt_id=attempt_id,
                )
            ),
        )
    return TaskResult(
        status="succeeded",
        value=dict(allocation),
        outputs_sha256=outputs,
        durable_effects=durable,
    )


def _safety_flags_from_params(
    params: Mapping[str, object],
    intent: CodegenFixApplyIntentV1,
) -> dict[str, object]:
    product = bool(params.get("product_code_modified", False))
    skip = bool(params.get("skip_or_xfail_added", False))
    unrelated = bool(params.get("unrelated_tests_modified", False))
    assertion = bool(params.get("assertion_expected_value_changes_detected", False))
    high_risk = bool(params.get("high_risk_proposal_applied", False))
    needs_review = bool(params.get("needs_review", skip or high_risk))
    passed = not (product or skip or unrelated or assertion or (high_risk and needs_review))
    if "passed" in params:
        passed = bool(params["passed"])
    return {
        "passed": passed,
        "needs_review": needs_review,
        "product_code_modified": product,
        "skip_or_xfail_added": skip,
        "unrelated_tests_modified": unrelated,
        "assertion_expected_value_changes_detected": assertion,
        "high_risk_proposal_applied": high_risk,
        "applied_proposal_count": len(intent.proposal_ids) if intent.outcome == "applied" else 0,
    }


def load_manifest_batch_id(change_dir: Path) -> str | None:
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    if not manifest_path.is_file():
        return None
    try:
        doc = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if isinstance(doc, dict) and isinstance(doc.get("batch_id"), str):
        return doc["batch_id"]
    return None


def derive_allocation_ids(
    *,
    change_id: str,
    batch_id: str,
    proposal_sha: str,
    attempt_number: int,
) -> dict[str, object]:
    episode_id = hashlib.sha256(f"{change_id}:{batch_id}:{proposal_sha}".encode()).hexdigest()
    operation_id = hashlib.sha256(f"{batch_id}:{proposal_sha}:{attempt_number}".encode()).hexdigest()
    attempt_id = f"ha-{episode_id[:12]}-{attempt_number}"
    return {
        "episode_id": episode_id,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "source_batch_id": batch_id,
        "entry_batch_id": batch_id,
    }


__all__ = [
    "AUTHORITY_REL",
    "BASELINE_REL",
    "build_fixer_authority_for_allocate",
    "derive_allocation_ids",
    "enhance_allocate_result_with_authority",
    "load_manifest_batch_id",
    "operation_combine_fixer_safety",
    "operation_fixer_authority_ready",
    "operation_fixer_dispatch",
    "operation_record_codegen_fix_apply",
    "operation_record_fixer_approval",
]
