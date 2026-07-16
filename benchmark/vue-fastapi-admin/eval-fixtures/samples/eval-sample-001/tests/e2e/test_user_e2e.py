"""E2E tests for system user management UI."""

from __future__ import annotations

import re
import uuid

from playwright.sync_api import Page, expect

from tests.config import settings
from tests.e2e.adapters.user import cleanup_user
from tests.e2e.conftest import (
    fill_user_modal,
    frontend_path,
    search_users,
    ui_login,
    user_row,
)


def test_tc_user_e2e_001__admin_enters_user_management(page: Page) -> None:
    """E2E-PLAN-NR-001: case steps require menu navigation, not direct goto."""
    ui_login(page, settings.admin_username, settings.admin_password)
    page.get_by_text("系统管理").click()
    user_menu = page.get_by_role("menuitem", name="用户管理")
    expect(user_menu).to_be_visible(timeout=8000)
    with page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
        user_menu.click()
    expect(page).to_have_url(re.compile(r".*/system/user"), timeout=8000)
    expect(page.get_by_text("用户列表")).to_be_visible()
    table = page.get_by_role("table")
    expect(table.get_by_text("名称")).to_be_visible()
    expect(table.get_by_text("邮箱")).to_be_visible()
    expect(page.get_by_role("button", name="新建用户")).to_be_visible()
    expect(page.get_by_placeholder("请输入用户名称")).to_be_visible()
    expect(page.get_by_placeholder("请输入邮箱")).to_be_visible()


def test_tc_user_e2e_002__admin_creates_user_in_list(
    admin_page: Page, admin_token: str
) -> None:
    suffix = uuid.uuid4().hex[:8]
    username = f"{settings.user_prefix}-c-{suffix}"
    email = f"{settings.user_prefix}-c-{suffix}@test.local"
    password = "123456"
    created_user_id: int | None = None

    try:
        admin_page.get_by_role("button", name="新建用户").click()
        dialog = admin_page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=8000)
        fill_user_modal(
            admin_page,
            username=username,
            email=email,
            password=password,
            select_first_role=True,
            select_first_dept=True,
        )
        with admin_page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
            dialog.get_by_role("button", name="保存").click()
        expect(dialog).not_to_be_visible(timeout=8000)

        search_users(admin_page, username=username)
        expect(admin_page.get_by_role("table").get_by_text(username)).to_be_visible(timeout=8000)
        expect(admin_page.get_by_role("table").get_by_text(email)).to_be_visible(timeout=8000)
        expect(
            admin_page.get_by_role("table")
            .get_by_role("cell")
            .filter(has_text=username)
            .locator(".n-tag")
            .first
        ).to_be_visible(timeout=8000)

        list_resp = admin_page.request.get(
            f"{settings.base_url}/api/v1/user/list",
            headers={"token": admin_token},
            params={"username": username, "page": 1, "page_size": 5},
        )
        assert list_resp.ok, list_resp.text()
        rows = list_resp.json().get("data") or []
        match = next((row for row in rows if row.get("username") == username), None)
        assert match is not None, f"created user {username!r} not found via API"
        created_user_id = match["id"]
    finally:
        if created_user_id is not None:
            cleanup_user(created_user_id)


def test_tc_user_e2e_003__admin_edits_user_roles_and_dept(
    admin_page: Page, editable_user: dict
) -> None:
    user = editable_user["user"]
    role_b = editable_user["role_b"]
    dept_b = editable_user["dept_b"]
    username = user["username"]

    search_users(admin_page, username=username)
    expect(user_row(admin_page, username)).to_be_visible(timeout=8000)
    user_row(admin_page, username).get_by_role("button", name="编辑").click()

    dialog = admin_page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=8000)
    dialog.get_by_role("checkbox", name=editable_user["role_a"]["name"]).uncheck()
    dialog.get_by_role("checkbox", name=role_b["name"]).check()
    dialog.get_by_placeholder("请选择部门").click()
    admin_page.get_by_text(dept_b["name"], exact=True).click()

    with admin_page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
        dialog.get_by_role("button", name="保存").click()

    expect(admin_page.get_by_text("编辑成功")).to_be_visible(timeout=5000)
    search_users(admin_page, username=username)
    row = user_row(admin_page, username)
    expect(row.get_by_text(role_b["name"])).to_be_visible(timeout=8000)
    expect(row.get_by_text(dept_b["name"])).to_be_visible(timeout=8000)


def test_tc_user_e2e_004__admin_deletes_user(
    admin_page: Page, deletable_user: dict
) -> None:
    username = deletable_user["username"]
    search_users(admin_page, username=username)
    row = user_row(admin_page, username)
    expect(row).to_be_visible(timeout=8000)
    row.get_by_role("button", name="删除").click()
    expect(admin_page.get_by_text("确定删除该用户吗?")).to_be_visible(timeout=5000)
    with admin_page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
        admin_page.get_by_role("button", name="确定").click()
    search_users(admin_page, username=username)
    expect(user_row(admin_page, username)).not_to_be_visible(timeout=8000)


def test_tc_user_e2e_005__admin_binds_dept_to_user(
    admin_page: Page, editable_user: dict
) -> None:
    """E2E-PLAN-NR-005: wait for dialog and tree node before dept selection."""
    user = editable_user["user"]
    dept_b = editable_user["dept_b"]
    username = user["username"]

    search_users(admin_page, username=username)
    expect(user_row(admin_page, username)).to_be_visible(timeout=8000)
    user_row(admin_page, username).get_by_role("button", name="编辑").click()

    dialog = admin_page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=8000)
    dialog.get_by_placeholder("请选择部门").click()
    dept_option = admin_page.get_by_text(dept_b["name"], exact=True)
    expect(dept_option).to_be_visible(timeout=8000)
    dept_option.click()

    with admin_page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
        dialog.get_by_role("button", name="保存").click()
    expect(dialog).not_to_be_visible(timeout=8000)

    search_users(admin_page, username=username)
    row = user_row(admin_page, username)
    expect(row.get_by_text(dept_b["name"])).to_be_visible(timeout=8000)


def test_tc_user_e2e_006__unauthorized_user_cannot_access(
    limited_user_page: Page,
) -> None:
    """E2E-PLAN-NR-002: remapped from misaligned test_tc_user_e2e_005; E2E-PLAN-NR-003/004: compose_limited_role empty bindings."""
    menuitem = limited_user_page.get_by_role("menuitem", name="用户管理")
    expect(menuitem).not_to_be_visible(timeout=8000)

    limited_user_page.goto(frontend_path("/system/user"))
    limited_user_page.wait_for_load_state("networkidle", timeout=15000)

    expect(limited_user_page.get_by_role("button", name="新建用户")).not_to_be_visible(timeout=5000)
    expect(limited_user_page.get_by_role("button", name="编辑")).not_to_be_visible(timeout=5000)
    expect(limited_user_page.get_by_role("button", name="删除")).not_to_be_visible(timeout=5000)
