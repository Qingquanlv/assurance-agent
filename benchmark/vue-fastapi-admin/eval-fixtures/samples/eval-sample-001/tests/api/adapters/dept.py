"""API adapter for dept domain factories (isolated_worker transport)."""

from __future__ import annotations

import pytest

from tests.api.adapters.isolated_worker import run_isolated
from tests.testdata.domain.dept import CLEANUP_DEPT, GET_DEPT_CLOSURE_ROWS, MAKE_DEPT


def factory_make_dept(**kwargs):
    return run_isolated(MAKE_DEPT, **kwargs)


def factory_cleanup_dept(dept_id: int):
    return run_isolated(CLEANUP_DEPT, dept_id=dept_id)


def factory_get_dept_closure_rows(dept_id: int):
    return run_isolated(GET_DEPT_CLOSURE_ROWS, dept_id=dept_id)


@pytest.fixture
def dept_fixture():
    snapshot = factory_make_dept()
    try:
        yield snapshot
    finally:
        factory_cleanup_dept(snapshot["id"])
