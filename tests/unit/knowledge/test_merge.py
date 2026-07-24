import yaml

from assurance_agent.knowledge.merge import merge_l2_into_l1, strip_proposal_metadata

L1 = {
    "version": 1,
    "accounts": {},
    "auth": {},
    "entities": {},
    "capabilities": {
        "domain_factories": {},
        "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
        "cleanup": {},
    },
}

PROPOSAL = yaml.safe_load(
    """
schema_version: "1"
based_on_l1_version: 1
mode: delta
auth:
  api_admin_token:
    method: token
    symbol: tests.api.conftest.admin_token
capabilities:
  domain_factories:
    menu:
      make_menu:
        kind: async_factory
        symbol: tests.testdata.domain.menu.make_menu
discovered_candidates: []
needs_review: []
promotion_checklist: []
"""
)


def test_strip_proposal_metadata_removes_l2_only_fields() -> None:
    stripped = strip_proposal_metadata(PROPOSAL)
    assert "schema_version" not in stripped
    assert "discovered_candidates" not in stripped
    assert stripped["auth"]["api_admin_token"]["method"] == "token"


def test_merge_adds_new_leaf_keys() -> None:
    result = merge_l2_into_l1(L1, PROPOSAL)
    assert result.changed is True
    assert "auth.api_admin_token" in result.merged_keys
    assert result.merged["auth"]["api_admin_token"]["symbol"] == "tests.api.conftest.admin_token"


def test_merge_is_idempotent_for_equal_keys() -> None:
    first = merge_l2_into_l1(L1, PROPOSAL)
    second = merge_l2_into_l1(first.merged, PROPOSAL)
    assert second.changed is False
    assert second.conflicts == []


def test_merge_records_conflicts_without_force() -> None:
    l1 = merge_l2_into_l1(L1, PROPOSAL).merged
    conflicting = yaml.safe_load(yaml.safe_dump(PROPOSAL))
    conflicting["auth"]["api_admin_token"]["symbol"] = "tests.other.token"
    result = merge_l2_into_l1(l1, conflicting)
    assert result.changed is False
    assert len(result.conflicts) == 1
    assert result.conflicts[0].key == "auth.api_admin_token"


def test_merge_force_overrides_conflicts() -> None:
    l1 = merge_l2_into_l1(L1, PROPOSAL).merged
    conflicting = yaml.safe_load(yaml.safe_dump(PROPOSAL))
    conflicting["auth"]["api_admin_token"]["symbol"] = "tests.other.token"
    result = merge_l2_into_l1(l1, conflicting, force=True)
    assert result.changed is True
    assert result.conflicts == []
    assert result.merged["auth"]["api_admin_token"]["symbol"] == "tests.other.token"
