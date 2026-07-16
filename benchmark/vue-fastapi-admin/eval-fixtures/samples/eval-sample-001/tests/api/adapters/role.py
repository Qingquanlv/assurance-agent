"""API adapter for role domain factories (isolated_worker transport)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api.adapters.isolated_worker import run_isolated
from tests.testdata.domain.role import BIND_ROLE_APIS, CLEANUP_ROLE, MAKE_ROLE

USER_API_PATHS = [
    {"method": "GET", "path": "/api/v1/user/list"},
    {"method": "GET", "path": "/api/v1/user/get"},
    {"method": "POST", "path": "/api/v1/user/create"},
    {"method": "POST", "path": "/api/v1/user/update"},
    {"method": "DELETE", "path": "/api/v1/user/delete"},
    {"method": "POST", "path": "/api/v1/user/reset_password"},
]

USER_API_PATHS_WITHOUT_RESET = [
    item for item in USER_API_PATHS if item["path"] != "/api/v1/user/reset_password"
]

ROLE_API_PATHS = [
    {"method": "GET", "path": "/api/v1/role/list"},
    {"method": "GET", "path": "/api/v1/role/get"},
    {"method": "POST", "path": "/api/v1/role/create"},
    {"method": "POST", "path": "/api/v1/role/update"},
    {"method": "DELETE", "path": "/api/v1/role/delete"},
    {"method": "GET", "path": "/api/v1/role/authorized"},
    {"method": "POST", "path": "/api/v1/role/authorized"},
]


def factory_make_role(**kwargs: Any) -> dict[str, Any]:
    return run_isolated(MAKE_ROLE, **kwargs)


def factory_bind_role_apis(
    role_id: int,
    api_infos: list[dict[str, str]],
    *,
    menu_ids: list[int] | None = None,
) -> dict[str, Any]:
    return run_isolated(
        BIND_ROLE_APIS,
        role_id=role_id,
        api_infos=api_infos,
        menu_ids=menu_ids or [],
    )


def factory_cleanup_role(role_id: int) -> dict[str, Any]:
    return run_isolated(CLEANUP_ROLE, role_id=role_id)


@pytest.fixture
def role_fixture():
    snapshot = factory_make_role()
    try:
        yield snapshot
    finally:
        factory_cleanup_role(snapshot["id"])


@pytest.fixture
def ephemeral_role():
    snapshot = factory_make_role()
    yield snapshot
    try:
        factory_cleanup_role(snapshot["id"])
    except RuntimeError:
        pass

