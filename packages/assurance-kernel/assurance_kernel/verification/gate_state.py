"""Pure plan-assurance evidence validation for gate DSL builtins."""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from assurance_kernel.artifacts.models.data_knowledge import DataKnowledge
from assurance_kernel.artifacts.models.plan_checks import PlanCheckDocument
from assurance_kernel.artifacts.models.review import PlanReview
from assurance_kernel.verification.checks.registry import validate_plan_check_document
from assurance_kernel.verification.profiles import get_layer_assurance_profile

PlanAssuranceState = Literal["invalid", "not_applicable", "applicable"]


def plan_assurance_state(
    checks_doc: object,
    review_doc: object,
    data_knowledge_doc: object,
    layer: object,
    *,
    change_id: str,
) -> PlanAssuranceState:
    if not isinstance(layer, str):
        return "invalid"
    try:
        profile = get_layer_assurance_profile(layer)
    except ValueError:
        return "invalid"

    if not isinstance(checks_doc, dict):
        return "invalid"
    try:
        document = PlanCheckDocument.model_validate(checks_doc)
    except (ValidationError, ValueError):
        return "invalid"
    if document.schema_version != "2":
        return "invalid"
    if document.layer != profile.layer:
        return "invalid"
    try:
        validate_plan_check_document(document, profile)
    except ValueError:
        return "invalid"

    applicability = document.applicability
    if applicability is None or not applicability.applicable:
        return "not_applicable"

    if not isinstance(review_doc, dict):
        return "invalid"
    try:
        review = profile.review_model.model_validate(review_doc)
    except (ValidationError, ValueError):
        return "invalid"
    if profile.review_model is PlanReview:
        if not isinstance(review, PlanReview):
            return "invalid"
        if review.review_type != f"{profile.layer}-plan":
            return "invalid"
        if review.change_id != change_id:
            return "invalid"

    if not isinstance(data_knowledge_doc, dict):
        return "invalid"
    try:
        DataKnowledge.model_validate(data_knowledge_doc)
    except (ValidationError, ValueError):
        return "invalid"

    return "applicable"
