"""Playwright E2E fixtures: UI login, RBAC personas, navigation helpers."""

from __future__ import annotations

import os
import re
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.config import settings
from tests.e2e.adapters.api import cleanup_api, make_api
from tests.e2e.adapters.dept import cleanup_dept, make_dept
from tests.e2e.adapters.menu import compose_role_with_menu_mgmt
from tests.e2e.adapters.role import (
    cleanup_role,
    compose_limited_role,
    compose_role_with_role_mgmt,
    compose_role_with_user_mgmt,
    make_role,
)
from tests.e2e.adapters.user import cleanup_user, make_user
from tests.helpers.user_assertions import login_token


@pytest.fixture(scope="session")
def base_url():
    return settings.frontend_url


@pytest.fixture(scope="session", autouse=True)
def _verify_frontend_ready() -> None:
    if os.getenv("QA_SKIP_SUT_READINESS") == "1":
        return
    try:
        response = httpx.get(settings.frontend_url, timeout=5.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        pytest.exit(
            f"Frontend SUT unreachable at {settings.frontend_url} "
            f"({settings.frontend_url_source}): {exc}",
            returncode=2,
        )
    if response.status_code >= 500:
        pytest.exit(
            f"Frontend SUT not ready at {settings.frontend_url}: "
            f"GET -> HTTP {response.status_code}",
            returncode=2,
        )


@pytest.fixture(scope="session")
def admin_token():
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        return login_token(client, settings.admin_username, settings.admin_password)


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"token": admin_token}


def frontend_path(path: str) -> str:
    base = settings.frontend_url.rstrip("/")
    normalized = path.lstrip("/")
    use_hash = os.getenv("VITE_USE_HASH", "").lower() in ("1", "true", "yes")
    if use_hash:
        return f"{base}/#/{normalized}"
    return f"{base}/{normalized}"


def ui_login(page: Page, username: str, password: str) -> None:
    page.goto(frontend_path("/login"))
    page.get_by_placeholder("admin").fill(username)
    page.get_by_placeholder("123456").fill(password)
    # Outer expect must be registered before click so usermenu is not missed.
    with page.expect_response(lambda r: "usermenu" in r.url, timeout=15000):
        with page.expect_response(lambda r: "access_token" in r.url, timeout=15000):
            page.get_by_role("button", name=re.compile(r"登录|Login")).click()


def e2e_login_admin(page: Page) -> None:
    ui_login(page, settings.admin_username, settings.admin_password)


def navigate_user_mgmt(page: Page) -> None:
    with page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
        page.goto(frontend_path("/system/user"))
    page.get_by_text("用户列表").wait_for(state="visible", timeout=15000)


def search_users(page: Page, *, username: str | None = None, email: str | None = None) -> None:
    if username is not None:
        name_input = page.get_by_placeholder("请输入用户名称")
        name_input.fill("")
        name_input.fill(username)
    if email is not None:
        email_input = page.get_by_placeholder("请输入邮箱")
        email_input.fill("")
        email_input.fill(email)
    with page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
        page.get_by_role("button", name="搜索").click()


def reset_search_filters(page: Page) -> None:
    with page.expect_response(lambda r: "user/list" in r.url, timeout=15000):
        page.get_by_role("button", name="重置").click()


def user_row(page: Page, username: str):
    return page.get_by_role("row").filter(has_text=username)


def fill_user_modal(
    page: Page,
    *,
    username: str,
    email: str,
    password: str = "123456",
    role_name: str | None = None,
    select_first_role: bool = False,
    select_first_dept: bool = False,
) -> None:
    dialog = page.get_by_role("dialog")
    dialog.get_by_placeholder("请输入用户名称").fill(username)
    dialog.get_by_placeholder("请输入邮箱").fill(email)
    password_fields = dialog.get_by_placeholder("请输入密码")
    if password_fields.count() > 0:
        password_fields.fill(password)
    confirm_fields = dialog.get_by_placeholder("请确认密码")
    if confirm_fields.count() > 0:
        confirm_fields.fill(password)
    if role_name:
        dialog.get_by_role("checkbox", name=role_name).check()
    elif select_first_role:
        dialog.get_by_role("checkbox").first.check()
    if select_first_dept:
        dialog.get_by_placeholder("请选择部门").click()
        page.locator(".n-tree-node").first.click()


def verify_user_list_contains(admin_headers: dict[str, str], username: str) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/user/list",
            headers=admin_headers,
            params={"username": username, "page": 1, "page_size": 10},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json().get("data") or []
    assert any(item.get("username") == username for item in data), (
        f"pre-UI verify failed: {username!r} not found in user list"
    )


