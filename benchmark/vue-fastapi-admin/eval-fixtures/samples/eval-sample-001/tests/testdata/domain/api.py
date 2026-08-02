"""Shared api registry domain factory — lazy product imports inside factories."""

from __future__ import annotations

import uuid
from typing import Any

MAKE_API = "tests.testdata.domain.api.make_api"
CLEANUP_API = "tests.testdata.domain.api.cleanup_api"
LIST_AUTH_ROUTES = "tests.testdata.domain.api.list_auth_routes"


def _unique_suffix() -> str:
    return uuid.uuid4().hex[:8]


async def make_api(
    *,
    path: str | None = None,
    method: str = "GET",
    summary: str = "QA test API",
    tags: str = "QA",
) -> dict[str, Any]:
    from app.controllers.api import api_controller
    from app.models.enums import MethodType
    from app.schemas.apis import ApiCreate
    from tests.config import settings

    suffix = _unique_suffix()
    resolved_path = path or f"/{settings.api_prefix}/{suffix}"
    resolved_method = MethodType(method) if isinstance(method, str) else method
    api = await api_controller.create(
        ApiCreate(
            path=resolved_path,
            method=resolved_method,
            summary=summary,
            tags=tags,
        )
    )
    return {
        "id": api.id,
        "path": api.path,
        "method": api.method.value if hasattr(api.method, "value") else str(api.method),
        "summary": api.summary,
        "tags": api.tags,
    }


async def list_auth_routes() -> list[dict[str, str]]:
    from fastapi.routing import APIRoute

    from app import app

    routes: list[dict[str, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and len(route.dependencies) > 0:
            method = list(route.methods)[0]
            path = route.path_format
            summary = route.summary or ""
            tags = list(route.tags)[0] if route.tags else ""
            routes.append(
                {
                    "method": method,
                    "path": path,
                    "summary": summary,
                    "tags": tags,
                }
            )
    return routes


async def cleanup_api(api_id: int) -> dict[str, int | bool]:
    from app.models.admin import Api

    api = await Api.filter(id=api_id).first()
    if not api:
        return {"api_id": api_id, "deleted": False}
    await api.role_apis.clear()
    await api.delete()
    return {"api_id": api_id, "deleted": True}
