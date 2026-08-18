"""Ledger fold: events.jsonl is the only authority for GraphProjection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GRAPH_EVENT_ADAPTER,
    BudgetConsumedEvent,
    CheckpointImportedEvent,
    DurableEffectAcknowledgedEvent,
    DurableEffectIntegrityFailedEvent,
    TopologySafetyCompatibilityRecordedEvent,
    FanOutExpandedEvent,
    GraphInterruptedEvent,
    GraphInvocationStartedEvent,
    GraphInvocationSupersededEvent,
    GraphResumedEvent,
    GraphTerminalEvent,
    ManualPlanRevisionEvent,
    NodeActivatedEvent,
    NodeSkippedEvent,
    SuperstepCommittedEvent,
    SuperstepPlannedEvent,
    TaskAttemptAbandonedEvent,
    TaskAttemptFailedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptStoppedEvent,
    TaskAttemptSucceededEvent,
    TaskImportedEvent,
    TaskRecoveryRoutedEvent,
    TaskSchedulingDeferredEvent,
)
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.migrate_events import migrate_events_for_fold
from assurance_agent.workflow.graph.models import (
    FanOutExpansion,
    GraphProjection,
    InterruptProjection,
    RecoveryProjection,
    TaskProjection,
    WorkflowStateProjection,
)
from assurance_agent.workflow.graph.node_history import GenerationFoldState

_LEDGER_ENVELOPE_KEYS = frozenset({"seq", "ts"})
_CONTRACT_FAILURE_KINDS = frozenset({"invalid_output", "forbidden_write"})
_TERMINAL_BY_TYPE: dict[str, Literal["completed", "stopped", "failed"]] = {
    "graph_completed": "completed",
    "graph_stopped": "stopped",
    "graph_failed": "failed",
}

_AttemptOutcomeEvent = (
    TaskAttemptSucceededEvent | TaskAttemptStoppedEvent | TaskAttemptFailedEvent | TaskAttemptAbandonedEvent
)


def _record_contract_failure(
    seen: tuple[ErrorKind, ...],
    error_kind: ErrorKind,
) -> tuple[ErrorKind, ...]:
    if error_kind not in _CONTRACT_FAILURE_KINDS or error_kind in seen:
        return seen
    return (*seen, error_kind)


def _require_task(tasks: dict[str, TaskProjection], event: _AttemptOutcomeEvent) -> TaskProjection:
    prev = tasks.get(event.task_id)
    if prev is None:
        raise LedgerIntegrityError(f"{event.type} references unknown task {event.task_id}")
    return prev


@dataclass(frozen=True)
class _TaskStartMeta:
    generation_ordinal: int | None
    task_key: str | None
    fan_out_child: bool
    fan_out_aggregate: bool


def _task_start_meta(
    event: TaskAttemptStartedEvent,
    fan_outs: dict[str, FanOutExpansion],
    generation: GenerationFoldState,
) -> _TaskStartMeta:
    expansion = fan_outs.get(event.node_id)
    generation_ordinal = generation.task_generation.get(event.task_id)
    fan_out_child = False
    fan_out_aggregate = False
    task_key: str | None = None
    if expansion is not None and event.task_id in expansion.task_ids:
        fan_out_child = True
        index = expansion.task_ids.index(event.task_id)
        task_key = expansion.task_keys[index]
        generation_ordinal = generation_ordinal or generation.task_generation.get(event.task_id)
    else:
        history = generation.node_histories.get(
            f"{generation.checkpoint_ns}\x1f{generation.graph_id}\x1f{event.node_id}"
        )
        if history is not None:
            for gen in history.generations_by_ordinal.values():
                if gen.aggregate_task_id == event.task_id:
                    fan_out_aggregate = True
                    generation_ordinal = gen.generation_ordinal
                    task_key = "__aggregate__"
                    break
            if generation_ordinal is None and history.latest_generation_ordinal >= 0:
                latest = history.generations_by_ordinal.get(history.latest_generation_ordinal)
                if latest is not None and (
                    latest.aggregate_task_id is None or latest.aggregate_task_id == event.task_id
                ):
                    generation_ordinal = latest.generation_ordinal
    return _TaskStartMeta(
        generation_ordinal=generation_ordinal,
        task_key=task_key,
        fan_out_child=fan_out_child,
        fan_out_aggregate=fan_out_aggregate,
    )


def _imported_task_id(event: TaskImportedEvent, fan_outs: dict[str, FanOutExpansion]) -> str:
    """导入事件的 task ID：fan-out child 复用 expansion 冻结的 ID，否则按 structural path 派生。"""
    if event.task_key is not None:
        expansion = fan_outs.get(event.node_id)
        if expansion is not None and event.task_key in expansion.task_keys:
            return expansion.task_ids[expansion.task_keys.index(event.task_key)]
        return f"{event.structural_path}:{event.node_id}:{event.task_key}"
    return f"{event.structural_path}:{event.node_id}"


def _require_v6_epoch(started: GraphInvocationStartedEvent | None, *, what: str) -> None:
    if started is None or started.event_schema_version != 6:
        raise LedgerIntegrityError(f"{what} requires event_schema_version 6")


def _fold_manual_plan_revision(
    *,
    event: ManualPlanRevisionEvent,
    started: GraphInvocationStartedEvent | None,
    invocation_id: str,
    current_tree_id: str,
    interrupts: dict[str, InterruptProjection],
    unconsumed_revision_transitions: dict[str, ManualPlanRevisionEvent],
    consumed_revision_transitions: set[str],
) -> None:
    _require_v6_epoch(started, what="manual_plan_revision")
    pending = interrupts.get(event.interrupt_id)
    if pending is None or pending.resolved_action is not None:
        raise LedgerIntegrityError(
            f"manual_plan_revision requires unresolved interrupt {event.interrupt_id} "
            f"in invocation {invocation_id}"
        )
    if pending.revision_owner_invocation_id != event.invocation_id:
        raise LedgerIntegrityError(
            f"manual_plan_revision owner mismatch for interrupt {event.interrupt_id}: "
            f"expected {pending.revision_owner_invocation_id!r}, got {event.invocation_id!r}"
        )
    if event.base_tree_id != current_tree_id:
        raise LedgerIntegrityError(
            f"manual_plan_revision base_tree mismatch for invocation {invocation_id}: "
            f"expected {current_tree_id}, got {event.base_tree_id}"
        )
    if event.target_tree_id == event.base_tree_id:
        raise LedgerIntegrityError(
            f"manual_plan_revision target_tree must differ from base_tree in invocation {invocation_id}"
        )
    logical = list(event.logical_paths)
    before = dict(event.before_sha256)
    after = dict(event.after_sha256)
    if set(logical) != set(before) or set(logical) != set(after):
        raise LedgerIntegrityError(
            f"manual_plan_revision path/digest mismatch for invocation {invocation_id}"
        )
    if pending.revision_paths is not None and list(pending.revision_paths) != logical:
        raise LedgerIntegrityError(
            f"manual_plan_revision path/digest mismatch for invocation {invocation_id}"
        )
    if pending.revision_before_sha256 is not None and dict(pending.revision_before_sha256) != before:
        raise LedgerIntegrityError(
            f"manual_plan_revision path/digest mismatch for invocation {invocation_id}"
        )
    transition_id = event.revision_transition_id
    if transition_id in unconsumed_revision_transitions or transition_id in consumed_revision_transitions:
        raise LedgerIntegrityError(
            f"manual_plan_revision reuses revision transition {transition_id} in invocation {invocation_id}"
        )
    unconsumed_revision_transitions[transition_id] = event


def _fold_graph_resumed_revision_and_source(
    *,
    event: GraphResumedEvent,
    pending: InterruptProjection,
    started: GraphInvocationStartedEvent | None,
    invocation_id: str,
    unconsumed_revision_transitions: dict[str, ManualPlanRevisionEvent],
    consumed_revision_transitions: set[str],
) -> None:
    has_revision_triple = event.revision_transition_id is not None
    if has_revision_triple:
        _require_v6_epoch(started, what="revision-tagged graph_resumed")
    if started is not None:
        pending_pair = (pending.source_gate_attempt_id, pending.source_gate_tree_id)
        resume_pair = (event.source_gate_attempt_id, event.source_gate_tree_id)
        if pending_pair != resume_pair:
            raise LedgerIntegrityError(
                f"graph_resumed source_gate pair mismatch for interrupt {event.interrupt_id} "
                f"in invocation {invocation_id}"
            )
    if not has_revision_triple:
        return
    transition_id = event.revision_transition_id
    assert transition_id is not None
    is_revision_owner = pending.revision_owner_invocation_id == event.invocation_id
    if not is_revision_owner:
        # Ancestor resume: validate the ordered triple only; tree stays unchanged.
        return
    if transition_id in consumed_revision_transitions:
        raise LedgerIntegrityError(
            f"graph_resumed reuses revision transition {transition_id} in invocation {invocation_id}"
        )
    prior = unconsumed_revision_transitions.pop(transition_id, None)
    if prior is None:
        raise LedgerIntegrityError(
            f"graph_resumed missing prior unconsumed revision transition {transition_id} "
            f"in invocation {invocation_id}"
        )
    if prior.interrupt_id != event.interrupt_id:
        raise LedgerIntegrityError(
            f"graph_resumed revision transition {transition_id} interrupt mismatch "
            f"in invocation {invocation_id}"
        )
    consumed_revision_transitions.add(transition_id)


def fold_invocation_events(invocation_id: str, events: list[dict[str, object]]) -> GraphProjection:
    """纯函数：把 strict ledger 事件折叠成 ``GraphProjection``（不触碰磁盘）。

    只折叠 ``source == "graph"`` 且属于该 invocation 的事件；投影绝不反向
    覆盖 ledger。重复 ``graph_invocation_started``、未知 task 的 attempt 结局、
    未配对的 ``graph_resumed``、重复 ``budget_consumed``（按
    ``(invocation_id, budget_id, consumption_id)`` 去重）都是完整性失败。
    """
    events = migrate_events_for_fold(events)
    started: GraphInvocationStartedEvent | None = None
    event_seq = 0
    supersteps = 0
    current_superstep_task_ids: list[str] = []
    current_tree_id = ""
    latest_checkpoint_id: str | None = None
    state_values: dict[str, object] = {}
    tasks: dict[str, TaskProjection] = {}
    budgets: dict[str, int] = {}
    seen_consumptions: set[tuple[str, str]] = set()
    fan_outs: dict[str, FanOutExpansion] = {}
    interrupts: dict[str, InterruptProjection] = {}
    recoveries: dict[str, RecoveryProjection] = {}
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None
    generation = GenerationFoldState()
    unconsumed_revision_transitions: dict[str, ManualPlanRevisionEvent] = {}
    consumed_revision_transitions: set[str] = set()
    seen_deferrals: dict[str, TaskSchedulingDeferredEvent] = {}
    seen_effect_acks: dict[str, DurableEffectAcknowledgedEvent] = {}
    seen_topology_receipts: dict[str, TopologySafetyCompatibilityRecordedEvent] = {}
    topology_compatibility_receipt_id: str | None = None
    supersede_id: str | None = None
    seen_supersedes: dict[str, GraphInvocationSupersededEvent] = {}

    for raw in events:
        if raw.get("source") != "graph":
            continue
        payload = {k: v for k, v in raw.items() if k not in _LEDGER_ENVELOPE_KEYS}
        try:
            event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise LedgerIntegrityError(f"invalid graph event payload: {exc}") from exc
        # Supersede fences the whole bound subtree; descendants see the root event.
        if event.invocation_id != invocation_id:
            if not (
                isinstance(event, GraphInvocationSupersededEvent)
                and invocation_id in event.descendant_invocation_ids
            ):
                continue
        seq = raw.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            event_seq = max(event_seq, seq)

        if isinstance(event, GraphInvocationStartedEvent):
            if started is not None:
                raise LedgerIntegrityError(
                    f"duplicate graph_invocation_started for invocation {invocation_id}"
                )
            started = event
            current_tree_id = event.root_tree_id
            generation.invocation_id = event.invocation_id
            generation.checkpoint_ns = event.checkpoint_ns
            generation.graph_id = event.graph_id
            generation.structural_path = event.structural_path
        elif isinstance(event, NodeActivatedEvent):
            generation.apply_node_activated(event)
        elif isinstance(event, NodeSkippedEvent):
            generation.apply_node_skipped(event)
        elif isinstance(event, FanOutExpandedEvent):
            expansion = FanOutExpansion(
                items=tuple(event.items),
                task_keys=tuple(event.task_keys),
                task_ids=tuple(event.task_ids),
                source_reads_sha256=dict(event.source_reads_sha256),
            )
            fan_outs[event.node_id] = expansion
            generation.apply_fan_out_expanded(event, expansion=expansion)
        elif isinstance(event, SuperstepPlannedEvent):
            supersteps += 1
            current_superstep_task_ids = list(event.task_ids)
        elif isinstance(event, TaskAttemptStartedEvent):
            meta = _task_start_meta(event, fan_outs, generation)
            prev = tasks.get(event.task_id)
            tasks[event.task_id] = TaskProjection(
                task_id=event.task_id,
                node_id=event.node_id,
                status="running",
                generation_ordinal=meta.generation_ordinal,
                task_key=meta.task_key,
                fan_out_child=meta.fan_out_child,
                fan_out_aggregate=meta.fan_out_aggregate,
                attempts_used=max(prev.attempts_used if prev else 0, event.attempt_number),
                latest_attempt_id=event.attempt_id,
                write_set_id=prev.write_set_id if prev else None,
                outputs_sha256=prev.outputs_sha256 if prev else {},
                frozen_outputs=dict(prev.frozen_outputs) if prev else {},
                outputs_committed=prev.outputs_committed if prev else False,
                gate_report=prev.gate_report if prev else None,
                state_updates=prev.state_updates if prev else {},
                contract_failure_kinds_seen=(prev.contract_failure_kinds_seen if prev else ()),
                lease_expires_at=event.lease_expires_at,
                input_snapshot_id=event.input_snapshot_id,
                runtime_context_sha256=event.runtime_context_sha256,
                precommit_validator=event.precommit_validator
                if event.precommit_validator is not None
                else (prev.precommit_validator if prev else None),
                target=event.target if event.target is not None else (prev.target if prev else None),
                deferral_ordinal=prev.deferral_ordinal if prev else 0,
                latest_deferral_id=prev.latest_deferral_id if prev else None,
            )
            generation.apply_task_started(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskAttemptSucceededEvent):
            prev = _require_task(tasks, event)
            if prev.input_snapshot_id is not None and event.input_snapshot_id != prev.input_snapshot_id:
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded input_snapshot_id mismatch for {event.task_id}"
                )
            if (
                prev.runtime_context_sha256 is not None
                and event.runtime_context_sha256 != prev.runtime_context_sha256
            ):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded runtime_context_sha256 mismatch for {event.task_id}"
                )
            if (
                prev.candidate_validation_receipt_id is not None
                and event.candidate_validation_receipt_id != prev.candidate_validation_receipt_id
            ):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded candidate_validation_receipt_id mismatch for {event.task_id}"
                )
            if prev.precommit_validator is not None and event.candidate_validation_receipt_id is None:
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded missing candidate_validation_receipt_id for "
                    f"validator {prev.precommit_validator} on {event.task_id}"
                )
            durable_effects = tuple(dict(item) for item in event.durable_effects)
            effect_ids = []
            for item in durable_effects:
                effect_id = item.get("effect_id")
                if not isinstance(effect_id, str) or not effect_id.strip():
                    raise LedgerIntegrityError(
                        f"task_attempt_succeeded durable_effects missing effect_id for {event.task_id}"
                    )
                effect_ids.append(effect_id)
            if len(set(effect_ids)) != len(effect_ids):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded durable_effects duplicate effect_id for {event.task_id}"
                )
            if tuple(sorted(effect_ids)) != tuple(effect_ids):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded durable_effects must be sorted by effect_id for {event.task_id}"
                )
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "succeeded",
                    "latest_attempt_id": event.attempt_id,
                    "write_set_id": event.write_set_id,
                    "outputs_sha256": dict(event.outputs_sha256),
                    "frozen_outputs": dict(event.frozen_outputs),
                    "outputs_committed": False,
                    "gate_report": event.gate_report,
                    "state_updates": dict(event.state_updates),
                    "value": event.value,
                    "error_kind": None,
                    "error": None,
                    "next_retry_at": None,
                    "input_snapshot_id": event.input_snapshot_id
                    if event.input_snapshot_id is not None
                    else prev.input_snapshot_id,
                    "runtime_context_sha256": event.runtime_context_sha256
                    if event.runtime_context_sha256 is not None
                    else prev.runtime_context_sha256,
                    "candidate_validation_receipt_id": event.candidate_validation_receipt_id
                    if event.candidate_validation_receipt_id is not None
                    else prev.candidate_validation_receipt_id,
                    "precommit_validator": prev.precommit_validator,
                    "target": prev.target,
                    "durable_effects": durable_effects,
                    "acknowledged_effect_ids": prev.acknowledged_effect_ids,
                }
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskSchedulingDeferredEvent):
            prior = seen_deferrals.get(event.deferral_id)
            if prior is not None:
                if prior.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise LedgerIntegrityError(
                        f"conflicting task_scheduling_deferred payload for {event.deferral_id}"
                    )
                continue
            seen_deferrals[event.deferral_id] = event
            prev = tasks.get(event.task_id)
            if prev is not None and event.deferral_ordinal < prev.deferral_ordinal:
                continue
            if prev is not None and event.deferral_ordinal == prev.deferral_ordinal:
                if prev.latest_deferral_id not in (None, event.deferral_id):
                    raise LedgerIntegrityError(
                        f"conflicting deferral ordinal {event.deferral_ordinal} for {event.task_id}"
                    )
            if prev is None:
                tasks[event.task_id] = TaskProjection(
                    task_id=event.task_id,
                    node_id=event.node_id,
                    status="pending",
                    next_retry_at=event.next_retry_at,
                    deferral_ordinal=event.deferral_ordinal,
                    latest_deferral_id=event.deferral_id,
                )
            else:
                tasks[event.task_id] = prev.model_copy(
                    update={
                        "next_retry_at": event.next_retry_at,
                        "deferral_ordinal": event.deferral_ordinal,
                        "latest_deferral_id": event.deferral_id,
                        "error_kind": None,
                        "error": None,
                    }
                )
        elif isinstance(event, TaskAttemptStoppedEvent):
            prev = _require_task(tasks, event)
            value = event.value
            if value is None:
                value = {"reason": event.reason}
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "stopped",
                    "latest_attempt_id": event.attempt_id,
                    "value": value,
                    "error_kind": None,
                    "error": None,
                    "next_retry_at": None,
                }
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskAttemptFailedEvent):
            prev = _require_task(tasks, event)
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "failed",
                    "latest_attempt_id": event.attempt_id,
                    "error_kind": event.error_kind,
                    "contract_failure_kinds_seen": _record_contract_failure(
                        prev.contract_failure_kinds_seen,
                        event.error_kind,
                    ),
                    "error": event.message,
                    "next_retry_at": event.next_retry_at,
                }
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskRecoveryRoutedEvent):
            if (
                started is None
                or event.checkpoint_ns != started.checkpoint_ns
                or event.graph_id != started.graph_id
            ):
                raise LedgerIntegrityError(
                    f"task_recovery_routed canonical identity does not match invocation {invocation_id}"
                )
            failed = tasks.get(event.task_id)
            if failed is None:
                raise LedgerIntegrityError(
                    f"task_recovery_routed references unknown task {event.task_id} "
                    f"in invocation {invocation_id}"
                )
            if event.task_id in recoveries:
                raise LedgerIntegrityError(
                    f"duplicate task recovery route for task {event.task_id} in invocation {invocation_id}"
                )
            if (
                failed.status != "failed"
                or failed.node_id != event.node_id
                or failed.generation_ordinal != event.generation_ordinal
            ):
                raise LedgerIntegrityError(
                    f"task_recovery_routed node/generation does not match failed task "
                    f"{event.task_id} in invocation {invocation_id}"
                )
            if failed.error_kind != event.error_kind or failed.error != event.message:
                raise LedgerIntegrityError(
                    f"task_recovery_routed error context does not match failed task "
                    f"{event.task_id} in invocation {invocation_id}"
                )
            recoveries[event.task_id] = RecoveryProjection(
                task_id=event.task_id,
                node_id=event.node_id,
                generation_ordinal=event.generation_ordinal,
                error_kind=event.error_kind,
                message=event.message,
                via=event.via,
                continue_to=event.continue_to,
            )
        elif isinstance(event, TaskAttemptAbandonedEvent):
            prev = _require_task(tasks, event)
            tasks[event.task_id] = prev.model_copy(
                update={"status": "abandoned", "latest_attempt_id": event.attempt_id}
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, BudgetConsumedEvent):
            key = (event.budget_id, event.consumption_id)
            if key in seen_consumptions:
                raise LedgerIntegrityError(
                    "duplicate budget_consumed event for "
                    f"(invocation {invocation_id}, budget {event.budget_id}, "
                    f"consumption {event.consumption_id})"
                )
            seen_consumptions.add(key)
            budgets[event.budget_id] = budgets.get(event.budget_id, 0) + 1
        elif isinstance(event, GraphInterruptedEvent):
            interrupts[event.interrupt_id] = InterruptProjection(
                interrupt_id=event.interrupt_id,
                checkpoint_ns=event.checkpoint_ns,
                node_id=event.node_id,
                checkpoint=event.checkpoint,
                actions=tuple(event.actions),
                audited_reads_sha256=dict(event.audited_reads_sha256),
                artifact_view=event.artifact_view,
                revision_owner_invocation_id=event.revision_owner_invocation_id,
                revision_base_tree_id=event.revision_base_tree_id,
                revision_view=event.revision_view,
                revision_paths=(tuple(event.revision_paths) if event.revision_paths is not None else None),
                revision_before_sha256=(
                    dict(event.revision_before_sha256) if event.revision_before_sha256 is not None else None
                ),
                source_gate_attempt_id=event.source_gate_attempt_id,
                source_gate_tree_id=event.source_gate_tree_id,
            )
            # 嵌套 child 上抛的 interrupt：父 task 不能算成功完成，否则 resume
            # 不会重进 SubgraphHandler。同 namespace 的 builtin:interrupt 节点保持
            # succeeded，以便 resolved 后按 resume.action 路由。
            if started is not None and event.checkpoint_ns != started.checkpoint_ns:
                for task_id, task in list(tasks.items()):
                    if task.node_id == event.node_id and task.status == "succeeded":
                        tasks[task_id] = task.model_copy(update={"status": "interrupted"})
            # §5.5：interrupt 落在本图某代时，驱动 NodeGeneration.status=interrupted。
            if started is not None and event.checkpoint_ns == started.checkpoint_ns:
                generation.apply_graph_interrupted(event.node_id)
        elif isinstance(event, ManualPlanRevisionEvent):
            _fold_manual_plan_revision(
                event=event,
                started=started,
                invocation_id=invocation_id,
                current_tree_id=current_tree_id,
                interrupts=interrupts,
                unconsumed_revision_transitions=unconsumed_revision_transitions,
                consumed_revision_transitions=consumed_revision_transitions,
            )
            current_tree_id = event.target_tree_id
        elif isinstance(event, GraphResumedEvent):
            pending = interrupts.get(event.interrupt_id)
            if pending is None:
                raise LedgerIntegrityError(
                    f"graph_resumed references unknown interrupt {event.interrupt_id} "
                    f"in invocation {invocation_id}"
                )
            _fold_graph_resumed_revision_and_source(
                event=event,
                pending=pending,
                started=started,
                invocation_id=invocation_id,
                unconsumed_revision_transitions=unconsumed_revision_transitions,
                consumed_revision_transitions=consumed_revision_transitions,
            )
            interrupts[event.interrupt_id] = pending.model_copy(update={"resolved_action": event.action})
        elif isinstance(event, SuperstepCommittedEvent):
            # sibling state 直到 Update（commit）才可见：state_values 只在这里推进。
            generation.apply_outputs_commit(list(event.committed_task_ids), tasks)
            # Recovery may close an earlier mixed-result wave and commit its
            # successful siblings together with the recovery task.  Mark the
            # IDs declared by the commit event, not merely the tasks planned
            # in the latest wave, or their old write-sets will be merged again
            # and can regress the current tree pointer.
            for task_id in set(current_superstep_task_ids) | set(event.committed_task_ids):
                task = tasks.get(task_id)
                if task is not None and task.status == "succeeded":
                    tasks[task_id] = task.model_copy(update={"outputs_committed": True})
            current_superstep_task_ids = []
            latest_checkpoint_id = event.checkpoint_id
            current_tree_id = event.target_tree_id
            state_values = dict(event.state_values)
        elif isinstance(event, DurableEffectAcknowledgedEvent):
            prior_ack = seen_effect_acks.get(event.effect_id)
            if prior_ack is not None:
                if prior_ack.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise LedgerIntegrityError(
                        f"conflicting durable_effect_acknowledged payload for {event.effect_id}"
                    )
                continue
            task = tasks.get(event.task_id)
            if task is None or task.status != "succeeded":
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged without succeeded task {event.task_id}"
                )
            if not task.outputs_committed:
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged before superstep commit for {event.effect_id}"
                )
            intent = next(
                (
                    item
                    for item in task.durable_effects
                    if isinstance(item, dict) and item.get("effect_id") == event.effect_id
                ),
                None,
            )
            if intent is None:
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged without matching inline intent {event.effect_id}"
                )
            if event.attempt_id != task.latest_attempt_id:
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged attempt mismatch for {event.effect_id}"
                )
            if event.kind != intent.get("kind"):
                raise LedgerIntegrityError(f"durable_effect_acknowledged kind mismatch for {event.effect_id}")
            if event.reconciler_semantics_digest != intent.get("reconciler_semantics_digest"):
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged reconciler_semantics_digest mismatch for {event.effect_id}"
                )
            if event.payload_sha256 != intent.get("payload_sha256"):
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged payload_sha256 mismatch for {event.effect_id}"
                )
            seen_effect_acks[event.effect_id] = event
            tasks[event.task_id] = task.model_copy(
                update={
                    "acknowledged_effect_ids": tuple(sorted({*task.acknowledged_effect_ids, event.effect_id}))
                }
            )
        elif isinstance(event, DurableEffectIntegrityFailedEvent):
            terminal = "failed"
            terminal_reason = "durable_effect_integrity_failed"
        elif isinstance(event, TopologySafetyCompatibilityRecordedEvent):
            prior = seen_topology_receipts.get(event.receipt_id)
            if prior is not None:
                if prior.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise LedgerIntegrityError(
                        f"conflicting topology_safety_compatibility_recorded payload for {event.receipt_id}"
                    )
                continue
            if (
                topology_compatibility_receipt_id is not None
                and topology_compatibility_receipt_id != event.receipt_id
            ):
                raise LedgerIntegrityError(
                    f"multiple topology compatibility receipts for invocation {invocation_id}"
                )
            if started is not None and event.invocation_id != started.invocation_id:
                raise LedgerIntegrityError(
                    f"topology compatibility receipt root mismatch for {event.receipt_id}"
                )
            seen_topology_receipts[event.receipt_id] = event
            topology_compatibility_receipt_id = event.receipt_id
        elif isinstance(event, GraphInvocationSupersededEvent):
            prior = seen_supersedes.get(event.supersede_id)
            if prior is not None:
                if prior.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise LedgerIntegrityError(
                        f"conflicting graph_invocation_superseded payload for {event.supersede_id}"
                    )
                continue
            if supersede_id is not None and supersede_id != event.supersede_id:
                raise LedgerIntegrityError(f"multiple supersede events for invocation {invocation_id}")
            seen_supersedes[event.supersede_id] = event
            supersede_id = event.supersede_id
            # Status renderers may say stopped; typed audit remains on supersede_id.
            terminal = "stopped"
            terminal_reason = "superseded"
        elif isinstance(event, GraphTerminalEvent):
            terminal = _TERMINAL_BY_TYPE[event.type]
            terminal_reason = event.reason
        elif isinstance(event, TaskImportedEvent):
            task_id = _imported_task_id(event, fan_outs)
            if task_id in tasks:
                raise LedgerIntegrityError(
                    f"task_imported duplicates existing task {task_id} in invocation {invocation_id}"
                )
            tasks[task_id] = TaskProjection(
                task_id=task_id,
                node_id=event.node_id,
                status="succeeded",
                attempts_used=0,  # 导入不伪造物理 attempt
                outputs_sha256=dict(event.outputs_sha256),
                gate_report=event.gate_report,
            )
            generation.apply_imported_task(
                graph_id=event.graph_id,
                node_id=event.node_id,
                task_id=task_id,
            )
        elif isinstance(event, CheckpointImportedEvent):
            pass  # fixture 导入记录不改动任务/预算投影

    if started is None:
        raise LedgerIntegrityError(f"no graph_invocation_started event for invocation {invocation_id}")
    generation.finalize_legacy_fan_out_generations(fan_outs, tasks)
    ir_digest = started.ir_digest or started.graph_digest
    return GraphProjection(
        invocation_id=started.invocation_id,
        entrypoint=started.entrypoint,
        checkpoint_ns=started.checkpoint_ns,
        parent_invocation_id=started.parent_invocation_id,
        parent_task_id=started.parent_task_id,
        structural_path=started.structural_path,
        graph_digest=started.graph_digest,
        event_schema_version=started.event_schema_version,
        ir_digest=ir_digest,
        ingest_catalog_digest=started.ingest_catalog_digest,
        contract_digests=dict(started.contract_digests),
        policy_digest=started.policy_digest,
        policy_origin=started.policy_origin,
        gate_semantics_digest=started.gate_semantics_digest,
        assurance_profile_digest=started.assurance_profile_digest,
        gate_semantics_object_id=started.gate_semantics_object_id,
        topology_safety_semantics_object_id=started.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=started.topology_safety_semantics_digest,
        commit_safety_semantics_object_id=started.commit_safety_semantics_object_id,
        commit_safety_semantics_digest=started.commit_safety_semantics_digest,
        capability_catalog_digest=started.capability_catalog_digest,
        params=dict(started.params),
        root_tree_id=started.root_tree_id,
        current_tree_id=current_tree_id,
        latest_checkpoint_id=latest_checkpoint_id,
        event_seq=event_seq,
        supersteps=supersteps,
        state_values=state_values,
        tasks=tasks,
        budgets=budgets,
        fan_out_expansions=fan_outs,
        node_histories=generation.node_histories,
        interrupts=interrupts,
        recoveries=recoveries,
        terminal=terminal,
        terminal_reason=terminal_reason,
        topology_compatibility_receipt_id=topology_compatibility_receipt_id,
        supersede_id=supersede_id,
    )


def project_invocation(change_dir: Path, invocation_id: str) -> GraphProjection:
    """从 strict ledger 重建指定 invocation 的投影（ledger 是唯一权威）。"""
    projection = fold_invocation_events(invocation_id, read_events_strict(change_dir))
    _verify_candidate_receipts_in_store(change_dir, projection)
    return projection


def _verify_candidate_receipts_in_store(change_dir: Path, projection: GraphProjection) -> None:
    """Fail closed when a named validator's receipt is missing or unbound in CAS."""
    from assurance_agent.workflow.graph.precommit import (
        CandidateValidationError,
        bind_receipt_to_success_event,
        load_candidate_receipt,
    )
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError

    required = [
        task
        for task in projection.tasks.values()
        if task.status == "succeeded" and task.precommit_validator is not None
    ]
    if not required:
        return
    store = TreeStore(change_dir)
    for task in required:
        validator_id = task.precommit_validator
        if validator_id is None:
            continue
        receipt_id = task.candidate_validation_receipt_id
        if receipt_id is None:
            raise LedgerIntegrityError(
                f"committed task {task.task_id} missing candidate_validation_receipt_id "
                f"for validator {validator_id}"
            )
        try:
            receipt = load_candidate_receipt(store, receipt_id)
        except (CandidateValidationError, WorkspaceError) as exc:
            raise LedgerIntegrityError(
                f"candidate receipt CAS verify failed for {task.task_id}: {exc}"
            ) from exc
        try:
            bind_receipt_to_success_event(
                receipt,
                validator_id=validator_id,
                invocation_id=projection.invocation_id,
                task_id=task.task_id,
                attempt_id=task.latest_attempt_id or "",
                input_snapshot_id=task.input_snapshot_id,
                write_set_id=task.write_set_id,
            )
        except CandidateValidationError as exc:
            raise LedgerIntegrityError(
                f"candidate receipt identity mismatch for {task.task_id}: {exc}"
            ) from exc


