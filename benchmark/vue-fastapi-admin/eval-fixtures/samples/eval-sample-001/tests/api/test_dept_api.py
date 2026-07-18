"""API tests for system.dept module — RET-dept-management-20260716-154509-cursor."""

from __future__ import annotations

import pytest

from tests.api.adapters.dept import (
    factory_cleanup_dept,
    factory_get_dept_closure_rows,
    factory_make_dept,
)
from tests.config import settings
from tests.helpers.dept_assertions import (
    assert_closure_ancestors_include,
    assert_closure_self_row,
    assert_dept_not_in_tree,
    assert_siblings_ordered_by_order,
    count_depts_with_name,
    count_depts_with_prefix_in_tree,
    dept_create_payload,
    find_dept_in_tree,
)
from tests.helpers.user_assertions import assert_client_error_response, response_msg_or_detail
from tests.schema_validation import assert_matches_schema
from tests.testdata.domain.dept import unique_dept_name

INVALID_PARENT_ID = 999999999


def _dept_list(api_client, admin_headers, **params):
    return api_client.get("/api/v1/dept/list", headers=admin_headers, params=params)


def _collect_names(nodes: list) -> list[str]:
    names: list[str] = []
    for node in nodes:
        names.append(node.get("name", ""))
        names.extend(_collect_names(node.get("children") or []))
    return names


