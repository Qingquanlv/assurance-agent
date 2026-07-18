"""API tests for system.user module — RET-user-management-20260716-163247-cursor."""

from __future__ import annotations

import uuid

import pytest

from tests.api.adapters.dept import factory_cleanup_dept, factory_make_dept
from tests.api.adapters.user import factory_cleanup_user, factory_make_user
from tests.config import settings
from tests.helpers.user_assertions import (
    assert_client_error_response,
    assert_dept_enriched,
    assert_list_user_has_no_password,
    count_users_matching,
    find_user_in_list,
    login_token,
    user_create_payload,
)
from tests.schema_validation import assert_matches_schema


def _example_user_payload(**kwargs):
    """Build a create payload with an RFC-valid @example.com email."""
    if "email" not in kwargs:
        kwargs = {
            **kwargs,
            "email": f"{settings.user_prefix}-{uuid.uuid4().hex[:8]}@example.com",
        }
    return user_create_payload(**kwargs)


def _count_users_page50(api_client, admin_headers, **filters):
    resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={
            "page": 1,
            "page_size": 50,
            "username": settings.user_prefix,
            "dept_id": 0,
            **filters,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json().get("total", 0)


def test_tc_user_api_001__create_user_success(
    api_client,
    admin_headers,
    role_fixture,
    dept_fixture,
):
    payload = _example_user_payload(role_ids=[role_fixture["id"]], dept_id=dept_fixture["id"])
    resp = api_client.post("/api/v1/user/create", json=payload, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200

    list_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"email": payload["email"]},
    )
    assert list_resp.status_code == 200
    list_body = list_resp.json()
    item = find_user_in_list(list_body["data"], email=payload["email"])
    assert item is not None
    assert item["email"] == payload["email"]
    assert item["username"] == payload["username"]
    role_ids = {r["id"] if isinstance(r, dict) else r for r in item.get("roles", [])}
    assert role_fixture["id"] in role_ids
    assert item.get("dept", {}).get("id") == dept_fixture["id"]

    get_resp = api_client.get(
        "/api/v1/user/get",
        headers=admin_headers,
        params={"user_id": item["id"]},
    )
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["code"] == 200
    assert get_body["data"]["email"] == payload["email"]
    assert get_body["data"]["username"] == payload["username"]
    assert "password" not in get_body["data"]

    assert_matches_schema(body, "POST /api/v1/user/create")

    factory_cleanup_user(item["id"])


