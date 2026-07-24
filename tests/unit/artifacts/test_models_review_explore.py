import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import Advisory, FactBaseline, Review


def make_review(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "review_type": "api-plan",
        "change_id": "CH-1",
        "decision": "pass",
        "risk_level": "low",
        "codegen_readiness": "ready",
        "auto_fix_allowed": False,
        "human_review_required": False,
        "summary": "Short review summary.",
        "findings": [],
        "required_capabilities": ["auth.api_admin_token"],
        "next_action": "continue",
    }
    doc.update(overrides)
    return doc


def make_advisory(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "context_ref": "explore/context.json",
        "generated_at": "2026-07-15T00:00:00Z",
        "executive_summary": "risk overview",
        "watchlist": [{"area": "auth"}],
        "evidence_inventory": {},
        "case_design_guidance": {},
        "minimum_required_coverage": {},
        "open_questions_for_case_design": [],
    }
    doc.update(overrides)
    return doc


def test_review_valid_full_fixture_parses_and_keeps_extras() -> None:
    model = Review.model_validate(make_review())
    assert model.decision == "pass"
    assert model.codegen_readiness == "ready"
    assert model.auto_fix_allowed is False
    assert model.human_review_required is False
    assert model.risk_level == "low"
    assert model.findings == []
    assert model.review_type == "api-plan"
    assert model.required_capabilities == ["auth.api_admin_token"]


def test_review_minimal_required_fields() -> None:
    model = Review.model_validate(
        {"schema_version": "1.0", "decision": "needs_fix", "findings": [{"id": "F1"}]}
    )
    assert model.auto_fix_allowed is None
    assert model.codegen_readiness is None
    assert model.review_type is None


@pytest.mark.parametrize("review_type", ["api-plan", "e2e-plan"])
def test_plan_review_missing_required_capabilities_fails(review_type: str) -> None:
    doc = make_review(review_type=review_type)
    del doc["required_capabilities"]
    with pytest.raises(ValidationError, match="required_capabilities"):
        Review.model_validate(doc)


@pytest.mark.parametrize("review_type", ["api-plan", "e2e-plan"])
def test_plan_review_empty_required_capabilities_fails(review_type: str) -> None:
    with pytest.raises(ValidationError, match="required_capabilities"):
        Review.model_validate(make_review(review_type=review_type, required_capabilities=[]))


@pytest.mark.parametrize("review_type", ["api-plan", "e2e-plan"])
def test_plan_review_null_required_capabilities_fails(review_type: str) -> None:
    with pytest.raises(ValidationError, match="required_capabilities"):
        Review.model_validate(make_review(review_type=review_type, required_capabilities=None))


def test_plan_review_blank_capability_item_fails() -> None:
    with pytest.raises(ValidationError, match="non-empty leaf key"):
        Review.model_validate(make_review(required_capabilities=["auth.api_admin_token", "  "]))


@pytest.mark.parametrize("review_type", ["case", "fuzz-plan", "performance-plan", None])
def test_non_plan_reviews_do_not_require_capabilities(review_type: str | None) -> None:
    doc = make_review(review_type=review_type)
    del doc["required_capabilities"]
    model = Review.model_validate(doc)
    assert model.required_capabilities is None


def test_review_decision_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        Review.model_validate(make_review(decision="maybe"))


def test_review_missing_findings_fails() -> None:
    doc = make_review()
    del doc["findings"]
    with pytest.raises(ValidationError):
        Review.model_validate(doc)


def test_review_codegen_readiness_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        Review.model_validate(make_review(codegen_readiness="almost_ready"))


def test_advisory_valid_fixture_parses() -> None:
    model = Advisory.model_validate(make_advisory())
    assert model.schema_version == "1.0"
    assert model.watchlist == [{"area": "auth"}]
    assert model.open_questions_for_case_design == []


def test_advisory_missing_watchlist_fails() -> None:
    doc = make_advisory()
    del doc["watchlist"]
    with pytest.raises(ValidationError):
        Advisory.model_validate(doc)


def test_fact_baseline_unavailable_variant_parses() -> None:
    model = FactBaseline.model_validate(
        {"source": "unavailable", "facts": None, "warnings": ["db unreachable"]}
    )
    assert model.root.source == "unavailable"


def test_fact_baseline_full_variant_requires_schema_version() -> None:
    with pytest.raises(ValidationError):
        FactBaseline.model_validate({"source": "db_probe", "warnings": []})
    model = FactBaseline.model_validate(
        {"source": "db_probe", "schema_version": "1.0", "warnings": [], "facts": {"users": 3}}
    )
    assert model.root.source == "db_probe"


def test_fact_baseline_unknown_source_fails() -> None:
    with pytest.raises(ValidationError):
        FactBaseline.model_validate({"source": "guesswork", "warnings": []})