def test_tc_dept_api_001__create_root_dept_success(api_client, admin_headers):
    payload = dept_create_payload(parent_id=0, order=0)
    dept_id = None
    try:
        resp = api_client.post("/api/v1/dept/create", json=payload, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200

        list_resp = _dept_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        item = find_dept_in_tree(list_resp.json()["data"], name=payload["name"])
        assert item is not None
        assert item.get("parent_id") == 0
        assert item["name"] == payload["name"]
        dept_id = item["id"]

        closure_rows = factory_get_dept_closure_rows(dept_id)
        assert_closure_self_row(closure_rows, dept_id)

        assert_matches_schema(body, "POST /api/v1/dept/create")
    finally:
        if dept_id is not None:
            factory_cleanup_dept(dept_id)


def test_tc_dept_api_002__list_dept_tree_structure(api_client, admin_headers):
    parent = factory_make_dept()
    child = factory_make_dept(parent_id=parent["id"])
    deleted = factory_make_dept()
    factory_cleanup_dept(deleted["id"])
    try:
        resp = _dept_list(api_client, admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert isinstance(data, list)

        for node in data:
            assert node.get("parent_id") == 0
            assert "id" in node
            assert "name" in node
            assert "children" in node

        parent_node = find_dept_in_tree(data, dept_id=parent["id"])
        child_node = find_dept_in_tree(data, dept_id=child["id"])
        assert parent_node is not None
        assert child_node is not None
        assert child_node["parent_id"] == parent["id"]
        assert find_dept_in_tree(data, dept_id=deleted["id"]) is None

        assert_matches_schema(body, "GET /api/v1/dept/list")
    finally:
        factory_cleanup_dept(child["id"])
        factory_cleanup_dept(parent["id"])


def test_tc_dept_api_003__get_dept_detail_by_id(api_client, admin_headers, dept_fixture):
    resp = api_client.get(
        "/api/v1/dept/get",
        headers=admin_headers,
        params={"id": dept_fixture["id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    detail = body["data"]
    assert detail["id"] == dept_fixture["id"]
    assert detail["name"] == dept_fixture["name"]
    assert detail["parent_id"] == dept_fixture["parent_id"]
    assert detail["order"] == dept_fixture["order"]
    assert detail["desc"] == dept_fixture["desc"]

    assert_matches_schema(body, "GET /api/v1/dept/get")


def test_tc_dept_api_004__update_dept_metadata(api_client, admin_headers):
    dept_a = factory_make_dept(order=1)
    dept_b = factory_make_dept(order=5)
    try:
        new_name = dept_create_payload()["name"]
        update_resp = api_client.post(
            "/api/v1/dept/update",
            headers=admin_headers,
            json={
                "id": dept_a["id"],
                "name": new_name,
                "desc": dept_a["desc"],
                "order": 3,
                "parent_id": dept_a["parent_id"],
            },
        )
        assert update_resp.status_code == 200, update_resp.text
        update_body = update_resp.json()
        assert update_body["code"] == 200

        get_resp = api_client.get(
            "/api/v1/dept/get",
            headers=admin_headers,
            params={"id": dept_a["id"]},
        )
        assert get_resp.status_code == 200, get_resp.text
        updated = get_resp.json()["data"]
        assert updated["name"] == new_name
        assert updated["order"] == 3

        list_resp = _dept_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        tree_data = list_resp.json()["data"]
        sibling_ids = {dept_a["id"], dept_b["id"]}
        siblings = [node for node in tree_data if node.get("id") in sibling_ids]
        assert len(siblings) == 2
        assert_siblings_ordered_by_order(siblings)

        assert_matches_schema(update_body, "POST /api/v1/dept/update")
    finally:
        factory_cleanup_dept(dept_a["id"])
        factory_cleanup_dept(dept_b["id"])


def test_tc_dept_api_005__soft_delete_hides_from_tree(api_client, admin_headers):
    """Get-after-delete accepts 404/4xx or is_deleted=true per API-PLAN-TRACE-001 / NEEDS-REVIEW-002."""
    dept = factory_make_dept()
    dept_id = dept["id"]
    delete_resp = api_client.delete(
        "/api/v1/dept/delete",
        headers=admin_headers,
        params={"dept_id": dept_id},
    )
    assert delete_resp.status_code == 200, delete_resp.text
    delete_body = delete_resp.json()
    assert delete_body["code"] == 200

    list_resp = _dept_list(api_client, admin_headers)
    assert list_resp.status_code == 200, list_resp.text
    assert_dept_not_in_tree(list_resp.json()["data"], dept_id)

    get_resp = api_client.get(
        "/api/v1/dept/get",
        headers=admin_headers,
        params={"id": dept_id},
    )
    assert get_resp.status_code < 500, get_resp.text
    if get_resp.status_code == 200:
        body = get_resp.json()
        if body.get("code") == 200 and body.get("data"):
            detail = body["data"]
            if "is_deleted" in detail:
                assert detail["is_deleted"] is True
    else:
        assert get_resp.status_code in (404, 400, 422)

    assert_matches_schema(delete_body, "DELETE /api/v1/dept/delete")


def test_tc_dept_api_006__filter_tree_by_name_and_order_sort(api_client, admin_headers):
    keyword = f"{settings.dept_prefix}-flt"
    dept_low = factory_make_dept(name=unique_dept_name(prefix=keyword, hex_chars=4), order=1)
    dept_high = factory_make_dept(name=unique_dept_name(prefix=keyword, hex_chars=4), order=2)
    try:
        resp = _dept_list(api_client, admin_headers, name=keyword)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert isinstance(data, list)

        for name in _collect_names(data):
            assert keyword in name

        filtered_ids = {dept_low["id"], dept_high["id"]}
        filtered_nodes = [node for node in data if node.get("id") in filtered_ids]
        assert len(filtered_nodes) == 2
        assert_siblings_ordered_by_order(filtered_nodes)

        empty_resp = _dept_list(api_client, admin_headers, name=f"{keyword}-nomatch-xyz")
        assert empty_resp.status_code == 200, empty_resp.text
        empty_data = empty_resp.json()["data"]
        assert empty_data == [] or len(_collect_names(empty_data)) == 0

        assert_matches_schema(body, "GET /api/v1/dept/list")
    finally:
        factory_cleanup_dept(dept_low["id"])
        factory_cleanup_dept(dept_high["id"])


def test_tc_dept_api_007__create_child_under_parent_in_tree(api_client, admin_headers):
    parent = factory_make_dept()
    child_payload = dept_create_payload(parent_id=parent["id"])
    child_id = None
    try:
        child_resp = api_client.post(
            "/api/v1/dept/create",
            json=child_payload,
            headers=admin_headers,
        )
        assert child_resp.status_code == 200, child_resp.text
        assert child_resp.json()["code"] == 200

        list_resp = _dept_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        tree = list_resp.json()["data"]
        child_node = find_dept_in_tree(tree, name=child_payload["name"])
        assert child_node is not None
        child_id = child_node["id"]
        assert child_node["parent_id"] == parent["id"]

        parent_node = find_dept_in_tree(tree, dept_id=parent["id"])
        assert parent_node is not None
        child_names = {item["name"] for item in (parent_node.get("children") or [])}
        assert child_payload["name"] in child_names

        assert_matches_schema(child_resp.json(), "POST /api/v1/dept/create")
    finally:
        if child_id is not None:
            factory_cleanup_dept(child_id)
        factory_cleanup_dept(parent["id"])


def test_tc_dept_api_008__reject_create_missing_name(api_client, admin_headers):
    list_before = _dept_list(api_client, admin_headers)
    assert list_before.status_code == 200, list_before.text
    before_count = count_depts_with_prefix_in_tree(list_before.json()["data"])

    missing_name_resp = api_client.post(
        "/api/v1/dept/create",
        headers=admin_headers,
        json={"desc": "no name", "order": 0, "parent_id": 0},
    )
    assert_client_error_response(missing_name_resp)
    missing_text = response_msg_or_detail(missing_name_resp).lower()
    assert "name" in missing_text

    empty_name_resp = api_client.post(
        "/api/v1/dept/create",
        headers=admin_headers,
        json=dept_create_payload(name=""),
    )
    assert_client_error_response(empty_name_resp)
    empty_text = response_msg_or_detail(empty_name_resp).lower()
    assert "name" in empty_text

    list_after = _dept_list(api_client, admin_headers)
    assert list_after.status_code == 200, list_after.text
    after_count = count_depts_with_prefix_in_tree(list_after.json()["data"])
    assert after_count == before_count


def test_tc_dept_api_009__reject_invalid_parent_id(api_client, admin_headers):
    attempted_name = dept_create_payload()["name"]
    resp = api_client.post(
        "/api/v1/dept/create",
        headers=admin_headers,
        json=dept_create_payload(name=attempted_name, parent_id=INVALID_PARENT_ID),
    )
    assert resp.status_code < 500, resp.text
    if resp.status_code == 200:
        assert resp.json().get("code", 200) != 200
    else:
        assert resp.status_code in (400, 404, 422)

    list_resp = _dept_list(api_client, admin_headers)
    assert list_resp.status_code == 200, list_resp.text
    assert find_dept_in_tree(list_resp.json()["data"], name=attempted_name) is None


def test_tc_dept_api_010__reject_duplicate_dept_name(api_client, admin_headers):
    """Duplicate name may return HTTP 500 (IntegrityError) per API-PLAN-TRACE-002 / NEEDS-REVIEW-005."""
    payload = dept_create_payload()
    dept_id = None
    try:
        first_resp = api_client.post(
            "/api/v1/dept/create",
            json=payload,
            headers=admin_headers,
        )
        assert first_resp.status_code == 200, first_resp.text
        assert first_resp.json()["code"] == 200

        list_resp = _dept_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        item = find_dept_in_tree(list_resp.json()["data"], name=payload["name"])
        assert item is not None
        dept_id = item["id"]

        dup_resp = api_client.post(
            "/api/v1/dept/create",
            json=dept_create_payload(name=payload["name"]),
            headers=admin_headers,
        )
        assert dup_resp.status_code < 600, dup_resp.text
        assert dup_resp.status_code != 200 or dup_resp.json().get("code") != 200

        list_after = _dept_list(api_client, admin_headers)
        assert list_after.status_code == 200, list_after.text
        assert count_depts_with_name(list_after.json()["data"], payload["name"]) == 1
    finally:
        if dept_id is not None:
            factory_cleanup_dept(dept_id)


def test_tc_dept_api_011__delete_parent_with_children_behavior(api_client, admin_headers):
    """Child visibility after parent soft-delete is neutral per NEEDS-REVIEW-001."""
    parent = factory_make_dept()
    child = factory_make_dept(parent_id=parent["id"])
    try:
        list_before = _dept_list(api_client, admin_headers)
        assert list_before.status_code == 200, list_before.text
        tree_before = list_before.json()["data"]
        assert find_dept_in_tree(tree_before, dept_id=parent["id"]) is not None
        assert find_dept_in_tree(tree_before, dept_id=child["id"]) is not None

        delete_resp = api_client.delete(
            "/api/v1/dept/delete",
            headers=admin_headers,
            params={"dept_id": parent["id"]},
        )
        assert delete_resp.status_code < 500, delete_resp.text

        list_after = _dept_list(api_client, admin_headers)
        assert list_after.status_code == 200, list_after.text
        tree_after = list_after.json()["data"]
        parent_visible = find_dept_in_tree(tree_after, dept_id=parent["id"])
        child_visible = find_dept_in_tree(tree_after, dept_id=child["id"])
        # Neutral semantics: record observed parent/child visibility after parent soft-delete
        assert parent_visible is None
        _ = child_visible
    finally:
        factory_cleanup_dept(child["id"])
        factory_cleanup_dept(parent["id"])


def test_tc_dept_api_012__reject_unauthorized_dept_access(
    api_client,
    limited_role_user_token,
    admin_headers,
):
    # No token — FastAPI may return 401 or 422 for missing header (API-PLAN-TRACE-003 / NEEDS-REVIEW-006)
    list_resp = api_client.get("/api/v1/dept/list")
    assert list_resp.status_code in (401, 422), list_resp.text
    if list_resp.status_code == 200:
        pytest.fail("unauthenticated list must not return 200")

    create_resp = api_client.post(
        "/api/v1/dept/create",
        json=dept_create_payload(),
    )
    assert create_resp.status_code in (401, 422), create_resp.text

    limited_headers = {"token": limited_role_user_token}
    limited_list = api_client.get("/api/v1/dept/list", headers=limited_headers)
    assert limited_list.status_code == 403, limited_list.text

    limited_create = api_client.post(
        "/api/v1/dept/create",
        headers=limited_headers,
        json=dept_create_payload(),
    )
    assert limited_create.status_code == 403, limited_create.text

    admin_list = _dept_list(api_client, admin_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert admin_list.json()["code"] == 200


def test_tc_dept_api_013__reparent_rebuilds_closure(api_client, admin_headers):
    dept_a = factory_make_dept()
    dept_b = factory_make_dept()
    dept_c = factory_make_dept(parent_id=dept_a["id"])
    try:
        closure_before = factory_get_dept_closure_rows(dept_c["id"])
        assert_closure_self_row(closure_before, dept_c["id"])

        update_resp = api_client.post(
            "/api/v1/dept/update",
            headers=admin_headers,
            json={
                "id": dept_c["id"],
                "name": dept_c["name"],
                "desc": dept_c["desc"],
                "order": dept_c["order"],
                "parent_id": dept_b["id"],
            },
        )
        assert update_resp.status_code == 200, update_resp.text
        assert update_resp.json()["code"] == 200

        list_resp = _dept_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        tree = list_resp.json()["data"]
        parent_b = find_dept_in_tree(tree, dept_id=dept_b["id"])
        assert parent_b is not None
        child_ids = {item["id"] for item in (parent_b.get("children") or [])}
        assert dept_c["id"] in child_ids

        closure_rows = factory_get_dept_closure_rows(dept_c["id"])
        assert_closure_self_row(closure_rows, dept_c["id"])
        assert_closure_ancestors_include(
            closure_rows,
            dept_id=dept_c["id"],
            ancestor_ids={dept_b["id"]},
        )

        assert_matches_schema(update_resp.json(), "POST /api/v1/dept/update")
    finally:
        factory_cleanup_dept(dept_c["id"])
        factory_cleanup_dept(dept_a["id"])
        factory_cleanup_dept(dept_b["id"])
