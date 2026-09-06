"""Normalize confined runner receipts into ExecutionEvidenceV1."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.agent import NormalizeInputV1
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.execution import (
    EXECUTION_FAMILIES,
    ExecutionCommandReceiptV1,
    ExecutionFamily,
    ExecutionReceiptV1,
    RawTestResultV1,
)
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_execution.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    json_digest,
    leafs_of,
    mapping_digest,
    validate_input,
)
from assurance_execution.operations.pytest_parser import parse_pytest_report, receipt_counts
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def _summary_count(summary: Mapping[str, Any], name: str, fallback: int) -> int:
    value = summary.get(name, fallback)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OutputError(f"pytest report summary {name} must be a non-negative integer")
    return value


def normalize_evidence(
    *,
    change_id: str,
    plan_digest: str,
    plan_ref: EvidenceArtifactRefV1,
    batch_id: str,
    selected_targets: object,
    mapping: ClosedMappingV1,
    capability_leafs: frozenset[str],
    case_ids: frozenset[str],
    baseline_tree_id: str,
    runner_profile_digest: str,
    command: tuple[str, ...],
    exit_code: int,
    report: Mapping[str, Any],
    receipt: ExecutionReceiptV1 | None = None,
) -> ExecutionEvidenceV1:
    try:
        targets = SelectedTargets.model_validate(selected_targets)
    except ValidationError as error:
        raise OutputError(str(error)) from error
    results = parse_pytest_report(report, mapping)
    summary = report.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    counts = receipt_counts(
        results,
        exit_code=exit_code,
        collected=_summary_count(summary, "collected", len(results)),
        passed=_summary_count(
            summary,
            "passed",
            sum(1 for item in results if item.status == "passed"),
        ),
        failed=_summary_count(
            summary,
            "failed",
            sum(1 for item in results if item.status == "failed"),
        ),
        skipped=_summary_count(
            summary,
            "skipped",
            sum(1 for item in results if item.status == "skipped"),
        ),
    )
    selected_families: tuple[ExecutionFamily, ...] = tuple(
        family for family in EXECUTION_FAMILIES if getattr(targets, family)
    )
    if len(selected_families) != 1 and receipt is None:
        raise OutputError("one-command normalization requires exactly one selected family")
    built = receipt or ExecutionReceiptV1(
        commands=(
            ExecutionCommandReceiptV1(
                family=selected_families[0],
                command=command,
                exit_code=counts["exit_code"],
                collected=counts["collected"],
                passed=counts["passed"],
                failed=counts["failed"],
                skipped=counts["skipped"],
            ),
        )
    )
    try:
        return ExecutionEvidenceV1.model_validate(
            {
                "change_id": change_id,
                "plan_digest": plan_digest,
                "plan_ref": plan_ref,
                "batch_id": batch_id,
                "status": "failed"
                if built.exit_code != 0 or any(item.status == "failed" for item in results)
                else "passed",
                "selected_targets": targets.model_dump(mode="json"),
                "mapping": mapping.model_dump(mode="json"),
                "mapping_digest": mapping_digest(mapping),
                "baseline_tree_id": baseline_tree_id,
                "runner_profile_digest": runner_profile_digest,
                "receipt_digest": json_digest(cast(JSONValue, built.model_dump(mode="json"))),
                "receipt": built.model_dump(mode="json"),
                "results": [item.model_dump(mode="json") for item in results],
            },
            context={"capability_leafs": capability_leafs, "case_ids": case_ids},
        )
    except ValidationError as error:
        raise OutputError(str(error)) from error


def raw_results(evidence: ExecutionEvidenceV1) -> tuple[RawTestResultV1, ...]:
    return evidence.results


class NormalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(NormalizeInputV1, request.input)
            leafs = leafs_of(payload.capability_leafs)
            case_ids = leafs_of(payload.case_ids)
            try:
                mapping = ClosedMappingV1.model_validate(
                    payload.mapping.model_dump(mode="json"),
                    context={"capability_leafs": leafs, "case_ids": case_ids},
                )
            except ValidationError as error:
                raise InputError(str(error)) from error
            evidence = normalize_evidence(
                change_id=payload.change_id,
                plan_digest=payload.plan_digest,
                plan_ref=payload.plan_ref,
                batch_id=payload.batch_id,
                selected_targets=payload.selected_targets,
                mapping=mapping,
                capability_leafs=leafs,
                case_ids=case_ids,
                baseline_tree_id=payload.baseline_tree_id,
                runner_profile_digest=payload.runner_profile_digest,
                command=payload.command,
                exit_code=payload.exit_code,
                report=payload.report,
                receipt=payload.receipt,
            )
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except ValidationError as error:
            return failed_input(error)
