"""Healing and coverage-repair status plus episode projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import CoverageRepairAllocateInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairBaseline, CoverageRepairStatus
from assurance_healing.contracts.status import HealingStatusV1
from assurance_healing.operations.common import InputError, failed_input, validate_input
from assurance_healing.operations.keys import mint_coverage_attempt_token

_HEAL_STATUSES = {
    "pending",
    "allocated",
    "proposed",
    "approved",
    "applied",
    "needs_review",
    "resolved",
    "not_needed",
    "skipped",
    "exhausted",
    "failed",
}
_REPAIR_STATUSES = {"repaired", "exhausted", "not_eligible", "failed", "in_progress"}


class RecordHealingStatusHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            raw = request.input
            if not isinstance(raw, dict):
                raise InputError("record-healing-status requires a status document")
            status = raw.get("status")
            if not isinstance(status, str) or status not in _HEAL_STATUSES:
                raise InputError(
                    f"record-healing-status requires status in: {', '.join(sorted(_HEAL_STATUSES))}"
                )
            document = HealingStatusV1.model_validate(
                {
                    "schema_version": "1",
                    "change_id": raw.get("change_id"),
                    "status": status,
                    "attempts_used": raw.get("attempts_used", 0),
                    "episode_id": raw.get("episode_id"),
                    "last_operation_id": raw.get("last_operation_id"),
                    "last_record_key": raw.get("last_record_key"),
                    "candidate_digest": raw.get("candidate_digest"),
                    "baseline_digest": raw.get("baseline_digest"),
                    "policy_digest": raw.get("policy_digest"),
                    "execution_evidence_digest": raw.get("execution_evidence_digest"),
                }
            )
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except Exception as error:
            return failed_input(InputError(str(error)))


class AllocateCoverageRepairAttemptHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(CoverageRepairAllocateInputV1, request.input)
            if not payload.brief.eligible:
                raise InputError("cannot allocate coverage-repair attempt for an ineligible brief")
            attempts_used = payload.prior_attempts_used + 1
            token = mint_coverage_attempt_token(
                change_id=payload.change_id,
                attempt=attempts_used,
                test_tree_sha256=payload.test_tree_sha256,
                product_tree_sha256=payload.product_tree_sha256,
                declaration_tree_sha256=payload.declaration_tree_sha256,
            )
            baseline = CoverageRepairBaseline(
                change_id=payload.change_id,
                attempt=attempts_used,
                attempt_token=token,
                test_tree_sha256=payload.test_tree_sha256,
                test_files_sha256=payload.test_files_sha256,
                product_tree_sha256=payload.product_tree_sha256,
                product_files_sha256=payload.product_files_sha256,
                declaration_tree_sha256=payload.declaration_tree_sha256,
                declaration_files_sha256=payload.declaration_files_sha256,
            )
            status = CoverageRepairStatus(
                change_id=payload.change_id,
                status="in_progress",
                attempts_used=attempts_used,
                last_batch_id=payload.brief.batch_id,
                deferred_to_intake=payload.brief.deferred_to_intake,
            )
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {
                        "attempts_used": attempts_used,
                        "last_batch_id": payload.brief.batch_id,
                        "attempt_token": token,
                        "baseline": baseline.model_dump(mode="json"),
                        "status": status.model_dump(mode="json"),
                    },
                )
            )
        except InputError as error:
            return failed_input(error)


class RecordCoverageRepairStatusHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            raw = request.input
            if not isinstance(raw, dict):
                raise InputError("record-coverage-repair-status requires a status document")
            status = raw.get("status")
            if not isinstance(status, str) or status not in _REPAIR_STATUSES:
                raise InputError(
                    f"record-coverage-repair-status requires status in: {', '.join(sorted(_REPAIR_STATUSES))}"
                )
            document = CoverageRepairStatus.model_validate(
                {
                    "schema_version": "1",
                    "change_id": raw.get("change_id"),
                    "status": status,
                    "attempts_used": raw.get("attempts_used", 0),
                    "last_batch_id": raw.get("last_batch_id"),
                    "deferred_to_intake": raw.get("deferred_to_intake") or (),
                }
            )
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except Exception as error:
            return failed_input(InputError(str(error)))


class ProjectEpisodeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            raw = request.input
            if not isinstance(raw, dict):
                raise InputError("project-episode requires events")
            events = raw.get("events")
            if not isinstance(events, list):
                raise InputError("project-episode requires an events list")
            documents: list[Mapping[str, Any]] = []
            for item in events:
                if not isinstance(item, dict):
                    raise InputError("project-episode events must be objects")
                documents.append(item)
            projection = project_episode(documents)
            return TaskOutcome.succeeded(cast(JSONValue, projection))
        except Exception as error:
            return failed_input(InputError(str(error)))


def _event_seq(event: Mapping[str, Any]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else 0


def _as_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


_CURRENT_EVENT_KINDS = frozenset(
    {
        "healing_attempt_allocated_v2",
        "fixer_proposal_approved",
        "heal_record_apply_v2",
    }
)
_FORMER_APPROVAL_FIELDS = frozenset(
    {
        "proposal_sha256",
        "fixer_authority_sha256",
        "entry_baseline_sha256",
        "policy_sha256",
    }
)
_FORMER_APPLY_FIELDS = frozenset({"attempt_key", "safety_payload_sha256", "files_modified"})


def _require_current_event(event: Mapping[str, Any]) -> str:
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in _CURRENT_EVENT_KINDS:
        raise ValueError("healing event is not a current schema")
    return event_type


def project_episode(events: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    ordered = sorted(events, key=_event_seq)
    allocations: list[dict[str, object]] = []
    approvals: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    baseline: dict[str, object] | None = None
    allocation_floor = 0
    for event in ordered:
        event_type = _require_current_event(event)
        if event_type == "healing_attempt_allocated_v2":
            allocations.append(
                {
                    "operation_id": event.get("operation_id"),
                    "episode_id": event.get("episode_id"),
                    "attempt_id": event.get("attempt_id"),
                    "attempt_number": event.get("attempt_number"),
                    "source_batch_id": event.get("source_batch_id"),
                    "entry_batch_id": event.get("entry_batch_id"),
                    "baseline_sha256": event.get("baseline_sha256"),
                }
            )
            allocation_floor = max(allocation_floor, _event_seq(event))
            if event.get("baseline_embedded") and baseline is None:
                baseline = {
                    "episode_id": event.get("episode_id"),
                    "entry_batch_id": event.get("entry_batch_id"),
                    "artifact_sha256": event.get("baseline_sha256"),
                    "form": "v2",
                }
        elif event_type == "fixer_proposal_approved":
            if _FORMER_APPROVAL_FIELDS.intersection(event):
                raise ValueError("healing event is not a current schema")
            approvals.append(
                {
                    "approval_id": event.get("approval_id"),
                    "root_invocation_id": event.get("root_invocation_id"),
                    "interrupt_task_id": event.get("interrupt_task_id"),
                    "source_gate_attempt_id": event.get("source_gate_attempt_id"),
                    "source_tree_id": event.get("source_tree_id"),
                    "proposal_digest": event.get("proposal_digest"),
                    "fixer_authority_digest": event.get("fixer_authority_digest"),
                    "baseline_digest": event.get("baseline_digest"),
                    "policy_digest": event.get("policy_digest"),
                    "targets": list(_as_tuple(event.get("targets"))),
                    "paths": list(_as_tuple(event.get("paths"))),
                    "target_tree_id": event.get("target_tree_id"),
                    "source_seq": _event_seq(event),
                }
            )
        elif event_type == "heal_record_apply_v2":
            if not event.get("record_key") or _FORMER_APPLY_FIELDS.intersection(event):
                raise ValueError("healing event is not a current schema")
            if _event_seq(event) <= allocation_floor:
                continue
            claimed = _as_tuple(event.get("claimed_modified_paths"))
            records.append(
                {
                    "record_key": event.get("record_key"),
                    "target": event.get("target"),
                    "outcome": event.get("outcome"),
                    "proposal_ids": list(_as_tuple(event.get("proposal_ids"))),
                    "claimed_modified_paths": list(claimed),
                    "intent_digest": event.get("intent_digest"),
                    "write_set_id": event.get("write_set_id"),
                    "safety_payload_digest": event.get("safety_payload_digest"),
                    "files_modified": list(claimed),
                    "source_seq": _event_seq(event),
                    "form": "v2",
                }
            )
    episode_id = baseline.get("episode_id") if baseline is not None else None
    if episode_id is None and allocations:
        episode_id = allocations[-1].get("episode_id")
    return {
        "baseline": baseline,
        "allocations": allocations,
        "approvals": approvals,
        "records": records,
        "attempts_used": len(allocations),
        "episode_id": episode_id,
    }
