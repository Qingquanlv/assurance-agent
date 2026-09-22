from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from pydantic import ValidationError

from assurance_execution.contracts.evidence import ExecutionEvidenceV1, FamilyExecutionOutcomeV1
from assurance_execution.contracts.execution import EXECUTION_FAMILIES
from assurance_execution.contracts.observations import ObservationBundleV1, RuntimeObservationV1
from assurance_generation.contracts.obligation_methods import (
    expectation_ready,
    validate_observation_binding,
)
from assurance_generation.contracts.plans import ObligationMethodPlanV1
from assurance_generation.contracts.reviews import ObligationSemanticReviewV1
from assurance_intake.contracts.obligations import PreparedObligationV1, VerificationRequirementV1
from assurance_intake.contracts.explore import PreparedExploreV1, load_exploration_document
from assurance_intake.contracts.plan import decode_plan
from assurance_intake.contracts.quality_goals import normalize_obligation_drafts
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import MaterializeAssessmentInputV1
from assurance_quality.contracts.obligations import (
    ObligationAssessmentRowV1,
    ObligationAssessmentV1,
    ObligationEvidenceFactsV1,
    ObligationGateFactsV1,
    ObligationVerdict,
)
from assurance_quality.operations.common import InputError

REPAIRABLE_OBLIGATION_GAPS = frozenset(
    {
        "obligation_case_missing",
        "obligation_mapping_missing",
        "obligation_observation_missing",
    }
)
HUMAN_OBLIGATION_GAPS = frozenset(
    {
        "capability_unresolved",
        "expectation_unconfirmed",
        "runner_unsupported",
        "profile_missing",
        "oracle_gap",
        "scope_gap",
        "subject_identity_unavailable",
        "capability_unknown",
        "method_unsupported",
        "no_verifiable_scope",
    }
)


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


def _families_for_layer(layer: str | None) -> frozenset[str]:
    if layer is None:
        return frozenset(EXECUTION_FAMILIES)
    if layer == "both":
        return frozenset({"api", "e2e"})
    return frozenset({layer})


def _blocked_reason(
    outcomes: tuple[FamilyExecutionOutcomeV1, ...],
    layer: str | None,
) -> str | None:
    families = _families_for_layer(layer)
    for item in outcomes:
        if item.family not in families:
            continue
        if item.state == "blocked" and item.reason_code is not None:
            return item.reason_code
    return None


class ObligationMethod(NamedTuple):
    """The reviewed method and observations bound to one obligation."""

    plan: ObligationMethodPlanV1 | None
    review: ObligationSemanticReviewV1 | None
    requirement: VerificationRequirementV1 | None
    plan_ref: EvidenceArtifactRefV1 | None


def _method_for_obligation(
    obligation: PreparedObligationV1,
    plans: Mapping[str, ObligationMethodPlanV1],
    reviews: Mapping[tuple[str, str], ObligationSemanticReviewV1],
    plan_refs: Mapping[str, EvidenceArtifactRefV1],
) -> ObligationMethod:
    plan = plans.get(obligation.mrc_id)
    requirement = next(
        (
            item
            for item in obligation.verification_requirements
            if plan is not None and item.requirement_id == plan.requirement_id
        ),
        None,
    )
    if requirement is None and plan is None and len(obligation.verification_requirements) == 1:
        requirement = obligation.verification_requirements[0]
    review = reviews.get((obligation.mrc_id, requirement.requirement_id)) if requirement is not None else None
    return ObligationMethod(
        plan=plan,
        review=review,
        requirement=requirement,
        plan_ref=plan_refs.get(obligation.mrc_id),
    )


def _method_is_bound(method: ObligationMethod) -> bool:
    if method.plan is None or method.requirement is None:
        return False
    if (
        method.plan.requirement_id != method.requirement.requirement_id
        or method.plan.profile_id != method.requirement.profile_id
        or method.plan.prerequisites != method.requirement.prerequisites
        or not method.plan.case_ids
    ):
        return False
    step_ids = tuple(item.step_id for item in method.plan.steps)
    if len(step_ids) != len(set(step_ids)):
        return False
    if any(item.step_id not in step_ids for item in method.plan.observations):
        return False
    try:
        validate_observation_binding(method.requirement, method.plan.observations)
    except ValueError:
        return False
    return True


