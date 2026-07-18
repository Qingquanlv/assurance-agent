"""API tests for system.api module — RET-api-management-20260716-192358-cursor."""

from __future__ import annotations

import uuid

from tests.api.adapters.api import factory_cleanup_api, factory_list_auth_routes, factory_make_api
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


def _assert_validation_mentions_field(resp, field: str) -> None:
    body = resp.json()
    detail = body.get("detail")
    if isinstance(detail, list):
        locs = [item.get("loc", ()) for item in detail if isinstance(item, dict)]
        assert any(field in loc for loc in locs), body
    else:
        assert field in str(body).lower(), body


def test_tc_apis_api_001__create_api_metadata_success(api_client, admin_headers):
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


def test_tc_apis_api_002__list_apis_pagination(api_client, admin_headers):
    resp = api_client.get(
        "/api/v1/api/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert isinstance(body["data"], list)
    assert "total" in body
    assert body["page"] == 1
    assert body["page_size"] == 10
    for row in body["data"]:
        assert "path" in row
        assert "method" in row
        assert "summary" in row
        assert "tags" in row
    assert_matches_schema(body, "GET /api/v1/api/list")


def test_tc_apis_api_003__list_filter_by_path_summary_tags(
    api_client,
    admin_headers,
    apis_for_filter,
):
    path_token = apis_for_filter["path_token"]
    summary_token = apis_for_filter["summary_token"]
    tags_token = apis_for_filter["tags_token"]

    default_resp = api_client.get(
        "/api/v1/api/list",
        headers=admin_headers,
        params={"page": 1, "page_size": 10},
    )
    assert default_resp.status_code == 200, default_resp.text
    default_body = default_resp.json()
    assert default_body["code"] == 200

    for param_name in ("path", "summary", "tags"):
        empty_filter_resp = api_client.get(
            "/api/v1/api/list",
            headers=admin_headers,
            params={"page": 1, "page_size": 10, param_name: ""},
        )
        assert empty_filter_resp.status_code == 200, empty_filter_resp.text
        empty_body = empty_filter_resp.json()
        assert empty_body["code"] == 200
        assert empty_body.get("total") == default_body.get("total")

    path_resp = _api_list(api_client, admin_headers, path=path_token)
    assert path_resp.status_code == 200, path_resp.text
    for row in path_resp.json()["data"]:
        assert path_token in row["path"]

    summary_resp = _api_list(api_client, admin_headers, summary=summary_token)
    assert summary_resp.status_code == 200, summary_resp.text
    for row in summary_resp.json()["data"]:
        assert summary_token in row["summary"]

    tags_resp = _api_list(api_client, admin_headers, tags=tags_token)
    assert tags_resp.status_code == 200, tags_resp.text
    for row in tags_resp.json()["data"]:
        assert tags_token in row["tags"]
    assert_matches_schema(tags_resp.json(), "GET /api/v1/api/list")

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


def test_tc_apis_api_004__get_api_detail_by_id(api_client, admin_headers, existing_api):
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


def test_tc_apis_api_005__update_api_metadata(api_client, admin_headers, existing_api):
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

    list_resp = _api_list(api_client, admin_headers, path=existing_api["path"])
    assert list_resp.status_code == 200, list_resp.text
    list_item = find_api_in_list(list_resp.json()["data"], api_id=existing_api["id"])
    assert list_item is not None
    assert list_item["summary"] == new_summary
    assert list_item["tags"] == new_tags
    assert list_item["path"] == existing_api["path"]
    assert list_item["method"] == existing_api["method"]

    assert_matches_schema(update_body, "POST /api/v1/api/update")


def test_tc_apis_api_006__delete_api_record(api_client, admin_headers, ephemeral_api):
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


def test_tc_apis_api_007__refresh_sync_route_registry(api_client, admin_headers):
    """Orphan row seeded via factory_make_api (not HTTP create) per factory-first contract."""
    orphan_path = orphan_api_path()
    orphan_id = None
    try:
        orphan_snapshot = factory_make_api(
            path=orphan_path,
            method="GET",
            summary="orphan row",
            tags="orphan",
        )
        orphan_id = orphan_snapshot["id"]

        auth_keys = {
            auth_route_lookup_key(route["method"], route["path"])
            for route in factory_list_auth_routes()
        }
        assert auth_route_lookup_key("GET", orphan_path) not in auth_keys

        before_resp = _api_list(api_client, admin_headers, path=orphan_path)
        assert before_resp.status_code == 200, before_resp.text
        assert find_api_in_list(before_resp.json()["data"], path=orphan_path) is not None

        refresh_resp = api_client.post("/api/v1/api/refresh", headers=admin_headers)
        assert refresh_resp.status_code == 200, refresh_resp.text
        refresh_body = refresh_resp.json()
        assert refresh_body["code"] == 200

        after_resp = _api_list(api_client, admin_headers, path=orphan_path)
        assert after_resp.status_code == 200, after_resp.text
        assert find_api_in_list(after_resp.json()["data"], path=orphan_path) is None
        orphan_id = None

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
            factory_cleanup_api(orphan_id)


def test_tc_apis_api_008__reject_unauthorized_api_access(
    api_client,
    admin_headers,
    limited_role_user_token,
):
    """API-PLAN-FINDING-008-001: no-token may return 401 or 422 (FastAPI header validation)."""
    list_resp = api_client.get("/api/v1/api/list")
    assert list_resp.status_code in (401, 422), list_resp.text
    assert list_resp.status_code != 200

    create_resp = api_client.post(
        "/api/v1/api/create",
        json=api_create_payload(),
    )
    assert create_resp.status_code in (401, 422), create_resp.text
    assert create_resp.status_code != 200

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


def test_tc_apis_api_009__reject_create_missing_required_fields(api_client, admin_headers):
    """Plan tests path/method/tags omission; ApiCreate.summary defaults to empty string."""
    before = count_apis_matching(api_client, admin_headers)

    omit_variants = (
        (frozenset({"path"}), "path"),
        (frozenset({"method"}), "method"),
        (frozenset({"tags"}), "tags"),
    )
    for omit, field in omit_variants:
        resp = api_client.post(
            "/api/v1/api/create",
            headers=admin_headers,
            json=api_create_payload(omit=omit),
        )
        assert resp.status_code == 422, resp.text
        _assert_validation_mentions_field(resp, field)

    after = count_apis_matching(api_client, admin_headers)
    assert after == before


def test_tc_apis_api_010__reject_invalid_method_enum(api_client, admin_headers):
    before = count_apis_matching(api_client, admin_headers)
    resp = api_client.post(
        "/api/v1/api/create",
        headers=admin_headers,
        json=api_create_payload(method="INVALID"),
    )
    assert resp.status_code == 422, resp.text
    after = count_apis_matching(api_client, admin_headers)
    assert after == before


def test_tc_apis_api_011__reject_invalid_api_id_operations(api_client, admin_headers):
    """API-PLAN-FINDING-011-001: backend returns HTTP 404 + code=404 for nonexistent id."""
    get_resp = api_client.get(
        "/api/v1/api/get",
        headers=admin_headers,
        params={"id": NONEXISTENT_API_ID},
    )
    assert get_resp.status_code == 404, get_resp.text
    assert get_resp.json().get("code") == 404
    assert get_resp.status_code < 500

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
    assert update_resp.status_code < 500

    delete_resp = api_client.delete(
        "/api/v1/api/delete",
        headers=admin_headers,
        params={"api_id": NONEXISTENT_API_ID},
    )
    assert delete_resp.status_code == 404, delete_resp.text
    assert delete_resp.json().get("code") == 404
    assert delete_resp.status_code < 500


def test_tc_apis_api_012__duplicate_path_method_create_behavior(api_client, admin_headers):
    """Neutral probe: no path+method unique constraint documented (Needs Review #1)."""
    payload = api_create_payload()
    created_ids: list[int] = []
    try:
        first_resp = api_client.post(
            "/api/v1/api/create",
            json=payload,
            headers=admin_headers,
        )
        assert first_resp.status_code == 200, first_resp.text
        assert first_resp.json().get("code") == 200

        list_resp = _api_list(api_client, admin_headers, path=payload["path"])
        assert list_resp.status_code == 200, list_resp.text
        first_item = find_api_in_list(
            list_resp.json()["data"],
            path=payload["path"],
            method=payload["method"],
        )
        assert first_item is not None
        created_ids.append(first_item["id"])

        second_resp = api_client.post(
            "/api/v1/api/create",
            json=payload,
            headers=admin_headers,
        )
        assert second_resp.status_code < 500, second_resp.text

        if second_resp.status_code == 200 and second_resp.json().get("code") == 200:
            after_list = _api_list(api_client, admin_headers, path=payload["path"])
            for row in after_list.json()["data"]:
                if row.get("path") == payload["path"] and row.get("method") == payload["method"]:
                    if row["id"] not in created_ids:
                        created_ids.append(row["id"])

        duplicate_count = count_apis_with_method_path(
            api_client,
            admin_headers,
            path=payload["path"],
            method=payload["method"],
        )
        assert duplicate_count >= 1
    finally:
        for api_id in created_ids:
            factory_cleanup_api(api_id)
