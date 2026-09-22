from __future__ import annotations

from pathlib import Path

from tests.product.obligation_loop_fixture import run_lockout_cycle


def test_unattested_http_subject_cannot_support_or_refute_an_obligation(tmp_path: Path) -> None:
    good = run_lockout_cycle(tmp_path / "good", lockout_enabled=True)
    bad = run_lockout_cycle(tmp_path / "bad", lockout_enabled=False)

    for result in (good, bad):
        row = result["assessment"].rows[0]
        assert row.verdict == "inconclusive"
        assert row.method_plan_refs
        assert "subject_identity_unavailable" in row.gap_codes
        assert result["gate_decision"] != "satisfied"
        assert result["bundle"].subject.status == "unavailable"
    # The observations still distinguish the implementations, but cannot become
    # a formal verdict until the remote subject is bound to the candidate.
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
    assert result["gate_decision"] != "satisfied"
