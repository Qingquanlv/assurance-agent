from __future__ import annotations

import importlib.util


def test_retro_v2_modules_are_gone() -> None:
    for name in (
        "assurance_agent.retro.collect_stage",
        "assurance_agent.retro.context",
    ):
        assert importlib.util.find_spec(name) is None, name
