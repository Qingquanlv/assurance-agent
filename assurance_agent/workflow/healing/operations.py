"""Graph-owned healing operations (dark-shipped until Task 15 activation)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.paths import existing_with_alias
from assurance_agent.artifacts.models.generated_files import GeneratedFilesV1
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
from assurance_agent.verification.generated_files import (
    get_generated_files_contract,
    get_generated_files_model,
)
from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.graph.durable_effects import (
    FIXER_PROPOSAL_APPROVED_V1,
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
    DurableEffectIntentV1,
    build_intent,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.precommit import (
    CODEGEN_FIX_CANDIDATE_V1,
    GENERATED_FILES_CANDIDATE_V1,
    CandidateValidationError,
    PrecommitValidationContext,
    bind_receipt_to_success_event,
    load_candidate_receipt,
    verify_candidate_receipt,
)
from assurance_agent.workflow.graph.task_inputs import load_task_input_snapshot
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError, WriteSet
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

_LAYER_CODEGEN_TARGETS: dict[str, Literal["api", "e2e"]] = {
    "skill:aa-api-codegen": "api",
    "skill:aa-e2e-codegen": "e2e",
}
_LAYER_MANIFEST_LOGICAL: dict[str, str] = {
    "api": "change:codegen/api-generated-files.json",
    "e2e": "change:codegen/e2e-generated-files.json",
}


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


def _effect_producer_attempt_id(task: ExecutableTask, context: RuntimeContext) -> str:
    """Return the scheduler attempt identity used by durable-effect validation."""
    return context.task_attempt_id or f"{task.task_id}-a1"


def operation_fixer_authority_ready(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Gate: pass when every active authority target is ready; else stop/unverified."""
    del task
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
    if not authority.targets:
        return TaskResult(status="succeeded", value={"route": "stop", "reason": "no_active_targets"})
    if any(target.status != "ready" for target in authority.targets):
        return TaskResult(
            status="succeeded",
            value={
                "route": "stop",
                "reason": _unverified_authority_stop_reason(
                    authority=authority,
                    change_dir=context.change_dir,
                ),
            },
        )
    outside = _proposal_paths_outside_authority(
        authority=authority,
        proposal_path=workspace.change_dir / "healing" / "fix-proposal.json",
    )
    if outside:
        return TaskResult(
            status="succeeded",
            value={
                "route": "stop",
                "reason": "proposal_paths_outside_fixer_authority:" + ",".join(outside),
            },
        )
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
        attempt_id=_effect_producer_attempt_id(task, context),
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
    explicit_params = task_with(task)
    params = dict(explicit_params)
    target = params.get("target")
    if target not in {"api", "e2e"}:
        return task_failure(
            "invalid_input",
            "record-codegen-fix-apply requires with.target in {api,e2e}",
        )
    discovered = _discover_fixer_candidate_inputs(
        change_dir=context.change_dir,
        store=TreeStore(context.change_dir),
        invocation_id=task.invocation_id,
        target=str(target),
        change_id=context.change_id,
    )
    params = {**discovered, **explicit_params}
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
    fixer_attempt_id = str(params.get("fixer_attempt_id") or "")
    if not fixer_attempt_id:
        return task_failure(
            "invalid_input",
            "record-codegen-fix-apply requires with.fixer_attempt_id",
        )
    receipt_error = _verify_fixer_candidate_receipt(
        workspace=workspace,
        context=context,
        params=params,
        target=str(target),
        write_set_id=write_set_id,
        fixer_attempt_id=fixer_attempt_id,
        task=task,
    )
    if receipt_error is not None:
        return receipt_error
    summary_model = ApiCodegenFixApplySummaryV1 if target == "api" else E2eCodegenFixApplySummaryV1
    summary = summary_model(
        schema_version="1",
        target=target,  # type: ignore[arg-type]
        outcome=intent_doc.outcome,
        proposal_ids=list(intent_doc.proposal_ids),
        claimed_modified_paths=list(intent_doc.claimed_modified_paths),
        intent_sha256=intent_digest,
        write_set_id=write_set_id,
        applied=intent_doc.outcome == "applied",
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
        fixer_attempt_id=fixer_attempt_id,
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
        attempt_id=_effect_producer_attempt_id(task, context),
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
    product_code_modified = False
    skip_or_xfail_added = False
    unrelated_tests_modified = False
    assertion_expected_value_changes_detected = False
    high_risk_proposal_applied = False
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
        product_code_modified = product_code_modified or fragment.product_code_modified
        skip_or_xfail_added = skip_or_xfail_added or fragment.skip_or_xfail_added
        unrelated_tests_modified = unrelated_tests_modified or fragment.unrelated_tests_modified
        assertion_expected_value_changes_detected = (
            assertion_expected_value_changes_detected or fragment.assertion_expected_value_changes_detected
        )
        high_risk_proposal_applied = high_risk_proposal_applied or fragment.high_risk_proposal_applied
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
        product_code_modified=product_code_modified,
        skip_or_xfail_added=skip_or_xfail_added,
        unrelated_tests_modified=unrelated_tests_modified,
        assertion_expected_value_changes_detected=assertion_expected_value_changes_detected,
        high_risk_proposal_applied=high_risk_proposal_applied,
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
    params = dict(task_with(task))
    active_raw = params.get("active_targets")
    if isinstance(active_raw, list) and active_raw:
        active: list[Literal["api", "e2e"]] = [
            item
            for item in active_raw
            if item in {"api", "e2e"}  # type: ignore[misc]
        ]
    else:
        active = _active_targets_from_proposal(workspace)
    if not active:
        active = ["api"]
    store = TreeStore(context.change_dir)
    discovered = discover_codegen_authority_inputs(
        change_dir=context.change_dir,
        store=store,
        active_targets=active,
    )
    merged_params = _merge_authority_params(discovered, params)
    bindings = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=context,
        active_targets=active,
        params=merged_params,
    )
    authority = build_fixer_authority_for_allocate(
        change_id=context.change_id,
        active_targets=active,
        target_bindings=bindings,
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
                    attempt_id=_effect_producer_attempt_id(task, context),
                )
            ),
        )
    return TaskResult(
        status="succeeded",
        value=dict(allocation),
        outputs_sha256=outputs,
        durable_effects=durable,
    )


