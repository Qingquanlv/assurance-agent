from __future__ import annotations

import importlib.util


def test_regression_gate_module_is_gone() -> None:
    assert importlib.util.find_spec("assurance_agent.eval.regression_gate") is None
