"""E2E tests for system API management UI.

Traceability: RET-api-management-20260716-163247-cursor
Cases: TC_APIM_E2E_001 – TC_APIM_E2E_006
Plan: qa/changes/RET-api-management-20260716-163247-cursor/plans/e2e-codegen-plan.md

Run: uv run pytest tests/e2e/test_api_e2e.py -v --headed
Filter: uv run pytest tests/e2e/test_api_e2e.py -v --headed -k "tc_apim_e2e"
"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

from tests.config import settings
from tests.e2e.adapters.api import cleanup_api
from tests.e2e.conftest import (
    api_row,
    confirm_api_delete,
    e2e_login_admin,
    fill_api_modal,
    frontend_path,
    search_apis,
)


def _capture_api_id_by_path(page: Page, token: str, path: str) -> int | None:
    list_resp = page.request.get(
        f"{settings.base_url}/api/v1/api/list",
        headers={"token": token},
        params={"path": path, "page": 1, "page_size": 50},
    )
    assert list_resp.ok, list_resp.text()
    data = list_resp.json().get("data") or []
    match = next((row for row in data if row.get("path") == path), None)
    return match["id"] if match else None


def test_tc_apim_e2e_001__admin_enters_api_management(page: Page) -> None:
    """Admin navigates via 系统管理 → API管理 per case steps (E2E-PLAN-NR-FLOW-001)."""
    e2e_login_admin(page)
    page.get_by_text("系统管理").click()
    api_menu = page.get_by_role("menuitem", name="API管理")
    expect(api_menu).to_be_visible(timeout=8000)
    with page.expect_response(lambda r: "api/list" in r.url, timeout=15000):
        api_menu.click()
    page.wait_for_url("**/system/api**", timeout=15000)

    expect(page.get_by_role("heading", name="API列表")).to_be_visible(timeout=8000)
    table = page.get_by_role("table")
    expect(table.get_by_text("API路径")).to_be_visible()
    expect(table.get_by_text("请求方式")).to_be_visible()
    expect(table.get_by_text("API简介")).to_be_visible()
    expect(table.get_by_text("Tags")).to_be_visible()
    expect(page.get_by_role("button", name="新建API")).to_be_visible(timeout=8000)
    expect(page.get_by_role("button", name="刷新API")).to_be_visible(timeout=8000)
    expect(page.get_by_placeholder("请输入API路径")).to_be_visible()
    expect(page.get_by_placeholder("请输入API简介")).to_be_visible()
    expect(page.get_by_placeholder("请输入API模块")).to_be_visible()


def test_tc_apim_e2e_002__admin_creates_api_via_modal(
    api_page: Page, admin_token: str
) -> None:
    suffix = uuid.uuid4().hex[:8]
    api_path = f"/api/v1/{settings.api_prefix}/create-{suffix}"
    method = "GET"
    summary = f"e2e-create-{suffix}"
    tags = f"{settings.api_prefix}-module"
    created_api_id: int | None = None

    try:
        api_page.get_by_role("button", name="新建API").click()
        dialog = api_page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=8000)
        fill_api_modal(
            api_page,
            path=api_path,
            method=method,
            summary=summary,
            tags=tags,
        )
        with api_page.expect_response(lambda r: "api/create" in r.url, timeout=15000):
            dialog.get_by_role("button", name="保存").click()
        expect(dialog).not_to_be_visible(timeout=8000)

        path_substring = api_path.rsplit("/", 1)[-1]
        search_apis(api_page, path=path_substring)
        row = api_row(api_page, api_path)
        expect(row).to_be_visible(timeout=8000)
        expect(row.get_by_text(method)).to_be_visible(timeout=8000)
        expect(row.get_by_text(tags)).to_be_visible(timeout=8000)

        created_api_id = _capture_api_id_by_path(api_page, admin_token, api_path)
        assert created_api_id is not None, f"created api {api_path!r} not found via API"
    finally:
        if created_api_id is not None:
            cleanup_api(created_api_id)


def test_tc_apim_e2e_003__admin_edits_api_metadata(
    api_page: Page, editable_api: dict
) -> None:
    path = editable_api["path"]
    path_substring = path.rsplit("/", 1)[-1]
    updated_summary = f"e2e-edited-{uuid.uuid4().hex[:8]}"
    updated_tags = f"{settings.api_prefix}-edited"

    search_apis(api_page, path=path_substring)
    row = api_row(api_page, path)
    expect(row).to_be_visible(timeout=8000)
    row.get_by_role("button", name="编辑").click()

    dialog = api_page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=8000)
    dialog.get_by_placeholder("请输入API简介").fill(updated_summary)
    dialog.get_by_placeholder("请输入Tags").fill(updated_tags)

    with api_page.expect_response(lambda r: "api/update" in r.url, timeout=15000):
        dialog.get_by_role("button", name="保存").click()
    expect(api_page.get_by_text("编辑成功")).to_be_visible(timeout=8000)
    expect(dialog).not_to_be_visible(timeout=8000)

    search_apis(api_page, path=path_substring)
    updated_row = api_row(api_page, path)
    expect(updated_row.get_by_text(updated_summary)).to_be_visible(timeout=8000)
    expect(updated_row.get_by_text(updated_tags)).to_be_visible(timeout=8000)


def test_tc_apim_e2e_004__admin_deletes_api(
    api_page: Page, deletable_api: dict
) -> None:
    path = deletable_api["path"]
    path_substring = path.rsplit("/", 1)[-1]

    search_apis(api_page, path=path_substring)
    row = api_row(api_page, path)
    expect(row).to_be_visible(timeout=8000)
    row.get_by_role("button", name="删除").click()

    expect(api_page.get_by_text("确定删除该API吗?")).to_be_visible(timeout=8000)
    with api_page.expect_response(lambda r: "api/list" in r.url, timeout=15000):
        confirm_api_delete(api_page)
    expect(api_row(api_page, path)).not_to_be_visible(timeout=8000)


def test_tc_apim_e2e_005__admin_refreshes_openapi_registry(api_page: Page) -> None:
    api_page.get_by_role("button", name="刷新API").click()
    expect(
        api_page.get_by_text("此操作会根据后端 app.routes 进行路由更新")
    ).to_be_visible(timeout=8000)

    with api_page.expect_response(lambda r: "api/refresh" in r.url, timeout=15000):
        api_page.get_by_role("button", name="确定").click()
    expect(api_page.get_by_text("刷新完成")).to_be_visible(timeout=8000)
    expect(api_page.get_by_role("heading", name="API列表")).to_be_visible(timeout=8000)


def test_tc_apim_e2e_006__non_admin_cannot_access_api_management(
    limited_user_page: Page,
) -> None:
    """TC_APIM_E2E_006: limited user denied — menu hidden and no operable CRUD UI.

    E2E-PLAN-FINDING-FLOW-003: product may redirect to /404 or render a permission-masked page;
    both branches assert absence of CRUD/refresh controls.
    """
    menuitem = limited_user_page.get_by_role("menuitem", name="API管理")
    expect(menuitem).not_to_be_visible(timeout=8000)

    limited_user_page.goto(frontend_path("/system/api"))
    limited_user_page.wait_for_load_state("networkidle", timeout=15000)

    on_404 = "/404" in limited_user_page.url or limited_user_page.get_by_text("404").count() > 0
    if on_404:
        expect(limited_user_page.get_by_role("button", name="新建API")).not_to_be_visible()
    else:
        expect(limited_user_page.get_by_role("button", name="新建API")).not_to_be_visible(
            timeout=5000
        )
        expect(limited_user_page.get_by_role("button", name="编辑")).not_to_be_visible(timeout=5000)
        expect(limited_user_page.get_by_role("button", name="删除")).not_to_be_visible(timeout=5000)
        expect(limited_user_page.get_by_role("button", name="刷新API")).not_to_be_visible(
            timeout=5000
        )
