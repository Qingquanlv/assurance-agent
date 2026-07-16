"""API tests for system.role module — RET-role-management-20260716-163247-cursor."""

from __future__ import annotations

import uuid

from tests.api.adapters.api import factory_cleanup_api, factory_make_api
from tests.api.adapters.menu import factory_cleanup_menu, factory_make_menu
from tests.api.adapters.role import (
    factory_bind_role_apis,
    factory_cleanup_role,
    factory_make_role,
)
from tests.config import settings
from tests.helpers.role_assertions import (
    assert_authorized_bindings,
    assert_menu_not_found_message,
    count_roles_matching,
    find_role_in_list,
    role_create_payload,
)
from tests.schema_validation import assert_matches_schema

NOT_FOUND_ROLE_ID = 99999
INVALID_MENU_ID = 99999


def test_tc_role_api_001__create_role_success(api_client, admin_headers):
    payload = role_create_payload(desc="created via API test")
    role_id = None
    try:
        resp = api_client.post("/api/v1/role/create", json=payload, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert "success" in body.get("msg", "").lower() or "created" in body.get("msg", "").lower()

        list_resp = api_client.get(
            "/api/v1/role/list",
            headers=admin_headers,
            params={"role_name": payload["name"], "page": 1, "page_size": 50},
        )
        assert list_resp.status_code == 200, list_resp.text
        item = find_role_in_list(list_resp.json()["data"], name=payload["name"])
        assert item is not None
        assert item["name"] == payload["name"]
        assert item["desc"] == payload["desc"]
        role_id = item["id"]

        get_resp = api_client.get(
            "/api/v1/role/get",
            headers=admin_headers,
            params={"role_id": role_id},
        )
        assert get_resp.status_code == 200, get_resp.text
        get_body = get_resp.json()
        assert get_body["code"] == 200
        assert get_body["data"]["id"] == role_id
        assert get_body["data"]["name"] == payload["name"]
        assert get_body["data"]["desc"] == payload["desc"]

        assert_matches_schema(body, "POST /api/v1/role/create")
    finally:
        if role_id is not None:
            factory_cleanup_role(role_id)


def test_tc_role_api_002__list_roles_pagination_and_role_name_filter(
    api_client,
    admin_headers,
    roles_for_filter,
):
    substring = roles_for_filter["filter_substring"]
    seeded_ids = {role["id"] for role in roles_for_filter["roles"]}

    page_resp = api_client.get(
        "/api/v1/role/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 10},
    )
    assert page_resp.status_code == 200, page_resp.text
    page_body = page_resp.json()
    assert page_body["code"] == 200
    assert isinstance(page_body["data"], list)
    assert "total" in page_body
    assert page_body["page"] == 1
    assert page_body["page_size"] == 10
    if page_body["data"]:
        sample = page_body["data"][0]
        assert "id" in sample
        assert "name" in sample
        assert "desc" in sample
    assert_matches_schema(page_body, "GET /api/v1/role/list")

    filter_resp = api_client.get(
        "/api/v1/role/list",
        headers=admin_headers,
        params={"role_name": substring, "page": 1, "page_size": 50},
    )
    assert filter_resp.status_code == 200, filter_resp.text
    filtered = filter_resp.json()["data"]
    assert filtered, "expected at least one role matching filter substring"
    assert all(substring in row["name"] for row in filtered)
    returned_ids = {row["id"] for row in filtered}
    assert seeded_ids & returned_ids, "seeded roles should appear in filter results"

    empty_resp = api_client.get(
        "/api/v1/role/list",
        headers=admin_headers,
        params={"role_name": "zzznomatch99999", "page": 1, "page_size": 50},
    )
    assert empty_resp.status_code == 200, empty_resp.text
    empty_data = empty_resp.json()["data"]
    assert not empty_data or all(
        "zzznomatch99999" not in row.get("name", "") for row in empty_data
    )


def test_tc_role_api_003__get_role_detail_by_id(api_client, admin_headers, role_fixture):
    resp = api_client.get(
        "/api/v1/role/get",
        headers=admin_headers,
        params={"role_id": role_fixture["id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    assert data["id"] == role_fixture["id"]
    assert data["name"] == role_fixture["name"]
    assert data["desc"] == role_fixture["desc"]
    assert_matches_schema(body, "GET /api/v1/role/get")


def test_tc_role_api_004__update_role_name_and_desc(api_client, admin_headers, role_fixture):
    new_name = f"{settings.role_prefix}-upd-{uuid.uuid4().hex[:6]}"
    new_desc = "updated description"
    update_resp = api_client.post(
        "/api/v1/role/update",
        headers=admin_headers,
        json={"id": role_fixture["id"], "name": new_name, "desc": new_desc},
    )
    assert update_resp.status_code == 200, update_resp.text
    update_body = update_resp.json()
    assert update_body["code"] == 200
    assert "updated" in update_body.get("msg", "").lower() or "成功" in update_body.get("msg", "")

    get_resp = api_client.get(
        "/api/v1/role/get",
        headers=admin_headers,
        params={"role_id": role_fixture["id"]},
    )
    updated = get_resp.json()["data"]
    assert updated["name"] == new_name
    assert updated["desc"] == new_desc

    list_resp = api_client.get(
        "/api/v1/role/list",
        headers=admin_headers,
        params={"role_name": new_name, "page": 1, "page_size": 50},
    )
    assert list_resp.status_code == 200, list_resp.text
    list_item = find_role_in_list(list_resp.json()["data"], role_id=role_fixture["id"])
    assert list_item is not None
    assert list_item["name"] == new_name
    assert list_item["desc"] == new_desc

    assert_matches_schema(update_body, "POST /api/v1/role/update")


def test_tc_role_api_005__delete_role_list_invisible(api_client, admin_headers, ephemeral_role):
    role_id = ephemeral_role["id"]
    delete_resp = api_client.delete(
        "/api/v1/role/delete",
        headers=admin_headers,
        params={"role_id": role_id},
    )
    assert delete_resp.status_code == 200, delete_resp.text
    delete_body = delete_resp.json()
    assert delete_body["code"] == 200
    assert "deleted" in delete_body.get("msg", "").lower() or "成功" in delete_body.get("msg", "")

    list_resp = api_client.get(
        "/api/v1/role/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 500},
    )
    assert list_resp.status_code == 200
    assert find_role_in_list(list_resp.json()["data"], role_id=role_id) is None

    get_resp = api_client.get(
        "/api/v1/role/get",
        headers=admin_headers,
        params={"role_id": role_id},
    )
    if get_resp.status_code == 200:
        get_body = get_resp.json()
        assert get_body.get("code") != 200 or get_body.get("data") is None
    else:
        assert 400 <= get_resp.status_code < 500, get_resp.text

    assert_matches_schema(delete_body, "DELETE /api/v1/role/delete")


def test_tc_role_api_006__get_role_authorized_menus_and_apis(
    api_client,
    admin_headers,
    authorized_role_fixture,
):
    role = authorized_role_fixture["role"]
    resp = api_client.get(
        "/api/v1/role/authorized",
        headers=admin_headers,
        params={"id": role["id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert_authorized_bindings(
        body["data"],
        menu_ids=authorized_role_fixture["menu_ids"],
        api_infos=authorized_role_fixture["api_infos"],
    )
    assert_matches_schema(body, "GET /api/v1/role/authorized")


def test_tc_role_api_007__update_role_authorized_bindings(api_client, admin_headers):
    role = factory_make_role()
    menu_a = factory_make_menu()
    api_a = factory_make_api()
    menu_b = factory_make_menu()
    api_b = factory_make_api()
    initial_api_infos = [{"path": api_a["path"], "method": api_a["method"]}]
    replacement_api_infos = [{"path": api_b["path"], "method": api_b["method"]}]
    factory_bind_role_apis(role["id"], api_infos=initial_api_infos, menu_ids=[menu_a["id"]])

    try:
        before_resp = api_client.get(
            "/api/v1/role/authorized",
            headers=admin_headers,
            params={"id": role["id"]},
        )
        assert before_resp.status_code == 200

        update_resp = api_client.post(
            "/api/v1/role/authorized",
            headers=admin_headers,
            json={
                "id": role["id"],
                "menu_ids": [menu_b["id"]],
                "api_infos": replacement_api_infos,
            },
        )
        assert update_resp.status_code == 200, update_resp.text
        update_body = update_resp.json()
        assert update_body["code"] == 200
        assert "updated" in update_body.get("msg", "").lower() or "成功" in update_body.get("msg", "")

        after_resp = api_client.get(
            "/api/v1/role/authorized",
            headers=admin_headers,
            params={"id": role["id"]},
        )
        after_body = after_resp.json()
        assert_authorized_bindings(
            after_body["data"],
            menu_ids=[menu_b["id"]],
            api_infos=replacement_api_infos,
        )
        after_menus = after_body["data"].get("menus") or []
        after_menu_ids = {item["id"] for item in after_menus if isinstance(item, dict)}
        assert menu_a["id"] not in after_menu_ids, "old menu binding must not be retained"
        after_apis = after_body["data"].get("apis") or []
        after_api_pairs = {
            (item.get("path"), item.get("method"))
            for item in after_apis
            if isinstance(item, dict)
        }
        assert (api_a["path"], api_a["method"]) not in after_api_pairs, (
            "old api binding must not be retained"
        )
        assert_matches_schema(update_body, "POST /api/v1/role/authorized")
    finally:
        factory_cleanup_role(role["id"])
        factory_cleanup_menu(menu_a["id"])
        factory_cleanup_menu(menu_b["id"])
        factory_cleanup_api(api_a["id"])
        factory_cleanup_api(api_b["id"])


def test_tc_role_api_008__reject_create_missing_or_empty_name(api_client, admin_headers):
    before = count_roles_matching(api_client, admin_headers)

    missing_resp = api_client.post(
        "/api/v1/role/create",
        headers=admin_headers,
        json={"desc": "missing name field"},
    )
    assert missing_resp.status_code in (400, 422), missing_resp.text
    missing_msg = str(
        missing_resp.json().get("msg", missing_resp.json().get("detail", ""))
    ).lower()
    assert "name" in missing_msg

    empty_resp = api_client.post(
        "/api/v1/role/create",
        headers=admin_headers,
        json={"name": "", "desc": "empty name"},
    )
    assert empty_resp.status_code in (400, 422), empty_resp.text
    empty_msg = str(
        empty_resp.json().get("msg", empty_resp.json().get("detail", ""))
    ).lower()
    assert "name" in empty_msg

    after = count_roles_matching(api_client, admin_headers)
    assert after == before


def test_tc_role_api_009__reject_create_duplicate_role_name(
    api_client,
    admin_headers,
    role_fixture,
):
    dup_resp = api_client.post(
        "/api/v1/role/create",
        headers=admin_headers,
        json=role_create_payload(name=role_fixture["name"], desc="duplicate attempt"),
    )
    assert dup_resp.status_code == 400 or (
        dup_resp.status_code == 200 and dup_resp.json().get("code") != 200
    ), dup_resp.text
    dup_body = dup_resp.json()
    dup_msg = str(dup_body.get("msg", dup_body.get("detail", ""))).lower()
    assert "exist" in dup_msg or "already" in dup_msg or "重复" in dup_msg

    list_resp = api_client.get(
        "/api/v1/role/list",
        headers=admin_headers,
        params={"role_name": role_fixture["name"], "page": 1, "page_size": 500},
    )
    assert list_resp.status_code == 200, list_resp.text
    matching = [
        row for row in list_resp.json()["data"] if row.get("name") == role_fixture["name"]
    ]
    assert len(matching) == 1


def test_tc_role_api_010__reject_get_nonexistent_role_id(api_client, admin_headers):
    get_resp = api_client.get(
        "/api/v1/role/get",
        headers=admin_headers,
        params={"role_id": NOT_FOUND_ROLE_ID},
    )
    assert 400 <= get_resp.status_code < 500, get_resp.text
    assert get_resp.status_code < 500


def test_tc_role_api_011__reject_delete_nonexistent_role_id(api_client, admin_headers):
    delete_resp = api_client.delete(
        "/api/v1/role/delete",
        headers=admin_headers,
        params={"role_id": NOT_FOUND_ROLE_ID},
    )
    assert 400 <= delete_resp.status_code < 500, delete_resp.text
    assert delete_resp.status_code < 500


def test_tc_role_api_012__reject_authorized_update_invalid_menu_id(
    api_client,
    admin_headers,
    authorized_role_fixture,
):
    """API-PLAN-NR-003: probe strategy — controller may not validate missing Menu rows."""
    role = authorized_role_fixture["role"]
    before_resp = api_client.get(
        "/api/v1/role/authorized",
        headers=admin_headers,
        params={"id": role["id"]},
    )
    before_menus = list((before_resp.json().get("data") or {}).get("menus") or [])

    resp = api_client.post(
        "/api/v1/role/authorized",
        headers=admin_headers,
        json={
            "id": role["id"],
            "menu_ids": [INVALID_MENU_ID],
            "api_infos": authorized_role_fixture["api_infos"],
        },
    )
    assert resp.status_code < 500, resp.text
    if resp.status_code >= 400:
        assert_menu_not_found_message(resp)

    after_resp = api_client.get(
        "/api/v1/role/authorized",
        headers=admin_headers,
        params={"id": role["id"]},
    )
    after_menus = list((after_resp.json().get("data") or {}).get("menus") or [])
    assert len(after_menus) >= len(before_menus), (
        "invalid menu_ids update must not silently clear prior menu bindings"
    )


def test_tc_role_api_013__reject_authorized_update_invalid_api_info(
    api_client,
    admin_headers,
    authorized_role_fixture,
):
    """API-PLAN-NR-003: probe strategy — invalid api_info should be non-5xx with binding guard."""
    role = authorized_role_fixture["role"]
    before_resp = api_client.get(
        "/api/v1/role/authorized",
        headers=admin_headers,
        params={"id": role["id"]},
    )
    before_apis = list((before_resp.json().get("data") or {}).get("apis") or [])

    resp = api_client.post(
        "/api/v1/role/authorized",
        headers=admin_headers,
        json={
            "id": role["id"],
            "menu_ids": authorized_role_fixture["menu_ids"],
            "api_infos": [{"path": f"/{settings.api_prefix}/missing", "method": "PATCH"}],
        },
    )
    assert resp.status_code < 500, resp.text

    after_resp = api_client.get(
        "/api/v1/role/authorized",
        headers=admin_headers,
        params={"id": role["id"]},
    )
    after_apis = list((after_resp.json().get("data") or {}).get("apis") or [])
    assert len(after_apis) >= len(before_apis), (
        "invalid api_infos update must not silently clear prior api bindings"
    )


def test_tc_role_api_014__reject_unauthorized_role_api_access(
    api_client,
    admin_headers,
    limited_role_user_token,
):
    list_unauth = api_client.get("/api/v1/role/list")
    assert list_unauth.status_code in (401, 422), list_unauth.text

    create_unauth = api_client.post(
        "/api/v1/role/create",
        json=role_create_payload(),
    )
    assert create_unauth.status_code in (401, 422), create_unauth.text

    limited_headers = {"token": limited_role_user_token}
    list_limited = api_client.get("/api/v1/role/list", headers=limited_headers)
    assert list_limited.status_code == 403, list_limited.text
    limited_msg = str(
        list_limited.json().get("msg", list_limited.json().get("detail", ""))
    ).lower()
    assert "permission" in limited_msg or "denied" in limited_msg or "403" in limited_msg

    create_limited = api_client.post(
        "/api/v1/role/create",
        headers=limited_headers,
        json=role_create_payload(),
    )
    assert create_limited.status_code == 403, create_limited.text

    admin_list = api_client.get("/api/v1/role/list", headers=admin_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert admin_list.json().get("code") == 200