def verify_user_not_superuser(admin_headers: dict[str, str], username: str) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/user/list",
            headers=admin_headers,
            params={"username": username, "page": 1, "page_size": 10},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json().get("data") or []
    user = next((item for item in data if item.get("username") == username), None)
    assert user is not None, f"reset target {username!r} missing from list"
    assert user.get("is_superuser") is False, f"{username!r} must not be superuser"


@pytest.fixture
def admin_page(page: Page):
    ui_login(page, settings.admin_username, settings.admin_password)
    navigate_user_mgmt(page)
    yield page


@pytest.fixture
def search_target_user(admin_headers):
    suffix = uuid.uuid4().hex[:8]
    username = f"{settings.user_prefix}-s-{suffix}"
    email = f"{settings.user_prefix}-s-{suffix}@test.local"
    dept = make_dept()
    user = make_user(username=username, email=email, dept_id=dept["id"])
    verify_user_list_contains(admin_headers, username)
    try:
        yield user
    finally:
        cleanup_user(user["id"])
        cleanup_dept(dept["id"])


@pytest.fixture
def editable_user(admin_headers):
    role_a = make_role()
    role_b = make_role()
    dept_a = make_dept()
    dept_b = make_dept()
    user = make_user(role_ids=[role_a["id"]], dept_id=dept_a["id"])
    verify_user_list_contains(admin_headers, user["username"])
    bundle = {
        "user": user,
        "role_a": role_a,
        "role_b": role_b,
        "dept_a": dept_a,
        "dept_b": dept_b,
    }
    try:
        yield bundle
    finally:
        cleanup_user(user["id"])
        cleanup_role(role_a["id"])
        cleanup_role(role_b["id"])
        cleanup_dept(dept_a["id"])
        cleanup_dept(dept_b["id"])


@pytest.fixture
def deletable_user(admin_headers):
    user = make_user()
    verify_user_list_contains(admin_headers, user["username"])
    try:
        yield user
    finally:
        try:
            cleanup_user(user["id"])
        except RuntimeError:
            pass


@pytest.fixture
def reset_target_user(admin_headers):
    user = make_user(is_superuser=False)
    verify_user_list_contains(admin_headers, user["username"])
    verify_user_not_superuser(admin_headers, user["username"])
    try:
        yield user
    finally:
        cleanup_user(user["id"])


@pytest.fixture
def role_with_user_mgmt_credentials(admin_headers):
    role = compose_role_with_user_mgmt(admin_headers)
    user = make_user(role_ids=[role["id"]], is_superuser=False)
    credentials = {"username": user["username"], "password": user["password"]}
    bundle = {"credentials": credentials, "user": user, "role": role}
    try:
        yield bundle
    finally:
        cleanup_user(user["id"])
        cleanup_role(role["id"])


@pytest.fixture
def limited_role_credentials(admin_headers):
    role = compose_limited_role(admin_headers)
    user = make_user(role_ids=[role["id"]], is_superuser=False)
    credentials = {"username": user["username"], "password": user["password"]}
    bundle = {"credentials": credentials, "user": user, "role": role}
    try:
        yield bundle
    finally:
        cleanup_user(user["id"])
        cleanup_role(role["id"])


@pytest.fixture
def user_mgmt_page(page: Page, role_with_user_mgmt_credentials):
    creds = role_with_user_mgmt_credentials["credentials"]
    ui_login(page, creds["username"], creds["password"])
    navigate_user_mgmt(page)
    yield page


@pytest.fixture
def limited_user_page(page: Page, limited_role_credentials):
    creds = limited_role_credentials["credentials"]
    ui_login(page, creds["username"], creds["password"])
    yield page


def navigate_role_mgmt(page: Page) -> None:
    with page.expect_response(lambda r: "role/list" in r.url, timeout=15000):
        page.goto(frontend_path("/system/role"))
    page.get_by_text("角色列表").wait_for(state="visible", timeout=15000)


def search_roles(page: Page, *, role_name: str) -> None:
    expect(page.get_by_role("dialog")).not_to_be_visible(timeout=8000)
    name_input = page.get_by_placeholder("请输入角色名", exact=True)
    name_input.fill("")
    name_input.fill(role_name)
    with page.expect_response(lambda r: "role/list" in r.url, timeout=15000):
        page.get_by_role("button", name="搜索").click()


def reset_role_search_filters(page: Page) -> None:
    with page.expect_response(lambda r: "role/list" in r.url, timeout=15000):
        page.get_by_role("button", name="重置").click()


def role_row(page: Page, name: str):
    return page.get_by_role("row").filter(has_text=name)


def fill_role_modal(page: Page, *, name: str, desc: str) -> None:
    dialog = page.get_by_role("dialog")
    dialog.get_by_placeholder("请输入角色名称").fill(name)
    dialog.get_by_placeholder("请输入角色描述").fill(desc)


