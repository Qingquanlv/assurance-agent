"""v1 real-provider loop tests retired in Task 15.

Covered by ``tests/integration/test_cli_workflow_v2.py`` and
``tests/integration/test_graph_runtime.py``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="v1 driver loop removed in Task 15")


def test_v1_real_provider_retired() -> None:
    assert False
