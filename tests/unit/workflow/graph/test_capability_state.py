import pytest

from assurance_agent.artifacts.models import Review
from assurance_agent.artifacts.registry import ArtifactSpec
from assurance_agent.workflow.graph.capability_state import (
    DEFAULT_OPERATION_NAMES,
    CapabilityCatalog,
    CapabilityCatalogError,
    CapabilityView,
    assert_capability_catalog_compatible,
    compute_capability_catalog_digest,
    current_validator_ids,
    default_capability_catalog_digest,
    install_capability_view,
    reset_capability_view,
)
from assurance_agent.workflow.graph.precommit import (
    KNOWN_PRECOMMIT_VALIDATORS,
    PLAN_MECHANICAL_CANDIDATE_V1,
)
from assurance_agent.workflow.graph.runtime import CapabilityCatalogDrift


def test_duplicate_operation_registration_fails() -> None:
    catalog = CapabilityCatalog()
    catalog.register_operation("operation:stop")
    with pytest.raises(CapabilityCatalogError, match="duplicate operation"):
        catalog.register_operation("operation:stop")


def test_duplicate_validator_registration_fails() -> None:
    catalog = CapabilityCatalog()
    catalog.register_validator(PLAN_MECHANICAL_CANDIDATE_V1)
    with pytest.raises(CapabilityCatalogError, match="duplicate validator"):
        catalog.register_validator(PLAN_MECHANICAL_CANDIDATE_V1)


def test_duplicate_artifact_pattern_fails() -> None:
    spec = ArtifactSpec(
        artifact_type="plan_review",
        pattern="review/api-plan-review.json",
        model=Review,
        compat="must_compat",
    )
    catalog = CapabilityCatalog()
    catalog.register_artifact(spec)
    with pytest.raises(CapabilityCatalogError, match="duplicate artifact"):
        catalog.register_artifact(spec)


def test_register_after_freeze_fails() -> None:
    catalog = CapabilityCatalog()
    catalog.register_operation("operation:stop")
    catalog.freeze()
    with pytest.raises(CapabilityCatalogError, match="frozen"):
        catalog.register_operation("operation:no-op")


def test_digest_is_stable_and_order_independent() -> None:
    spec = ArtifactSpec(
        artifact_type="plan_review",
        pattern="review/api-plan-review.json",
        model=Review,
        compat="must_compat",
    )
    a = CapabilityCatalog()
    a.register_operation("operation:stop")
    a.register_operation("operation:no-op")
    a.register_validator(PLAN_MECHANICAL_CANDIDATE_V1)
    a.register_artifact(spec)
    b = CapabilityCatalog()
    b.register_artifact(spec)
    b.register_validator(PLAN_MECHANICAL_CANDIDATE_V1)
    b.register_operation("operation:no-op")
    b.register_operation("operation:stop")
    assert a.freeze().digest == b.freeze().digest
    assert a.freeze().digest == compute_capability_catalog_digest(
        operations=("operation:no-op", "operation:stop"),
        validators=(PLAN_MECHANICAL_CANDIDATE_V1,),
        artifacts=(spec,),
    )


def test_unset_view_uses_known_precommit_validators() -> None:
    reset_capability_view()
    assert current_validator_ids() == KNOWN_PRECOMMIT_VALIDATORS


def test_empty_view_hides_known_validators() -> None:
    empty = CapabilityCatalog().freeze()
    install_capability_view(empty)
    try:
        assert current_validator_ids() == frozenset()
        assert PLAN_MECHANICAL_CANDIDATE_V1 not in current_validator_ids()
    finally:
        reset_capability_view()


def test_empty_pin_allows_default_catalog() -> None:
    reset_capability_view()
    assert_capability_catalog_compatible("")


def test_empty_pin_rejects_non_default_catalog() -> None:
    empty = CapabilityCatalog().freeze()
    install_capability_view(empty)
    try:
        with pytest.raises(CapabilityCatalogDrift, match="capability_catalog_digest"):
            assert_capability_catalog_compatible("")
    finally:
        reset_capability_view()


def test_matching_pin_allows_resume() -> None:
    digest = default_capability_catalog_digest()
    view = CapabilityView(
        operation_names=DEFAULT_OPERATION_NAMES,
        validator_ids=KNOWN_PRECOMMIT_VALIDATORS,
        digest=digest,
    )
    install_capability_view(view)
    try:
        assert_capability_catalog_compatible(digest)
    finally:
        reset_capability_view()


def test_mismatched_pin_rejects_resume() -> None:
    digest = default_capability_catalog_digest()
    view = CapabilityView(
        operation_names=DEFAULT_OPERATION_NAMES,
        validator_ids=KNOWN_PRECOMMIT_VALIDATORS,
        digest=digest,
    )
    install_capability_view(view)
    try:
        with pytest.raises(CapabilityCatalogDrift, match="capability_catalog_digest"):
            assert_capability_catalog_compatible("0" * 64)
    finally:
        reset_capability_view()
