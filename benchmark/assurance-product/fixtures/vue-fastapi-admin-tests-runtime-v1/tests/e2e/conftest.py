"""Login and department lifecycle fixtures for generated browser tests."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, TypeVar

import pytest

from tests.config import settings
from tests.e2e.adapters.dept import cleanup_dept, make_dept

_T = TypeVar("_T")


def _sync(awaitable: Coroutine[Any, Any, _T]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


def _login(page: Any, username: str, password: str) -> None:
    page.goto(f"{settings.frontend_url}/login")
    page.get_by_placeholder("admin").fill(username)
    page.get_by_placeholder("123456").fill(password)
    page.get_by_role("button", name=re.compile("login|登录", re.I)).click()
    page.wait_for_url(re.compile(r"(?!.*?/login$).+"))


@pytest.fixture
def e2e_login_admin(page: Any) -> Any:
    _login(page, settings.admin_username, settings.admin_password)
    return page


@pytest.fixture
def e2e_login_limited_user(page: Any) -> Any:
    username = os.environ.get("QA_LIMITED_USERNAME")
    password = os.environ.get("QA_LIMITED_PASSWORD")
    if not username or not password:
        pytest.skip("QA_LIMITED_USERNAME and QA_LIMITED_PASSWORD are required")
    _login(page, username, password)
    return page


@pytest.fixture
def seeded_dept() -> Iterator[dict[str, Any]]:
    record = _sync(make_dept())
    try:
        yield record
    finally:
        _sync(cleanup_dept(record))
