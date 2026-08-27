from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.conformance import EVIDENCE_ROOT

pytest_plugins = (
    "tests.product.composition_harness",
    "tests.product.product_runner",
    "tests.product.cli_support",
)


@pytest.fixture
def evidence_root() -> Path:
    return EVIDENCE_ROOT
