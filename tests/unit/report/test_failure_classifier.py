import pytest

from assurance_agent.workflow.report.failure_classifier import classify_failure


@pytest.mark.parametrize(
    "message,target,expected_category,expected_eligible,expected_review",
    [
        ("Connection refused: cannot connect to server", "api", "environment_failure", False, False),
        ("expected-product-fail: known product issue documented", "api", "known_product_issue", False, False),
        ("Locator resolve failed: waiting for selector '#submit'", "e2e", "locator_failure", True, False),
        ("Timeout exceeded 30000ms waiting for networkidle", "e2e", "wait_strategy_failure", True, False),
        ("Fixture 'seed_user' not found: database empty", "api", "test_data_failure", True, False),
        ("AssertionError: expected 200 received 404", "api", "assertion_failure", False, False),
        ("403 forbidden: permission denied for role", "api", "business_logic_failure", False, False),
        ("TypeError: object is not a function", "api", "test_code_error", True, False),
        ("Step not covered: precondition not met", "api", "case_semantic_failure", False, False),
        ("some totally opaque failure with no signal", "api", "unknown", False, True),
        ("schemathesis: failed to load schema from openapi", "fuzz", "fuzz_configuration_error", False, False),
        ("stateful state machine transition failed", "fuzz", "fuzz_stateful_failure", False, True),
        ("Server error: 503 during generated sequence", "fuzz", "environment_failure", False, False),
        ("500 internal server error on generated input", "fuzz", "business_logic_failure", False, False),
    ],
)
def test_classification_golden(message, target, expected_category, expected_eligible, expected_review) -> None:
    result = classify_failure(message=message, log_excerpt="", target=target)
    assert result.category == expected_category
    assert result.fix_proposal_eligible is expected_eligible
    assert result.needs_review is expected_review


def test_e2e_locator_pattern_does_not_fire_for_api_target() -> None:
    result = classify_failure(message="invalid selector syntax in query", log_excerpt="", target="api")
    assert result.category != "locator_failure"


def test_severity_matches_category() -> None:
    env = classify_failure(message="connection refused", log_excerpt="", target="api")
    assert env.severity == "critical"
    loc = classify_failure(message="element not found", log_excerpt="", target="e2e")
    assert loc.severity == "medium"


def test_anomaly_needs_fact_baseline_context() -> None:
    bare = classify_failure(message="anomaly-7 detected in response", log_excerpt="", target="api")
    assert bare.category != "known_product_issue"
    with_ctx = classify_failure(
        message="anomaly-7 detected", log_excerpt="fact-baseline: documented", target="api"
    )
    assert with_ctx.category == "known_product_issue"
