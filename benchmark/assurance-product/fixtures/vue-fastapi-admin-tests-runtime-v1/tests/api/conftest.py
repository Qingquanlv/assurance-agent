"""HTTP fixtures for generated API tests."""

from __future__ import annotations

import os
import secrets
from contextlib import ExitStack
from uuid import uuid4

import httpx
import pytest

from tests.config import settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def api_client():
    with httpx.Client(base_url=settings.base_url, timeout=15.0) as client:
        yield client


def _configured_or_login_token(name: str) -> str:
    configured = os.environ.get(name)
    if configured:
        return configured
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


@pytest.fixture
def admin_token() -> str:
    return _configured_or_login_token("API_ADMIN_TOKEN")


def _identity_token(api_client, admin_token: str, *, limited_role: bool, is_superuser: bool):
    """Keep ephemeral test credentials inside pytest, never in Agent tool output."""
    name = f"qa_{uuid4().hex[:12]}"
    password = secrets.token_urlsafe(24)

    def request(method: str, path: str, **kwargs):
        response = api_client.request(method, path, **{"headers": {"token": admin_token}, **kwargs})
        body = response.json()
        if response.status_code != 200 or not isinstance(body, dict) or body.get("code") != 200:
            raise RuntimeError(f"authentication fixture request failed: {path}")
        return body.get("data")

    def created_id(rows, key: str, value: str) -> int:
        matches = [row for row in rows or [] if row.get(key) == value]
        if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] <= 0:
            raise RuntimeError("authentication fixture identity lookup failed")
        return matches[0]["id"]

    with ExitStack() as cleanup:
        role_ids = []
        if limited_role:
            request("POST", "/api/v1/role/create", json={"name": name, "desc": "QA no API grants"})
            roles = request("GET", "/api/v1/role/list", params={"role_name": name, "page_size": 100})
            role_id = created_id(roles, "name", name)
            cleanup.callback(request, "DELETE", "/api/v1/role/delete", params={"role_id": role_id})
            request("POST", "/api/v1/role/authorized", json={"id": role_id, "menu_ids": [], "api_infos": []})
            role_ids = [role_id]
        email = f"{name}@example.com"
        request(
            "POST",
            "/api/v1/user/create",
            json={
                "email": email,
                "username": name,
                "password": password,
                "is_active": True,
                "is_superuser": is_superuser,
                "role_ids": role_ids,
                "dept_id": 0,
            },
        )
        users = request("GET", "/api/v1/user/list", params={"email": email, "page_size": 100})
        user_id = created_id(users, "email", email)
        cleanup.callback(request, "DELETE", "/api/v1/user/delete", params={"user_id": user_id})
        data = request(
            "POST", "/api/v1/base/access_token", headers={}, json={"username": name, "password": password}
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("authentication fixture login returned no token")
        yield token


@pytest.fixture
def limited_role_user_token(api_client, admin_token):
    yield from _identity_token(api_client, admin_token, limited_role=True, is_superuser=False)


@pytest.fixture
def no_role_superuser_token(api_client, admin_token):
    yield from _identity_token(api_client, admin_token, limited_role=False, is_superuser=True)


@pytest.fixture
def no_role_user_token(api_client, admin_token):
    yield from _identity_token(api_client, admin_token, limited_role=False, is_superuser=False)
