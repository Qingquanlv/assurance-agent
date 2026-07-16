"""API-layer pytest fixtures: HTTP client, auth headers, RBAC personas."""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.api.adapters.role import (
    USER_API_PATHS_WITHOUT_RESET,
    factory_bind_role_apis,
    factory_cleanup_role,
    factory_make_role,
)
from tests.api.adapters.user import factory_cleanup_user, factory_make_user
from tests.config import candidate_sqlite_paths, settings
from tests.helpers.dept_assertions import find_dept_in_tree
from tests.helpers.role_assertions import find_role_in_list, role_create_payload
from tests.helpers.user_assertions import find_user_in_list, login_token, user_create_payload
from tests.schema_validation import _LOCAL_SCHEMAS, _SUCCESS_WRAPPER

_MENU_LIST_SUCCESS = {
    "type": "object",
    "required": ["code", "msg", "data", "total", "page", "page_size"],
    "properties": {
        "code": {"type": "integer"},
        "msg": {"type": ["string", "null"]},
        "data": {"type": "array"},
        "total": {"type": "integer"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
    },
    "additionalProperties": True,
}

_DEPT_LIST_SUCCESS = {
    "type": "object",
    "required": ["code", "msg", "data"],
    "properties": {
        "code": {"type": "integer"},
        "msg": {"type": ["string", "null"]},
        "data": {"type": "array"},
    },
    "additionalProperties": True,
}

_API_LIST_SUCCESS = {
    "type": "object",
    "required": ["code", "msg", "data", "total", "page", "page_size"],
    "properties": {
        "code": {"type": "integer"},
        "msg": {"type": ["string", "null"]},
        "data": {"type": "array"},
        "total": {"type": "integer"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
    },
    "additionalProperties": True,
}

_USER_SUCCESS = {
    "type": "object",
    "required": ["code", "msg"],
    "properties": {
        "code": {"type": "integer"},
        "msg": {"type": ["string", "null"]},
        "data": {},
    },
    "additionalProperties": True,
}

_USER_LIST_SUCCESS = {
    "type": "object",
    "required": ["code", "msg", "data", "total", "page", "page_size"],
    "properties": {
        "code": {"type": "integer"},
        "msg": {"type": ["string", "null"]},
        "data": {"type": "array"},
        "total": {"type": "integer"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
    },
    "additionalProperties": True,
}

_ROLE_LIST_SUCCESS = {
    "type": "object",
    "required": ["code", "msg", "data", "total", "page", "page_size"],
    "properties": {
        "code": {"type": "integer"},
        "msg": {"type": ["string", "null"]},
        "data": {"type": "array"},
        "total": {"type": "integer"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
    },
    "additionalProperties": True,
}

_LOCAL_SCHEMAS.update(
    {
        "GET /api/v1/role/list": _ROLE_LIST_SUCCESS,
        "POST /api/v1/menu/create": _SUCCESS_WRAPPER,
        "GET /api/v1/menu/list": _MENU_LIST_SUCCESS,
        "GET /api/v1/menu/get": _SUCCESS_WRAPPER,
        "POST /api/v1/menu/update": _SUCCESS_WRAPPER,
        "DELETE /api/v1/menu/delete": _SUCCESS_WRAPPER,
        "POST /api/v1/dept/create": _SUCCESS_WRAPPER,
        "GET /api/v1/dept/list": _DEPT_LIST_SUCCESS,
        "GET /api/v1/dept/get": _SUCCESS_WRAPPER,
        "POST /api/v1/dept/update": _SUCCESS_WRAPPER,
        "DELETE /api/v1/dept/delete": _SUCCESS_WRAPPER,
        "POST /api/v1/api/create": _SUCCESS_WRAPPER,
        "GET /api/v1/api/list": _API_LIST_SUCCESS,
        "GET /api/v1/api/get": _SUCCESS_WRAPPER,
        "POST /api/v1/api/update": _SUCCESS_WRAPPER,
        "DELETE /api/v1/api/delete": _SUCCESS_WRAPPER,
        "POST /api/v1/api/refresh": _SUCCESS_WRAPPER,
        "POST /api/v1/user/create": _USER_SUCCESS,
        "GET /api/v1/user/list": _USER_LIST_SUCCESS,
        "GET /api/v1/user/get": _USER_SUCCESS,
        "POST /api/v1/user/update": _USER_SUCCESS,
        "DELETE /api/v1/user/delete": _USER_SUCCESS,
        "POST /api/v1/user/reset_password": _USER_SUCCESS,
    }
)

pytest_plugins = [
    "tests.api.adapters.user",
    "tests.api.adapters.role",
    "tests.api.adapters.dept",
    "tests.api.adapters.menu",
    "tests.api.adapters.api",
]


@pytest.fixture(scope="session")
def api_client():
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session")
def admin_token(api_client):
    return login_token(api_client, settings.admin_username, settings.admin_password)


@pytest.fixture(scope="session", autouse=True)
def align_qa_sqlite_file(api_client, admin_token):
    """Probe candidate SQLite files so isolated_worker reads the same DB as the live SUT."""
    if os.getenv("QA_SQLITE_FILE"):
        yield
        return

    marker_name = f"{settings.dept_prefix}-sqlite-probe-{uuid.uuid4().hex[:8]}"
    headers = {"token": admin_token}
    marker_id = None
    try:
        create_resp = api_client.post(
            "/api/v1/dept/create",
            json={"name": marker_name, "desc": "", "order": 0, "parent_id": 0},
            headers=headers,
        )
        if create_resp.status_code != 200 or create_resp.json().get("code") != 200:
            yield
            return

        list_resp = api_client.get(
            "/api/v1/dept/list",
            headers=headers,
            params={"name": marker_name},
        )
        if list_resp.status_code != 200:
            yield
            return
        item = find_dept_in_tree(list_resp.json().get("data") or [], name=marker_name)
        if item is None:
            yield
            return
        marker_id = item["id"]

        matched_path = None
        for candidate in candidate_sqlite_paths():
            path = Path(candidate)
            if not path.is_file():
                continue
            try:
                with sqlite3.connect(str(path)) as conn:
                    row = conn.execute(
                        "SELECT id FROM dept WHERE name = ? AND is_deleted = 0",
                        (marker_name,),
                    ).fetchone()
                if row and row[0] == marker_id:
                    matched_path = str(path.resolve())
                    break
            except sqlite3.Error:
                continue

        if matched_path:
            os.environ["QA_SQLITE_FILE"] = matched_path
        yield
    finally:
        if marker_id is not None:
            api_client.delete(
                f"/api/v1/dept/delete?dept_id={marker_id}",
                headers=headers,
            )


@pytest.fixture
def admin_headers(admin_token):
    return {"token": admin_token}


def _login_token_with_retry(
    api_client,
    username: str,
    password: str,
    *,
    max_attempts: int = 5,
    initial_delay_s: float = 0.2,
) -> str:
    last_error = "unknown error"
    for attempt in range(max_attempts):
        resp = api_client.post(
            "/api/v1/base/access_token",
            json={"username": username, "password": password},
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("code") == 200:
                return body["data"]["access_token"]
            last_error = f"business error: {body}"
        else:
            last_error = f"HTTP {resp.status_code} {resp.text}"
        if attempt < max_attempts - 1:
            time.sleep(initial_delay_s * (attempt + 1))
    raise RuntimeError(
        f"login_token failed for {username!r} after {max_attempts} attempts: {last_error}"
    )


@pytest.fixture
def limited_role_user_token(api_client, admin_headers):
    role_payload = role_create_payload()
    role_id = None
    user_id = None
    try:
        role_resp = api_client.post(
            "/api/v1/role/create",
            json=role_payload,
            headers=admin_headers,
        )
        if role_resp.status_code != 200 or role_resp.json().get("code") != 200:
            raise RuntimeError(
                f"role create failed: HTTP {role_resp.status_code} {role_resp.text}"
            )

        role_list_resp = api_client.get(
            "/api/v1/role/list",
            headers=admin_headers,
            params={"role_name": role_payload["name"], "page": 1, "page_size": 50},
        )
        if role_list_resp.status_code != 200:
            raise RuntimeError(
                f"role list failed: HTTP {role_list_resp.status_code} {role_list_resp.text}"
            )
        role_item = find_role_in_list(
            role_list_resp.json().get("data") or [],
            name=role_payload["name"],
        )
        if role_item is None:
            raise RuntimeError(f"role {role_payload['name']!r} not visible after create")
        role_id = role_item["id"]

        auth_resp = api_client.post(
            "/api/v1/role/authorized",
            headers=admin_headers,
            json={"id": role_id, "menu_ids": [], "api_infos": []},
        )
        if auth_resp.status_code != 200 or auth_resp.json().get("code") != 200:
            raise RuntimeError(
                f"role authorized update failed: HTTP {auth_resp.status_code} {auth_resp.text}"
            )

        user_suffix = uuid.uuid4().hex[:8]
        user_payload = user_create_payload(
            role_ids=[role_id],
            email=f"{settings.user_prefix}-{user_suffix}@example.com",
            username=f"{settings.user_prefix}-{user_suffix}",
        )
        user_resp = api_client.post(
            "/api/v1/user/create",
            json=user_payload,
            headers=admin_headers,
        )
        if user_resp.status_code != 200 or user_resp.json().get("code") != 200:
            raise RuntimeError(
                f"user create failed: HTTP {user_resp.status_code} {user_resp.text}"
            )

        user_list_resp = api_client.get(
            "/api/v1/user/list",
            headers=admin_headers,
            params={"email": user_payload["email"], "page": 1, "page_size": 50},
        )
        if user_list_resp.status_code != 200:
            raise RuntimeError(
                f"user list failed before login: HTTP {user_list_resp.status_code} {user_list_resp.text}"
            )
        user_item = find_user_in_list(
            user_list_resp.json().get("data") or [],
            email=user_payload["email"],
        )
        if user_item is None:
            raise RuntimeError(f"user {user_payload['email']!r} not visible in list before login")
        user_id = user_item["id"]

        get_resp = api_client.get(
            "/api/v1/user/get",
            headers=admin_headers,
            params={"user_id": user_id},
        )
        if get_resp.status_code != 200 or get_resp.json().get("code") != 200:
            raise RuntimeError(
                f"user {user_id} not readable via GET before login: "
                f"HTTP {get_resp.status_code} {get_resp.text}"
            )

        token = _login_token_with_retry(
            api_client,
            user_payload["username"],
            user_payload["password"],
        )
        yield token
    finally:
        if user_id is not None:
            factory_cleanup_user(user_id)
        if role_id is not None:
            factory_cleanup_role(role_id)


@pytest.fixture
def limited_reset_user_token(api_client):
    role = factory_make_role()
    factory_bind_role_apis(role["id"], api_infos=USER_API_PATHS_WITHOUT_RESET)
    user = factory_make_user(role_ids=[role["id"]])
    token = login_token(api_client, user["username"], user["password"])
    try:
        yield token
    finally:
        factory_cleanup_user(user["id"])
        factory_cleanup_role(role["id"])


@pytest.fixture
def no_role_user_token(api_client):
    user = factory_make_user(role_ids=[])
    token = login_token(api_client, user["username"], user["password"])
    try:
        yield token
    finally:
        factory_cleanup_user(user["id"])


@pytest.fixture
def users_for_filter(api_client, admin_headers):
    from tests.api.adapters.dept import factory_cleanup_dept, factory_make_dept

    dept_a = factory_make_dept()
    dept_b = factory_make_dept()
    user1 = factory_make_user(username=f"{settings.user_prefix}-f1", email=f"{settings.user_prefix}-f1@example.com", dept_id=dept_a["id"])
    user2 = factory_make_user(username=f"{settings.user_prefix}-f2", email=f"{settings.user_prefix}-f2@example.com", dept_id=dept_b["id"])
    user3 = factory_make_user(username=f"{settings.user_prefix}-f3", email=f"{settings.user_prefix}-f3@example.com", dept_id=dept_a["id"])
    bundle = {
        "dept_a": dept_a,
        "dept_b": dept_b,
        "users": [user1, user2, user3],
    }
    try:
        yield bundle
    finally:
        for user in (user1, user2, user3):
            factory_cleanup_user(user["id"])
        factory_cleanup_dept(dept_a["id"])
        factory_cleanup_dept(dept_b["id"])


@pytest.fixture
def roles_for_filter():
    role1 = factory_make_role(name=f"{settings.role_prefix}-f1")
    role2 = factory_make_role(name=f"{settings.role_prefix}-f2")
    role3 = factory_make_role(name=f"{settings.role_prefix}-f3-alt")
    bundle = {"roles": [role1, role2, role3], "filter_substring": f"{settings.role_prefix}-f"}
    try:
        yield bundle
    finally:
        for role in (role1, role2, role3):
            factory_cleanup_role(role["id"])


@pytest.fixture
def authorized_role_fixture():
    from tests.api.adapters.api import factory_cleanup_api, factory_make_api
    from tests.api.adapters.menu import factory_cleanup_menu, factory_make_menu

    role = factory_make_role()
    menu = factory_make_menu()
    api = factory_make_api()
    api_infos = [{"path": api["path"], "method": api["method"]}]
    factory_bind_role_apis(
        role["id"],
        api_infos=api_infos,
        menu_ids=[menu["id"]],
    )
    bundle = {
        "role": role,
        "menu": menu,
        "api": api,
        "menu_ids": [menu["id"]],
        "api_infos": api_infos,
    }
    try:
        yield bundle
    finally:
        factory_cleanup_role(role["id"])
        factory_cleanup_menu(menu["id"])
        factory_cleanup_api(api["id"])


@pytest.fixture
def existing_api():
    from tests.api.adapters.api import factory_cleanup_api, factory_make_api

    snapshot = factory_make_api()
    try:
        yield snapshot
    finally:
        factory_cleanup_api(snapshot["id"])


@pytest.fixture
def ephemeral_api():
    from tests.api.adapters.api import factory_cleanup_api, factory_make_api

    snapshot = factory_make_api()
    try:
        yield snapshot
    finally:
        factory_cleanup_api(snapshot["id"])


@pytest.fixture
def apis_for_filter():
    from tests.api.adapters.api import factory_cleanup_api, factory_make_api

    path_token = f"{settings.api_prefix}-path-{uuid.uuid4().hex[:6]}"
    summary_token = f"{settings.api_prefix}-summary-{uuid.uuid4().hex[:6]}"
    tags_token = f"{settings.api_prefix}-tags-{uuid.uuid4().hex[:6]}"
    snapshot = factory_make_api(
        path=f"/api/v1/{settings.api_prefix}/{path_token}",
        method="GET",
        summary=f"filter {summary_token}",
        tags=tags_token,
    )
    bundle = {
        "api": snapshot,
        "path_token": path_token,
        "summary_token": summary_token,
        "tags_token": tags_token,
    }
    try:
        yield bundle
    finally:
        factory_cleanup_api(snapshot["id"])


@pytest.fixture
def superuser_target_id(api_client, admin_headers):
    resp = api_client.get(
        "/api/v1/user/list",
        headers=admin_headers,
        params={"username": settings.admin_username, "page": 1, "page_size": 10},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json().get("data") or []
    for item in data:
        if item.get("username") == settings.admin_username and item.get("is_superuser"):
            return item["id"]
    raise RuntimeError("seed superuser admin not found in user list")
