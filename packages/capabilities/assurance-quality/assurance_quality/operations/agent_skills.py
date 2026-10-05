"""Provider-neutral quality Agent finalize helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from agent_runtime_contracts.ops import InputError, OutputError
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.artifacts import ArtifactReadError, open_artifact, read_workspace_file
from graph_engine.canonical import JSONValue

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.agent import QualitySkillInputV1
from assurance_quality.contracts.assessment import (
    AssessmentSkillInputV1,
    FactBaselineSkillInputV1,
    ReportSkillInputV1,
)
from assurance_quality.contracts.issues import IssueEvidenceManifest, ObservationDocument

FACT_BASELINE_RESULT_ID = "assurance.quality.result.fact-baseline.v1"

TRIAGE_ACTIONS = frozenset(
    {
        "confirm_assessment",
        "mark_not_an_issue",
        "accept_risk",
        "start_work",
        "reopen",
        "merge",
        "stop",
    }
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_BoundError = type[InputError] | type[OutputError]


def open_quality_artifact(
    root: Path,
    ref: EvidenceArtifactRefV1,
    model: type[_ModelT],
    error: _BoundError,
) -> _ModelT:
    """Open one ledger ref. Prepare maps a bad read to input; finalize maps it to output."""

    try:
        return open_artifact(root, ref, model=model)
    except ArtifactReadError as read_error:
        if read_error.reason == "digest":
            raise error(f"evidence digest changed: {read_error.path}") from read_error
        raise error(str(read_error)) from read_error


def _read_evidence(root: Path, relative: str) -> bytes:
    try:
        return read_workspace_file(root, relative)
    except ArtifactReadError as error:
        raise OutputError(str(error)) from error


def _authenticate_ref(root: Path, ref: object) -> bytes:
    artifact = EvidenceArtifactRefV1.model_validate(ref)
    try:
        return open_artifact(root, artifact)
    except ArtifactReadError as error:
        if error.reason == "digest":
            raise OutputError(f"evidence digest changed: {artifact.path}") from error
        raise OutputError(str(error)) from error


def authenticate_fact_baseline_input(business: FactBaselineSkillInputV1, root: Path) -> None:
    refs = (
        *business.reviewed_case.preparation_refs,
        *business.reviewed_case.case_refs,
        business.reviewed_case.review_ref,
        business.plan_ref,
    )
    for ref in refs:
        _authenticate_ref(root, ref)


def authenticate_assessment_input(business: AssessmentSkillInputV1, root: Path) -> None:
    if business.assessment is None:
        raise OutputError("Inspect requires the loaded assessment")
    refs = (
        *business.reviewed_case.preparation_refs,
        *business.reviewed_case.case_refs,
        business.reviewed_case.review_ref,
        business.mapping_ref,
        business.assessment.trace_ref,
        business.assessment.gaps_ref,
        business.assessment.metrics_ref,
        business.assessment.sufficiency_ref,
        business.assessment.execution_ref,
    )
    for ref in refs:
        _authenticate_ref(root, ref)
    for optional in (business.assessment.healing_ref, business.assessment.issue_ref):
        if optional is not None:
            _authenticate_ref(root, optional)
    if business.fact_baseline_ref is not None:
        _authenticate_ref(root, business.fact_baseline_ref)
    _require_policy(root, business.assessment.scope.policy_digest, "assessment materialization")


def authenticate_report_input(business: ReportSkillInputV1, root: Path) -> None:
    refs = (
        *business.inspection.reviewed_case.preparation_refs,
        *business.inspection.reviewed_case.case_refs,
        business.inspection.reviewed_case.review_ref,
        business.inspection.mapping_ref,
        *business.generation.source_refs,
        *business.generation.plan_refs,
        *business.inspection.assessment_refs,
    )
    for ref in refs:
        _authenticate_ref(root, ref)
    if business.issue_analysis_ref is not None:
        _authenticate_ref(root, business.issue_analysis_ref)
    _require_policy(root, business.assessment.scope.policy_digest, "inspection")


def authenticate_issue_analysis_input(business: QualitySkillInputV1, root: Path) -> None:
    expected_prefix = "qa/results/inspect/"

    def unique_path(name: str) -> str:
        matches = [
            path
            for path in business.artifact_paths
            if path.startswith(expected_prefix) and PurePosixPath(path).name == name
        ]
        if len(matches) != 1:
            raise OutputError(f"issue analysis requires exactly one current {name}")
        return matches[0]

    observations_path = unique_path("observations.json")
    manifest_path = unique_path("issue-evidence-manifest.json")
    try:
        observations = ObservationDocument.model_validate_json(_read_evidence(root, observations_path))
        manifest = IssueEvidenceManifest.model_validate_json(_read_evidence(root, manifest_path))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    identity = (business.change_id, business.batch_id)
    if identity != (observations.change_id, observations.batch_id) or identity != (
        manifest.change_id,
        manifest.batch_id,
    ):
        raise OutputError("issue analysis evidence belongs to another execution batch")
    entries = [entry.model_dump(mode="json") for entry in manifest.entries]
    expected_bundle = f"sha256:{canonical_digest(cast(JSONValue, entries))}"
    if manifest.digest != expected_bundle or business.evidence_bundle_digest != expected_bundle:
        raise OutputError("issue analysis evidence bundle digest changed")
    observation_ids = tuple(sorted(item.observation_id for item in observations.observations))
    if len(observation_ids) != len(set(observation_ids)):
        raise OutputError("issue analysis observations must have unique ids")
    if observation_ids != business.owned_evidence_ids:
        raise OutputError("issue analysis owned evidence does not match observations")
    manifest_paths = {entry.path for entry in manifest.entries}
    if observations_path not in manifest_paths:
        raise OutputError("issue analysis observations must be bound by the evidence manifest")
    if len(manifest_paths) != len(manifest.entries):
        raise OutputError("issue analysis evidence manifest paths must be unique")
    if manifest_paths != set(business.artifact_paths) - {manifest_path}:
        raise OutputError("issue analysis artifacts must match the authenticated manifest")
    for observation in observations.observations:
        if (observation.change_id, observation.batch_id) != identity:
            raise OutputError("issue analysis observation belongs to another execution batch")
        if observation.source.artifact not in manifest_paths:
            raise OutputError("issue analysis observation source is not authenticated")
        if not set(observation.evidence_refs).issubset(manifest_paths):
            raise OutputError("issue analysis observation evidence is not authenticated")
    for entry in manifest.entries:
        actual = hashlib.sha256(_read_evidence(root, entry.path)).hexdigest()
        if entry.digest != f"sha256:{actual}":
            raise OutputError(f"issue analysis evidence digest changed: {entry.path}")


def load_json_ref(root: Path, ref: object, model: type[Any]) -> Any:
    try:
        return model.model_validate_json(_authenticate_ref(root, ref))
    except ValidationError as error:
        raise OutputError(str(error)) from error


class _WriteRoot(Protocol):
    write_root: Path


def _require_policy(root: Path, digest: str, phase: str) -> None:
    try:
        open_artifact(root, {"path": ".aa/policy.yaml", "digest": digest})
    except ArtifactReadError as error:
        if error.reason == "digest":
            raise OutputError(f"product policy digest changed after {phase}") from error
        raise OutputError(str(error)) from error


def captured_agent_document(context: Any, relative: str, result: object) -> EvidenceArtifactRefV1:
    """Compare a file ``strict_files`` already authenticated with the typed result."""
    staged = context.file(relative)
    if staged != result:
        raise OutputError(f"staged agent document differs from the structured result: {relative}")
    return EvidenceArtifactRefV1.model_validate(context.ref(relative).model_dump(mode="json"))


def staged_agent_document(
    *,
    context: _WriteRoot,
    relative: str,
    result: object,
    model: type[Any],
) -> tuple[Any, EvidenceArtifactRefV1]:
    data = _read_evidence(context.write_root, relative)
    try:
        staged = model.model_validate_json(data)
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if staged != result:
        raise OutputError(f"staged agent document differs from the structured result: {relative}")
    return staged, EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())