def test_tc_user_api_002__list_users_pagination(api_client, admin_headers):
    user = factory_make_user()
    try:
        resp = api_client.get(
            "/api/v1/user/list",
            headers=admin_headers,
            params={"page": 1, "page_size": 10},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert isinstance(body["data"], list)
        assert "total" in body
        assert "page" in body
        assert "page_size" in body
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert_list_user_has_no_password(body["data"])
        assert_matches_schema(body, "GET /api/v1/user/list")
    finally:
        factory_cleanup_user(user["id"])


def test_tc_user_api_003__get_user_detail_by_id(api_client, admin_headers, existing_user):
    resp = api_client.get(
        "/api/v1/user/get",
        headers=admin_headers,
        params={"user_id": existing_user["id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    assert data["id"] == existing_user["id"]
    assert data["username"] == existing_user["username"]
    assert data["email"] == existing_user["email"]
    assert "password" not in data
    assert_matches_schema(body, "GET /api/v1/user/get")


def test_tc_user_api_004__update_user_metadata_and_roles(
    api_client,
    admin_headers,
    role_pair_fixture,
):
    role_a, role_b = role_pair_fixture
    create_payload = _example_user_payload(role_ids=[role_a["id"]])
    create_resp = api_client.post(
        "/api/v1/user/create", json=create_payload, headers=admin_headers
    )
    assert create_resp.status_code == 200, create_resp.text
    list_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"email": create_payload["email"]},
    )
    assert list_resp.status_code == 200
    user = find_user_in_list(list_resp.json()["data"], email=create_payload["email"])
    assert user is not None
    role_ids_after_create = {
        r["id"] if isinstance(r, dict) else r for r in user.get("roles", [])
    }
    assert role_a["id"] in role_ids_after_create
    new_username = f"{settings.user_prefix}-upd-{uuid.uuid4().hex[:6]}"
    new_email = f"{settings.user_prefix}-upd-{uuid.uuid4().hex[:6]}@example.com"
    try:
        update_resp = api_client.post(
            "/api/v1/user/update",
            headers=admin_headers,
            json={
                "id": user["id"],
                "email": new_email,
                "username": new_username,
                "is_active": True,
                "is_superuser": False,
                "role_ids": [role_b["id"]],
                "dept_id": user.get("dept_id", 0),
            },
        )
        assert update_resp.status_code == 200, update_resp.text
        update_body = update_resp.json()
        assert update_body["code"] == 200

        get_resp = api_client.get(
            "/api/v1/user/get",
            headers=admin_headers,
            params={"user_id": user["id"]},
        )
        updated = get_resp.json()["data"]
        assert updated["username"] == new_username
        assert updated["email"] == new_email

        list_resp = api_client.get(
            "/api/v1/user/list",
            headers=admin_headers,
            params={"email": new_email},
        )
        assert list_resp.status_code == 200, list_resp.text
        item = find_user_in_list(list_resp.json()["data"], email=new_email)
        assert item is not None
        assert item["username"] == new_username
        list_role_ids = {r["id"] if isinstance(r, dict) else r for r in item.get("roles", [])}
        assert role_b["id"] in list_role_ids
        assert role_a["id"] not in list_role_ids

        assert_matches_schema(update_body, "POST /api/v1/user/update")
    finally:
        factory_cleanup_user(user["id"])


def test_tc_user_api_005__delete_user_list_invisible(api_client, admin_headers, ephemeral_user):
    """Post-delete get outcome neutral probe (API-PLAN-NR-006): 404, empty data, or code!=200."""
    user_id = ephemeral_user["id"]
    delete_resp = api_client.delete(
        "/api/v1/user/delete",
        headers=admin_headers,
        params={"user_id": user_id},
    )
    assert delete_resp.status_code == 200, delete_resp.text
    delete_body = delete_resp.json()
    assert delete_body["code"] == 200

    list_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 500},
    )
    assert list_resp.status_code == 200
    assert find_user_in_list(list_resp.json()["data"], user_id=user_id) is None

    get_resp = api_client.get(
        "/api/v1/user/get",
        headers=admin_headers,
        params={"user_id": user_id},
    )
    if get_resp.status_code == 200:
        get_body = get_resp.json()
        assert get_body.get("code") != 200 or not get_body.get("data")
    else:
        assert get_resp.status_code < 500

    assert_matches_schema(delete_body, "DELETE /api/v1/user/delete")


def test_tc_user_api_006__reset_password_normal_user(api_client, admin_headers, existing_user):
    resp = api_client.post(
        "/api/v1/user/reset_password",
        headers=admin_headers,
        json={"user_id": existing_user["id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert "123456" in body.get("msg", "")
    assert_matches_schema(body, "POST /api/v1/user/reset_password")

    token = login_token(api_client, existing_user["username"], "123456")
    assert token


def test_tc_user_api_007__list_filter_by_username_email_dept(
    api_client,
    admin_headers,
    users_for_filter,
):
    bundle = users_for_filter
    user1, user2, user3 = bundle["users"]
    dept_a = bundle["dept_a"]
    dept_b = bundle["dept_b"]

    username_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"username": user1["username"], "page": 1, "page_size": 50},
    )
    assert username_resp.status_code == 200, username_resp.text
    username_data = username_resp.json()["data"]
    assert all(user1["username"] in row["username"] for row in username_data)

    email_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"email": user2["email"], "page": 1, "page_size": 50},
    )
    assert email_resp.status_code == 200, email_resp.text
    email_data = email_resp.json()["data"]
    assert all(user2["email"] in row["email"] for row in email_data)

    dept_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"dept_id": dept_a["id"], "page": 1, "page_size": 50},
    )
    assert dept_resp.status_code == 200, dept_resp.text
    dept_data = dept_resp.json()["data"]
    for row in dept_data:
        dept = row.get("dept") or {}
        assert dept.get("id") == dept_a["id"]

    empty_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"username": f"{settings.user_prefix}-nonexistent-{uuid.uuid4().hex}", "page": 1, "page_size": 50},
    )
    assert empty_resp.status_code == 200, empty_resp.text
    empty_body = empty_resp.json()
    assert empty_body["total"] == 0 or len(empty_body["data"]) == 0

    assert_list_user_has_no_password(dept_data)


