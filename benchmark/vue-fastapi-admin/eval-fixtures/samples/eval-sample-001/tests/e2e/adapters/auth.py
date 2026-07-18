"""E2E limited-user seed/cleanup for dept permission tests (subprocess transport)."""

from __future__ import annotations

from typing import Any

from tests.e2e.adapters.role import cleanup_role, compose_limited_role
from tests.e2e.adapters.user import cleanup_user, make_user


def e2e_seed_limited_user(admin_headers: dict[str, str]) -> dict[str, Any]:
    role = compose_limited_role(admin_headers)
    user = make_user(role_ids=[role["id"]], is_superuser=False)
    return {
        "role": role,
        "user": user,
        "username": user["username"],
        "password": user["password"],
    }


def e2e_cleanup_limited_user(bundle: dict[str, Any]) -> None:
    cleanup_user(bundle["user"]["id"])
    cleanup_role(bundle["role"]["id"])
