"""Case-review input authentication and automatic-repair scope checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, OutputError

from assurance_intake.contracts.review import (
    CaseReviewResultV1,
    ReviewRepairActionV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from graph_engine.artifacts import ArtifactReadError, open_artifact
from assurance_intake.ops.case_review.models import CaseReviewInputV1


def validate_case_review_repair_scope(
    document: CaseReviewResultV1,
    payload: CaseReviewInputV1,
    *,
    change_id: str,
) -> None:
    if document.public_outcome != "needs_fix":
        return
    if not payload.case_delta_paths:
        raise InputError("case_delta_paths are required to lock case-review automatic repairs")
    change_root = "qa"
    allowed = {
        f"{change_root}/.qa.yaml",
        f"{change_root}/proposal.md",
        f"{change_root}/results/trace/minimum-coverage-matrix.json",
        *payload.case_delta_paths,
    }
    if document.next_action != "run_case_design":
        raise OutputError("needs_fix case review next_action must be run_case_design")
    findings = {finding.id: finding for finding in document.findings}
    if not document.auto_fix_plan:
        raise OutputError("needs_fix case review must provide at least one auto_fix_plan item")
    planned_findings: set[str] = set()
    for item in document.auto_fix_plan:
        if not isinstance(item, Mapping):
            raise OutputError("case review auto_fix_plan items must be mappings")
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise OutputError("case review auto_fix_plan finding_id must reference an existing finding")
        if finding_id in planned_findings:
            raise OutputError("case review auto_fix_plan must not duplicate a finding_id")
        planned_findings.add(finding_id)
        finding = findings[finding_id]
        flags = finding.model_extra or {}
        if flags.get("auto_fix_allowed") is not True or flags.get("human_review_required") is not False:
            raise OutputError(
                "automatic repair finding must explicitly allow auto-fix and forbid human review"
            )
        if finding.severity in {"critical", "blocking"}:
            raise OutputError("critical or blocking case review finding cannot be auto-fixed")
        artifact = item.get("artifact")
        if not isinstance(artifact, str) or artifact not in allowed:
            raise OutputError(
                f"automatic repair artifact is outside the locked case-design write set: {artifact!r}"
            )
        if artifact != finding.locator.artifact:
            raise OutputError("automatic repair artifact must match the finding locator")
        try:
            case_id = normalized_auto_fix_case_id(item, finding.locator.case_id)
            edits = normalized_auto_fix_edits(item)
        except ValueError as error:
            raise OutputError(str(error)) from error
        if case_id != finding.locator.case_id:
            raise OutputError("automatic repair case_id must match the finding locator")
        key = finding.locator.key
        if not isinstance(key, str) or not key.strip():
            raise OutputError("automatic case repair requires an exact locator key")
        locator_paths = tuple(part.strip() for part in key.split(",") if part.strip())
        if artifact in payload.case_delta_paths and "case_id" in locator_paths:
            raise OutputError(
                "case_id identifies the case and cannot be an automatic repair field; "
                "use added or modified to authorize whole-case removal"
            )
        try:
            ReviewRepairActionV1(
                finding_id=finding_id,
                artifact=artifact,
                case_id=finding.locator.case_id,
                allowed_paths=locator_paths,
                instructions=edits,
            )
        except ValidationError as error:
            raise OutputError(f"invalid automatic repair locator: {error}") from error


def read_case_review_inputs(
    workspace: Path,
    payload: CaseReviewInputV1,
    matrix_relative: str,
) -> dict[str, bytes]:
    if not payload.case_delta_paths:
        raise InputError("case_delta_paths are required for case-review scope validation")
    if payload.case_delta_paths and not set(payload.case_delta_paths) <= {
        ref.path for ref in payload.case_refs
    }:
        raise InputError("case_refs must authenticate every locked case.yaml for case review")
    matrix_refs = tuple(ref for ref in payload.preparation_refs if ref.path == matrix_relative)
    if len(matrix_refs) != 1:
        raise InputError("preparation_refs must authenticate the minimum coverage matrix for case review")
    images: dict[str, bytes] = {}
    for ref in (*payload.case_refs, *matrix_refs):
        try:
            data = open_artifact(workspace, ref)
        except ArtifactReadError as error:
            if error.reason == "digest":
                raise OutputError(f"case-review input digest mismatch: {ref.path}") from error
            raise OutputError(f"case-review input is missing or is not a regular file: {ref.path}") from error
        images[ref.path] = data
    return images
