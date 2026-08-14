from __future__ import annotations

import importlib.util


def test_eval_package_does_not_ship_specialty_modules() -> None:
    for name in (
        "assurance_agent.eval.specialty_models",
        "assurance_agent.eval.specialty_render",
        "assurance_agent.eval.specialty_replay",
    ):
        assert importlib.util.find_spec(name) is None, name


def test_specialty_stack_lives_under_benchmark() -> None:
    from benchmark.specialty.specialty_models import load_specialty_report
    from benchmark.specialty.specialty_render import render_specialty_sections
    from benchmark.specialty.specialty_replay import collect_capability_policy_replay

    assert callable(load_specialty_report)
    assert callable(render_specialty_sections)
    assert callable(collect_capability_policy_replay)
