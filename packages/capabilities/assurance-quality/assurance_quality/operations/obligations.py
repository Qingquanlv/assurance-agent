from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from assurance_execution.contracts.evidence import ExecutionEvidenceV1, FamilyExecutionOutcomeV1
from assurance_intake.contracts.obligations import PreparedObligationV1
from assurance_intake.contracts.plan import decode_plan
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import MaterializeAssessmentInputV1
from assurance_quality.contracts.obligations import (
    ObligationAssessmentRowV1,
    ObligationAssessmentV1,
    ObligationEvidenceFactsV1,
    ObligationVerdict,
)
from assurance_quality.operations.common import InputError


def decide_obligation(facts: ObligationEvidenceFactsV1) -> ObligationVerdict:
    if not facts.expectation_confirmed:
        return "inconclusive"
    if facts.eligible_counterexample:
        return "refuted"
    necessary = (
        facts.subject_valid,
        facts.method_valid,
        facts.prerequisites_valid,
        facts.observations_complete,
        facts.required_reviews_passed,
        facts.supporting_evidence_current,
    )
    return "supported" if all(necessary) else "inconclusive"


def _regular_file(workspace: Path, relative: str) -> Path:
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError(f"assessment input escapes the workspace: {relative}") from error
    if not path.is_file() or path.is_symlink():
        raise InputError(f"assessment input is missing: {relative}")
    return path


def _authenticate_ref(workspace: Path, ref: EvidenceArtifactRefV1) -> bytes:
    payload = _regular_file(workspace, ref.path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != ref.digest:
        raise InputError(f"assessment source digest changed: {ref.path}")
    return payload


def _blocked_reason(
    outcomes: tuple[FamilyExecutionOutcomeV1, ...],
    layer: str | None,
) -> str | None:
    for item in outcomes:
        if layer is not None and item.family not in {layer, "both"}:
            if layer == "both" and item.family in {"api", "e2e"}:
                pass
            elif item.family != layer:
                continue
        if item.state == "blocked" and item.reason_code is not None:
            return item.reason_code
    return None


def _facts_for_obligation(
    *,
    obligation: PreparedObligationV1,
    evidence: ExecutionEvidenceV1,
    expectation_ready: bool,
    reviews_passed: bool,
    counterexample_ref: EvidenceArtifactRefV1 | None,
) -> ObligationEvidenceFactsV1:
    blocked = _blocked_reason(evidence.family_outcomes, obligation.layer)
    failed_results = tuple(item for item in evidence.results if item.status == "failed")
    eligible = (
        expectation_ready
        and reviews_passed
        and blocked is None
        and bool(failed_results)
        and counterexample_ref is not None
    )
    complete = bool(evidence.results) and blocked is None
    return ObligationEvidenceFactsV1.model_validate(
        {
            "expectation_confirmed": expectation_ready,
            "eligible_counterexample": eligible,
            "subject_valid": blocked is None,
            "method_valid": blocked is None,
            "prerequisites_valid": blocked is None,
            "observations_complete": complete,
            "required_reviews_passed": reviews_passed,
            "supporting_evidence_current": complete,
            "counterexample_evidence_current": eligible,
            "counterexample_refs": (counterexample_ref,) if eligible and counterexample_ref else (),
        }
    )


def _load_obligations(workspace: Path, request: MaterializeAssessmentInputV1) -> tuple[PreparedObligationV1, ...]:
    for ref in request.reviewed_case.preparation_refs:
        if not ref.path.endswith(("exploration.json", "quality-goals.json", "prepared-explore.json")):
            continue
        try:
            payload = json.loads(_authenticate_ref(workspace, ref).decode("utf-8"))
        except (InputError, UnicodeError, json.JSONDecodeError):
            continue
        raw = payload.get("minimum_required_coverage") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            continue
        rows: list[PreparedObligationV1] = []
        for item in raw:
            try:
                rows.append(PreparedObligationV1.model_validate(item))
            except ValidationError:
                continue
        if rows:
            return tuple(rows)
    return ()


def assess_obligations(
    *,
    workspace: Path,
    request: MaterializeAssessmentInputV1,
) -> ObligationAssessmentV1:
    _authenticate_ref(workspace, request.plan_ref)
    try:
        decode_plan(_authenticate_ref(workspace, request.plan_ref), request.plan_ref)
    except ValueError as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    evidence_bytes = _authenticate_ref(workspace, request.execution.evidence_ref)
    try:
        evidence = ExecutionEvidenceV1.model_validate(json.loads(evidence_bytes.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise InputError(f"invalid execution evidence: {error}") from error
    obligations = _load_obligations(workspace, request)
    rows: list[ObligationAssessmentRowV1] = []
    for obligation in obligations:
        if obligation.scope_disposition == "excluded":
            continue
        blocked = _blocked_reason(evidence.family_outcomes, obligation.layer)
        facts = _facts_for_obligation(
            obligation=obligation,
            evidence=evidence,
            expectation_ready=True,
            reviews_passed=True,
            counterexample_ref=request.execution.evidence_ref if evidence.results else None,
        )
        verdict = decide_obligation(facts)
        gaps: list[str] = []
        if blocked:
            gaps.append(blocked)
            verdict = "inconclusive"
        elif verdict == "inconclusive" and not facts.observations_complete:
            gaps.append("obligation_observation_missing")
        rows.append(
            ObligationAssessmentRowV1(
                plan_digest=request.plan_digest,
                mrc_id=obligation.mrc_id,
                verdict=verdict,
                evidence_refs=(request.execution.evidence_ref,),
                gap_codes=tuple(gaps),
            )
        )
    excluded = tuple(
        item.mrc_id for item in obligations if item.scope_disposition == "excluded"
    )
    return ObligationAssessmentV1(
        plan_ref=request.plan_ref,
        rows=tuple(rows),
        excluded_mrc_ids=excluded,
        excluded_basis_refs=tuple(
            item.exclusion_basis for item in obligations if item.exclusion_basis is not None
        ),
    )


def write_obligation_assessment(
    write_root: Path,
    request: MaterializeAssessmentInputV1,
    assessment: ObligationAssessmentV1,
) -> EvidenceArtifactRefV1:
    relative = (
        f"qa/results/inspect/epochs/{request.reviewed_case.coverage_epoch}/"
        f"batches/{request.execution.batch_id}/obligation-assessment.json"
    )
    path = write_root.joinpath(*PurePosixPath(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(assessment.model_dump(mode="json"), indent=2) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(encoded).hexdigest())


__all__ = [
    "assess_obligations",
    "decide_obligation",
    "write_obligation_assessment",
]
