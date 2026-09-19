from __future__ import annotations

from pathlib import Path

from tests.product.obligation_loop_fixture import run_lockout_cycle


def test_lockout_fault_is_detected_by_the_same_verification(tmp_path: Path) -> None:
    good = run_lockout_cycle(tmp_path / "good", lockout_enabled=True)
    bad = run_lockout_cycle(tmp_path / "bad", lockout_enabled=False)

    assert good["assessment"].rows[0].verdict == "supported"
    assert good["public_status"] == "achieved"
    assert bad["assessment"].rows[0].verdict == "refuted"
    assert bad["public_status"] != "achieved"
    # The same test bytes produced both conclusions; only the subject differed.
    assert bad["experiment_receipt"]["test_source_digest"] == good["experiment_receipt"]["test_source_digest"]


def test_the_verdict_comes_from_a_collected_status_not_a_pass_label(tmp_path: Path) -> None:
    good = run_lockout_cycle(tmp_path / "good", lockout_enabled=True)
    bad = run_lockout_cycle(tmp_path / "bad", lockout_enabled=False)

    assert good["experiment_receipt"]["observed_status"] == 423
    assert bad["experiment_receipt"]["observed_status"] == 200
    assert good["bundle"].observations[0].predicate_passed is True
    assert bad["bundle"].observations[0].predicate_passed is False


def test_pass_without_lockout_observation_does_not_support(tmp_path: Path) -> None:
    result = run_lockout_cycle(tmp_path, lockout_enabled=True, omit_locked_observation=True)

    assert result["bundle"].observations == ()
    assert result["assessment"].rows[0].verdict == "inconclusive"
    assert "obligation_observation_missing" in result["assessment"].rows[0].gap_codes
    assert result["public_status"] != "achieved"