def _expectation_is_confirmed(obligation: PreparedObligationV1, method: ObligationMethod) -> bool:
    if method.requirement is None or not method.requirement.observations:
        return False
    return all(
        expectation_ready(obligation, method.requirement, item.observation_key, method.review)
        for item in method.requirement.observations
    )


def _observations_for_obligation(
    obligation: PreparedObligationV1,
    method: ObligationMethod,
    bundle: ObservationBundleV1 | None,
) -> tuple[RuntimeObservationV1, ...]:
    if bundle is None or method.plan is None:
        return ()
    bindings = {item.observation_id: item for item in method.plan.observations}
    indexes = {item.observation_id: index for index, item in enumerate(method.plan.observations)}
    return tuple(
        item
        for item in bundle.observations
        if (binding := bindings.get(item.observation_id)) is not None
        and item.mrc_id == obligation.mrc_id
        and item.requirement_id == method.plan.requirement_id
        and item.observation_key == binding.observation_key
        and item.test_nodeid == binding.test_nodeid
        and item.assertion_id == binding.assertion_id
        and item.step_id == binding.step_id
        and item.sequence_index == indexes[item.observation_id]
    )


def _observation_passed(method: ObligationMethod, observation: RuntimeObservationV1) -> bool:
    if method.requirement is None:
        return False
    expected = next(
        (
            item
            for item in method.requirement.observations
            if item.observation_key == observation.observation_key
        ),
        None,
    )
    if expected is None or expected.predicate != "status_code_eq" or expected.expected is None:
        return False
    return observation.actual_status == expected.expected


def _facts_for_obligation(
    *,
    obligation: PreparedObligationV1,
    method: ObligationMethod,
    blocked: str | None,
    observations: tuple[RuntimeObservationV1, ...],
    subject_matched: bool,
    counterexample_ref: EvidenceArtifactRefV1 | None,
) -> ObligationEvidenceFactsV1:
    confirmed = obligation.key is not None and _expectation_is_confirmed(obligation, method)
    method_valid = _method_is_bound(method)
    reviews_passed = method.review is not None and method.review.status == "pass"
    required_ids = (
        {item.observation_id for item in method.plan.observations} if method.plan is not None else set()
    )
    complete = (
        blocked is None
        and bool(required_ids)
        and len(observations) == len(required_ids)
        and {item.observation_id for item in observations} == required_ids
    )
    contradicted = tuple(item for item in observations if not _observation_passed(method, item))
    prerequisites_valid = blocked is None and (
        not (method.requirement and method.requirement.prerequisites)
        or (bool(observations) and all(item.prerequisite_refs for item in observations))
    )
    eligible = (
        confirmed
        and method_valid
        and reviews_passed
        and subject_matched
        and blocked is None
        and complete
        and prerequisites_valid
        and bool(contradicted)
        and counterexample_ref is not None
    )
    return ObligationEvidenceFactsV1.model_validate(
        {
            "expectation_confirmed": confirmed,
            "eligible_counterexample": eligible,
            "subject_valid": subject_matched and blocked is None,
            "method_valid": method_valid and blocked is None,
            "prerequisites_valid": prerequisites_valid,
            "observations_complete": complete,
            "required_reviews_passed": reviews_passed,
            "supporting_evidence_current": complete and not contradicted,
            "counterexample_evidence_current": eligible,
            "counterexample_refs": (counterexample_ref,) if eligible and counterexample_ref else (),
        }
    )


def _gap_codes(
    *,
    obligation: PreparedObligationV1,
    blocked: str | None,
    facts: ObligationEvidenceFactsV1,
    method: ObligationMethod,
) -> tuple[str, ...]:
    codes: list[str] = []
    if obligation.key is None:
        codes.append("capability_unresolved")
    if blocked is not None:
        return tuple((*codes, blocked))
    if method.plan is None:
        codes.append("obligation_case_missing")
    elif not facts.method_valid:
        codes.append("obligation_mapping_missing")
    if not facts.expectation_confirmed:
        codes.append("expectation_unconfirmed")
    if not facts.subject_valid:
        codes.append("subject_identity_unavailable")
    if not facts.observations_complete:
        codes.append("obligation_observation_missing")
    return tuple(codes)


