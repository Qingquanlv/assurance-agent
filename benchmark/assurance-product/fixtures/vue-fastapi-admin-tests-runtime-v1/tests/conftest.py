"""Session gate proving the managed benchmark backend is ready and usable."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.config import settings


@pytest.fixture(scope="session", autouse=True)
def _verify_sut_ready() -> None:
    if os.environ.get("QA_SKIP_SUT_READINESS") == "1":
        return
    try:
        schema = httpx.get(f"{settings.base_url}/openapi.json", timeout=5.0)
        login = httpx.post(
            f"{settings.base_url}/api/v1/base/access_token",
            json={"username": settings.admin_username, "password": settings.admin_password},
            timeout=5.0,
        )
    except httpx.HTTPError as error:
        pytest.exit(f"managed SUT is unreachable at {settings.base_url}: {error}", returncode=2)
    if schema.status_code != 200 or login.status_code != 200:
        pytest.exit(
            f"managed SUT readiness failed: schema={schema.status_code}, login={login.status_code}",
            returncode=2,
        )
