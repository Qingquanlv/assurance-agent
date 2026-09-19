from __future__ import annotations

from tests.product.obligation_loop_fixture import run_lockout_cycle


def test_lockout_fault_is_detected_by_the_same_verification(tmp_path):
    good = run_lockout_cycle(tmp_path / "good", lockout_enabled=True)
    bad = run_lockout_cycle(tmp_path / "bad", lockout_enabled=False)
    assert good["assessment"].rows[0].verdict == "supported"
    assert bad["assessment"].rows[0].verdict == "refuted"
    assert bad["public_status"] != "achieved"
    assert bad["experiment_receipt"]["test_source_digest"] == good["experiment_receipt"]["test_source_digest"]


def test_pass_without_lockout_observation_does_not_support(tmp_path):
    result = run_lockout_cycle(tmp_path, lockout_enabled=True, omit_locked_observation=True)
    assert result["assessment"].rows[0].verdict == "inconclusive"
    assert result["public_status"] != "achieved"