def _verify_fixer_candidate_receipt(
    *,
    workspace: TaskWorkspace,
    context: RuntimeContext,
    params: Mapping[str, object],
    target: str,
    write_set_id: str,
    fixer_attempt_id: str,
    task: ExecutableTask,
) -> TaskResult | None:
    """Fail closed unless the attempt's CandidateValidationReceiptV1 binds and verifies."""
    receipt_id = params.get("candidate_validation_receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        return task_failure(
            "invalid_input",
            "record-codegen-fix-apply requires with.candidate_validation_receipt_id",
        )
    # CAS lives on the invocation change_dir object store, not the materialized
    # task workspace copy.
    store = TreeStore(context.change_dir)
    try:
        receipt = load_candidate_receipt(store, receipt_id)
    except (CandidateValidationError, WorkspaceError, OSError) as exc:
        return task_failure("invalid_input", f"candidate receipt load failed: {exc}")
    if receipt.validator_id != CODEGEN_FIX_CANDIDATE_V1:
        return task_failure(
            "invalid_input",
            f"candidate receipt validator_id must be {CODEGEN_FIX_CANDIDATE_V1}",
        )
    try:
        bind_receipt_to_success_event(
            receipt,
            validator_id=CODEGEN_FIX_CANDIDATE_V1,
            invocation_id=str(params.get("fixer_invocation_id") or task.invocation_id),
            task_id=str(params.get("fixer_task_id") or receipt.task_id),
            attempt_id=fixer_attempt_id,
            input_snapshot_id=str(params.get("input_snapshot_id") or receipt.input_snapshot_id),
            write_set_id=write_set_id,
        )
    except CandidateValidationError as exc:
        return task_failure("invalid_input", f"candidate receipt identity mismatch: {exc}")
    intent_key = f"change:healing/{target}-apply-intent.json"
    intent_path = workspace.change_dir / f"healing/{target}-apply-intent.json"
    try:
        on_disk_digest = hashlib.sha256(intent_path.read_bytes()).hexdigest()
    except OSError as exc:
        return task_failure("invalid_input", f"unable to digest on-disk intent: {exc}")
    if receipt.output_digests.get(intent_key) != on_disk_digest:
        return task_failure(
            "invalid_input",
            "candidate receipt intent digest does not match on-disk intent",
        )
    verify_bundle = params.get("candidate_receipt_verify")
    if not isinstance(verify_bundle, Mapping):
        return task_failure(
            "invalid_input",
            "record-codegen-fix-apply requires with.candidate_receipt_verify",
        )
    try:
        precommit_context = PrecommitValidationContext.model_validate(verify_bundle["context"])
        write_set = store.load_write_set(write_set_id)
        snapshot = load_task_input_snapshot(store, precommit_context.input_snapshot_id)
        verify_candidate_receipt(
            receipt,
            precommit_context,
            store=store,
            write_set=write_set,
            input_snapshot=snapshot,
            plan_text=str(verify_bundle.get("plan_text") or ""),
            cases=list(verify_bundle.get("cases") or []),
            change_id=context.change_id,
            layer=target,
            current_change_repo_path=str(
                verify_bundle.get("current_change_repo_path") or f"qa/changes/{context.change_id}"
            ),
            project_root=context.resolved_host_root,
        )
    except (CandidateValidationError, WorkspaceError, KeyError, TypeError, ValueError) as exc:
        return task_failure("invalid_input", f"candidate receipt verify failed: {exc}")
    return None


