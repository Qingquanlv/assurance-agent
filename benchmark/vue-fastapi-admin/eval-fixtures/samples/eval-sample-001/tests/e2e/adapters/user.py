"""E2E sync-safe bridge to shared user domain factories (subprocess transport)."""

from __future__ import annotations

from tests.api.adapters.isolated_worker import run_isolated
from tests.testdata.domain.user import CLEANUP_USER, MAKE_USER


def make_user(**kwargs):
    return run_isolated(MAKE_USER, **kwargs)


def cleanup_user(user_id: int):
    return run_isolated(CLEANUP_USER, user_id=user_id)