def load_prepared_obligations(
    workspace: Path, request: MaterializeAssessmentInputV1
) -> tuple[PreparedObligationV1, ...]:
    try:
        plan = decode_plan(_authenticate_ref(workspace, request.plan_ref), request.plan_ref)
        exploration = load_exploration_document(
            _authenticate_ref(workspace, plan.quality_goal.obligations_ref)
        )
        if isinstance(exploration, PreparedExploreV1):
            return exploration.minimum_required_coverage
        return normalize_obligation_drafts(exploration.minimum_required_coverage, resolved_quotes={})
    except (ValueError, ValidationError) as error:
        raise InputError(f"invalid plan-bound obligations: {error}") from error


def load_obligation_methods(
    workspace: Path, request: MaterializeAssessmentInputV1
) -> tuple[
    dict[str, ObligationMethodPlanV1],
    dict[tuple[str, str], ObligationSemanticReviewV1],
    dict[str, EvidenceArtifactRefV1],
]:
    """Read the reviewed method plans the generation cycle froze for this plan."""

    plans: dict[str, ObligationMethodPlanV1] = {}
    reviews: dict[tuple[str, str], ObligationSemanticReviewV1] = {}
    plan_refs: dict[str, EvidenceArtifactRefV1] = {}
    ref = request.generation.method_plan_ref
    try:
        payload = json.loads(_authenticate_ref(workspace, ref).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InputError(f"invalid obligation method artifact: {error}") from error
    if not isinstance(payload, dict):
        raise InputError("obligation method artifact is not a document")
    if payload.get("plan_digest") != request.plan_digest or payload.get(
        "plan_ref"
    ) != request.plan_ref.model_dump(mode="json"):
        raise InputError("obligation method artifact belongs to another frozen plan")
    try:
        typed_plans = tuple(
            ObligationMethodPlanV1.model_validate(item) for item in payload.get("method_plans") or ()
        )
        typed_reviews = tuple(
            ObligationSemanticReviewV1.model_validate(item) for item in payload.get("semantic_reviews") or ()
        )
    except ValidationError as error:
        raise InputError(f"invalid obligation method artifact: {error}") from error
    for plan in typed_plans:
        if plan.mrc_id in plans:
            raise InputError(f"obligation has more than one reviewed method: {plan.mrc_id}")
        plans[plan.mrc_id] = plan
        plan_refs[plan.mrc_id] = ref
    for review in typed_reviews:
        if review.frozen_plan_digest != request.plan_digest or review.plan_ref != request.plan_ref:
            raise InputError(f"semantic review belongs to another plan: {review.mrc_id}")
        key = (review.mrc_id, review.requirement_id)
        if key in reviews:
            raise InputError(f"obligation has more than one semantic review: {review.mrc_id}")
        reviews[key] = review
    return plans, reviews, plan_refs


def _authenticate_observation_identity(
    *,
    request: MaterializeAssessmentInputV1,
    evidence: ExecutionEvidenceV1,
    bundle: ObservationBundleV1,
    method_refs: tuple[EvidenceArtifactRefV1, ...],
) -> None:
    identity = bundle.identity
    expected_refs = tuple(sorted(method_refs, key=lambda item: (item.path, item.digest)))
    actual_refs = tuple(sorted(identity.method_plan_refs, key=lambda item: (item.path, item.digest)))
    if (
        identity.plan_digest != request.plan_digest
        or identity.batch_id != request.execution.batch_id
        or identity.mapping_digest != evidence.mapping_digest
        or identity.baseline_tree_id != evidence.baseline_tree_id
        or identity.runner_profile_digest != evidence.runner_profile_digest
        or actual_refs != expected_refs
    ):
        raise InputError("runtime observation identity does not match execution evidence")


def load_observation_bundle(
    workspace: Path, request: MaterializeAssessmentInputV1
) -> ObservationBundleV1 | None:
    ref = request.execution.observations_ref
    if ref is None:
        return None
    try:
        payload = json.loads(_authenticate_ref(workspace, ref).decode("utf-8"))
        return ObservationBundleV1.model_validate(payload)
    except (UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise InputError(f"invalid runtime observation bundle: {error}") from error


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
    obligations = load_prepared_obligations(workspace, request)
    plans, reviews, plan_refs = load_obligation_methods(workspace, request)
    bundle = load_observation_bundle(workspace, request)
    method_refs = (request.generation.method_plan_ref,)
    if bundle is not None:
        _authenticate_observation_identity(
            request=request,
            evidence=evidence,
            bundle=bundle,
            method_refs=method_refs,
        )
    subject_matched = bundle is not None and bundle.subject.status == "matched"
    observations_ref = request.execution.observations_ref
    rows: list[ObligationAssessmentRowV1] = []
    for obligation in obligations:
        if obligation.scope_disposition == "excluded":
            continue
        method = _method_for_obligation(obligation, plans, reviews, plan_refs)
        blocked = _blocked_reason(evidence.family_outcomes, obligation.layer)
        observations = (
            ()
            if bundle is not None and bundle.collection_errors
            else _observations_for_obligation(obligation, method, bundle)
        )
        for observation in observations:
            for prerequisite_ref in observation.prerequisite_refs:
                _authenticate_ref(workspace, prerequisite_ref)
        facts = _facts_for_obligation(
            obligation=obligation,
            method=method,
            blocked=blocked,
            observations=observations,
            subject_matched=subject_matched,
            counterexample_ref=observations_ref,
        )
        verdict = decide_obligation(facts)
        evidence_refs = (
            (request.execution.evidence_ref,)
            if observations_ref is None
            else (request.execution.evidence_ref, observations_ref)
        )
        rows.append(
            ObligationAssessmentRowV1(
                plan_digest=request.plan_digest,
                mrc_id=obligation.mrc_id,
                verdict=verdict,
                method_plan_refs=(method.plan_ref,) if method.plan_ref is not None else (),
                evidence_refs=evidence_refs,
                gap_codes=()
                if verdict == "supported"
                else _gap_codes(obligation=obligation, blocked=blocked, facts=facts, method=method),
            )
        )
    excluded = tuple(item.mrc_id for item in obligations if item.scope_disposition == "excluded")
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


def _row_identity(row: ObligationAssessmentRowV1) -> tuple[str, str]:
    return (row.plan_digest, row.mrc_id)


def _classify_inconclusive_gaps(codes: tuple[str, ...]) -> Literal["human", "repairable", "unclassified"]:
    if any(code in HUMAN_OBLIGATION_GAPS for code in codes):
        return "human"
    if any(code in REPAIRABLE_OBLIGATION_GAPS for code in codes):
        return "repairable"
    return "unclassified"


def derive_obligation_gate_facts(
    assessment: ObligationAssessmentV1,
    *,
    required_ids: tuple[tuple[str, str], ...],
) -> ObligationGateFactsV1:
    if len(required_ids) != len(set(required_ids)):
        raise InputError("required obligation identities are not unique")
    identities = tuple(_row_identity(row) for row in assessment.rows)
    if len(identities) != len(set(identities)):
        raise InputError("assessment rows are not unique by (plan_digest, mrc_id)")
    required_set = set(required_ids)
    if set(identities) != required_set:
        raise InputError("assessment rows do not match the required obligation identities")
    excluded = set(assessment.excluded_mrc_ids)
    required_mrcs = {mrc_id for _, mrc_id in required_ids}
    if excluded & required_mrcs:
        raise InputError("excluded obligations overlap the required identity set")
    supported = sum(1 for row in assessment.rows if row.verdict == "supported")
    refuted = sum(1 for row in assessment.rows if row.verdict == "refuted")
    inconclusive_rows = tuple(row for row in assessment.rows if row.verdict == "inconclusive")
    human = 0
    repairable = 0
    for row in inconclusive_rows:
        kind = _classify_inconclusive_gaps(row.gap_codes)
        if kind == "human":
            human += 1
        elif kind == "repairable":
            repairable += 1
    return ObligationGateFactsV1(
        required_count=len(required_ids),
        supported_count=supported,
        refuted_count=refuted,
        inconclusive_count=len(inconclusive_rows),
        repairable_gap_count=repairable,
        human_gap_count=human,
    )


__all__ = [
    "HUMAN_OBLIGATION_GAPS",
    "REPAIRABLE_OBLIGATION_GAPS",
    "assess_obligations",
    "decide_obligation",
    "derive_obligation_gate_facts",
    "load_prepared_obligations",
    "write_obligation_assessment",
]