def verify_role_list_contains(admin_headers: dict[str, str], role_name: str) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/role/list",
            headers=admin_headers,
            params={"role_name": role_name, "page": 1, "page_size": 10},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json().get("data") or []
    assert any(item.get("name") == role_name for item in data), (
        f"pre-UI verify failed: {role_name!r} not found in role list"
    )


@pytest.fixture
def admin_role_page(page: Page):
    ui_login(page, settings.admin_username, settings.admin_password)
    navigate_role_mgmt(page)
    yield page


@pytest.fixture
def search_target_role(admin_headers):
    suffix = uuid.uuid4().hex[:8]
    role_name = f"{settings.role_prefix}-s-{suffix}"
    role = make_role(name=role_name, desc=f"search-target-{suffix}")
    verify_role_list_contains(admin_headers, role_name)
    try:
        yield role
    finally:
        cleanup_role(role["id"])


@pytest.fixture
def editable_role(admin_headers):
    role = make_role()
    verify_role_list_contains(admin_headers, role["name"])
    try:
        yield role
    finally:
        cleanup_role(role["id"])


@pytest.fixture
def configurable_role(admin_headers):
    role = make_role()
    verify_role_list_contains(admin_headers, role["name"])
    try:
        yield role
    finally:
        cleanup_role(role["id"])


@pytest.fixture
def deletable_role(admin_headers):
    role = make_role()
    verify_role_list_contains(admin_headers, role["name"])
    try:
        yield role
    finally:
        try:
            cleanup_role(role["id"])
        except RuntimeError:
            pass


@pytest.fixture
def role_with_role_mgmt_credentials(admin_headers):
    role = compose_role_with_role_mgmt(admin_headers)
    user = make_user(role_ids=[role["id"]], is_superuser=False)
    credentials = {"username": user["username"], "password": user["password"]}
    bundle = {"credentials": credentials, "user": user, "role": role}
    try:
        yield bundle
    finally:
        cleanup_user(user["id"])
        cleanup_role(role["id"])


@pytest.fixture
def role_mgmt_page(page: Page, role_with_role_mgmt_credentials):
    creds = role_with_role_mgmt_credentials["credentials"]
    ui_login(page, creds["username"], creds["password"])
    navigate_role_mgmt(page)
    yield page


def navigate_menu_mgmt(page: Page) -> None:
    page.goto(frontend_path("/system/menu"))
    page.get_by_text("菜单列表").wait_for(state="visible", timeout=15000)


def menu_row(page: Page, name: str):
    return page.get_by_role("row").filter(has_text=name)


def fill_menu_modal(
    page: Page,
    *,
    name: str,
    path: str,
    order: int = 1,
) -> None:
    dialog = page.get_by_role("dialog")
    dialog.get_by_placeholder("请输入唯一菜单名称").fill(name)
    dialog.get_by_placeholder("请输入访问路径").fill(path)
    order_input = dialog.locator(".n-input-number input")
    if order_input.count() > 0:
        order_input.fill("")
        order_input.fill(str(order))


def _find_menu_id_by_name(menus: list[dict], name: str) -> int | None:
    for menu in menus:
        if menu.get("name") == name:
            return menu["id"]
        for child in menu.get("children") or []:
            found = _find_menu_id_by_name([child], name)
            if found is not None:
                return found
    return None


def verify_menu_list_contains(admin_headers: dict[str, str], menu_name: str) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/menu/list",
            headers=admin_headers,
            params={"page": 1, "page_size": 200},
        )
    assert resp.status_code == 200, resp.text
    menus = resp.json().get("data") or []
    menu_id = _find_menu_id_by_name(menus, menu_name)
    assert menu_id is not None, f"pre-UI verify failed: {menu_name!r} not found in menu list"


@pytest.fixture
def admin_menu_page(page: Page):
    ui_login(page, settings.admin_username, settings.admin_password)
    navigate_menu_mgmt(page)
    yield page


@pytest.fixture
def role_with_menu_mgmt_credentials(admin_headers):
    role = compose_role_with_menu_mgmt(admin_headers)
    user = make_user(role_ids=[role["id"]], is_superuser=False)
    credentials = {"username": user["username"], "password": user["password"]}
    bundle = {"credentials": credentials, "user": user, "role": role}
    try:
        yield bundle
    finally:
        cleanup_user(user["id"])
        cleanup_role(role["id"])


@pytest.fixture
def menu_mgmt_page(page: Page, role_with_menu_mgmt_credentials):
    creds = role_with_menu_mgmt_credentials["credentials"]
    ui_login(page, creds["username"], creds["password"])
    navigate_menu_mgmt(page)
    yield page


