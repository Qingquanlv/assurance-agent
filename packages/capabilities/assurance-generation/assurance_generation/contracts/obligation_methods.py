"""Pure predicates over frozen obligation methods, bindings, and reviews."""

from __future__ import annotations

from assurance_generation.contracts.plans import ObservationBindingV1
from assurance_generation.contracts.reviews import ObligationSemanticReviewV1
from assurance_intake.contracts.obligations import PreparedObligationV1, VerificationRequirementV1


def required_observation_keys(requirement: VerificationRequirementV1) -> frozenset[str]:
    return frozenset(item.observation_key for item in requirement.observations)


def validate_observation_binding(
    requirement: VerificationRequirementV1,
    bindings: tuple[ObservationBindingV1, ...],
) -> None:
    required = required_observation_keys(requirement)
    actual_keys = {item.observation_key for item in bindings}
    actual_ids = [item.observation_id for item in bindings]
    if len(actual_ids) != len(set(actual_ids)) or actual_keys != required:
        raise ValueError("observation bindings must cover frozen semantic requirements")


def expectation_ready(
    obligation: PreparedObligationV1,
    requirement: VerificationRequirementV1,
    observation_key: str,
    review: ObligationSemanticReviewV1 | None,
) -> bool:
    if review is None or obligation.open_questions:
        return False
    observation = next(
        (item for item in requirement.observations if item.observation_key == observation_key),
        None,
    )
    if observation is None or observation.expected is None or not observation.basis_refs:
        return False
    authenticated = {
        (basis.source.kind, basis.source.artifact.path, basis.source.artifact.digest, basis.source.locator)
        for basis in obligation.expected_basis_refs
        if basis.source_status == "authenticated"
    }
    if any(
        (ref.kind, ref.artifact.path, ref.artifact.digest, ref.locator) not in authenticated
        for ref in observation.basis_refs
    ):
        return False
    matches = [item for item in review.expectation_reviews if item.observation_key == observation_key]
    if len(matches) != 1 or matches[0].status != "pass" or not matches[0].reason.strip():
        return False
    observed = {
        (ref.kind, ref.artifact.path, ref.artifact.digest, ref.locator) for ref in observation.basis_refs
    }
    if not matches[0].basis_refs or any(
        (ref.kind, ref.artifact.path, ref.artifact.digest, ref.locator) not in observed
        for ref in matches[0].basis_refs
    ):
        return False
    return review.status == "pass" and review.requirement_id == requirement.requirement_id


__all__ = [
    "expectation_ready",
    "required_observation_keys",
    "validate_observation_binding",
]
