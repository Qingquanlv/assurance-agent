"""E2E tests for system menu management UI.

Traceability: RET-menu-management-20260716-163247-cursor
Cases: TC_MENU_E2E_001 – TC_MENU_E2E_006
Plan: qa/changes/RET-menu-management-20260716-163247-cursor/plans/e2e-codegen-plan.md

Run: uv run pytest tests/e2e/test_menu_e2e.py -v --headed
"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

from tests.config import settings
from tests.e2e.adapters.menu import cleanup_menu, make_menu
from tests.e2e.conftest import (
    fill_menu_modal,
    frontend_path,
    menu_row,
    navigate_menu_mgmt,
    verify_menu_list_contains,
)
from tests.testdata.domain.menu import unique_menu_name


def _capture_menu_id_by_name(page: Page, token: str, menu_name: str) -> int | None:
    list_resp = page.request.get(
        f"{settings.base_url}/api/v1/menu/list",
        headers={"token": token},
        params={"page": 1, "page_size": 200},
    )
    assert list_resp.ok, list_resp.text()
    menus = list_resp.json().get("data") or []

    def _walk(nodes: list[dict]) -> int | None:
        for node in nodes:
            if node.get("name") == menu_name:
                return node["id"]
            found = _walk(node.get("children") or [])
            if found is not None:
                return found
        return None

    return _walk(menus)


def test_tc_menu_e2e_001__admin_enters_menu_management_page(
    admin_menu_page: Page,
) -> None:
    expect(admin_menu_page.get_by_text("菜单列表")).to_be_visible(timeout=8000)
    expect(admin_menu_page.get_by_text("菜单名称")).to_be_visible(timeout=8000)
    expect(admin_menu_page.get_by_text("访问路径")).to_be_visible(timeout=8000)
    expect(admin_menu_page.get_by_role("table")).to_be_visible(timeout=8000)
    expect(admin_menu_page.get_by_role("button", name="新建根菜单")).to_be_visible(timeout=8000)


def test_tc_menu_e2e_002__admin_creates_menu_and_sees_in_tree(
    admin_menu_page: Page, admin_token: str
) -> None:
    menu_name = unique_menu_name()
    suffix = uuid.uuid4().hex[:8]
    menu_path = f"/{settings.menu_prefix}/{suffix}"
    created_menu_id: int | None = None

    try:
        expect(admin_menu_page.get_by_text("菜单列表")).to_be_visible(timeout=8000)
        expect(admin_menu_page.get_by_role("button", name="新建根菜单")).to_be_visible(timeout=8000)

        admin_menu_page.get_by_role("button", name="新建根菜单").click()
        dialog = admin_menu_page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=8000)
        fill_menu_modal(admin_menu_page, name=menu_name, path=menu_path)

        with admin_menu_page.expect_response(lambda r: "menu/list" in r.url, timeout=15000):
            dialog.get_by_role("button", name="保存").click()
        expect(dialog).not_to_be_visible(timeout=8000)

        expect(menu_row(admin_menu_page, menu_name)).to_be_visible(timeout=8000)

        created_menu_id = _capture_menu_id_by_name(admin_menu_page, admin_token, menu_name)
        assert created_menu_id is not None, f"created menu {menu_name!r} not found via API"
    finally:
        if created_menu_id is not None:
            cleanup_menu(created_menu_id)


def test_tc_menu_e2e_003__non_admin_cannot_access_menu_management(
    limited_user_page: Page,
) -> None:
    menuitem = limited_user_page.get_by_role("menuitem", name="菜单管理")
    expect(menuitem).not_to_be_visible(timeout=8000)

    limited_user_page.goto(frontend_path("/system/menu"))
    limited_user_page.wait_for_load_state("networkidle", timeout=15000)

    on_404 = "/404" in limited_user_page.url or limited_user_page.get_by_text("404").count() > 0
    if on_404:
        expect(limited_user_page.get_by_role("button", name="新建根菜单")).not_to_be_visible()
    else:
        expect(limited_user_page.get_by_role("button", name="新建根菜单")).not_to_be_visible(
            timeout=5000
        )
        expect(limited_user_page.get_by_role("button", name="编辑")).not_to_be_visible(timeout=5000)
        expect(limited_user_page.get_by_role("button", name="删除")).not_to_be_visible(timeout=5000)


def _confirm_menu_delete(page: Page) -> None:
    popconfirm = page.locator(".n-popconfirm").filter(has_text="确定删除该菜单吗?")
    popconfirm.wait_for(state="visible", timeout=8000)
    confirm_btn = popconfirm.get_by_role("button", name="确认")
    if confirm_btn.count() > 0:
        confirm_btn.click()
    else:
        popconfirm.get_by_role("button", name="确定").click()


def test_tc_menu_e2e_004__admin_edits_menu_metadata(
    admin_menu_page: Page, admin_headers: dict[str, str]
) -> None:
    """API pre-seed via isolated_worker make_menu; edit name/path in modal (index.vue)."""
    menu = make_menu()
    original_name = menu["name"]
    menu_id = menu["id"]
    new_name = unique_menu_name()
    suffix = uuid.uuid4().hex[:8]
    new_path = f"/{settings.menu_prefix}/e-{suffix}"
    try:
        verify_menu_list_contains(admin_headers, original_name)
        navigate_menu_mgmt(admin_menu_page)

        row = menu_row(admin_menu_page, original_name)
        expect(row).to_be_visible(timeout=8000)
        row.get_by_role("button", name="编辑").click()

        dialog = admin_menu_page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=8000)
        fill_menu_modal(admin_menu_page, name=new_name, path=new_path)

        with admin_menu_page.expect_response(lambda r: "menu/list" in r.url, timeout=15000):
            dialog.get_by_role("button", name="保存").click()
        expect(dialog).not_to_be_visible(timeout=8000)

        expect(menu_row(admin_menu_page, new_name)).to_be_visible(timeout=8000)
        expect(admin_menu_page.get_by_text(new_path)).to_be_visible(timeout=8000)
    finally:
        cleanup_menu(menu_id)


def test_tc_menu_e2e_005__admin_deletes_leaf_menu(
    admin_menu_page: Page, admin_headers: dict[str, str]
) -> None:
    """API pre-seed via isolated_worker make_menu; delete leaf row via UI popconfirm (index.vue)."""
    menu = make_menu()
    menu_name = menu["name"]
    menu_id = menu["id"]
    try:
        verify_menu_list_contains(admin_headers, menu_name)
        navigate_menu_mgmt(admin_menu_page)

        row = menu_row(admin_menu_page, menu_name)
        expect(row).to_be_visible(timeout=8000)
        row.get_by_role("button", name="删除").click()
        expect(admin_menu_page.get_by_text("确定删除该菜单吗?")).to_be_visible(timeout=5000)
        with admin_menu_page.expect_response(lambda r: "menu/list" in r.url, timeout=15000):
            _confirm_menu_delete(admin_menu_page)
        expect(menu_row(admin_menu_page, menu_name)).not_to_be_visible(timeout=8000)
    finally:
        try:
            cleanup_menu(menu_id)
        except RuntimeError:
            pass


def test_tc_menu_e2e_006__admin_cannot_delete_menu_with_children(
    admin_menu_page: Page, admin_headers: dict[str, str]
) -> None:
    """E2E-PLAN-NR-006: index.vue hides 删除 when row.children.length > 0 (line 203)."""
    parent = make_menu()
    child = make_menu(parent_id=parent["id"])
    parent_name = parent["name"]
    child_name = child["name"]
    parent_id = parent["id"]
    child_id = child["id"]
    try:
        verify_menu_list_contains(admin_headers, parent_name)
        verify_menu_list_contains(admin_headers, child_name)
        navigate_menu_mgmt(admin_menu_page)

        parent_row = menu_row(admin_menu_page, parent_name)
        expect(parent_row).to_be_visible(timeout=8000)
        expect(parent_row.get_by_role("button", name="删除")).not_to_be_visible(timeout=5000)

        expect(menu_row(admin_menu_page, child_name)).to_be_visible(timeout=8000)
        expect(admin_menu_page.get_by_text(parent_name)).to_be_visible(timeout=8000)
    finally:
        cleanup_menu(child_id)
        cleanup_menu(parent_id)