def _discover_fixer_candidate_inputs(
    *,
    change_dir: Path,
    store: TreeStore,
    invocation_id: str,
    target: str,
    change_id: str,
) -> dict[str, object]:
    """Recover the committed fixer candidate identity consumed by ``record-*``.

    Packaged healing nodes can only declare the static target in ``with``.  The
    write-set and receipt identities are scheduler-owned outputs, so bind them
    from the directly preceding committed fixer success in the same invocation.
    """
    expected_target = f"skill:aa-{target}-codegen-fixer"
    expected_node = f"fix-{target}"
    starts: dict[str, Mapping[str, object]] = {}
    successes: dict[str, Mapping[str, object]] = {}
    committed_tasks: set[str] = set()
    committed_write_sets: set[str] = set()
    invocation_start: Mapping[str, object] | None = None

    for event in read_events(change_dir):
        if event.get("source") != "graph" or event.get("invocation_id") != invocation_id:
            continue
        event_type = event.get("type")
        if event_type == "graph_invocation_started":
            invocation_start = event
        elif event_type == "task_attempt_started":
            task_id = event.get("task_id")
            if (
                isinstance(task_id, str)
                and event.get("node_id") == expected_node
                and event.get("target") == expected_target
                and event.get("precommit_validator") == CODEGEN_FIX_CANDIDATE_V1
            ):
                starts[task_id] = event
        elif event_type == "task_attempt_succeeded":
            task_id = event.get("task_id")
            if isinstance(task_id, str) and task_id in starts:
                successes[task_id] = event
        elif event_type == "superstep_committed":
            raw_task_ids = event.get("committed_task_ids")
            if isinstance(raw_task_ids, list):
                committed_tasks.update(task_id for task_id in raw_task_ids if isinstance(task_id, str))
            raw_write_set_ids = event.get("write_set_ids")
            if isinstance(raw_write_set_ids, list):
                committed_write_sets.update(
                    write_set_id for write_set_id in raw_write_set_ids if isinstance(write_set_id, str)
                )

    if invocation_start is None:
        return {}
    candidates: list[tuple[int, str, Mapping[str, object], Mapping[str, object]]] = []
    for task_id, success in successes.items():
        start = starts[task_id]
        write_set_id = success.get("write_set_id")
        receipt_id = success.get("candidate_validation_receipt_id")
        if not isinstance(write_set_id, str) or not write_set_id:
            continue
        if not isinstance(receipt_id, str) or not receipt_id:
            continue
        if task_id not in committed_tasks and write_set_id not in committed_write_sets:
            continue
        seq = success.get("seq")
        candidates.append((seq if isinstance(seq, int) else -1, task_id, start, success))
    if not candidates:
        return {}

    _seq, fixer_task_id, started, success = max(candidates, key=lambda item: item[0])
    write_set_id = str(success["write_set_id"])
    fixer_attempt_id = str(success.get("attempt_id") or started.get("attempt_id") or "")
    input_snapshot_id = str(success.get("input_snapshot_id") or started.get("input_snapshot_id") or "")
    receipt_id = str(success["candidate_validation_receipt_id"])
    if not fixer_attempt_id or not input_snapshot_id:
        return {}

    try:
        write_set = store.load_write_set(write_set_id)
    except (WorkspaceError, OSError, ValueError):
        return {}
    policy_digest = str(invocation_start.get("policy_digest") or "0" * 64)
    contract_digest = str(started.get("contract_digest") or "")
    graph_digest = str(invocation_start.get("graph_digest") or "")
    if not contract_digest or not graph_digest:
        return {}
    definition_semantics = {
        "assurance_profile_digest": str(invocation_start.get("assurance_profile_digest") or "unbound"),
        "commit_safety_semantics_digest": str(
            invocation_start.get("commit_safety_semantics_digest") or "unbound"
        ),
        "commit_safety_semantics_object_id": str(
            invocation_start.get("commit_safety_semantics_object_id") or "unbound"
        ),
        "contract_digest": contract_digest,
        "gate_semantics_digest": str(invocation_start.get("gate_semantics_digest") or "unbound"),
        "gate_semantics_object_id": str(invocation_start.get("gate_semantics_object_id") or "unbound"),
        "graph_digest": graph_digest,
        "topology_safety_semantics_digest": str(
            invocation_start.get("topology_safety_semantics_digest") or "unbound"
        ),
        "topology_safety_semantics_object_id": str(
            invocation_start.get("topology_safety_semantics_object_id") or "unbound"
        ),
    }
    precommit_context = PrecommitValidationContext(
        root_invocation_id=str(invocation_start.get("parent_invocation_id") or invocation_id),
        invocation_id=invocation_id,
        task_id=fixer_task_id,
        attempt_id=fixer_attempt_id,
        target=expected_target,
        base_tree_id=write_set.base_tree_id,
        current_tree_id=write_set.base_tree_id,
        input_snapshot_id=input_snapshot_id,
        contract_digest=contract_digest,
        policy_object_id=policy_digest.removeprefix("sha256:"),
        policy_digest=(policy_digest if policy_digest.startswith("sha256:") else f"sha256:{policy_digest}"),
        gate_attempt_id=None,
        interrupt_id=None,
        output_digests=dict(sorted(write_set.outputs_sha256.items())),
        write_set_id=write_set_id,
        definition_semantics=definition_semantics,
    )
    roots = write_set.base_tree_roots or {}
    current_change_repo_path = roots.get("change") or f"qa/changes/{change_id}"
    return {
        "write_set_id": write_set_id,
        "fixer_attempt_id": fixer_attempt_id,
        "fixer_task_id": fixer_task_id,
        "fixer_invocation_id": invocation_id,
        "input_snapshot_id": input_snapshot_id,
        "candidate_validation_receipt_id": receipt_id,
        "candidate_receipt_verify": {
            "context": precommit_context.model_dump(mode="json"),
            "plan_text": "",
            "cases": [],
            "current_change_repo_path": current_change_repo_path,
        },
    }


