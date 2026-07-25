import pytest
import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.data_knowledge import AuthLeaf
from assurance_agent.knowledge.capabilities import (
    capabilities_present,
    compute_missing_capabilities,
    is_leaf_present,
)

REVIEW = {
    "schema_version": "1",
    "decision": "pass",
    "findings": [],
    "codegen_readiness": "ready",
}

L1 = yaml.safe_load(
    """
version: 1
accounts: {}
auth:
  api_admin_token:
    method: token
entities: {}
capabilities:
  domain_factories:
    api:
      make_api:
        kind: async_factory
        symbol: tests.testdata.domain.api.make_api
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
"""
)


def test_auth_leaf_without_symbol_counts_as_present() -> None:
    assert is_leaf_present(L1, "auth.api_admin_token") is True
    AuthLeaf.model_validate(L1["auth"]["api_admin_token"])


def test_missing_capability_leaf_detected() -> None:
    review = {
        **REVIEW,
        "required_capabilities": [
            "auth.api_admin_token",
            "capabilities.adapters.api.user.make_user",
        ],
    }
    missing = compute_missing_capabilities(review, L1)
    assert missing == ["capabilities.adapters.api.user.make_user"]


def test_container_path_counts_as_missing() -> None:
    review = {**REVIEW, "required_capabilities": ["capabilities.adapters.api.user"]}
    assert compute_missing_capabilities(review, L1) == ["capabilities.adapters.api.user"]


def test_capabilities_present_true_when_all_leaves_exist() -> None:
    review = {**REVIEW, "required_capabilities": ["auth.api_admin_token"]}
    assert capabilities_present(review, L1) is True


def test_plan_review_route_splits_missing_capabilities() -> None:
    from assurance_agent.knowledge.capabilities import plan_review_route

    assert (
        plan_review_route(
            {
                "gate": {
                    "verdict": "needs_human_review",
                    "details": {"missing_capabilities": ["auth.api_admin_token"]},
                }
            }
        )
        == "knowledge_remediation"
    )
    assert (
        plan_review_route(
            {"gate": {"verdict": "needs_human_review", "details": {"missing_capabilities": []}}}
        )
        == "needs_human_review"
    )
    assert plan_review_route({"gate": {"verdict": "pass", "details": {"missing_capabilities": []}}}) == "pass"


def test_invalid_auth_leaf_is_missing() -> None:
    bad_l1 = yaml.safe_load(yaml.safe_dump(L1))
    bad_l1["auth"]["api_admin_token"] = {"symbol": "x"}
    with pytest.raises(ValidationError):
        AuthLeaf.model_validate(bad_l1["auth"]["api_admin_token"])
    review = {**REVIEW, "required_capabilities": ["auth.api_admin_token"]}
    assert compute_missing_capabilities(review, bad_l1) == ["auth.api_admin_token"]
