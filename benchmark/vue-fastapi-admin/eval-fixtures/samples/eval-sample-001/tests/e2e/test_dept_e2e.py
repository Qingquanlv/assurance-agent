"""E2E tests for system dept management UI.

Traceability: RET-dept-management-20260716-163247-cursor
Cases: TC_DEPT_E2E_001 – TC_DEPT_E2E_005
Plan: qa/changes/RET-dept-management-20260716-163247-cursor/plans/e2e-codegen-plan.md

Run: uv run pytest tests/e2e/test_dept_e2e.py -v --headed
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.config import settings
from tests.e2e.adapters.dept import cleanup_dept, e2e_cleanup_dept_by_name, make_dept
from tests.e2e.conftest import e2e_login_admin, frontend_path, navigate_dept_mgmt
from tests.helpers.dept_assertions import find_dept_in_tree
from tests.testdata.domain.dept import unique_dept_name


def _verify_dept_list_contains(admin_headers: dict[str, str], dept_name: str) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get("/api/v1/dept/list", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    item = find_dept_in_tree(resp.json().get("data") or [], name=dept_name)
    assert item is not None, f"pre-UI verify failed: {dept_name!r} not found in dept list"


def test_tc_dept_e2e_001__admin_enters_dept_management(page: Page) -> None:
    """Admin navigates via 系统管理 → 部门管理; asserts CommonPage title 部门列表 (index.vue)."""
    e2e_login_admin(page)
    page.get_by_text("系统管理").click()
    with page.expect_response(lambda r: "dept/list" in r.url, timeout=15000):
        page.get_by_text("部门管理").click()
    page.wait_for_url("**/system/dept**", timeout=15000)

    expect(page.get_by_role("heading", name="部门列表")).to_be_visible(timeout=8000)
    table = page.get_by_role("table")
    expect(table.get_by_role("columnheader", name="部门名称")).to_be_visible(timeout=8000)
    expect(page.get_by_role("button", name="新建部门")).to_be_visible(timeout=8000)


def test_tc_dept_e2e_002__admin_creates_dept_in_tree(
    admin_dept_page: Page, admin_headers: dict[str, str]
) -> None:
    dept_name = unique_dept_name()
    try:
        admin_dept_page.get_by_role("button", name="新建部门").click()
        dialog = admin_dept_page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=8000)
        dialog.get_by_label("部门名称").fill(dept_name)
        with admin_dept_page.expect_response(lambda r: "dept/list" in r.url, timeout=15000):
            dialog.get_by_role("button", name="保存").click()
        expect(dialog).not_to_be_visible(timeout=8000)
        expect(admin_dept_page.get_by_role("cell", name=dept_name)).to_be_visible(timeout=8000)
    finally:
        with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
            e2e_cleanup_dept_by_name(client, admin_headers, dept_name)


def test_tc_dept_e2e_003__unauthorized_user_cannot_access_dept(
    limited_user_page: Page,
) -> None:
    menuitem = limited_user_page.get_by_role("menuitem", name="部门管理")
    expect(menuitem).not_to_be_visible(timeout=8000)

    limited_user_page.goto(frontend_path("/system/dept"))
    limited_user_page.wait_for_load_state("networkidle", timeout=15000)

    expect(limited_user_page.get_by_role("button", name="新建部门")).not_to_be_visible(timeout=5000)
    expect(limited_user_page.get_by_role("button", name="编辑")).not_to_be_visible(timeout=5000)
    expect(limited_user_page.get_by_role("button", name="删除")).not_to_be_visible(timeout=5000)


def test_tc_dept_e2e_004__admin_edits_dept_metadata(
    admin_dept_page: Page, admin_headers: dict[str, str]
) -> None:
    """API pre-seed via isolated_worker make_dept; edit name/desc in modal (index.vue)."""
    dept = make_dept()
    original_name = dept["name"]
    dept_id = dept["id"]
    new_name = unique_dept_name()
    new_desc = f"e2e-desc-{uuid.uuid4().hex[:8]}"
    try:
        _verify_dept_list_contains(admin_headers, original_name)
        navigate_dept_mgmt(admin_dept_page)

        row = admin_dept_page.get_by_role("row").filter(has_text=original_name)
        expect(row.get_by_role("cell", name=original_name)).to_be_visible(timeout=8000)
        row.get_by_role("button", name="编辑").click()

        dialog = admin_dept_page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=8000)
        dialog.get_by_label("部门名称").fill(new_name)
        dialog.get_by_label("备注").fill(new_desc)
        with admin_dept_page.expect_response(lambda r: "dept/list" in r.url, timeout=15000):
            dialog.get_by_role("button", name="保存").click()
        expect(dialog).not_to_be_visible(timeout=8000)
        expect(admin_dept_page.get_by_role("cell", name=new_name)).to_be_visible(timeout=8000)
        expect(admin_dept_page.get_by_text(new_desc)).to_be_visible(timeout=8000)
    finally:
        cleanup_dept(dept_id)


def test_tc_dept_e2e_005__admin_deletes_dept_via_ui(
    admin_dept_page: Page, admin_headers: dict[str, str]
) -> None:
    """API pre-seed via isolated_worker make_dept; delete row via UI popconfirm (index.vue)."""
    dept = make_dept()
    dept_name = dept["name"]
    dept_id = dept["id"]
    try:
        _verify_dept_list_contains(admin_headers, dept_name)
        navigate_dept_mgmt(admin_dept_page)

        row = admin_dept_page.get_by_role("row").filter(has_text=dept_name)
        expect(row.get_by_role("cell", name=dept_name)).to_be_visible(timeout=8000)
        row.get_by_role("button", name="删除").click()
        expect(admin_dept_page.get_by_text("确定删除该部门吗?")).to_be_visible(timeout=5000)
        with admin_dept_page.expect_response(lambda r: "dept/list" in r.url, timeout=15000):
            admin_dept_page.get_by_role("dialog").get_by_role("button", name="确定").click()
        expect(admin_dept_page.get_by_role("cell", name=dept_name)).not_to_be_visible(timeout=8000)
    finally:
        try:
            cleanup_dept(dept_id)
        except RuntimeError:
            pass
