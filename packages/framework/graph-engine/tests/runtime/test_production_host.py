from __future__ import annotations

import sys

import pytest

from graph_engine.attempts.production_host import UnsupportedProductionPlatform, _ProcessSupervisor


def test_production_host_rejects_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(UnsupportedProductionPlatform, match="Linux and macOS"):
        _ProcessSupervisor.for_platform()
