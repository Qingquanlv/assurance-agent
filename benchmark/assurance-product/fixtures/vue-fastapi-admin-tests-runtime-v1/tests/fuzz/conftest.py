"""Live-backend fixtures for generated fuzz tests."""

from __future__ import annotations

from collections.abc import Generator

import httpx
import pytest

from tests.fuzz.settings import admin_credentials, base_url


@pytest.fixture(scope="session")
def client() -> Generator[httpx.Client, None, None]:
    with httpx.Client(base_url=base_url(), timeout=30.0) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def admin_token(client: httpx.Client) -> str:
    username, password = admin_credentials()
    response = client.post(
        "/api/v1/base/access_token",
        json={"username": username, "password": password},
    )
    response.raise_for_status()
    body = response.json()
    token = body.get("data", {}).get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("admin login response did not contain data.access_token")
    return token
