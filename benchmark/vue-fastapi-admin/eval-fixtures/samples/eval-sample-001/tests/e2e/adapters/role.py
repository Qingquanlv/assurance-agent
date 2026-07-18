"""E2E sync-safe bridge to role factories and user-module RBAC binding."""

from __future__ import annotations

from typing import Any

import httpx

from tests.api.adapters.isolated_worker import run_isolated
from tests.config import settings
from tests.testdata.domain.role import BIND_ROLE_APIS, CLEANUP_ROLE, MAKE_ROLE

USER_MGMT_MENU_NAME = "用户管理"
USER_MGMT_MENU_COMPONENT = "/system/user"
USER_MODULE_API_TAG = "用户模块"

ROLE_MGMT_MENU_NAME = "角色管理"
ROLE_MGMT_MENU_COMPONENT = "/system/role"
ROLE_MODULE_API_TAG = "角色模块"


def make_role(**kwargs):
    return run_isolated(MAKE_ROLE, **kwargs)


def bind_role_apis(role_id: int, api_infos: list[dict[str, str]], *, menu_ids=None):
    return run_isolated(
        BIND_ROLE_APIS,
        role_id=role_id,
        api_infos=api_infos,
        menu_ids=menu_ids or [],
    )


def cleanup_role(role_id: int):
    return run_isolated(CLEANUP_ROLE, role_id=role_id)


def _find_menu_id(menus: list[dict[str, Any]], *, name: str, component: str) -> int | None:
    for menu in menus:
        if menu.get("name") == name and menu.get("component") == component:
            return menu["id"]
        for child in menu.get("children") or []:
            found = _find_menu_id([child], name=name, component=component)
            if found is not None:
                return found
    return None


def resolve_user_mgmt_menu_id(admin_headers: dict[str, str]) -> int:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/menu/list",
            headers=admin_headers,
            params={"page": 1, "page_size": 100},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"menu list failed: HTTP {resp.status_code} {resp.text}")
    menus = resp.json().get("data") or []
    menu_id = _find_menu_id(
        menus,
        name=USER_MGMT_MENU_NAME,
        component=USER_MGMT_MENU_COMPONENT,
    )
    if menu_id is None:
        raise RuntimeError(
            f"user management menu not found (name={USER_MGMT_MENU_NAME!r}, "
            f"component={USER_MGMT_MENU_COMPONENT!r})"
        )
    return menu_id


def resolve_user_module_api_infos(admin_headers: dict[str, str]) -> list[dict[str, str]]:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/api/list",
            headers=admin_headers,
            params={"tags": USER_MODULE_API_TAG, "page": 1, "page_size": 200},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"api list failed: HTTP {resp.status_code} {resp.text}")
    data = resp.json().get("data") or []
    api_infos = [{"method": item["method"], "path": item["path"]} for item in data]
    if not any(
        item["method"] == "POST" and item["path"] == "/api/v1/user/create" for item in api_infos
    ):
        raise RuntimeError("user module APIs missing POST /api/v1/user/create binding source")
    return api_infos


def compose_role_with_user_mgmt(admin_headers: dict[str, str]) -> dict[str, Any]:
    role = make_role()
    menu_id = resolve_user_mgmt_menu_id(admin_headers)
    api_infos = resolve_user_module_api_infos(admin_headers)
    bind_role_apis(role["id"], api_infos=api_infos, menu_ids=[menu_id])
    return role


def compose_limited_role(admin_headers: dict[str, str]) -> dict[str, Any]:
    role = make_role()
    bind_role_apis(role["id"], api_infos=[], menu_ids=[])
    return role


def resolve_role_mgmt_menu_id(admin_headers: dict[str, str]) -> int:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/menu/list",
            headers=admin_headers,
            params={"page": 1, "page_size": 100},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"menu list failed: HTTP {resp.status_code} {resp.text}")
    menus = resp.json().get("data") or []
    menu_id = _find_menu_id(
        menus,
        name=ROLE_MGMT_MENU_NAME,
        component=ROLE_MGMT_MENU_COMPONENT,
    )
    if menu_id is None:
        raise RuntimeError(
            f"role management menu not found (name={ROLE_MGMT_MENU_NAME!r}, "
            f"component={ROLE_MGMT_MENU_COMPONENT!r})"
        )
    return menu_id


def resolve_role_module_api_infos(admin_headers: dict[str, str]) -> list[dict[str, str]]:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/api/list",
            headers=admin_headers,
            params={"tags": ROLE_MODULE_API_TAG, "page": 1, "page_size": 200},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"api list failed: HTTP {resp.status_code} {resp.text}")
    data = resp.json().get("data") or []
    api_infos = [{"method": item["method"], "path": item["path"]} for item in data]
    if not any(
        item["method"] == "POST" and item["path"] == "/api/v1/role/create" for item in api_infos
    ):
        raise RuntimeError("role module APIs missing POST /api/v1/role/create binding source")
    return api_infos


def compose_role_with_role_mgmt(admin_headers: dict[str, str]) -> dict[str, Any]:
    role = make_role()
    menu_id = resolve_role_mgmt_menu_id(admin_headers)
    api_infos = resolve_role_module_api_infos(admin_headers)
    bind_role_apis(role["id"], api_infos=api_infos, menu_ids=[menu_id])
    return role
