from __future__ import annotations

from assurance_improvement.operations.knowledge_merge import merge_l2_into_l1


def test_merge_inserts_a_missing_entity() -> None:
    merged = merge_l2_into_l1(
        {
            "version": 1,
            "journeys": ["dept_management_crud"],
            "entities": {"dept": {"required_fields": ["name"]}},
        },
        {
            "schema_version": "1",
            "mode": "delta",
            "needs_review": ["entities.user"],
            "entities": {"user": {"required_fields": ["username"]}},
        },
    )

    assert merged.changed is True
    assert merged.merged_keys == ["entities.user"]
    assert merged.conflicts == []
    assert merged.merged["journeys"] == ["dept_management_crud"]
    assert merged.merged["entities"]["dept"] == {"required_fields": ["name"]}
    assert merged.merged["entities"]["user"] == {"required_fields": ["username"]}
    assert "needs_review" not in merged.merged


def test_merge_ignores_an_identical_leaf() -> None:
    l1 = {"entities": {"dept": {"required_fields": ["name"]}}}
    merged = merge_l2_into_l1(l1, {"mode": "delta", "entities": {"dept": {"required_fields": ["name"]}}})

    assert merged.changed is False
    assert merged.merged_keys == []
    assert merged.merged == l1


def test_merge_omits_proposal_none_fields_as_identical() -> None:
    l1 = {"entities": {"dept": {"required_fields": ["name"]}}}
    merged = merge_l2_into_l1(
        l1,
        {
            "entities": {
                "dept": {
                    "required_fields": ["name"],
                    "notes": None,
                    "constraints": None,
                }
            }
        },
    )

    assert merged.changed is False
    assert merged.conflicts == []
    assert merged.merged_keys == []
    assert merged.merged["entities"]["dept"] == {"required_fields": ["name"]}


def test_merge_reports_a_different_leaf_as_a_conflict() -> None:
    merged = merge_l2_into_l1(
        {"entities": {"dept": {"required_fields": ["name"]}}},
        {"entities": {"dept": {"required_fields": ["name", "parent_id"]}}},
    )

    assert merged.changed is False
    assert merged.merged_keys == []
    assert merged.merged["entities"]["dept"]["required_fields"] == ["name"]
    assert [item.key for item in merged.conflicts] == ["entities.dept"]


def test_merge_force_overwrites_a_different_leaf() -> None:
    merged = merge_l2_into_l1(
        {"entities": {"dept": {"required_fields": ["name"]}}},
        {"entities": {"dept": {"required_fields": ["name", "parent_id"]}}},
        force=True,
    )

    assert merged.conflicts == []
    assert merged.merged_keys == ["entities.dept"]
    assert merged.merged["entities"]["dept"]["required_fields"] == ["name", "parent_id"]


def test_merge_collects_auth_matrix_and_capability_leaves() -> None:
    merged = merge_l2_into_l1(
        {"version": 1},
        {
            "auth_matrix": {
                "dept_create_deny": {
                    "route": "/api/v1/dept/create",
                    "method": "POST",
                    "token": "api_limited_role_user_token",
                    "expected": "deny",
                    "allowed_status_codes": [403],
                }
            },
            "capabilities": {
                "domain_factories": {
                    "user": {
                        "make_user": {
                            "kind": "async_factory",
                            "symbol": "tests.testdata.domain.user.make_user",
                        }
                    }
                }
            },
        },
    )

    assert merged.merged_keys == [
        "auth_matrix.dept_create_deny",
        "capabilities.domain_factories.user.make_user",
    ]
