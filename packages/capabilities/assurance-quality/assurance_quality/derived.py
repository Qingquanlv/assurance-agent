"""Fill flow inputs from the typed documents already on the payload.

A caller that already passed a key keeps that value. A document is validated
only when a fill needs it. Invalid data fails. A payload with none of the
source documents is left unchanged so a thin entry still omits those fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import AssessmentInputsV1, InspectionOutcomeV1

_ISSUE_ANALYSIS_PATH = "qa/results/inspect/issue-analysis.json"
_FACT_BASELINE_PATH = "qa/results/facts/fact-baseline.json"


def shared_quality_digests(
    inspection: InspectionOutcomeV1,
    assessment: AssessmentInputsV1,
) -> dict[str, Any]:
    """Digests shared by issue analysis and report. Plan stays with the caller."""
    return {
        "execution_evidence_digest": assessment.execution_ref.digest,
        "healing_digest": None if assessment.healing_ref is None else assessment.healing_ref.digest,
        "trace_digest": assessment.trace_ref.digest,
        "coverage_digest": assessment.gaps_ref.digest,
        "metrics_digest": assessment.metrics_ref.digest,
        "case_digest": inspection.reviewed_case.review_ref.digest,
        "mapping_digest": inspection.mapping_ref.digest,
        "issue_digest": None if assessment.issue_ref is None else assessment.issue_ref.digest,
    }


def derive_issue_analysis_input(value: Mapping[str, Any]) -> dict[str, Any]:
    filled = dict(value)
    if not _any_present(value, "inspection_outcome", "assessment_inputs", "generation_result"):
        return filled
    if not _missing(
        value,
        "change_id",
        "batch_id",
        "artifact_paths",
        "owned_evidence_ids",
        "evidence_bundle_digest",
        "execution_evidence_digest",
        "healing_digest",
        "trace_digest",
        "coverage_digest",
        "metrics_digest",
        "case_digest",
        "plan_digest",
        "plan_ref",
        "mapping_digest",
        "issue_digest",
        "inspection_disposition",
    ):
        return filled
    inspection = _as(InspectionOutcomeV1, value.get("inspection_outcome"), "inspection_outcome")
    assessment = _as(AssessmentInputsV1, value.get("assessment_inputs"), "assessment_inputs")
    generation = _as(GenerationCycleResultV1, value.get("generation_result"), "generation_result")
    digests = shared_quality_digests(inspection, assessment)
    digests["plan_digest"] = inspection.plan_digest
    digests["plan_ref"] = inspection.plan_ref
    digests["change_id"] = inspection.change_id
    digests["batch_id"] = inspection.batch_id
    digests["owned_evidence_ids"] = assessment.owned_evidence_ids
    digests["evidence_bundle_digest"] = assessment.evidence_bundle_digest
    digests["inspection_disposition"] = inspection.disposition
    digests["artifact_paths"] = _issue_artifact_paths(inspection, assessment, generation)
    for key, item in digests.items():
        _fill(filled, value, key, item)
    return filled


def bound_issue_analysis_ref(evidence_refs: object) -> dict[str, str]:
    """The one current issue-analysis file, matching ``bind_issue_analysis``."""
    if not isinstance(evidence_refs, (list, tuple)):
        raise ValueError("issue analysis evidence refs must be a list")
    matches = [
        ref for ref in evidence_refs if isinstance(ref, Mapping) and ref.get("path") == _ISSUE_ANALYSIS_PATH
    ]
    if len(matches) != 1:
        raise ValueError("issue analysis must publish exactly one current-change result")
    path = matches[0].get("path")
    digest = matches[0].get("digest")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise ValueError("issue analysis evidence refs must be a list")
    return {"path": path, "digest": digest}


def derive_reconcile_input(value: Mapping[str, Any]) -> dict[str, Any]:
    filled = dict(value)
    if "issue_analysis_ref" not in value and "evidence_refs" in value:
        filled["issue_analysis_ref"] = bound_issue_analysis_ref(value.get("evidence_refs"))
    return derive_issue_analysis_input(filled)


def derive_report_input(value: Mapping[str, Any]) -> dict[str, Any]:
    filled = dict(value)
    inspection_raw = _merged_inspection(value, filled)
    if not _missing(
        value,
        "execution_evidence_digest",
        "healing_digest",
        "trace_digest",
        "coverage_digest",
        "metrics_digest",
        "case_digest",
        "plan_digest",
        "plan_ref",
        "mapping_digest",
        "issue_digest",
        "batch_id",
        "coverage_epoch",
        "change_id",
        "artifact_paths",
    ):
        return filled
    if not _any_present(value, "inspection_outcome", "assessment_inputs", "generation_result"):
        return filled
    inspection = _as(InspectionOutcomeV1, inspection_raw, "inspection_outcome")
    assessment = _as(AssessmentInputsV1, value.get("assessment_inputs"), "assessment_inputs")
    generation = _as(GenerationCycleResultV1, value.get("generation_result"), "generation_result")
    digests = shared_quality_digests(inspection, assessment)
    digests["plan_digest"] = generation.plan_digest
    digests["plan_ref"] = generation.plan_ref
    digests["batch_id"] = inspection.batch_id
    digests["coverage_epoch"] = inspection.coverage_epoch
    digests["change_id"] = inspection.change_id
    analysis = value.get("issue_analysis_ref")
    if analysis is not None:
        ref = _as(EvidenceArtifactRefV1, analysis, "issue_analysis_ref")
        digests["issue_digest"] = ref.digest
    for key, item in digests.items():
        _fill(filled, value, key, item)
    if "artifact_paths" not in value and isinstance(value.get("allowed_artifact_paths"), (list, tuple)):
        filled["artifact_paths"] = list(value["allowed_artifact_paths"])
    return filled


def _merged_inspection(value: Mapping[str, Any], filled: dict[str, Any]) -> object:
    raw = value.get("inspection_outcome")
    data = _dump(raw) if raw is not None else None
    if not isinstance(data, dict):
        return raw
    if "inspection_receipt" in data or value.get("inspection_receipt") is None:
        return raw
    merged = {**data, "inspection_receipt": _dump(value.get("inspection_receipt"))}
    filled["inspection_outcome"] = merged
    return merged


def _issue_artifact_paths(
    inspection: InspectionOutcomeV1,
    assessment: AssessmentInputsV1,
    generation: GenerationCycleResultV1,
) -> list[str]:
    reviewed = inspection.reviewed_case
    refs = (
        *inspection.assessment_refs,
        assessment.observations_ref,
        assessment.issue_evidence_manifest_ref,
        inspection.mapping_ref,
        *reviewed.case_refs,
        *reviewed.preparation_refs,
        reviewed.review_ref,
        *generation.source_refs,
        *generation.plan_refs,
    )
    return sorted({ref.path for ref in refs if ref.path != _FACT_BASELINE_PATH})


def _as(model: type[BaseModel], value: object, name: str) -> Any:
    if isinstance(value, model):
        return value
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise ValueError(f"{name} is not a {model.__name__}") from error


def _dump(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return value


def _missing(value: Mapping[str, Any], *keys: str) -> bool:
    return any(key not in value for key in keys)


def _any_present(value: Mapping[str, Any], *keys: str) -> bool:
    return any(key in value and value[key] is not None for key in keys)


def _fill(filled: dict[str, Any], original: Mapping[str, Any], key: str, item: object) -> None:
    if key not in original and item is not None:
        filled[key] = item


__all__ = [
    "bound_issue_analysis_ref",
    "derive_issue_analysis_input",
    "derive_reconcile_input",
    "derive_report_input",
    "shared_quality_digests",
]
