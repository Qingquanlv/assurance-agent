"""E2E tests for system role management UI.

Traceability: RET-role-management-20260716-163247-cursor
Cases: TC_ROLE_E2E_001 – TC_ROLE_E2E_006
Plan: qa/changes/RET-role-management-20260716-163247-cursor/plans/e2e-codegen-plan.md

Run: uv run pytest tests/e2e/test_role_e2e.py -v --headed
"""

from __future__ import annotations

import re
import uuid

from playwright.sync_api import Page, expect

from tests.config import settings
from tests.e2e.adapters.role import cleanup_role
from tests.e2e.conftest import (
    fill_role_modal,
    frontend_path,
    role_row,
    search_roles,
    ui_login,
)


def test_tc_role_e2e_001__admin_enters_role_management(page: Page) -> None:
    """E2E-PLAN-NR-001: case steps require menu navigation, not direct goto."""
    ui_login(page, settings.admin_username, settings.admin_password)
    page.get_by_text("系统管理").click()
    role_menu = page.get_by_role("menuitem", name="角色管理")
    expect(role_menu).to_be_visible(timeout=8000)
    with page.expect_response(lambda r: "role/list" in r.url, timeout=15000):
        role_menu.click()
    expect(page).to_have_url(re.compile(r".*/system/role"), timeout=8000)

    expect(page.get_by_text("角色列表")).to_be_visible()
    table = page.get_by_role("table")
    expect(table.get_by_text("角色名")).to_be_visible()
    expect(table.get_by_text("角色描述")).to_be_visible()
    expect(page.get_by_placeholder("请输入角色名")).to_be_visible()
    expect(page.get_by_role("button", name="新建角色")).to_be_visible()


def test_tc_role_e2e_002__admin_creates_role_and_sees_in_list(
    admin_role_page: Page, admin_token: str
) -> None:
    suffix = uuid.uuid4().hex[:8]
    role_name = f"{settings.role_prefix}-c-{suffix}"
    role_desc = f"create-desc-{suffix}"
    created_role_id: int | None = None

    try:
        admin_role_page.get_by_role("button", name="新建角色").click()
        dialog = admin_role_page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=8000)
        fill_role_modal(admin_role_page, name=role_name, desc=role_desc)
        with admin_role_page.expect_response(lambda r: "role/list" in r.url, timeout=15000):
            dialog.get_by_role("button", name="保存").click()
        expect(dialog).not_to_be_visible(timeout=8000)

        search_roles(admin_role_page, role_name=role_name)
        table = admin_role_page.get_by_role("table")
        expect(table.get_by_text(role_name)).to_be_visible(timeout=8000)
        expect(table.get_by_text(role_desc)).to_be_visible(timeout=8000)

        list_resp = admin_role_page.request.get(
            f"{settings.base_url}/api/v1/role/list",
            headers={"token": admin_token},
            params={"role_name": role_name, "page": 1, "page_size": 5},
        )
        assert list_resp.ok, list_resp.text()
        rows = list_resp.json().get("data") or []
        match = next((row for row in rows if row.get("name") == role_name), None)
        assert match is not None, f"created role {role_name!r} not found via API"
        created_role_id = match["id"]
    finally:
        if created_role_id is not None:
            cleanup_role(created_role_id)


def test_tc_role_e2e_003__admin_edits_role_metadata(
    admin_role_page: Page, editable_role: dict
) -> None:
    original_name = editable_role["name"]
    suffix = uuid.uuid4().hex[:8]
    new_name = f"{settings.role_prefix}-e-{suffix}"
    new_desc = f"updated-desc-{suffix}"

    search_roles(admin_role_page, role_name=original_name)
    expect(role_row(admin_role_page, original_name)).to_be_visible(timeout=8000)
    role_row(admin_role_page, original_name).get_by_role("button", name="编辑").click()

    dialog = admin_role_page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=8000)
    fill_role_modal(admin_role_page, name=new_name, desc=new_desc)

    with admin_role_page.expect_response(lambda r: "role/list" in r.url, timeout=15000):
        dialog.get_by_role("button", name="保存").click()
    expect(dialog).not_to_be_visible(timeout=8000)

    search_roles(admin_role_page, role_name=new_name)
    row = role_row(admin_role_page, new_name)
    expect(row).to_be_visible(timeout=8000)
    expect(row.get_by_text(new_name)).to_be_visible(timeout=8000)
    expect(row.get_by_text(new_desc)).to_be_visible(timeout=8000)


