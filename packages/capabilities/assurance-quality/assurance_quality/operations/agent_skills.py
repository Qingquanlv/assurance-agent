"""Provider-neutral quality Agent finalize helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import OutputError
from agent_runtime_contracts.wire.schema import canonical_digest
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


def canonical_file(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or "\\" in relative or any(part in {"", ".", ".."} for part in posix.parts):
        raise OutputError(f"evidence path must be canonical and relative: {relative}")
    path = root.joinpath(*posix.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise OutputError(f"evidence file is missing: {relative}") from error
    if resolved != path or path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise OutputError(f"evidence file must be a regular single-link file: {relative}")
    return path


def _authenticate_ref(root: Path, ref: object) -> bytes:
    artifact = EvidenceArtifactRefV1.model_validate(ref)
    data = canonical_file(root, artifact.path).read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact.digest:
        raise OutputError(f"evidence digest changed: {artifact.path}")
    return data


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
    policy = canonical_file(root, ".aa/policy.yaml").read_bytes()
    if hashlib.sha256(policy).hexdigest() != business.assessment.scope.policy_digest:
        raise OutputError("product policy digest changed after assessment materialization")


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
    policy = canonical_file(root, ".aa/policy.yaml").read_bytes()
    if hashlib.sha256(policy).hexdigest() != business.assessment.scope.policy_digest:
        raise OutputError("product policy digest changed after inspection")


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
        observations = ObservationDocument.model_validate_json(
            canonical_file(root, observations_path).read_bytes()
        )
        manifest = IssueEvidenceManifest.model_validate_json(canonical_file(root, manifest_path).read_bytes())
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
        actual = hashlib.sha256(canonical_file(root, entry.path).read_bytes()).hexdigest()
        if entry.digest != f"sha256:{actual}":
            raise OutputError(f"issue analysis evidence digest changed: {entry.path}")


def load_json_ref(root: Path, ref: object, model: type[Any]) -> Any:
    try:
        return model.model_validate_json(_authenticate_ref(root, ref))
    except ValidationError as error:
        raise OutputError(str(error)) from error


class _WriteRoot(Protocol):
    write_root: Path


def staged_agent_document(
    *,
    context: _WriteRoot,
    relative: str,
    result: object,
    model: type[Any],
) -> tuple[Any, EvidenceArtifactRefV1]:
    path = canonical_file(context.write_root, relative)
    data = path.read_bytes()
    try:
        staged = model.model_validate_json(data)
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if staged != result:
        raise OutputError(f"staged agent document differs from the structured result: {relative}")
    return staged, EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())
