"""API adapter for api registry domain factories (isolated_worker transport)."""

from __future__ import annotations

from tests.api.adapters.isolated_worker import run_isolated
from tests.testdata.domain.api import CLEANUP_API, LIST_AUTH_ROUTES, MAKE_API


def factory_make_api(**kwargs):
    return run_isolated(MAKE_API, **kwargs)


def factory_cleanup_api(api_id: int):
    return run_isolated(CLEANUP_API, api_id=api_id)


def factory_list_auth_routes():
    return run_isolated(LIST_AUTH_ROUTES)
