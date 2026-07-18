"""E2E sync-safe bridge to menu factories and menu-mgmt RBAC binding."""

from __future__ import annotations

from typing import Any

import httpx

from tests.api.adapters.isolated_worker import run_isolated
from tests.config import settings
from tests.e2e.adapters.role import bind_role_apis, make_role
from tests.testdata.domain.menu import CLEANUP_MENU, MAKE_MENU

MENU_MGMT_MENU_NAME = "菜单管理"
MENU_MGMT_MENU_COMPONENT = "/system/menu"
MENU_MODULE_API_TAG = "菜单模块"


def make_menu(**kwargs):
    return run_isolated(MAKE_MENU, **kwargs)


def cleanup_menu(menu_id: int):
    return run_isolated(CLEANUP_MENU, menu_id=menu_id)


def _find_menu_id(menus: list[dict[str, Any]], *, name: str, component: str) -> int | None:
    for menu in menus:
        if menu.get("name") == name and menu.get("component") == component:
            return menu["id"]
        for child in menu.get("children") or []:
            found = _find_menu_id([child], name=name, component=component)
            if found is not None:
                return found
    return None


def resolve_menu_mgmt_menu_id(admin_headers: dict[str, str]) -> int:
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
        name=MENU_MGMT_MENU_NAME,
        component=MENU_MGMT_MENU_COMPONENT,
    )
    if menu_id is None:
        raise RuntimeError(
            f"menu management menu not found (name={MENU_MGMT_MENU_NAME!r}, "
            f"component={MENU_MGMT_MENU_COMPONENT!r})"
        )
    return menu_id


def resolve_menu_module_api_infos(admin_headers: dict[str, str]) -> list[dict[str, str]]:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        resp = client.get(
            "/api/v1/api/list",
            headers=admin_headers,
            params={"tags": MENU_MODULE_API_TAG, "page": 1, "page_size": 200},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"api list failed: HTTP {resp.status_code} {resp.text}")
    data = resp.json().get("data") or []
    api_infos = [{"method": item["method"], "path": item["path"]} for item in data]
    if not any(
        item["method"] == "POST" and item["path"] == "/api/v1/menu/create" for item in api_infos
    ):
        raise RuntimeError("menu module APIs missing POST /api/v1/menu/create binding source")
    return api_infos


def compose_role_with_menu_mgmt(admin_headers: dict[str, str]) -> dict[str, Any]:
    role = make_role()
    menu_id = resolve_menu_mgmt_menu_id(admin_headers)
    api_infos = resolve_menu_module_api_infos(admin_headers)
    bind_role_apis(role["id"], api_infos=api_infos, menu_ids=[menu_id])
    return role
