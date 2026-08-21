from __future__ import annotations

import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    test_fn = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_fn):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in inspect.signature(test_fn).parameters
        if name in pyfuncitem.funcargs
    }
    import asyncio

    asyncio.run(test_fn(**kwargs))
    return True
