from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field

from projection import (
    BehavioralProjectionV1,
    JSONValue,
    ProjectionError,
    ProjectionModel,
    jsonable,
    project_legacy_export,
    project_new_export,
)

CASE_IDS = (
    "full-api-only-success",
    "full-e2e-only-success",
    "full-fuzz-only-success",
    "full-performance-only-success",
    "full-all-four-family-success",
    "intake-review-needs-fix-then-pass",
    "plan-review-invalid-output-bounded-retry",
    "codegen-validation-failure-bounded-fix",
    "execution-closed-mapping-no-stale-test",
    "coverage-insufficient-repair-reexecution-pass",
    "coverage-repair-no-progress-exhausted",
    "healing-disallowed-business-stop",
    "report-generation-required-outputs",
    "issue-analysis-reconcile-path",
    "archive-durable-effect-replay",
    "retro-collect-analyze-propose-reconcile",
    "improvement-review-evaluate-export-apply",
    "improvement-rollback",
    "human-interrupt-exact-resume",
    "transient-local-retry",
    "opencode-ambiguous-create-recovery",
    "cursor-unknown-process-indeterminate",
    "engine-crash-after-provider-terminal-receipt",
    "config-model-graph-source-drift-rejection",
    "replay-after-provider-state-removal",
)

COMPARED_FIELDS = (
    "input_digest",
    "runtime_identity",
    "terminal_class",
    "terminal_reason_category",
    "selected_families",
    "activated_families",
    "completed_families",
    "skipped_families",
    "gate_decisions",
    "artifact_contract",
    "changed_files",
    "execution_evidence",
    "quality_metrics",
    "issue_healing_decisions",
    "durable_effects",
    "report",
    "retro",
    "improvement",
    "archive",
    "semantic_counts",
    "diagnostics",
)

SET_FIELDS = frozenset(
    {
        "selected_families",
        "activated_families",
        "completed_families",
        "skipped_families",
    }
)


class DispositionV1(ProjectionModel):
    case_id: str
    field: str
    mode: Literal["exact", "set", "predicate", "intentionally-different"]
    classification: Literal["required", "legacy-bug", "unspecified", "observational-noise"]
    predicate: str | None
    governing_contract: str = Field(min_length=1)


class ComparisonResultV1(ProjectionModel):
    case_id: str
    passed: bool
    governed_differences: tuple[object, ...]
    undisposed_differences: tuple[object, ...]


def compare_case(
    case_id: str,
    legacy_export: Path,
    new_export: Path,
    dispositions: Mapping[str, DispositionV1],
) -> ComparisonResultV1:
    if case_id not in CASE_IDS:
        raise ProjectionError("case_id must be one of the Task 1 comparison cases")
    legacy = project_legacy_export(legacy_export)
    current = project_new_export(new_export)
    if legacy.case_id != case_id or current.case_id != case_id:
        raise ProjectionError("export case_id must match the compared case")
    return apply_governed_dispositions(case_id, diff_projection(legacy, current), dispositions)


def diff_projection(
    legacy: BehavioralProjectionV1,
    current: BehavioralProjectionV1,
) -> tuple[JSONValue, ...]:
    differences: list[JSONValue] = []
    for field in COMPARED_FIELDS:
        left = jsonable(getattr(legacy, field))
        right = jsonable(getattr(current, field))
        if left != right:
            differences.append(
                {
                    "field": field,
                    "legacy": left,
                    "current": right,
                    "mode": "set" if field in SET_FIELDS else "exact",
                }
            )
    return tuple(differences)


def apply_governed_dispositions(
    case_id: str,
    differences: tuple[JSONValue, ...],
    dispositions: Mapping[str, DispositionV1],
) -> ComparisonResultV1:
    governed: list[JSONValue] = []
    undisposed: list[JSONValue] = []
    seen_fields: set[str] = set()
    indexed = {
        field: item
        for item in differences
        if isinstance(item, Mapping) and isinstance((field := item.get("field")), str)
    }
    for field, item in indexed.items():
        seen_fields.add(field)
        disposition = _matching_disposition(case_id, field, dispositions)
        if disposition is None:
            undisposed.append(item)
            continue
        if disposition.mode == "intentionally-different":
            governed.append(_annotate(item, disposition))
            continue
        if disposition.mode == "predicate":
            if _predicate_holds(disposition.predicate, item.get("legacy"), item.get("current")):
                governed.append(_annotate(item, disposition))
            else:
                undisposed.append(item)
            continue
        governed.append(_annotate(item, disposition))
    for field, disposition in dispositions.items():
        if disposition.mode != "intentionally-different":
            continue
        if not _disposition_matches(case_id, field, disposition):
            continue
        if field in seen_fields:
            continue
        undisposed.append(
            {
                "field": field,
                "legacy": None,
                "current": None,
                "mode": "intentionally-different",
                "reason": "values match but the disposition requires them to differ",
            }
        )
    return ComparisonResultV1(
        case_id=case_id,
        passed=not undisposed,
        governed_differences=tuple(governed),
        undisposed_differences=tuple(undisposed),
    )


def _matching_disposition(
    case_id: str,
    field: str,
    dispositions: Mapping[str, DispositionV1],
) -> DispositionV1 | None:
    candidate = dispositions.get(field)
    if candidate is None:
        return None
    if not _disposition_matches(case_id, field, candidate):
        return None
    return candidate


def _disposition_matches(case_id: str, field: str, disposition: DispositionV1) -> bool:
    return disposition.case_id == case_id and disposition.field == field


def _annotate(item: Mapping[str, JSONValue], disposition: DispositionV1) -> JSONValue:
    annotated = dict(item)
    annotated["mode"] = disposition.mode
    annotated["classification"] = disposition.classification
    annotated["governing_contract"] = disposition.governing_contract
    if disposition.predicate is not None:
        annotated["predicate"] = disposition.predicate
    return annotated


def _predicate_holds(name: str | None, legacy: JSONValue, current: JSONValue) -> bool:
    if name == "report-required-sections":
        return _has_report_sections(legacy) and _has_report_sections(current)
    if name == "equal":
        return legacy == current
    return False


def _has_report_sections(value: JSONValue) -> bool:
    if not isinstance(value, Mapping):
        return False
    fields = value.get("semantic_fields")
    return value.get("present") is True and isinstance(fields, Mapping) and "decision" in fields
