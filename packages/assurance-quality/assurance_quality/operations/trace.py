"""Deterministic trace projection from closed mapping + canonical evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_healing.contracts.status import HealingStatusV1
from assurance_quality.contracts.trace import (
    TraceExecution,
    TraceProjectionV2,
    TraceRow,
    TraceTestRef,
)
from assurance_quality.operations.common import (
    InputError,
    OutputError,
    catalog_context,
    failed_input,
    failed_output,
    leafs_of,
    validate_input,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_CASE_TYPE_TO_TARGET: dict[str, Literal["api", "e2e", "fuzz", "performance"]] = {
    "API": "api",
    "E2E": "e2e",
    "Fuzz": "fuzz",
    "Performance": "performance",
}


class TraceCaseInput(BaseModel):
    model_config = _FROZEN

    case_id: str
    module: str = "unknown"
    case_type: Literal["API", "E2E", "Fuzz", "Performance"] = "API"
    automation_required: bool = True
    assertions: tuple[str, ...] = ()
    capability: str | None = None
    plan_id: str | None = None


class TraceOperationInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    phase: Literal["execution", "reconciled"] = "execution"
    closed_mapping: tuple[str, ...]
    observed: tuple[str, ...] = ()
    cases: tuple[TraceCaseInput, ...] = ()
    case_covering: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...] = ()
    plan_ids: tuple[str, ...] = ()
    test_ids: tuple[str, ...] = ()
    schema_ids: tuple[str, ...] = ()
    issue_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    execution_evidence: ExecutionEvidenceV1 | None = None
    healing_status: HealingStatusV1 | None = None
    executed_at: datetime = Field(default_factory=lambda: datetime(2026, 8, 22, tzinfo=UTC))

    def closed_paths(self) -> frozenset[str]:
        if self.execution_evidence is not None:
            return frozenset(self.execution_evidence.mapping.selected)
        return frozenset(self.closed_mapping)


def _test_name(path: str) -> str:
    stem = PurePosixPath(path).stem
    if stem.startswith("test_") or stem.endswith("_test"):
        return stem if stem.startswith("test_") else f"test_{stem.removesuffix('_test')}"
    return f"test_{stem}"


def _observed_in_mapping(payload: TraceOperationInput) -> tuple[str, ...]:
    allowed = payload.closed_paths()
    if payload.execution_evidence is not None:
        return tuple(result.test for result in payload.execution_evidence.results if result.test in allowed)
    return tuple(path for path in payload.observed if path in allowed)


def _status_for(path: str, payload: TraceOperationInput) -> Literal["passed", "failed", "skipped"]:
    if payload.execution_evidence is None:
        return "passed"
    for result in payload.execution_evidence.results:
        if result.test == path:
            return result.status
    return "passed"


def _mapped_paths(
    case: TraceCaseInput,
    payload: TraceOperationInput,
    allowed: frozenset[str],
    cases: tuple[TraceCaseInput, ...],
) -> tuple[str, ...]:
    if payload.execution_evidence is not None:
        return tuple(
            entry.test
            for entry in payload.execution_evidence.mapping.mappings
            if entry.case_id == case.case_id
        )
    explicit = tuple(path for path in payload.case_covering.get(case.case_id, ()) if path in allowed)
    if explicit:
        return explicit
    if len(cases) == 1:
        return tuple(path for path in payload.closed_mapping if path in allowed)
    return ()


def project_trace(payload: TraceOperationInput) -> TraceProjectionV2:
    allowed = payload.closed_paths()
    observed = _observed_in_mapping(payload)
    observed_set = set(observed)
    cases = payload.cases or (
        TraceCaseInput(case_id=payload.case_ids[0] if payload.case_ids else "TC_UNBOUND", module="unknown"),
    )
    rows: list[TraceRow] = []
    for case in cases:
        mapped = _mapped_paths(case, payload, allowed, cases)
        covering = tuple(
            TraceTestRef(file=path, test_name=_test_name(path)) for path in mapped if path in observed_set
        )
        executed = bool(covering)
        status = _status_for(covering[0].file, payload) if covering else "skipped"
        target = _CASE_TYPE_TO_TARGET[case.case_type]
        execution = (
            TraceExecution(
                batch_id=payload.batch_id,
                target=target,
                status=status,
                ts=payload.executed_at,
                ts_source="executed_at",
            )
            if executed
            else None
        )
        coverage_state: Literal["covered", "uncovered", "not_required"]
        if not case.automation_required:
            coverage_state = "not_required"
        elif covering:
            coverage_state = "covered"
        else:
            coverage_state = "uncovered"
        rows.append(
            TraceRow(
                case_id=case.case_id,
                module=case.module,
                case_type=case.case_type,
                automation_required=case.automation_required,
                assertions=case.assertions,
                covering_tests=covering,
                coverage_state=coverage_state,
                latest_execution=execution,
                freshest_pass=execution if execution is not None and execution.status == "passed" else None,
                presence_in_current_batch="executed" if executed else "not_in_current_batch",
                capability=case.capability,
                plan_id=case.plan_id,
            )
        )
    integrity: Literal["complete", "complete_with_gaps"] = (
        "complete" if set(allowed) <= observed_set else "complete_with_gaps"
    )
    context = catalog_context(
        capability_leafs=payload.capability_leafs,
        case_ids=payload.case_ids or (row.case_id for row in rows),
        plan_ids=payload.plan_ids,
        test_ids=payload.test_ids
        or (f"{ref.file}::{ref.test_name}" for row in rows for ref in row.covering_tests),
        schema_ids=payload.schema_ids,
        issue_ids=payload.issue_ids,
        evidence_refs=payload.evidence_refs,
    )
    try:
        return TraceProjectionV2.model_validate(
            {
                "schema_version": "2",
                "change_id": payload.change_id,
                "phase": payload.phase,
                "authoritative_batch_id": payload.batch_id,
                "sources": [],
                "rows": [row.model_dump(mode="json") for row in rows],
                "unmapped_tests": [],
                "gaps": [],
                "integrity": integrity,
            },
            context=context,
        )
    except ValidationError as error:
        raise OutputError(str(error)) from error


class MaterializeTraceHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(TraceOperationInput, request.input)
            if payload.execution_evidence is not None:
                try:
                    ExecutionEvidenceV1.model_validate(
                        payload.execution_evidence.model_dump(mode="json"),
                        context={
                            "capability_leafs": leafs_of(payload.capability_leafs),
                            "case_ids": leafs_of(payload.case_ids),
                        },
                    )
                except ValidationError as error:
                    raise InputError(str(error)) from error
            projection = project_trace(payload)
            return TaskOutcome.succeeded(cast(JSONValue, projection.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except ValidationError as error:
            return failed_input(error)
