"""Case-design preparation and bounded review-repair input."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, OutputError

from assurance_intake.contracts.agent import (
    CaseDesignInputV1,
    ReviewRepairActionV1,
    ReviewRepairContractV1,
)
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.review import (
    CaseReviewResultV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from assurance_intake.operations.prepare import case_design_outputs
from assurance_intake.operations.prepare_evidence import require_regular_project_input


def review_repair_contract(
    project_root: Path,
    *,
    business: CaseDesignInputV1,
    plan: ResolvedAssurancePlan,
) -> ReviewRepairContractV1 | None:
    review_relative = "qa/results/review/case-review.json"
    review_path = project_root.joinpath(*review_relative.split("/"))
    if not review_path.exists() and not review_path.is_symlink():
        return None
    require_regular_project_input(project_root, review_relative)
    review_bytes = review_path.read_bytes()
    try:
        review = CaseReviewResultV1.model_validate_json(review_bytes)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid case-review.json for repair: {error}") from error
    if review.change_id != business.change_id:
        raise InputError("case-review.json change_id does not match case-design change_id")
    if review.public_outcome != "needs_fix":
        return None
    # Imported lazily: finalize imports this module at load time.
    from assurance_intake.operations.finalize import (
        bound_obligations,
        reject_unbound_covered_repairs,
        _review_requests_matrix_coverage,
    )

    if _review_requests_matrix_coverage(review):
        try:
            reject_unbound_covered_repairs(review, bound_obligations(project_root, plan))
        except OutputError as error:
            raise InputError(str(error)) from error

    findings = {finding.id: finding for finding in review.findings}
    actions: list[ReviewRepairActionV1] = []
    for raw_plan in review.auto_fix_plan:
        if not isinstance(raw_plan, Mapping):
            raise InputError("case-review auto_fix_plan items must be mappings")
        finding_id = raw_plan.get("finding_id")
        artifact = raw_plan.get("artifact")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise InputError("case-review repair finding_id must reference an existing finding")
        finding = findings[finding_id]
        try:
            case_id = normalized_auto_fix_case_id(raw_plan, finding.locator.case_id)
            edits = normalized_auto_fix_edits(raw_plan)
        except ValueError as error:
            raise InputError(str(error)) from error
        if finding.severity in {"critical", "blocking"}:
            raise InputError("critical or blocking case-review findings cannot be auto-fixed")
        if not isinstance(artifact, str) or artifact != finding.locator.artifact:
            raise InputError("case-review repair artifact must match its finding locator")
        if case_id != finding.locator.case_id:
            raise InputError("case-review repair case_id must match its finding locator")
        key = finding.locator.key
        if not isinstance(key, str) or not key.strip():
            raise InputError("automatic case repair requires an exact locator key")
        allowed_paths = tuple(part.strip() for part in key.split(",") if part.strip())
        try:
            actions.append(
                ReviewRepairActionV1(
                    finding_id=finding_id,
                    artifact=artifact,
                    case_id=case_id,
                    allowed_paths=allowed_paths,
                    instructions=edits,
                )
            )
        except ValidationError as error:
            raise InputError(f"invalid case-review repair action: {error}") from error
    if not actions:
        raise InputError("needs_fix case-review must provide at least one bounded repair action")

    outputs = case_design_outputs(business.change_id, business.case_delta_paths)
    allowed = set(outputs)
    for action in actions:
        if action.artifact not in allowed:
            raise InputError(
                f"case-review repair artifact is outside the locked case-design write set: {action.artifact}"
            )
    baseline_file_digests: dict[str, str] = {}
    baseline_case_documents: dict[str, object] = {}
    for relative in outputs:
        require_regular_project_input(project_root, relative)
        data = project_root.joinpath(*relative.split("/")).read_bytes()
        baseline_file_digests[relative] = hashlib.sha256(data).hexdigest()
        if relative.endswith("/case.yaml"):
            try:
                baseline_case_documents[relative] = yaml.safe_load(data)
            except yaml.YAMLError as error:
                raise InputError(f"invalid baseline case.yaml {relative}: {error}") from error
    try:
        return ReviewRepairContractV1(
            review_path=review_relative,
            review_sha256=hashlib.sha256(review_bytes).hexdigest(),
            baseline_file_digests=baseline_file_digests,
            baseline_case_documents=baseline_case_documents,
            actions=tuple(actions),
        )
    except ValidationError as error:
        raise InputError(f"invalid deterministic case-review repair contract: {error}") from error
