"""Shared pytest readiness gate for tests that require the running SUT."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.config import settings


@pytest.fixture(scope="session", autouse=True)
def _verify_sut_ready() -> None:
    if os.getenv("QA_SKIP_SUT_READINESS") == "1":
        return

    source = settings.base_url_source
    try:
        response = httpx.get(f"{settings.base_url}/openapi.json", timeout=5.0)
    except httpx.HTTPError as exc:
        pytest.exit(
            f"SUT unreachable at {settings.base_url} ({source}): {exc}",
            returncode=2,
        )
    if response.status_code != 200:
        pytest.exit(
            f"SUT not ready at {settings.base_url} ({source}): "
            f"GET /openapi.json -> HTTP {response.status_code}",
            returncode=2,
        )

    try:
        response = httpx.post(
            f"{settings.base_url}/api/v1/base/access_token",
            json={
                "username": settings.admin_username,
                "password": settings.admin_password,
            },
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        pytest.exit(
            f"SUT login unavailable at {settings.base_url} ({source}): {exc}",
            returncode=2,
        )
    if response.status_code != 200:
        pytest.exit(
            f"SUT admin login failed at {settings.base_url} ({source}): "
            f"POST /api/v1/base/access_token -> HTTP {response.status_code}",
            returncode=2,
        )
