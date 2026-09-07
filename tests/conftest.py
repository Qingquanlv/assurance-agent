"""Shared pytest hooks.

The deleted legacy package is no longer selected during collection.
Product tests import assurance_product themselves.
"""

from __future__ import annotations

import os

import pytest

pytest_plugins = (
    "tests.product.composition_harness",
    "tests.product.product_runner",
    "tests.product.cli_support",
)


@pytest.fixture(scope="session", autouse=True)
def _isolated_host_selection_authority(tmp_path_factory: pytest.TempPathFactory):
    """Keep persistent host-sealing keys outside every test project workspace."""

    variable = "ASSURANCE_AGENT_HOST_AUTHORITY_ROOT"
    previous = os.environ.get(variable)
    root = tmp_path_factory.getbasetemp() / "host-selection-authority"
    os.environ[variable] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous
