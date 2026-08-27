from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("product_runner")


def test_low_coverage_reenters_generation_until_policy_passes(product_runner):
    trace = product_runner(coverage_sequence=(0.40, 0.72, 0.91), threshold=0.90).run_to_report()
    assert trace.activations("assurance.healing.coverage-repair") == 2
    assert trace.report.coverage == 0.91
    assert trace.status == "succeeded"
    assert trace.report.exists


def test_coverage_repair_exhaustion_stops_with_a_report(product_runner):
    trace = product_runner(
        coverage_sequence=(0.40, 0.41, 0.42),
        threshold=0.90,
        coverage_rounds=1,
    ).run_to_report()
    assert trace.activations("assurance.healing.coverage-repair") == 1
    assert trace.status == "stopped"
    assert trace.report.exists
    assert "quality.report" in trace.logical_steps
