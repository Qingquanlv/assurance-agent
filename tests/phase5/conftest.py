from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase5.conformance import EVIDENCE_ROOT


@pytest.fixture
def evidence_root() -> Path:
    return EVIDENCE_ROOT
