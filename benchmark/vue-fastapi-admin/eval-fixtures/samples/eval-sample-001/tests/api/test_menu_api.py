"""API tests for system.menu module — RET-menu-management-20260716-163247-cursor."""

from __future__ import annotations

import pytest

from tests.api.adapters.menu import factory_cleanup_menu, factory_make_menu
from tests.config import settings
from tests.helpers.menu_assertions import (
    assert_menu_not_in_tree,
    assert_siblings_ordered_by_order,
    collect_menu_id_parent_pairs,
    count_menus_with_prefix_in_tree,
    find_menu_in_tree,
    menu_create_payload,
)
from tests.schema_validation import assert_matches_schema

INVALID_PARENT_ID = 999999
INVALID_MENU_ID = 999999


def _menu_list(api_client, admin_headers, **params):
    return api_client.get(
        "/api/v1/menu/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 500, **params},
    )


def _count_nodes_at_parent(data, parent_id: int) -> int:
    if parent_id == 0:
        return sum(1 for item in data if item.get("parent_id") == 0)
    parent = find_menu_in_tree(data, menu_id=parent_id)
    if parent is None:
        return 0
    return len(parent.get("children") or [])


def test_tc_menu_api_001__create_top_level_menu_success(api_client, admin_headers):
    payload = menu_create_payload(parent_id=0, order=1)
    menu_id = None
    try:
        resp = api_client.post("/api/v1/menu/create", json=payload, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200

        list_resp = _menu_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        item = find_menu_in_tree(list_resp.json()["data"], name=payload["name"])
        assert item is not None
        assert item.get("parent_id") == 0
        menu_id = item["id"]

        get_resp = api_client.get(
            "/api/v1/menu/get",
            headers=admin_headers,
            params={"menu_id": menu_id},
        )
        assert get_resp.status_code == 200, get_resp.text
        detail = get_resp.json()["data"]
        assert detail["id"] == menu_id
        assert detail["name"] == payload["name"]
        assert detail.get("menu_type") == payload["menu_type"]
        assert detail["parent_id"] == 0

        assert_matches_schema(body, "POST /api/v1/menu/create")
    finally:
        if menu_id is not None:
            factory_cleanup_menu(menu_id)


def test_tc_menu_api_002__list_menu_tree_structure(api_client, admin_headers):
    parent = factory_make_menu()
    child = factory_make_menu(parent_id=parent["id"])
    try:
        resp = _menu_list(api_client, admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert isinstance(data, list)

        parent_node = find_menu_in_tree(data, menu_id=parent["id"])
        child_node = find_menu_in_tree(data, menu_id=child["id"])
        assert parent_node is not None
        assert child_node is not None
        assert "id" in child_node
        assert "name" in child_node
        assert "parent_id" in child_node
        assert "children" in parent_node
        assert child_node["parent_id"] == parent["id"]

        assert_matches_schema(body, "GET /api/v1/menu/list")
    finally:
        factory_cleanup_menu(child["id"])
        factory_cleanup_menu(parent["id"])


def test_tc_menu_api_003__get_menu_detail_by_id(api_client, admin_headers):
    menu = factory_make_menu()
    try:
        resp = api_client.get(
            "/api/v1/menu/get",
            headers=admin_headers,
            params={"menu_id": menu["id"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        detail = body["data"]
        assert detail["id"] == menu["id"]
        assert detail["name"] == menu["name"]
        assert detail["path"] == menu["path"]
        assert detail["parent_id"] == menu["parent_id"]

        assert_matches_schema(body, "GET /api/v1/menu/get")
    finally:
        factory_cleanup_menu(menu["id"])


def test_tc_menu_api_004__update_menu_name_and_order(api_client, admin_headers):
    menu_a = factory_make_menu(order=1)
    menu_b = factory_make_menu(order=2)
    try:
        new_name = menu_create_payload()["name"]
        update_resp = api_client.post(
            "/api/v1/menu/update",
            headers=admin_headers,
            json={
                "id": menu_a["id"],
                "name": new_name,
                "path": menu_a["path"],
                "order": 3,
                "component": "Layout",
                "menu_type": "catalog",
                "parent_id": menu_a["parent_id"],
            },
        )
        assert update_resp.status_code == 200, update_resp.text
        update_body = update_resp.json()
        assert update_body["code"] == 200

        get_resp = api_client.get(
            "/api/v1/menu/get",
            headers=admin_headers,
            params={"menu_id": menu_a["id"]},
        )
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["data"]["name"] == new_name

        list_resp = _menu_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        data = list_resp.json()["data"]
        siblings = [
            item
            for item in data
            if item.get("parent_id") == menu_a["parent_id"]
            and item["id"] in {menu_a["id"], menu_b["id"]}
        ]
        assert_siblings_ordered_by_order(siblings)

        assert_matches_schema(update_body, "POST /api/v1/menu/update")
    finally:
        factory_cleanup_menu(menu_a["id"])
        factory_cleanup_menu(menu_b["id"])


def test_tc_menu_api_005__delete_leaf_menu_success(api_client, admin_headers):
    sibling = factory_make_menu()
    menu = factory_make_menu()
    menu_id = menu["id"]
    try:
        list_before = _menu_list(api_client, admin_headers)
        assert list_before.status_code == 200, list_before.text
        sibling_count_before = _count_nodes_at_parent(list_before.json()["data"], 0) - 1

        delete_resp = api_client.delete(
            "/api/v1/menu/delete",
            headers=admin_headers,
            params={"id": menu_id},
        )
        assert delete_resp.status_code == 200, delete_resp.text
        delete_body = delete_resp.json()
        assert delete_body["code"] == 200

        get_resp = api_client.get(
            "/api/v1/menu/get",
            headers=admin_headers,
            params={"menu_id": menu_id},
        )
        assert get_resp.status_code == 404, get_resp.text

        list_resp = _menu_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        list_data = list_resp.json()["data"]
        assert_menu_not_in_tree(list_data, menu_id)
        sibling_count_after = _count_nodes_at_parent(list_data, 0)
        assert sibling_count_after == sibling_count_before
        assert find_menu_in_tree(list_data, menu_id=sibling["id"]) is not None

        assert_matches_schema(delete_body, "DELETE /api/v1/menu/delete")
    finally:
        factory_cleanup_menu(sibling["id"])


def test_tc_menu_api_006__create_child_menu_in_parent_children(api_client, admin_headers):
    parent_payload = menu_create_payload(parent_id=0, order=1)
    parent_id = None
    child_id = None
    try:
        parent_resp = api_client.post(
            "/api/v1/menu/create",
            json=parent_payload,
            headers=admin_headers,
        )
        assert parent_resp.status_code == 200, parent_resp.text
        assert parent_resp.json()["code"] == 200

        list_after_parent = _menu_list(api_client, admin_headers)
        assert list_after_parent.status_code == 200, list_after_parent.text
        parent_node = find_menu_in_tree(
            list_after_parent.json()["data"],
            name=parent_payload["name"],
        )
        assert parent_node is not None
        parent_id = parent_node["id"]

        child_payload = menu_create_payload(parent_id=parent_id, order=1)
        child_resp = api_client.post(
            "/api/v1/menu/create",
            json=child_payload,
            headers=admin_headers,
        )
        assert child_resp.status_code == 200, child_resp.text
        assert child_resp.json()["code"] == 200

        list_resp = _menu_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        list_data = list_resp.json()["data"]
        child_node = find_menu_in_tree(list_data, name=child_payload["name"])
        assert child_node is not None
        child_id = child_node["id"]
        assert child_node["parent_id"] == parent_id

        parent_in_tree = find_menu_in_tree(list_data, menu_id=parent_id)
        assert parent_in_tree is not None
        child_names = {item["name"] for item in (parent_in_tree.get("children") or [])}
        assert child_payload["name"] in child_names

        assert_matches_schema(child_resp.json(), "POST /api/v1/menu/create")
    finally:
        if child_id is not None:
            factory_cleanup_menu(child_id)
        if parent_id is not None:
            factory_cleanup_menu(parent_id)


def test_tc_menu_api_007__reject_delete_parent_with_children(api_client, admin_headers):
    parent = factory_make_menu()
    child = factory_make_menu(parent_id=parent["id"])
    try:
        delete_resp = api_client.delete(
            "/api/v1/menu/delete",
            headers=admin_headers,
            params={"id": parent["id"]},
        )
        assert delete_resp.status_code == 400, delete_resp.text
        body = delete_resp.json()
        assert body.get("code") == 400
        msg = str(body.get("msg", "")).lower()
        assert "child" in msg

        list_resp = _menu_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        parent_node = find_menu_in_tree(list_resp.json()["data"], menu_id=parent["id"])
        assert parent_node is not None
        child_ids = {item["id"] for item in (parent_node.get("children") or [])}
        assert child["id"] in child_ids
    finally:
        factory_cleanup_menu(child["id"])
        factory_cleanup_menu(parent["id"])


def test_tc_menu_api_008__reject_create_missing_required_fields(api_client, admin_headers):
    """API-PLAN-FINDING-TR-008: MenuCreate.menu_type defaults to catalog — tests missing name and path."""
    list_before = _menu_list(api_client, admin_headers)
    assert list_before.status_code == 200, list_before.text
    before_count = count_menus_with_prefix_in_tree(list_before.json()["data"])

    missing_name_resp = api_client.post(
        "/api/v1/menu/create",
        headers=admin_headers,
        json={"path": f"/{settings.menu_prefix}/no-name", "component": "Layout"},
    )
    assert missing_name_resp.status_code in (400, 422), missing_name_resp.text
    assert missing_name_resp.status_code < 500

    missing_path_resp = api_client.post(
        "/api/v1/menu/create",
        headers=admin_headers,
        json={"name": menu_create_payload()["name"], "component": "Layout"},
    )
    assert missing_path_resp.status_code in (400, 422), missing_path_resp.text
    assert missing_path_resp.status_code < 500

    list_after = _menu_list(api_client, admin_headers)
    assert list_after.status_code == 200, list_after.text
    after_count = count_menus_with_prefix_in_tree(list_after.json()["data"])
    assert after_count == before_count


def test_tc_menu_api_009__probe_invalid_parent_id_create(api_client, admin_headers):
    """API-PLAN-NR-009-001: neutral probe — SC-ROUTE-003; record status and tree delta, no ideal-only 4xx."""
    list_before = _menu_list(api_client, admin_headers)
    assert list_before.status_code == 200, list_before.text
    snapshot_before = collect_menu_id_parent_pairs(list_before.json()["data"])

    payload = menu_create_payload(parent_id=INVALID_PARENT_ID)
    create_resp = api_client.post(
        "/api/v1/menu/create",
        headers=admin_headers,
        json=payload,
    )
    assert create_resp.status_code < 500, create_resp.text
    observed_status = create_resp.status_code

    orphan_id = None
    try:
        list_after = _menu_list(api_client, admin_headers)
        assert list_after.status_code == 200, list_after.text
        snapshot_after = collect_menu_id_parent_pairs(list_after.json()["data"])

        if observed_status == 200:
            orphan = find_menu_in_tree(list_after.json()["data"], name=payload["name"])
            assert orphan is not None, "HTTP 200 create should persist a row for neutral probe"
            orphan_id = orphan["id"]
            assert orphan.get("parent_id") == INVALID_PARENT_ID
        else:
            assert observed_status in (400, 404, 422), (
                f"unexpected client error status {observed_status}"
            )
            # Tree unchanged when rejected; orphan rows would appear in snapshot_after
            new_pairs = set(snapshot_after) - set(snapshot_before)
            if new_pairs:
                for menu_id, parent_id in new_pairs:
                    orphan_id = menu_id
                    assert parent_id == INVALID_PARENT_ID
    finally:
        if orphan_id is not None:
            factory_cleanup_menu(orphan_id)


def test_tc_menu_api_010__reject_invalid_menu_id_get(api_client, admin_headers):
    resp = api_client.get(
        "/api/v1/menu/get",
        headers=admin_headers,
        params={"menu_id": INVALID_MENU_ID},
    )
    assert resp.status_code < 500, resp.text
    assert resp.status_code in (400, 404), resp.text


def test_tc_menu_api_011__reject_unauthorized_menu_access(
    api_client,
    limited_role_user_token,
    admin_headers,
):
    """API-PLAN-FINDING-TR-011: no-token list may return 401 or 422 (FastAPI header validation)."""
    list_resp = api_client.get("/api/v1/menu/list")
    assert list_resp.status_code in (401, 422), list_resp.text
    assert list_resp.status_code < 500
    if list_resp.status_code == 200:
        pytest.fail("unauthenticated list must not return 200")

    create_no_token = api_client.post(
        "/api/v1/menu/create",
        json=menu_create_payload(),
    )
    assert create_no_token.status_code in (401, 422), create_no_token.text
    assert create_no_token.status_code < 500

    limited_headers = {"token": limited_role_user_token}
    limited_list = api_client.get("/api/v1/menu/list", headers=limited_headers)
    assert limited_list.status_code == 403, limited_list.text

    limited_create = api_client.post(
        "/api/v1/menu/create",
        headers=limited_headers,
        json=menu_create_payload(),
    )
    assert limited_create.status_code == 403, limited_create.text

    admin_list = _menu_list(api_client, admin_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert admin_list.json()["code"] == 200


def test_tc_menu_api_012__distinguish_catalog_and_menu_types(api_client, admin_headers):
    catalog_id = None
    menu_id = None
    try:
        catalog_payload = menu_create_payload(menu_type="catalog")
        catalog_resp = api_client.post(
            "/api/v1/menu/create",
            json=catalog_payload,
            headers=admin_headers,
        )
        assert catalog_resp.status_code == 200, catalog_resp.text
        catalog_body = catalog_resp.json()
        assert catalog_body["code"] == 200

        list_after_catalog = _menu_list(api_client, admin_headers)
        assert list_after_catalog.status_code == 200, list_after_catalog.text
        catalog_node = find_menu_in_tree(
            list_after_catalog.json()["data"],
            name=catalog_payload["name"],
        )
        assert catalog_node is not None
        catalog_id = catalog_node["id"]

        get_catalog = api_client.get(
            "/api/v1/menu/get",
            headers=admin_headers,
            params={"menu_id": catalog_id},
        )
        assert get_catalog.status_code == 200, get_catalog.text
        assert get_catalog.json()["data"]["menu_type"] == "catalog"

        menu_payload = menu_create_payload(menu_type="menu", component="/system/example")
        menu_resp = api_client.post(
            "/api/v1/menu/create",
            json=menu_payload,
            headers=admin_headers,
        )
        assert menu_resp.status_code == 200, menu_resp.text
        menu_body = menu_resp.json()
        assert menu_body["code"] == 200

        list_after_menu = _menu_list(api_client, admin_headers)
        assert list_after_menu.status_code == 200, list_after_menu.text
        menu_node = find_menu_in_tree(
            list_after_menu.json()["data"],
            name=menu_payload["name"],
        )
        assert menu_node is not None
        menu_id = menu_node["id"]

        get_menu = api_client.get(
            "/api/v1/menu/get",
            headers=admin_headers,
            params={"menu_id": menu_id},
        )
        assert get_menu.status_code == 200, get_menu.text
        assert get_menu.json()["data"]["menu_type"] == "menu"

        assert_matches_schema(catalog_body, "POST /api/v1/menu/create")
        assert_matches_schema(menu_body, "POST /api/v1/menu/create")
    finally:
        if menu_id is not None:
            factory_cleanup_menu(menu_id)
        if catalog_id is not None:
            factory_cleanup_menu(catalog_id)
