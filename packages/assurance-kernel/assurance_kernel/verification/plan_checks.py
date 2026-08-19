"""Build a PlanCheckDocument from in-memory plan, review, and case payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from assurance_kernel.artifacts.models.data_knowledge import DataKnowledge
from assurance_kernel.artifacts.models.plan_checks import PlanCheckDocument
from assurance_kernel.artifacts.models.review import PlanReview
from assurance_kernel.verification.applicability import derive_layer_applicability
from assurance_kernel.verification.checks.base import CheckContext
from assurance_kernel.verification.checks.registry import run_plan_checks, validate_plan_check_document
from assurance_kernel.verification.profiles import get_layer_assurance_profile


def run_layer_plan_checks(
    *,
    layer: str,
    cases: Sequence[Mapping[str, object]],
    plan_texts: Mapping[str, str] | None = None,
    review_payload: Mapping[str, object] | None = None,
    data_knowledge: Mapping[str, object] | None = None,
    change_id: str | None = None,
    require_review: bool = False,
) -> PlanCheckDocument:
    """Run the layer's mechanical plan checks and return the evidence document.

    Does not read policy and does not decide block/warn/require_human.
    """
    profile = get_layer_assurance_profile(layer)
    applicability = derive_layer_applicability(cases, profile)
    if not applicability.applicable:
        return run_plan_checks(
            CheckContext(
                plan_texts={},
                cases=cases,
                data_knowledge={},
                layer=profile.layer,
            ),
            applicability=applicability,
        )

    texts = dict(plan_texts or {})
    if require_review:
        if review_payload is None:
            raise ValueError(f"missing review artifact: {profile.review_artifact}")
        review = profile.review_model.model_validate(review_payload)
        if not isinstance(review, PlanReview):
            raise ValueError("review-required mode requires PlanReview")
        if review.review_type != f"{profile.layer}-plan":
            raise ValueError("review_type does not match layer")
        if change_id is not None and review.change_id != change_id:
            raise ValueError("review change_id does not match runtime change")
        if data_knowledge is None:
            raise ValueError("missing repo L1 artifact: .aa/data-knowledge.yaml")
        knowledge = DataKnowledge.model_validate(data_knowledge)
        required_capabilities = tuple(review.required_capabilities or ())
        document = run_plan_checks(
            CheckContext(
                plan_texts=texts,
                cases=cases,
                data_knowledge=knowledge.model_dump(mode="json"),
                layer=profile.layer,
                required_capabilities=required_capabilities,
            ),
            applicability=applicability,
        )
        return validate_plan_check_document(document, profile)

    required_capabilities: tuple[str, ...] = ()
    if review_payload is not None:
        profile.review_model.model_validate(review_payload)
        required = review_payload.get("required_capabilities", [])
        if not isinstance(required, list) or not all(
            isinstance(item, str) and item.strip() for item in required
        ):
            raise ValueError("required_capabilities must be a list of non-empty strings")
        required_capabilities = tuple(required)
    if data_knowledge is None:
        raise ValueError("missing repo L1 artifact: .aa/data-knowledge.yaml")
    return run_plan_checks(
        CheckContext(
            plan_texts=texts,
            cases=cases,
            data_knowledge=data_knowledge,
            layer=profile.layer,
            required_capabilities=required_capabilities,
        ),
        applicability=applicability,
    )


__all__ = ["run_layer_plan_checks"]
