"""v1 packaged happy-path loop tests retired in Task 15.

Covered by ``tests/integration/test_cli_workflow_v2.py`` (minimal GraphRuntime run)
and Task 16 eval migration.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="v1 driver loop removed in Task 15")


def test_v1_packaged_happy_path_retired() -> None:
    assert False
