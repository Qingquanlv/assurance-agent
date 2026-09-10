"""Read-only delivery authentication for verified execution profiles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import cast

from assurance_execution.contracts.verification import (
    VerificationManifestV1,
    VerifiedExecutionResultV1,
)
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.verification import VerificationVerdictV1
from graph_engine.canonical import JSONValue, canonical_digest

from assurance_product.models import ExecutionGateRefV1, QualityGateRefV1


def _read_ref(project: Path, ref: EvidenceArtifactRefV1, label: str) -> bytes:
    relative = PurePosixPath(ref.path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"verified delivery {label} path is invalid")
    path = project.joinpath(*relative.parts)
    current = project
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"verified delivery {label} path is a symlink")
    try:
        if not path.is_file() or path.stat().st_nlink != 1:
            raise OSError
        content = path.read_bytes()
    except OSError as error:
        raise ValueError(f"verified delivery {label} is missing") from error
    if hashlib.sha256(content).hexdigest() != ref.digest:
        raise ValueError(f"verified delivery {label} digest drifted")
    return content


def _read_current_execution(
    project: Path,
    change_id: str,
    gate: ExecutionGateRefV1,
    quality: QualityGateRefV1,
) -> VerifiedExecutionResultV1:
    filename = "run-result.json" if gate.semantic_node_id == "execution.run" else "execute-result.json"
    relative = f"qa/changes/{change_id}/execution/{filename}"
    execution_ref = next(
        (ref for ref in quality.inspection.assessment_refs if ref.path == relative),
        None,
    )
    if execution_ref is None:
        raise ValueError("verified delivery execution assessment is missing")
    try:
        payload = json.loads(_read_ref(project, execution_ref, "execution assessment"))
        verified = VerifiedExecutionResultV1.model_validate(payload)
    except (OSError, ValueError) as error:
        raise ValueError("verified delivery execution evidence is invalid") from error
    document = verified.model_dump(mode="json")
    if (
        verified.change_id != change_id
        or verified.validation_profile != gate.validation_profile
        or verified.batch_id != gate.batch_id
        or canonical_digest(cast(JSONValue, document)) != gate.execution_digest
    ):
        raise ValueError("verified delivery execution identity drifted")
    if verified.completion_status != "collected":
        raise ValueError("verified delivery execution is incomplete")
    if gate.semantic_node_id == "execution.execute" and verified.repair_round != 0:
        raise ValueError("verified delivery initial execution has a repair round")
    if gate.semantic_node_id == "execution.run" and verified.repair_round == 0:
        raise ValueError("verified delivery rerun is missing its repair round")
    return verified


def _authenticate_manifest(
    project: Path,
    invocation_id: str,
    verified: VerifiedExecutionResultV1,
) -> VerificationManifestV1:
    try:
        manifest = VerificationManifestV1.model_validate_json(
            _read_ref(project, verified.manifest_ref, "manifest")
        )
    except ValueError as error:
        raise ValueError("verified delivery manifest is invalid") from error
    expected = (
        verified.execution_id,
        verified.change_id,
        verified.case_id,
        verified.attempt_key,
        verified.coverage_epoch,
        verified.repair_round,
        verified.plan_ref.path,
        verified.plan_digest,
        verified.case_execution_plan_ref.path,
        verified.case_execution_plan_digest,
        verified.spec_digest,
        verified.mapping_digest,
        verified.validation_profile,
    )
    observed = (
        manifest.execution_id,
        manifest.change_id,
        manifest.case_id,
        manifest.attempt_key,
        manifest.coverage_epoch,
        manifest.repair_round,
        manifest.plan_ref,
        manifest.plan_digest,
        manifest.case_execution_plan_ref,
        manifest.case_execution_plan_digest,
        manifest.spec_digest,
        manifest.mapping_digest,
        manifest.validation_profile,
    )
    if observed != expected or manifest.invocation_id != invocation_id:
        raise ValueError("verified delivery manifest identity drifted")
    manifest_digest = canonical_digest(cast(JSONValue, manifest.model_dump(mode="json")))
    if verified.evidence.manifest_digest != manifest_digest:
        raise ValueError("verified delivery manifest digest drifted")
    return manifest


def _authenticate_execution_refs(
    project: Path,
    verified: VerifiedExecutionResultV1,
) -> None:
    evidence = verified.evidence
    if evidence.receipt_ref not in verified.raw_evidence_refs:
        raise ValueError("verified delivery process receipt is missing")
    for ref in (verified.evidence_ref, verified.execution_authority_ref, *verified.raw_evidence_refs):
        _read_ref(project, ref, "raw execution evidence")
    observation_ids = tuple(sorted(item.obligation_id for item in evidence.observations))
    if (
        evidence.state != "collected"
        or any(item.state != "observed" for item in evidence.observations)
        or len(observation_ids) != len(set(observation_ids))
    ):
        raise ValueError("verified delivery evidence is incomplete")


def _authenticate_plan_and_verdict(
    project: Path,
    verified: VerifiedExecutionResultV1,
    quality: QualityGateRefV1,
) -> None:
    inspection = quality.inspection
    if (
        inspection.change_id != verified.change_id
        or inspection.batch_id != verified.batch_id
        or inspection.coverage_epoch != verified.coverage_epoch
        or inspection.plan_digest != verified.plan_digest
        or inspection.plan_ref != verified.plan_ref
        or inspection.reviewed_case != verified.reviewed_case
        or inspection.mapping_ref.digest != verified.mapping_digest
        or inspection.verification_status != "PASSED"
        or inspection.verification_ref is None
        or inspection.verification_ref not in inspection.assessment_refs
    ):
        raise ValueError("verified delivery quality identity drifted")
    try:
        plan_set = CaseExecutionPlanSetV1.model_validate_json(
            _read_ref(project, verified.case_execution_plan_ref, "machine plan")
        )
        plan = next(item for item in plan_set.cases if item.case_id == verified.case_id)
        verdict = VerificationVerdictV1.model_validate_json(
            _read_ref(project, inspection.verification_ref, "verification verdict")
        )
    except (StopIteration, ValueError) as error:
        raise ValueError("verified delivery plan or verdict is invalid") from error
    if (
        plan.validation_profile != verified.validation_profile
        or plan.plan_digest != verified.plan_digest
        or plan.plan_ref != verified.plan_ref
        or plan.reviewed_case != verified.reviewed_case
        or plan.spec_digest != verified.spec_digest
    ):
        raise ValueError("verified delivery machine plan identity drifted")
    required = tuple(sorted(plan.required))
    observed = tuple(sorted(item.obligation_id for item in verified.evidence.observations))
    decided = tuple(sorted(item.obligation_id for item in verdict.obligations))
    if observed != required or decided != required:
        raise ValueError("verified delivery required obligations are incomplete")
    if (
        verdict.verdict != "PASSED"
        or verdict.validation_profile != verified.validation_profile
        or verdict.execution_id != verified.execution_id
        or verdict.case_id != verified.case_id
        or verdict.required != len(required)
        or verdict.satisfied != len(required)
    ):
        raise ValueError("verified delivery verdict is not passed")


def authenticate_verified_delivery(
    project: Path,
    change_id: str,
    invocation_id: str,
    *,
    execution_gate: ExecutionGateRefV1,
    quality_gate: QualityGateRefV1,
) -> VerifiedExecutionResultV1 | None:
    """Authenticate current verified material; return ``None`` for the legacy profile."""

    if execution_gate.validation_profile is None:
        filename = (
            "run-result.json" if execution_gate.semantic_node_id == "execution.run" else "execute-result.json"
        )
        relative = f"qa/changes/{change_id}/execution/{filename}"
        execution_ref = next(
            (ref for ref in quality_gate.inspection.assessment_refs if ref.path == relative),
            None,
        )
        if execution_ref is not None:
            payload = json.loads(_read_ref(project, execution_ref, "execution assessment"))
            if isinstance(payload, dict) and payload.get("validation_profile") in {
                "api_db.v1",
                "api_db_trace.v1",
            }:
                raise ValueError("verified delivery profile was removed from the execution gate")
        if (
            quality_gate.inspection.verification_ref is not None
            or quality_gate.inspection.verification_status is not None
        ):
            raise ValueError("verified delivery profile was removed from the execution gate")
        return None
    if quality_gate.report is None:
        raise ValueError("verified delivery quality report is missing")
    verified = _read_current_execution(project, change_id, execution_gate, quality_gate)
    _authenticate_manifest(project, invocation_id, verified)
    _authenticate_execution_refs(project, verified)
    _authenticate_plan_and_verdict(project, verified, quality_gate)
    for ref in (
        verified.plan_ref,
        verified.case_execution_plan_ref,
        quality_gate.inspection.mapping_ref,
        *verified.reviewed_case.preparation_refs,
        *verified.reviewed_case.case_refs,
        verified.reviewed_case.review_ref,
        *quality_gate.inspection.assessment_refs,
        *quality_gate.report.report_refs,
    ):
        _read_ref(project, ref, "bound evidence")
    return verified


__all__ = ["authenticate_verified_delivery"]