def project_workflow_state(projection: GraphProjection) -> WorkflowStateProjection:
    """派生 ``workflow-state.yaml`` 兼容视图；每个字段仅来自 ``GraphProjection``。"""
    nodes: dict[str, list[str]] = {}
    for task_id in sorted(projection.tasks):
        nodes.setdefault(projection.tasks[task_id].node_id, []).append(task_id)
    return WorkflowStateProjection(
        invocation_id=projection.invocation_id,
        entrypoint=projection.entrypoint,
        terminal=projection.terminal,
        terminal_reason=projection.terminal_reason,
        latest_checkpoint_id=projection.latest_checkpoint_id,
        event_seq=projection.event_seq,
        nodes={node_id: tuple(task_ids) for node_id, task_ids in sorted(nodes.items())},
        tasks={task_id: projection.tasks[task_id] for task_id in sorted(projection.tasks)},
        pending_interrupts=tuple(
            interrupt
            for _, interrupt in sorted(projection.interrupts.items())
            if interrupt.resolved_action is None
        ),
        budgets=dict(sorted(projection.budgets.items())),
    )


def derive_graph_state(
    invocation_id: str,
    events: list[dict[str, object]],
) -> WorkflowStateProjection:
    """Graph-visible state is a pure projection of ledger events."""
    return project_workflow_state(fold_invocation_events(invocation_id, events))


def render_workflow_state_yaml(projection: GraphProjection) -> bytes:
    """Render the compatibility view as ``workflow-state.json`` bytes for staging."""
    view = project_workflow_state(projection)
    text = json.dumps(view.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def latest_root_invocation_id(events: list[dict[str, object]], entrypoint: str | None = None) -> str | None:
    """严格 ledger 中最近一次无 parent 的 root ``graph_invocation_started``。

    ``entrypoint`` 非空时只看该 entrypoint 的 invocation：同一 change 上
    standalone entrypoint（``archive``/``retro``）与主 ``full`` 图各自独立成
    invocation，不该互相当成「已在跑/已完成」。
    """
    latest: str | None = None
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
            continue
        if raw.get("parent_invocation_id") is not None:
            continue
        if entrypoint is not None and raw.get("entrypoint") != entrypoint:
            continue
        invocation_id = raw.get("invocation_id")
        if isinstance(invocation_id, str):
            latest = invocation_id
    return latest
