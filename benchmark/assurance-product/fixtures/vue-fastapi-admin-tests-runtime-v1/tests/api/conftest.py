"""HTTP fixtures for generated API tests."""

from __future__ import annotations

import os

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


def _optional_token(name: str) -> str:
    token = os.environ.get(name)
    if not token:
        pytest.skip(f"{name} is required for this authorization case")
    return token


@pytest.fixture
def limited_role_user_token() -> str:
    return _optional_token("API_LIMITED_ROLE_USER_TOKEN")


@pytest.fixture
def no_role_superuser_token() -> str:
    return _optional_token("API_NO_ROLE_SUPERUSER_TOKEN")


@pytest.fixture
def no_role_user_token() -> str:
    return _optional_token("API_NO_ROLE_USER_TOKEN")
