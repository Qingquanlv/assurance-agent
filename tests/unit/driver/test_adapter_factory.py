from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver.adapter_factory import build_adapter


def test_build_adapter_rejects_unknown_adapter(tmp_path: Path) -> None:
    with pytest.raises(DriverError, match="unsupported adapter"):
        build_adapter(
            "custom",
            tmp_path,
            None,
            None,
            None,
            None,
            "custom-agent --print",
        )