def _repo_path_from_write_logical(logical_path: str) -> str | None:
    for prefix in ("repo:", "project:"):
        if logical_path.startswith(prefix):
            return logical_path[len(prefix) :]
    return None


def _write_after_by_repo_path(write_set: WriteSet) -> dict[str, tuple[str, str]]:
    """Map repo path -> (operation, after_sha256 bare hex) for add/modify entries."""
    result: dict[str, tuple[str, str]] = {}
    for entry in write_set.entries:
        repo_path = _repo_path_from_write_logical(entry.logical_path)
        if repo_path is None or entry.operation == "delete" or entry.after_sha256 is None:
            continue
        result[repo_path] = (entry.operation, entry.after_sha256.removeprefix("sha256:"))
    return result


def _snapshot_digest_for_repo_path(
    snapshot_entries: list[Mapping[str, object]] | None,
    repo_path: str,
) -> str | None:
    if not snapshot_entries:
        return None
    for entry in snapshot_entries:
        if entry.get("kind") != "file":
            continue
        sha = entry.get("sha256")
        if not isinstance(sha, str) or not sha:
            continue
        if entry.get("repo_relpath") == repo_path:
            return sha if sha.startswith("sha256:") else f"sha256:{sha}"
        aliases = entry.get("logical_aliases")
        if isinstance(aliases, list) and (
            f"repo:{repo_path}" in aliases or f"project:{repo_path}" in aliases
        ):
            return sha if sha.startswith("sha256:") else f"sha256:{sha}"
    return None