def test_tc_role_e2e_004__admin_deletes_role_via_ui(
    admin_role_page: Page, deletable_role: dict
) -> None:
    role_name = deletable_role["name"]
    search_roles(admin_role_page, role_name=role_name)
    row = role_row(admin_role_page, role_name)
    expect(row).to_be_visible(timeout=8000)
    row.get_by_role("button", name="删除").click()
    expect(admin_role_page.get_by_text("确定删除该角色吗?")).to_be_visible(timeout=5000)
    with admin_role_page.expect_response(lambda r: "role/list" in r.url, timeout=15000):
        admin_role_page.get_by_role("dialog").get_by_role("button", name="确定").click()
    search_roles(admin_role_page, role_name=role_name)
    expect(role_row(admin_role_page, role_name)).not_to_be_visible(timeout=8000)


def test_tc_role_e2e_005__admin_configures_menu_and_api_permissions(
    admin_role_page: Page, configurable_role: dict
) -> None:
    """E2E-PLAN-NR-005: first-checkbox smoke for menu/API permission tree."""
    role_name = configurable_role["name"]
    search_roles(admin_role_page, role_name=role_name)
    row = role_row(admin_role_page, role_name)
    expect(row).to_be_visible(timeout=8000)

    row.get_by_role("button", name="设置权限").click()
    drawer = admin_role_page.locator(".n-drawer")
    expect(drawer.get_by_text("设置权限")).to_be_visible(timeout=15000)
    expect(drawer.get_by_text("菜单权限")).to_be_visible(timeout=8000)

    menu_checkboxes = drawer.get_by_role("checkbox")
    expect(menu_checkboxes.first).to_be_visible(timeout=15000)
    assert menu_checkboxes.count() > 0, "menu tree must expose at least one checkable node"
    menu_checkbox = menu_checkboxes.first
    menu_checkbox.check()
    expect(menu_checkbox).to_be_checked()

    drawer.get_by_text("接口权限").click()
    api_checkboxes = drawer.get_by_role("checkbox")
    expect(api_checkboxes.first).to_be_visible(timeout=15000)
    assert api_checkboxes.count() > 0, "API tree must expose at least one checkable node"
    api_checkbox = api_checkboxes.first
    api_checkbox.check()
    expect(api_checkbox).to_be_checked()

    with admin_role_page.expect_response(
        lambda r: "role/authorized" in r.url and r.request.method == "POST",
        timeout=15000,
    ):
        drawer.get_by_role("button", name="确定").click()
    expect(admin_role_page.get_by_text("设置成功")).to_be_visible(timeout=5000)
    expect(admin_role_page.locator(".n-drawer")).not_to_be_visible(timeout=8000)

    row.get_by_role("button", name="设置权限").click()
    expect(drawer.get_by_text("设置权限")).to_be_visible(timeout=15000)
    expect(drawer.get_by_role("checkbox").first).to_be_checked(timeout=8000)
    drawer.get_by_text("接口权限").click()
    expect(drawer.get_by_role("checkbox").first).to_be_checked(timeout=8000)


def test_tc_role_e2e_006__non_admin_cannot_access_role_management(
    limited_user_page: Page,
) -> None:
    """E2E-PLAN-NR-003/004: compose_limited_role deny-all; accept 404 or buttons-hidden."""
    menuitem = limited_user_page.get_by_role("menuitem", name="角色管理")
    expect(menuitem).not_to_be_visible(timeout=8000)

    limited_user_page.goto(frontend_path("/system/role"))
    limited_user_page.wait_for_load_state("networkidle", timeout=15000)

    on_404 = "/404" in limited_user_page.url or limited_user_page.get_by_text("404").count() > 0
    if on_404:
        expect(limited_user_page.get_by_role("button", name="新建角色")).not_to_be_visible()
    else:
        expect(limited_user_page.get_by_role("button", name="新建角色")).not_to_be_visible(timeout=5000)
        expect(limited_user_page.get_by_role("button", name="编辑")).not_to_be_visible(timeout=5000)
        expect(limited_user_page.get_by_role("button", name="删除")).not_to_be_visible(timeout=5000)
        expect(limited_user_page.get_by_role("button", name="设置权限")).not_to_be_visible(timeout=5000)
