"""Plan-check registry; document check ordering is defined by ``PLAN_CHECK_IDS``, not this mapping's declaration order."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS, PlanCheckId
from assurance_agent.artifacts.models.plan_checks import (
    CheckEvidence,
    LayerApplicability,
    PlanCheckDocument,
)
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.assert_ideal import check_assert_ideal
from assurance_agent.verification.checks.base import CheckContext, CheckFn
from assurance_agent.verification.checks.capability_keys import check_capability_keys
from assurance_agent.verification.checks.l1_path import check_l1_path
from assurance_agent.verification.checks.shared_factory import check_shared_factory
from assurance_agent.verification.profiles import LayerAssuranceProfile, get_layer_assurance_profile

CHECKS_BY_ID: Mapping[PlanCheckId, CheckFn] = MappingProxyType(
    {
        "l1_path": check_l1_path,
        "shared_factory": check_shared_factory,
        "assert_ideal": check_assert_ideal,
        "capability_keys": check_capability_keys,
    }
)

# Retained for callers/tests that still import a plain function tuple.
PLAN_CHECKS: tuple[CheckFn, ...] = tuple(CHECKS_BY_ID[check_id] for check_id in PLAN_CHECK_IDS)


def run_plan_checks(
    ctx: CheckContext, *, applicability: LayerApplicability | None = None
) -> PlanCheckDocument:
    profile = get_layer_assurance_profile(ctx.layer)
    ctx = replace(ctx, review_artifact=profile.review_artifact)
    if applicability is None:
        applicability = derive_layer_applicability(ctx.cases, profile)
    elif applicability.layer != profile.layer:
        raise ValueError(
            f"applicability layer {applicability.layer!r} does not match context layer {profile.layer!r}"
        )
    document = _run_profile_checks(ctx, profile, applicability, CHECKS_BY_ID)
    return validate_plan_check_document(document, profile)


def _run_profile_checks(
    ctx: CheckContext,
    profile: LayerAssuranceProfile,
    applicability: LayerApplicability,
    checks_by_id: Mapping[PlanCheckId, CheckFn],
) -> PlanCheckDocument:
    if applicability.applicable:
        for path in profile.plan_artifacts:
            if path not in ctx.plan_texts:
                raise ValueError(f"missing plan artifact for layer {profile.layer!r}: {path}")

    checks: list[CheckEvidence] = []
    for check_id in PLAN_CHECK_IDS:
        if not applicability.applicable:
            checks.append(
                CheckEvidence(
                    check_id=check_id,
                    status="not_applicable",
                    applicability_reason="layer_not_applicable",
                )
            )
            continue
        if check_id not in profile.applicable_check_ids:
            checks.append(
                CheckEvidence(
                    check_id=check_id,
                    status="not_applicable",
                    applicability_reason="check_not_in_profile",
                )
            )
            continue
        check_fn = checks_by_id[check_id]
        result = check_fn(ctx)
        if result.check_id != check_id:
            raise ValueError(f"check registered under {check_id!r} returned check_id {result.check_id!r}")
        checks.append(result)

    return PlanCheckDocument.from_checks(layer=profile.layer, applicability=applicability, checks=checks)


def validate_plan_check_document(
    document: PlanCheckDocument, profile: LayerAssuranceProfile
) -> PlanCheckDocument:
    if document.layer != profile.layer:
        raise ValueError(f"document layer {document.layer!r} does not match profile layer {profile.layer!r}")
    applicability = document.applicability
    if applicability is None or applicability.layer != profile.layer:
        raise ValueError("document applicability layer does not match profile layer")

    checks_by_id = {check.check_id: check for check in document.checks}
    for check_id in PLAN_CHECK_IDS:
        check = checks_by_id.get(check_id)
        if check is None:
            raise ValueError(f"document is missing check {check_id!r}")
        if not applicability.applicable:
            if check.status != "not_applicable" or check.applicability_reason != "layer_not_applicable":
                raise ValueError(
                    f"check {check_id!r} must be not_applicable/layer_not_applicable "
                    "for an inapplicable layer"
                )
            continue
        if check_id not in profile.applicable_check_ids:
            if check.status != "not_applicable" or check.applicability_reason != "check_not_in_profile":
                raise ValueError(
                    f"check {check_id!r} must be not_applicable/check_not_in_profile per the static profile"
                )
        elif check.status == "not_applicable":
            raise ValueError(f"check {check_id!r} is applicable and must not be not_applicable")
    return document