def test_tc_user_api_008__reject_create_missing_required_fields(api_client, admin_headers):
    base = _example_user_payload()
    before = _count_users_page50(api_client, admin_headers)

    field_messages = {
        "email": ("email", "邮箱"),
        "username": ("username", "用户名"),
        "password": ("password", "密码"),
    }
    for field, tokens in field_messages.items():
        payload = {k: v for k, v in base.items() if k != field}
        resp = api_client.post("/api/v1/user/create", headers=admin_headers, json=payload)
        assert resp.status_code < 500, resp.text
        assert_client_error_response(resp)
        msg = str(resp.json().get("msg", resp.json().get("detail", ""))).lower()
        assert any(token in msg for token in tokens), f"expected {field} in error message, got: {msg}"

    after = _count_users_page50(api_client, admin_headers)
    assert after == before


def test_tc_user_api_009__reject_create_duplicate_email(api_client, admin_headers, existing_user):
    before = count_users_matching(api_client, admin_headers, email=existing_user["email"])
    resp = api_client.post(
        "/api/v1/user/create",
        headers=admin_headers,
        json=_example_user_payload(email=existing_user["email"]),
    )
    assert_client_error_response(resp)
    body = resp.json()
    msg = body.get("msg", "").lower()
    assert "email" in msg and ("exist" in msg or "already" in msg or "已存在" in msg)
    after = count_users_matching(api_client, admin_headers, email=existing_user["email"])
    assert after == before == 1


def test_tc_user_api_010__reject_create_invalid_role_ids(api_client, admin_headers):
    payload = _example_user_payload(role_ids=[999999])
    resp = api_client.post("/api/v1/user/create", headers=admin_headers, json=payload)
    assert_client_error_response(resp)

    list_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"email": payload["email"]},
    )
    assert list_resp.status_code == 200
    item = find_user_in_list(list_resp.json()["data"], email=payload["email"])
    assert item is None


def test_tc_user_api_011__reject_create_invalid_dept_id(api_client, admin_headers):
    """Invalid dept_id behavior (API-PLAN-NR-003): User.dept_id has no FK; pin on execution."""
    payload = _example_user_payload(dept_id=999999)
    resp = api_client.post("/api/v1/user/create", headers=admin_headers, json=payload)
    assert resp.status_code < 500, resp.text

    if resp.status_code == 200 and resp.json().get("code") == 200:
        list_resp = api_client.get(
            "/api/v1/user/list",
            headers=admin_headers,
            params={"email": payload["email"]},
        )
        item = find_user_in_list(list_resp.json()["data"], email=payload["email"])
        if item:
            factory_cleanup_user(item["id"])
        pytest.fail("invalid dept_id create unexpectedly succeeded")
    else:
        assert_client_error_response(resp, allow_envelope_400=False)
        assert count_users_matching(api_client, admin_headers, email=payload["email"]) == 0


def test_tc_user_api_012__reject_reset_superuser_password(
    api_client,
    admin_headers,
    superuser_target_id,
):
    resp = api_client.post(
        "/api/v1/user/reset_password",
        headers=admin_headers,
        json={"user_id": superuser_target_id},
    )
    assert resp.status_code == 403, resp.text
    assert resp.status_code < 500
    body = resp.json()
    msg = str(body.get("msg", body.get("detail", ""))).lower()
    assert "superuser" in msg or "超级" in msg or "管理员" in msg or "forbidden" in msg

    token = login_token(api_client, settings.admin_username, settings.admin_password)
    assert token