def _active_targets_from_proposal(workspace: TaskWorkspace) -> list[Literal["api", "e2e"]]:
    """Derive allocate active targets from the frozen fix-proposal (packaged path)."""
    proposal_path = workspace.change_dir / "healing" / "fix-proposal.json"
    if not proposal_path.is_file():
        return []
    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = proposal.get("proposals") if isinstance(proposal, dict) else None
    active: list[Literal["api", "e2e"]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or not item.get("eligible"):
                continue
            target = item.get("target")
            if target in {"api", "e2e"} and target not in active:
                active.append(target)  # type: ignore[arg-type]
    return active


def _layer_from_codegen_outputs(outputs: Mapping[str, object] | None) -> Literal["api", "e2e"] | None:
    if not isinstance(outputs, Mapping):
        return None
    for layer, logical in _LAYER_MANIFEST_LOGICAL.items():
        if logical in outputs or logical.removeprefix("change:") in outputs:
            return layer  # type: ignore[return-value]
        # outputs_sha256 keys are sometimes bare change-relative paths
        bare = logical.removeprefix("change:")
        for key in outputs:
            if str(key) in {logical, bare, f"change:{bare}"}:
                return layer  # type: ignore[return-value]
    return None


def _layer_from_started_event(started: Mapping[str, object]) -> Literal["api", "e2e"] | None:
    target = started.get("target")
    if isinstance(target, str) and target in _LAYER_CODEGEN_TARGETS:
        return _LAYER_CODEGEN_TARGETS[target]
    return None


def _layer_from_imported_event(imported: Mapping[str, object]) -> Literal["api", "e2e"] | None:
    layer = _layer_from_codegen_outputs(imported.get("outputs_sha256"))  # type: ignore[arg-type]
    if layer is not None:
        return layer
    structural = str(imported.get("structural_path") or "")
    node_id = imported.get("node_id")
    if node_id != "codegen":
        return None
    if "/api" in f"/{structural}" or structural.startswith("api") or "api-branch" in structural:
        return "api"
    if "/e2e" in f"/{structural}" or structural.startswith("e2e") or "e2e-branch" in structural:
        return "e2e"
    graph_id = str(imported.get("graph_id") or "")
    if graph_id.startswith("api"):
        return "api"
    if graph_id.startswith("e2e"):
        return "e2e"
    return None


def discover_codegen_authority_inputs(
    *,
    change_dir: Path,
    store: TreeStore,
    active_targets: Sequence[str],
) -> dict[str, object]:
    """Resolve committed codegen write-set IDs / snapshot digests from the ledger.

    Packaged ``allocate`` does not receive ``with.codegen_write_set_ids``; the
    production path discovers them from committed ``generated_files_candidate/v1``
    successes (and marks ``task_imported`` codegen as imported).
    """
    wanted = {layer for layer in active_targets if layer in {"api", "e2e"}}
    if not wanted:
        return {}
    events = read_events(change_dir)
    started_by_task: dict[str, dict[str, object]] = {}
    success_by_task: dict[str, dict[str, object]] = {}
    committed_write_sets: set[str] = set()
    committed_tasks: set[str] = set()
    imported_layers: set[str] = set()

    for raw in events:
        if raw.get("source") != "graph":
            continue
        event_type = raw.get("type")
        if event_type == "task_attempt_started":
            task_id = raw.get("task_id")
            if isinstance(task_id, str):
                started_by_task[task_id] = raw
        elif event_type == "task_attempt_succeeded":
            task_id = raw.get("task_id")
            if isinstance(task_id, str):
                success_by_task[task_id] = raw
        elif event_type == "superstep_committed":
            raw_write_ids = raw.get("write_set_ids")
            if isinstance(raw_write_ids, list):
                for write_set_id in raw_write_ids:
                    if isinstance(write_set_id, str):
                        committed_write_sets.add(write_set_id)
            raw_task_ids = raw.get("committed_task_ids")
            if isinstance(raw_task_ids, list):
                for task_id in raw_task_ids:
                    if isinstance(task_id, str):
                        committed_tasks.add(task_id)
        elif event_type == "task_imported":
            layer = _layer_from_imported_event(raw)
            if layer in wanted:
                imported_layers.add(layer)

    write_set_ids: dict[str, str] = {}
    attempt_ids: dict[str, str] = {}
    snapshot_entries: list[dict[str, object]] = []
    live_layers: set[str] = set()

    for task_id, started in started_by_task.items():
        layer = _layer_from_started_event(started)
        success = success_by_task.get(task_id)
        if success is not None and layer is None:
            layer = _layer_from_codegen_outputs(success.get("outputs_sha256"))  # type: ignore[arg-type]
        if layer not in wanted:
            continue
        validator = started.get("precommit_validator")
        target = started.get("target")
        if validator != GENERATED_FILES_CANDIDATE_V1 and target not in _LAYER_CODEGEN_TARGETS:
            continue
        if success is None:
            continue
        write_set_id = success.get("write_set_id")
        if not isinstance(write_set_id, str) or not write_set_id:
            continue
        if task_id not in committed_tasks and write_set_id not in committed_write_sets:
            continue
        write_set_ids[layer] = write_set_id
        live_layers.add(layer)
        attempt_id = success.get("attempt_id") or started.get("attempt_id") or task_id
        if isinstance(attempt_id, str):
            attempt_ids[layer] = attempt_id
        snap_id = success.get("input_snapshot_id") or started.get("input_snapshot_id")
        if isinstance(snap_id, str) and snap_id:
            try:
                snapshot = load_task_input_snapshot(store, snap_id)
            except (WorkspaceError, OSError, ValueError, TypeError):
                continue
            snapshot_entries.extend(entry.model_dump(mode="json") for entry in snapshot.entries)

    still_imported = sorted(imported_layers - live_layers)
    result: dict[str, object] = {}
    if write_set_ids:
        result["codegen_write_set_ids"] = write_set_ids
    if attempt_ids:
        result["codegen_attempt_ids"] = attempt_ids
    if snapshot_entries:
        result["input_snapshot_entries"] = snapshot_entries
    if still_imported:
        result["imported_targets"] = still_imported
    return result


def _merge_authority_params(
    discovered: Mapping[str, object],
    params: Mapping[str, object],
) -> dict[str, object]:
    """Fill gaps from ledger discovery; explicit ``with`` params win per key/layer."""
    merged: dict[str, object] = dict(discovered)
    for key, value in params.items():
        if key in {"codegen_write_set_ids", "codegen_attempt_ids"} and isinstance(value, Mapping):
            existing = merged.get(key)
            base: dict[str, object] = (
                {str(item_key): item_val for item_key, item_val in existing.items()}
                if isinstance(existing, Mapping)
                else {}
            )
            base.update({str(item_key): item_val for item_key, item_val in value.items()})
            merged[key] = base
        elif key == "imported_targets" and isinstance(value, list):
            existing = merged.get(key)
            prior = [str(item) for item in existing] if isinstance(existing, list) else []
            merged[key] = sorted({*prior, *(str(item) for item in value)})
        elif key == "input_snapshot_entries" and isinstance(value, list):
            merged[key] = value
        else:
            merged[key] = value
    return merged


def _unverified_authority_stop_reason(
    *,
    authority: FixerAuthorityV1,
    change_dir: Path,
) -> str:
    """Distinguish imported narrowing from missing write-set binding failures."""
    non_ready = [target for target in authority.targets if target.status != "ready"]
    if not non_ready:
        return "unverified_fixer_authority"
    active = [target.target for target in non_ready]
    try:
        discovered = discover_codegen_authority_inputs(
            change_dir=change_dir,
            store=TreeStore(change_dir),
            active_targets=active,
        )
    except (OSError, ValueError, TypeError):
        discovered = {}
    imported_raw = discovered.get("imported_targets")
    imported = (
        {str(item) for item in imported_raw if isinstance(item, str)}
        if isinstance(imported_raw, list)
        else set()
    )
    live_write_sets = discovered.get("codegen_write_set_ids")
    live_layers = {str(key) for key in live_write_sets} if isinstance(live_write_sets, Mapping) else set()
    classes: list[str] = []
    for target in non_ready:
        layer = target.target
        if layer in imported and layer not in live_layers:
            classes.append("imported")
        elif target.write_set_id is None and layer not in live_layers:
            classes.append("missing_write_set")
        else:
            classes.append("unverified")
    if classes and all(item == "imported" for item in classes):
        return "unverified_imported_codegen"
    if "missing_write_set" in classes:
        return "missing_codegen_write_set_binding"
    return "unverified_fixer_authority"


def _proposal_paths_outside_authority(
    *,
    authority: FixerAuthorityV1,
    proposal_path: Path,
) -> list[str]:
    """Return eligible proposal paths that no ready target authorizes."""
    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    proposals = proposal.get("proposals") if isinstance(proposal, Mapping) else None
    if not isinstance(proposals, list):
        return []
    allowed = {
        target.target: {path.repo_path for path in target.paths}
        for target in authority.targets
        if target.status == "ready"
    }
    outside: set[str] = set()
    for item in proposals:
        if not isinstance(item, Mapping) or item.get("eligible") is not True:
            continue
        target = item.get("target")
        files = item.get("files_to_modify")
        if target not in {"api", "e2e"} or not isinstance(files, list):
            continue
        target_allowed = allowed.get(str(target), set())
        outside.update(path for path in files if isinstance(path, str) and path not in target_allowed)
    return sorted(outside)


def allocate_authority_bindings_from_artifacts(
    *,
    workspace: TaskWorkspace,
    context: RuntimeContext,
    active_targets: list[Literal["api", "e2e"]],
    params: Mapping[str, object],
) -> dict[str, dict[str, Any]]:
    """Bind authority from codegen manifests/write sets; passthrough only as test seam."""
    passthrough = params.get("authority_bindings")
    passthrough_map = passthrough if isinstance(passthrough, Mapping) else {}
    write_set_ids = params.get("codegen_write_set_ids")
    write_set_id_map = write_set_ids if isinstance(write_set_ids, Mapping) else {}
    attempt_ids = params.get("codegen_attempt_ids")
    attempt_id_map = attempt_ids if isinstance(attempt_ids, Mapping) else {}
    imported_raw = params.get("imported_targets")
    imported_targets = {str(item) for item in imported_raw} if isinstance(imported_raw, list) else set()
    if bool(params.get("imported")):
        imported_targets.update(active_targets)
    snapshot_entries_raw = params.get("input_snapshot_entries")
    snapshot_entries = (
        [item for item in snapshot_entries_raw if isinstance(item, Mapping)]
        if isinstance(snapshot_entries_raw, list)
        else None
    )
    store = TreeStore(context.change_dir)
    batch_id = load_manifest_batch_id(workspace.change_dir) or str(
        params.get("execution_batch_id") or "unknown"
    )
    bindings: dict[str, dict[str, Any]] = {}
    for target in active_targets:
        if target in imported_targets:
            bindings[target] = {"status": "unverified", "paths": []}
            continue
        contract = get_generated_files_contract(target)
        manifest_rel = contract.manifest_path.removeprefix("change:")
        summary_rel = contract.summary_path.removeprefix("change:")
        manifest_path = workspace.change_dir / manifest_rel
        summary_path = workspace.change_dir / summary_rel
        if not manifest_path.is_file() or not summary_path.is_file():
            # Test seam: allow with.authority_bindings only when artifacts are absent.
            seam = passthrough_map.get(target)
            if isinstance(seam, Mapping):
                bindings[target] = dict(seam)
            else:
                bindings[target] = {"status": "unverified", "paths": []}
            continue
        try:
            manifest = get_generated_files_model(target).model_validate(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except Exception:
            bindings[target] = {"status": "unverified", "paths": []}
            continue
        if manifest.change_id != context.change_id:
            bindings[target] = {"status": "unverified", "paths": []}
            continue
        write_set_id = str(write_set_id_map.get(target) or params.get("write_set_id") or "")
        if not write_set_id:
            bindings[target] = {"status": "unverified", "paths": []}
            continue
        try:
            write_set = store.load_write_set(write_set_id)
        except (WorkspaceError, OSError, ValueError):
            bindings[target] = {"status": "unverified", "paths": []}
            continue
        path_bindings = _authority_paths_from_manifest_and_write_set(
            manifest=manifest,
            write_set=write_set,
            private_root=contract.private_test_root,
            snapshot_entries=snapshot_entries,
            repo_root=workspace.repo_root,
        )
        if path_bindings is None:
            bindings[target] = {"status": "unverified", "paths": []}
            continue
        generated_files_sha256 = sha256_bytes(canonical_json_bytes(manifest))
        summary_sha256 = sha256_bytes(summary_path.read_bytes())
        bindings[target] = {
            "status": "ready",
            "codegen_attempt_id": str(
                attempt_id_map.get(target) or params.get("codegen_attempt_id") or f"codegen-{target}"
            ),
            "generated_files_sha256": generated_files_sha256,
            "summary_sha256": summary_sha256,
            "write_set_id": write_set_id,
            "execution_batch_id": batch_id,
            "paths": path_bindings,
        }
    return bindings


def _authority_paths_from_manifest_and_write_set(
    *,
    manifest: GeneratedFilesV1,
    write_set: WriteSet,
    private_root: str,
    snapshot_entries: list[Mapping[str, object]] | None,
    repo_root: Path,
) -> list[FixerAuthorityPathV1] | None:
    writes = _write_after_by_repo_path(write_set)
    paths: list[FixerAuthorityPathV1] = []
    for entry in manifest.files:
        if entry.disposition in {"generated", "updated"}:
            write = writes.get(entry.repo_path)
            if write is None:
                return None
            operation, after = write
            if operation not in {"add", "modify"}:
                return None
            if f"sha256:{after}" != entry.content_sha256:
                return None
            current_path = repo_root / entry.repo_path
            if not current_path.is_file():
                return None
            paths.append(
                FixerAuthorityPathV1(
                    repo_path=entry.repo_path,
                    disposition=entry.disposition,
                    # Keep the original codegen write-set/manifest checks above as
                    # provenance, but bind this healing attempt to its current
                    # committed baseline. Earlier healing attempts may already
                    # have changed the same generated test.
                    content_sha256=sha256_bytes(current_path.read_bytes()),
                )
            )
        elif entry.disposition == "reused":
            if (
                not entry.repo_path.startswith(private_root.rstrip("/") + "/")
                and entry.repo_path != private_root
            ):
                # Summary-only / shared helpers outside private root do not gain edit authority.
                continue
            # Fail closed: reuse authority requires a present, matching input-snapshot digest.
            # Never fall back to the manifest's self-attested content_sha256.
            digest = _snapshot_digest_for_repo_path(snapshot_entries, entry.repo_path)
            if digest is None or digest != entry.content_sha256:
                return None
            current_path = repo_root / entry.repo_path
            if not current_path.is_file():
                return None
            paths.append(
                FixerAuthorityPathV1(
                    repo_path=entry.repo_path,
                    disposition="reused",
                    content_sha256=sha256_bytes(current_path.read_bytes()),
                )
            )
    return paths


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
    manifest_path = existing_with_alias(change_dir / "execution" / "execution-manifest.json")
    if manifest_path is None:
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
    "allocate_authority_bindings_from_artifacts",
    "build_fixer_authority_for_allocate",
    "derive_allocation_ids",
    "discover_codegen_authority_inputs",
    "enhance_allocate_result_with_authority",
    "load_manifest_batch_id",
    "operation_combine_fixer_safety",
    "operation_fixer_authority_ready",
    "operation_fixer_dispatch",
    "operation_record_codegen_fix_apply",
    "operation_record_fixer_approval",
]
