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


def project_episode(events: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    ordered = sorted(events, key=lambda item: int(item.get("seq") or 0))
    allocations: list[dict[str, object]] = []
    baseline: dict[str, object] | None = None
    for event in ordered:
        event_type = event.get("type")
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
            if event.get("baseline_embedded") and baseline is None:
                baseline = {
                    "episode_id": event.get("episode_id"),
                    "entry_batch_id": event.get("entry_batch_id"),
                    "artifact_sha256": event.get("baseline_sha256"),
                    "form": "v2",
                }
        elif event_type == "healing_entry_baseline_pinned" and baseline is None:
            baseline = {
                "episode_id": event.get("episode_id"),
                "entry_batch_id": event.get("entry_batch_id"),
                "artifact_sha256": event.get("artifact_sha256"),
                "form": "legacy",
            }
    episode_id = baseline.get("episode_id") if baseline is not None else None
    if episode_id is None and allocations:
        episode_id = allocations[-1].get("episode_id")
    return {
        "baseline": baseline,
        "allocations": allocations,
        "approvals": [],
        "records": [],
        "attempts_used": len(allocations),
        "episode_id": episode_id,
    }
