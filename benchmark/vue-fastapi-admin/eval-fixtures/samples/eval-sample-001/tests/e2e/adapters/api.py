"""E2E sync-safe bridge to api registry factories."""

from __future__ import annotations

from tests.api.adapters.isolated_worker import run_isolated
from tests.testdata.domain.api import CLEANUP_API, MAKE_API


def make_api(**kwargs):
    return run_isolated(MAKE_API, **kwargs)


def cleanup_api(api_id: int):
    return run_isolated(CLEANUP_API, api_id=api_id)
