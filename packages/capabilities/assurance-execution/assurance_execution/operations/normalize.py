"""Normalize confined runner receipts into ExecutionEvidenceV1."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import (
    OutputError,
)
from graph_engine.canonical import JSONValue

from assurance_execution.contracts.evidence import ExecutionEvidenceV1, FamilyExecutionOutcomeV1
from assurance_execution.contracts.execution import (
    EXECUTION_FAMILIES,
    ExecutionCommandReceiptV1,
    ExecutionFamily,
    ExecutionReceiptV1,
)
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_execution.operations.common import json_digest, mapping_digest
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
    plan_ref_dump = getattr(plan_ref, "model_dump", None)
    plan_ref_data = plan_ref_dump(mode="json") if callable(plan_ref_dump) else plan_ref
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
    # Normalization observes a real run, so every family that produced a
    # command receipt executed. The evidence states this; it is not inferred
    # again when the sealed document is read back.
    outcomes = tuple(
        FamilyExecutionOutcomeV1(family=item.family, state="executed") for item in built.commands
    )
    try:
        return ExecutionEvidenceV1.model_validate(
            {
                "family_outcomes": [item.model_dump(mode="json") for item in outcomes],
                "change_id": change_id,
                "plan_digest": plan_digest,
                "plan_ref": plan_ref_data,
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
