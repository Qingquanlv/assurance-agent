"""API tests for system.api module — RET-api-management-20260715-154441-cursor."""

from __future__ import annotations

import uuid

from tests.api.adapters.api import (
    factory_cleanup_api,
    factory_list_auth_routes,
    factory_make_api,
)
from tests.config import settings
from tests.helpers.api_assertions import (
    api_create_payload,
    auth_route_lookup_key,
    count_apis_matching,
    count_apis_with_method_path,
    find_api_in_list,
    orphan_api_path,
)
from tests.schema_validation import assert_matches_schema

NONEXISTENT_API_ID = 999999999


def _api_list(api_client, admin_headers, **params):
    return api_client.get(
        "/api/v1/api/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 500, **params},
    )


def _assert_tags_then_id_order(rows: list[dict]) -> None:
    for prev, curr in zip(rows, rows[1:]):
        prev_tags = prev.get("tags") or ""
        curr_tags = curr.get("tags") or ""
        if prev_tags == curr_tags:
            assert prev["id"] <= curr["id"]
        else:
            assert prev_tags <= curr_tags


def test_tc_api_api_001__create_api_metadata_success(api_client, admin_headers):
    payload = api_create_payload(method="GET", summary="created via API test")
    api_id = None
    try:
        resp = api_client.post("/api/v1/api/create", json=payload, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200

        list_resp = _api_list(api_client, admin_headers, path=payload["path"])
        assert list_resp.status_code == 200, list_resp.text
        item = find_api_in_list(
            list_resp.json()["data"],
            path=payload["path"],
            method=payload["method"],
        )
        assert item is not None
        api_id = item["id"]
        assert item["summary"] == payload["summary"]
        assert item["tags"] == payload["tags"]

        get_resp = api_client.get(
            "/api/v1/api/get",
            headers=admin_headers,
            params={"id": api_id},
        )
        assert get_resp.status_code == 200, get_resp.text
        detail = get_resp.json()["data"]
        assert detail["path"] == payload["path"]
        assert detail["method"] == payload["method"]
        assert detail["summary"] == payload["summary"]
        assert detail["tags"] == payload["tags"]

        assert_matches_schema(body, "POST /api/v1/api/create")
    finally:
        if api_id is not None:
            factory_cleanup_api(api_id)


def test_tc_api_api_002__list_apis_pagination_and_filter(
    api_client,
    admin_headers,
    apis_for_filter,
):
    path_token = apis_for_filter["path_token"]
    tags_token = apis_for_filter["tags_token"]

    default_resp = api_client.get(
        "/api/v1/api/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 10},
    )
    assert default_resp.status_code == 200, default_resp.text
    default_body = default_resp.json()
    assert default_body["code"] == 200
    assert isinstance(default_body["data"], list)
    assert "total" in default_body
    assert default_body["page"] == 1
    assert default_body["page_size"] == 10
    _assert_tags_then_id_order(default_body["data"])
    assert_matches_schema(default_body, "GET /api/v1/api/list")

    path_resp = _api_list(api_client, admin_headers, path=path_token)
    assert path_resp.status_code == 200, path_resp.text
    for row in path_resp.json()["data"]:
        assert path_token in row["path"]

    tags_resp = _api_list(api_client, admin_headers, tags=tags_token)
    assert tags_resp.status_code == 200, tags_resp.text
    for row in tags_resp.json()["data"]:
        assert tags_token in row["tags"]


def test_tc_api_api_003__get_api_detail_by_id(api_client, admin_headers, existing_api):
    resp = api_client.get(
        "/api/v1/api/get",
        headers=admin_headers,
        params={"id": existing_api["id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    assert data["id"] == existing_api["id"]
    assert data["path"] == existing_api["path"]
    assert data["method"] == existing_api["method"]
    assert data["summary"] == existing_api["summary"]
    assert data["tags"] == existing_api["tags"]
    assert_matches_schema(body, "GET /api/v1/api/get")


def test_tc_api_api_004__update_api_metadata(api_client, admin_headers, existing_api):
    new_summary = f"{settings.api_prefix}-updated-summary"
    new_tags = f"{settings.api_prefix}-updated-tags"
    update_resp = api_client.post(
        "/api/v1/api/update",
        headers=admin_headers,
        json={
            "id": existing_api["id"],
            "path": existing_api["path"],
            "method": existing_api["method"],
            "summary": new_summary,
            "tags": new_tags,
        },
    )
    assert update_resp.status_code == 200, update_resp.text
    update_body = update_resp.json()
    assert update_body["code"] == 200

    get_resp = api_client.get(
        "/api/v1/api/get",
        headers=admin_headers,
        params={"id": existing_api["id"]},
    )
    assert get_resp.status_code == 200, get_resp.text
    detail = get_resp.json()["data"]
    assert detail["summary"] == new_summary
    assert detail["tags"] == new_tags
    assert detail["path"] == existing_api["path"]
    assert detail["method"] == existing_api["method"]

    assert_matches_schema(update_body, "POST /api/v1/api/update")


def test_tc_api_api_005__delete_api_record(api_client, admin_headers, ephemeral_api):
    api_id = ephemeral_api["id"]
    delete_resp = api_client.delete(
        "/api/v1/api/delete",
        headers=admin_headers,
        params={"api_id": api_id},
    )
    assert delete_resp.status_code == 200, delete_resp.text
    delete_body = delete_resp.json()
    assert delete_body["code"] == 200

    get_resp = api_client.get(
        "/api/v1/api/get",
        headers=admin_headers,
        params={"id": api_id},
    )
    assert get_resp.status_code == 404, get_resp.text
    assert get_resp.json().get("code") == 404

    list_resp = _api_list(api_client, admin_headers)
    assert list_resp.status_code == 200
    assert find_api_in_list(list_resp.json()["data"], api_id=api_id) is None

    assert_matches_schema(delete_body, "DELETE /api/v1/api/delete")


def test_tc_api_api_006__refresh_sync_openapi_registry(api_client, admin_headers):
    orphan_path = orphan_api_path()
    payload = api_create_payload(
        path=orphan_path,
        method="GET",
        summary="orphan row",
        tags="orphan",
    )
    orphan_id = None
    try:
        create_resp = api_client.post(
            "/api/v1/api/create",
            json=payload,
            headers=admin_headers,
        )
        assert create_resp.status_code == 200, create_resp.text
        assert create_resp.json().get("code") == 200

        before_resp = _api_list(api_client, admin_headers, path=orphan_path)
        assert before_resp.status_code == 200, before_resp.text
        orphan_item = find_api_in_list(before_resp.json()["data"], path=orphan_path)
        assert orphan_item is not None
        orphan_id = orphan_item["id"]

        refresh_resp = api_client.post("/api/v1/api/refresh", headers=admin_headers)
        assert refresh_resp.status_code == 200, refresh_resp.text
        refresh_body = refresh_resp.json()
        assert refresh_body["code"] == 200

        after_resp = _api_list(api_client, admin_headers, path=orphan_path)
        assert after_resp.status_code == 200, after_resp.text
        assert find_api_in_list(after_resp.json()["data"], path=orphan_path) is None

        list_resp = _api_list(api_client, admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        lookup = {
            auth_route_lookup_key(row["method"], row["path"]): row
            for row in list_resp.json()["data"]
        }
        for route in factory_list_auth_routes():
            key = auth_route_lookup_key(route["method"], route["path"])
            assert key in lookup, f"missing auth route row: {route['method']} {route['path']}"
            row = lookup[key]
            assert row["summary"] == route["summary"]
            assert row["tags"] == route["tags"]

        assert_matches_schema(refresh_body, "POST /api/v1/api/refresh")
    finally:
        if orphan_id is not None:
            api_client.delete(
                "/api/v1/api/delete",
                headers=admin_headers,
                params={"api_id": orphan_id},
            )


def test_tc_api_api_007__reject_create_missing_required_fields(api_client, admin_headers):
    before = count_apis_matching(api_client, admin_headers)

    for omit in (frozenset({"path"}), frozenset({"method"}), frozenset({"tags"})):
        resp = api_client.post(
            "/api/v1/api/create",
            headers=admin_headers,
            json=api_create_payload(omit=omit),
        )
        assert resp.status_code == 422, resp.text

    after = count_apis_matching(api_client, admin_headers)
    assert after == before


def test_tc_api_api_008__reject_invalid_method_enum(api_client, admin_headers):
    before = count_apis_matching(api_client, admin_headers)
    resp = api_client.post(
        "/api/v1/api/create",
        headers=admin_headers,
        json=api_create_payload(method="INVALID"),
    )
    assert resp.status_code == 422, resp.text
    after = count_apis_matching(api_client, admin_headers)
    assert after == before


def test_tc_api_api_009__reject_invalid_api_id_operations(api_client, admin_headers):
    get_resp = api_client.get(
        "/api/v1/api/get",
        headers=admin_headers,
        params={"id": NONEXISTENT_API_ID},
    )
    assert get_resp.status_code == 404, get_resp.text
    assert get_resp.json().get("code") == 404

    update_resp = api_client.post(
        "/api/v1/api/update",
        headers=admin_headers,
        json={
            "id": NONEXISTENT_API_ID,
            "path": f"/api/v1/{settings.api_prefix}/missing",
            "method": "GET",
            "summary": "missing",
            "tags": "missing",
        },
    )
    assert update_resp.status_code == 404, update_resp.text
    assert update_resp.json().get("code") == 404

    delete_resp = api_client.delete(
        "/api/v1/api/delete",
        headers=admin_headers,
        params={"api_id": NONEXISTENT_API_ID},
    )
    assert delete_resp.status_code == 404, delete_resp.text
    assert delete_resp.json().get("code") == 404


def test_tc_api_api_010__reject_unauthorized_api_access(
    api_client,
    admin_headers,
    limited_role_user_token,
):
    """API-PLAN-FINDING-TR-001: no-token list may return 401 or 422 (FastAPI header validation)."""
    list_resp = api_client.get("/api/v1/api/list")
    assert list_resp.status_code in (401, 422), list_resp.text
    assert list_resp.status_code != 200

    limited_headers = {"token": limited_role_user_token}
    limited_list = api_client.get("/api/v1/api/list", headers=limited_headers)
    assert limited_list.status_code == 403, limited_list.text

    limited_create = api_client.post(
        "/api/v1/api/create",
        headers=limited_headers,
        json=api_create_payload(),
    )
    assert limited_create.status_code == 403, limited_create.text

    admin_list = api_client.get("/api/v1/api/list", headers=admin_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert admin_list.json()["code"] == 200


def test_tc_api_api_011__list_filter_boundary_values(api_client, admin_headers):
    missing_keyword = f"{settings.api_prefix}-nomatch-{uuid.uuid4().hex}"
    no_match_resp = api_client.get(
        "/api/v1/api/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 10, "path": missing_keyword},
    )
    assert no_match_resp.status_code == 200, no_match_resp.text
    no_match_body = no_match_resp.json()
    assert no_match_body["code"] == 200
    data = no_match_body.get("data") or []
    total = no_match_body.get("total", 0)
    assert data == [] or total == 0

    page_resp = api_client.get(
        "/api/v1/api/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 1},
    )
    assert page_resp.status_code == 200, page_resp.text
    page_body = page_resp.json()
    assert page_body["code"] == 200
    assert len(page_body.get("data") or []) <= 1


def test_tc_api_api_012__duplicate_path_method_create_behavior(api_client, admin_headers):
    """Neutral probe: documents whether duplicate path+method is rejected or allowed."""
    payload = api_create_payload(method="POST", summary="duplicate path test")
    created_ids: list[int] = []
    try:
        first_resp = api_client.post("/api/v1/api/create", json=payload, headers=admin_headers)
        assert first_resp.status_code == 200, first_resp.text
        assert first_resp.json()["code"] == 200

        second_resp = api_client.post("/api/v1/api/create", json=payload, headers=admin_headers)
        assert second_resp.status_code != 500, second_resp.text

        count = count_apis_with_method_path(
            api_client,
            admin_headers,
            path=payload["path"],
            method=payload["method"],
        )
        assert count >= 1

        list_resp = _api_list(api_client, admin_headers, path=payload["path"])
        assert list_resp.status_code == 200, list_resp.text
        for row in list_resp.json()["data"]:
            if row.get("path") == payload["path"] and row.get("method") == payload["method"]:
                created_ids.append(row["id"])
    finally:
        for api_id in set(created_ids):
            factory_cleanup_api(api_id)
