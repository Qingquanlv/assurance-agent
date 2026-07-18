"""API adapter for user domain factories (isolated_worker transport)."""

from __future__ import annotations

import pytest

from tests.api.adapters.isolated_worker import run_isolated
from tests.testdata.domain.user import CLEANUP_USER, MAKE_USER


def factory_make_user(**kwargs):
    return run_isolated(MAKE_USER, **kwargs)


def factory_cleanup_user(user_id: int):
    return run_isolated(CLEANUP_USER, user_id=user_id)


@pytest.fixture
def existing_user():
    snapshot = factory_make_user()
    try:
        yield snapshot
    finally:
        factory_cleanup_user(snapshot["id"])


@pytest.fixture
def existing_user_pair():
    user_a = factory_make_user()
    user_b = factory_make_user()
    try:
        yield user_a, user_b
    finally:
        factory_cleanup_user(user_b["id"])
        factory_cleanup_user(user_a["id"])


@pytest.fixture
def ephemeral_user():
    snapshot = factory_make_user()
    yield snapshot
    # TC_USER_API_005 deletes via HTTP — cleanup only if still present
    try:
        factory_cleanup_user(snapshot["id"])
    except RuntimeError:
        pass