def test_tc_user_api_013__reject_unauthorized_user_access(
    api_client,
    admin_headers,
    limited_role_user_token,
):
    list_resp = api_client.get("/api/v1/user/list")
    assert list_resp.status_code in (401, 422), list_resp.text
    assert list_resp.status_code < 500

    limited_headers = {"token": limited_role_user_token}
    limited_list = api_client.get("/api/v1/user/list", headers=limited_headers)
    assert limited_list.status_code == 403, limited_list.text

    limited_create = api_client.post(
        "/api/v1/user/create",
        headers=limited_headers,
        json=_example_user_payload(),
    )
    assert limited_create.status_code == 403

    admin_list = api_client.get("/api/v1/user/list", headers=admin_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert admin_list.json()["code"] == 200


def test_tc_user_api_014__update_user_dept_association(api_client, admin_headers):
    dept_a = factory_make_dept()
    dept_b = factory_make_dept()
    create_payload = _example_user_payload(dept_id=dept_a["id"])
    create_resp = api_client.post(
        "/api/v1/user/create", json=create_payload, headers=admin_headers
    )
    assert create_resp.status_code == 200, create_resp.text
    list_resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"email": create_payload["email"]},
    )
    assert list_resp.status_code == 200
    user = find_user_in_list(list_resp.json()["data"], email=create_payload["email"])
    assert user is not None
    assert user.get("dept", {}).get("id") == dept_a["id"]
    try:
        update_resp = api_client.post(
            "/api/v1/user/update",
            headers=admin_headers,
            json={
                "id": user["id"],
                "email": user["email"],
                "username": user["username"],
                "is_active": True,
                "is_superuser": False,
                "role_ids": list(user.get("role_ids", [])),
                "dept_id": dept_b["id"],
            },
        )
        assert update_resp.status_code == 200, update_resp.text
        assert update_resp.json()["code"] == 200

        get_resp = api_client.get(
            "/api/v1/user/get",
            headers=admin_headers,
            params={"user_id": user["id"]},
        )
        assert get_resp.status_code == 200
        get_data = get_resp.json()["data"]
        assert get_data.get("dept_id") == dept_b["id"]

        list_resp = api_client.get(
            "/api/v1/user/list",
            headers=admin_headers,
            params={"email": user["email"]},
        )
        assert list_resp.status_code == 200
        item = find_user_in_list(list_resp.json()["data"], email=user["email"])
        assert item is not None
        assert_dept_enriched(item, dept_b)

        assert_matches_schema(update_resp.json(), "POST /api/v1/user/update")
    finally:
        factory_cleanup_user(user["id"])
        factory_cleanup_dept(dept_a["id"])
        factory_cleanup_dept(dept_b["id"])


def test_tc_user_api_015__reject_create_duplicate_username(api_client, admin_headers, existing_user):
    """Duplicate username may return 4xx or HTTP 500 IntegrityError (API-PLAN-NR-005); count guard == 1."""
    resp = api_client.post(
        "/api/v1/user/create",
        headers=admin_headers,
        json=_example_user_payload(username=existing_user["username"]),
    )
    assert_client_error_response(resp)
    dup_count = count_users_matching(
        api_client,
        admin_headers,
        username=existing_user["username"],
    )
    assert dup_count == 1


def test_tc_user_api_016__reject_get_nonexistent_user_id(api_client, admin_headers):
    """Nonexistent get response shape (API-PLAN-NR-004): pin 404 vs envelope failure at execution."""
    resp = api_client.get(
        "/api/v1/user/get",
        headers=admin_headers,
        params={"user_id": 999999999},
    )
    assert resp.status_code < 500, resp.text
    if resp.status_code == 200:
        body = resp.json()
        assert body.get("code") != 200 or not body.get("data")
    else:
        assert resp.status_code in (400, 404, 422)
