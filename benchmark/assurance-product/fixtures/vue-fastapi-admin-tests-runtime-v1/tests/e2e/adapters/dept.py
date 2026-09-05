"""Department adapters for browser-owned records."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from tests.config import settings
from tests.testdata.domain.dept import cleanup_dept, make_dept, unique_dept_name


def _nodes(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        yield node
        children = node.get("children")
        if isinstance(children, list):
            yield from _nodes(child for child in children if isinstance(child, dict))


def _token() -> str:
    response = httpx.post(
        f"{settings.base_url}/api/v1/base/access_token",
        json={"username": settings.admin_username, "password": settings.admin_password},
        timeout=15.0,
    )
    response.raise_for_status()
    body = response.json()
    token = body.get("data", {}).get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("admin login response did not contain data.access_token")
    return token


def e2e_cleanup_dept_by_name(name: str) -> None:
    with httpx.Client(base_url=settings.base_url, headers={"token": _token()}) as client:
        response = client.get("/api/v1/dept/list")
        response.raise_for_status()
        body = response.json()
        tree = body.get("data", []) if isinstance(body, dict) else []
        target = next((node for node in _nodes(tree) if node.get("name") == name), None)
        if target is not None:
            e2e_cleanup_dept(target)


def e2e_cleanup_dept(dept: int | str | dict[str, Any]) -> None:
    identifier = dept.get("id", dept.get("dept_id")) if isinstance(dept, dict) else dept
    if identifier is None:
        return
    response = httpx.delete(
        f"{settings.base_url}/api/v1/dept/delete",
        params={"dept_id": identifier},
        headers={"token": _token()},
        timeout=15.0,
    )
    if response.status_code != 404:
        response.raise_for_status()


__all__ = [
    "cleanup_dept",
    "e2e_cleanup_dept",
    "e2e_cleanup_dept_by_name",
    "make_dept",
    "unique_dept_name",
]
