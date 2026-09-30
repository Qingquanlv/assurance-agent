"""Pure initial family selection and frozen plan construction."""

from __future__ import annotations

from assurance_intake.contracts.common import TEST_FAMILY_ORDER, TestFamily
from assurance_intake.contracts.explore import TestStrategyV1
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.operations.impact_validation import impact_required_families
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.plan import (
    FallbackDetail,
    PreparedQualityGoalV1,
    ResolutionReasonV1,
    ResolvePlanInputV1,
    ResolvedAssurancePlan,
    TestFamilyPolicyV1,
    resolution_reason_sort_key,
)
from assurance_intake.domain.plan_codec import seal_plan


class InputError(ValueError):
    """Validated plan inputs describe an impossible execution scope."""


_LAYER_TO_FAMILY: dict[str, TestFamily] = {
    "API": "api",
    "E2E": "e2e",
    "Fuzz": "fuzz",
    "Performance": "performance",
}


def _canonical(values: set[TestFamily]) -> tuple[TestFamily, ...]:
    return tuple(family for family in TEST_FAMILY_ORDER if family in values)


def derive_family_proposal(strategy: TestStrategyV1) -> tuple[TestFamily, ...]:
    """Project the strict four-row Explore recommendation into family names."""
    return tuple(_LAYER_TO_FAMILY[row.layer] for row in strategy.layer_recommendation if row.recommended)


def resolve_families(
    *,
    candidate: tuple[TestFamily, ...],
    proposed: tuple[TestFamily, ...],
    policy: TestFamilyPolicyV1,
    goal_required: tuple[TestFamily, ...],
    impact_required: tuple[TestFamily, ...] = (),
) -> tuple[tuple[TestFamily, ...], FallbackDetail | None]:
    admissible = set(candidate) & set(policy.allowed)
    required = set(policy.required) | set(goal_required)
    if not admissible or not required <= admissible:
        raise InputError("candidate, policy, and quality goals are incompatible")

    unreliable = not proposed or not set(proposed) <= set(candidate)
    base = admissible if unreliable else set(proposed) & admissible
    selected = base | required | (set(impact_required) & admissible)
    cause: FallbackDetail | None = None
    if not selected:
        selected = admissible
        cause = "empty_after_policy"
    elif unreliable:
        cause = "empty_proposal" if not proposed else "outside_candidate"
    return _canonical(selected), cause


def _reasons(
    *,
    request: ResolvePlanInputV1,
    proposed: tuple[TestFamily, ...],
    fallback_cause: FallbackDetail | None,
) -> tuple[ResolutionReasonV1, ...]:
    reasons = [
        ResolutionReasonV1(
            reason_code=("accepted_proposal" if fallback_cause is None else "fallback_all_candidates"),
            detail=fallback_cause,
            summary=(
                "Accepted the Explore family proposal."
                if fallback_cause is None
                else "Used every admissible candidate because the proposal was unreliable or empty after policy."
            ),
        )
    ]
    for family in request.candidate_test_families:
        if family not in request.family_policy.allowed:
            reasons.append(
                ResolutionReasonV1(
                    reason_code="candidate_outside_allowed",
                    family=family,
                    summary="Excluded a candidate family that organization policy does not allow.",
                )
            )
    for family in proposed:
        if family not in request.candidate_test_families:
            reasons.append(
                ResolutionReasonV1(
                    reason_code="proposed_outside_candidate",
                    family=family,
                    summary="Explore recommended a known family outside the caller candidate set.",
                )
            )
    return tuple(reasons)


def resolve_plan(
    *,
    request: ResolvePlanInputV1,
    proposed: tuple[TestFamily, ...],
    quality_goal: PreparedQualityGoalV1,
    inventory: ChangeImpactInventoryV1,
    exploration_ref: EvidenceArtifactRefV1 | None = None,
) -> ResolvedAssurancePlan:
    stored_exploration = exploration_ref or request.exploration_ref
    if quality_goal.obligations_ref != stored_exploration:
        raise InputError("quality goal obligations must bind the exploration artifact")
    if quality_goal.source_resource_digests != request.source_resource_digests:
        raise InputError("quality goal sources do not match the resolver input")
    if inventory.change_id != request.change_id:
        raise InputError("impact inventory does not belong to the plan's change")

    admissible = set(request.candidate_test_families) & set(request.family_policy.allowed)
    impact_required = impact_required_families(inventory)
    selected, fallback_cause = resolve_families(
        candidate=request.candidate_test_families,
        proposed=proposed,
        policy=request.family_policy,
        goal_required=quality_goal.required_test_families,
        impact_required=tuple(family for family in impact_required if family in admissible),
    )
    reasons = list(_reasons(request=request, proposed=proposed, fallback_cause=fallback_cause))
    required = set(request.family_policy.required) | set(quality_goal.required_test_families)
    for family in TEST_FAMILY_ORDER:
        if family not in required or family in proposed:
            continue
        sources = []
        if family in request.family_policy.required:
            sources.append("policy")
        if family in quality_goal.required_test_families:
            sources.append("prepared goals")
        reasons.append(
            ResolutionReasonV1(
                reason_code="required_retained",
                family=family,
                summary=f"Retained a family required by {' and '.join(sources)}.",
            )
        )
    for family in impact_required:
        if family not in admissible:
            reasons.append(
                ResolutionReasonV1(
                    reason_code="impact_family_unavailable",
                    family=family,
                    summary=(
                        "Closed impact inventory rows need a family outside the admissible candidate "
                        "scope; those rows stay visible for review."
                    ),
                )
            )
        elif family not in proposed and family not in required:
            reasons.append(
                ResolutionReasonV1(
                    reason_code="impact_retained",
                    family=family,
                    summary="Retained a family required by closed impact inventory rows.",
                )
            )
    pending = [row.row_id for row in inventory.rows if row.disposition == "pending_confirmation"]
    if pending:
        reasons.append(
            ResolutionReasonV1(
                reason_code="impact_pending_confirmation",
                summary=f"{len(pending)} impact row(s) await confirmation: {', '.join(pending)}.",
            )
        )
    reasons.sort(key=resolution_reason_sort_key)
    return seal_plan(
        {
            "schema_version": "1",
            "change_id": request.change_id,
            "requirement_digest": request.requirement_digest,
            "gdt": "in-execution",
            "gpm": "select",
            "candidate_test_families": request.candidate_test_families,
            "proposed_test_families": proposed,
            "selected_test_families": selected,
            "quality_goal": quality_goal.model_dump(mode="json"),
            "resolved_budgets": request.budgets.model_dump(mode="json"),
            "policy_resource_id": request.policy_resource_id,
            "policy_digest": request.policy_digest,
            "exploration_ref": stored_exploration.model_dump(mode="json"),
            "impact_inventory_ref": request.impact_inventory_ref.model_dump(mode="json"),
            "resolution_reasons": [reason.model_dump(mode="json") for reason in reasons],
        }
    )


__all__ = [
    "InputError",
    "derive_family_proposal",
    "resolve_families",
    "resolve_plan",
]