def navigate_api_mgmt(page: Page) -> None:
    with page.expect_response(lambda r: "api/list" in r.url, timeout=15000):
        page.goto(frontend_path("/system/api"))
    page.get_by_role("heading", name="API列表").wait_for(state="visible", timeout=15000)


def _api_list_panel(page: Page):
    return page.get_by_role("article")


def search_apis(
    page: Page,
    *,
    path: str | None = None,
    summary: str | None = None,
    tags: str | None = None,
) -> None:
    panel = _api_list_panel(page)
    trigger = panel.get_by_placeholder("请输入API路径")
    if path is not None:
        path_input = panel.get_by_placeholder("请输入API路径")
        path_input.fill("")
        path_input.fill(path)
        trigger = path_input
    if summary is not None:
        summary_input = panel.get_by_placeholder("请输入API简介")
        summary_input.fill("")
        summary_input.fill(summary)
        trigger = summary_input
    if tags is not None:
        tags_input = panel.get_by_placeholder("请输入API模块")
        tags_input.fill("")
        tags_input.fill(tags)
        trigger = tags_input
    with page.expect_response(lambda r: "api/list" in r.url, timeout=15000):
        trigger.press("Enter")


def reset_api_search(page: Page) -> None:
    panel = _api_list_panel(page)
    for placeholder in ("请输入API路径", "请输入API简介", "请输入API模块"):
        field = panel.get_by_placeholder(placeholder)
        field.fill("")
    with page.expect_response(lambda r: "api/list" in r.url, timeout=15000):
        panel.get_by_placeholder("请输入API路径").press("Enter")


def confirm_api_delete(page: Page) -> None:
    popconfirm = page.locator(".n-popconfirm").filter(has_text="确定删除该API吗?")
    popconfirm.wait_for(state="visible", timeout=8000)
    confirm_btn = popconfirm.get_by_role("button", name="确认")
    if confirm_btn.count() > 0:
        confirm_btn.click()
    else:
        popconfirm.get_by_role("button", name="确定").click()


def api_row(page: Page, path: str):
    return page.get_by_role("row").filter(has_text=path)


def fill_api_modal(
    page: Page,
    *,
    path: str,
    method: str,
    summary: str,
    tags: str,
) -> None:
    dialog = page.get_by_role("dialog")
    dialog.get_by_placeholder("请输入API路径").fill(path)
    dialog.get_by_placeholder("请输入请求方式").fill(method)
    dialog.get_by_placeholder("请输入API简介").fill(summary)
    dialog.get_by_placeholder("请输入Tags").fill(tags)


def verify_api_list_contains(admin_headers: dict[str, str], path: str) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/api/list",
            headers=admin_headers,
            params={"path": path, "page": 1, "page_size": 50},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json().get("data") or []
    assert any(item.get("path") == path for item in data), (
        f"pre-UI verify failed: {path!r} not found in api list"
    )


@pytest.fixture
def api_page(page: Page):
    ui_login(page, settings.admin_username, settings.admin_password)
    navigate_api_mgmt(page)
    yield page


@pytest.fixture
def search_target_api(admin_headers):
    suffix = uuid.uuid4().hex[:8]
    path = f"/api/v1/{settings.api_prefix}/s-{suffix}"
    api = make_api(path=path, summary=f"search-target-{suffix}", tags=f"{settings.api_prefix}-search")
    verify_api_list_contains(admin_headers, path)
    try:
        yield api
    finally:
        cleanup_api(api["id"])


@pytest.fixture
def editable_api(admin_headers):
    suffix = uuid.uuid4().hex[:8]
    path = f"/api/v1/{settings.api_prefix}/e-{suffix}"
    api = make_api(
        path=path,
        summary=f"editable-{suffix}",
        tags=f"{settings.api_prefix}-edit",
    )
    verify_api_list_contains(admin_headers, path)
    try:
        yield api
    finally:
        cleanup_api(api["id"])


@pytest.fixture
def deletable_api(admin_headers):
    suffix = uuid.uuid4().hex[:8]
    path = f"/api/v1/{settings.api_prefix}/d-{suffix}"
    api = make_api(
        path=path,
        summary=f"deletable-{suffix}",
        tags=f"{settings.api_prefix}-delete",
    )
    verify_api_list_contains(admin_headers, path)
    try:
        yield api
    finally:
        try:
            cleanup_api(api["id"])
        except RuntimeError:
            pass


def navigate_dept_mgmt(page: Page) -> None:
    with page.expect_response(lambda r: "dept/list" in r.url, timeout=15000):
        page.goto(frontend_path("/system/dept"))
    page.get_by_text("部门列表").wait_for(state="visible", timeout=15000)


@pytest.fixture
def admin_dept_page(page: Page):
    e2e_login_admin(page)
    navigate_dept_mgmt(page)
    yield page
