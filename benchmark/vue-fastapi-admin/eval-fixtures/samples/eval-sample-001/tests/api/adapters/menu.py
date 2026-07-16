"""API adapter for menu domain factories (HTTP seeding against live SUT)."""

from __future__ import annotations

from typing import Any

import httpx

from tests.config import settings
from tests.helpers.menu_assertions import find_menu_in_tree, menu_create_payload
from tests.helpers.user_assertions import login_token


def _admin_headers() -> dict[str, str]:
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        token = login_token(client, settings.admin_username, settings.admin_password)
    return {"token": token}


def _resolve_menu_id(
    client: httpx.Client,
    headers: dict[str, str],
    *,
    name: str | None = None,
    menu_id: int | None = None,
) -> int | None:
    resp = client.get(
        "/api/v1/menu/list",
        headers=headers,
        params={"page": 1, "page_size": 500},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"menu list failed: HTTP {resp.status_code} {resp.text}")
    item = find_menu_in_tree(resp.json().get("data") or [], menu_id=menu_id, name=name)
    return item["id"] if item else None


def factory_make_menu(*, api_client=None, admin_headers=None, **kwargs):
    payload = menu_create_payload(
        name=kwargs.get("name"),
        path=kwargs.get("path"),
        menu_type=kwargs.get("menu_type", "catalog"),
        parent_id=kwargs.get("parent_id", 0),
        order=kwargs.get("order", 0),
        component=kwargs.get("component", "Layout"),
    )

    def _make(client: httpx.Client, headers: dict[str, str]) -> dict[str, Any]:
        resp = client.post("/api/v1/menu/create", json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"menu create failed: HTTP {resp.status_code} {resp.text}")
        body = resp.json()
        if body.get("code") != 200:
            raise RuntimeError(f"menu create failed: {body}")
        resolved_id = _resolve_menu_id(client, headers, name=payload["name"])
        if resolved_id is None:
            raise RuntimeError(f"menu {payload['name']!r} not found after create")
        return {
            "id": resolved_id,
            "name": payload["name"],
            "path": payload["path"],
            "parent_id": payload["parent_id"],
            "order": payload["order"],
        }

    if api_client is not None and admin_headers is not None:
        return _make(api_client, admin_headers)

    headers = _admin_headers()
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        return _make(client, headers)


def factory_cleanup_menu(menu_id: int, *, api_client=None, admin_headers=None):
    def _cleanup(client: httpx.Client, headers: dict[str, str]) -> dict[str, Any]:
        resp = client.delete(
            "/api/v1/menu/delete",
            headers=headers,
            params={"id": menu_id},
        )
        if resp.status_code >= 500:
            raise RuntimeError(f"menu delete failed: HTTP {resp.status_code} {resp.text}")
        body = resp.json()
        return {
            "menu_id": menu_id,
            "deleted": resp.status_code == 200 and body.get("code") == 200,
        }

    if api_client is not None and admin_headers is not None:
        return _cleanup(api_client, admin_headers)

    headers = _admin_headers()
    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        return _cleanup(client, headers)
