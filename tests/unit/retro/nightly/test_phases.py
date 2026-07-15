from __future__ import annotations

from pathlib import Path

from assurance_agent.retro.nightly.phase_a import enumerate_candidates, has_required_evidence
from assurance_agent.retro.nightly.phase_d import (
    build_review_queue_markdown,
    partition_proposals_for_review,
)
from assurance_agent.retro.nightly.phase_f import (
    classify_eval_gate,
    compare_suite_regression,
    should_auto_apply,
)
from assurance_agent.retro.types import RetroProposal, RetroPromoteRecord
from tests.unit.retro.archive_fixtures import make_archived_change


def test_has_required_evidence(tmp_path: Path) -> None:
    root = make_archived_change(tmp_path, "CH-1", failures=[])
    assert has_required_evidence(root) is True
    (root / "events.jsonl").unlink()
    assert has_required_evidence(root) is False


def test_enumerate_candidates_skips_consumed_and_non_terminal(tmp_path: Path) -> None:
    make_archived_change(tmp_path, "CH-A", failures=[])
    make_archived_change(tmp_path, "CH-B", failures=[])
    state = {"consumed_changes": {"CH-A": {"terminal": True}}}
    candidates, incomplete = enumerate_candidates(
        tmp_path, state, is_terminal=lambda root, cid: cid != "CH-B"
    )
    ids = [c.change_id for c in candidates]
    assert "CH-A" not in ids
    assert "CH-B" not in ids
    assert incomplete == []


def test_partition_forwards_for_review(tmp_path: Path) -> None:
    proposals = [
        RetroProposal(id="P-1", apply_kind="memory_append", body="x", eval_suite="s"),
        RetroProposal(id="P-2", apply_kind="memory_append", body="y", eval_suite="s"),
    ]
    promotions: list[RetroPromoteRecord] = []
    partition = partition_proposals_for_review(proposals, promotions, min_evidence=2, rework_alert=3)
    assert {p.id for p in partition.for_review} == {"P-1", "P-2"}
    md = build_review_queue_markdown("retro-1", partition)
    assert "P-1" in md and "retro-1" in md


def test_partition_flags_stuck_after_rework_alert() -> None:
    proposals = [RetroProposal(id="P-1", apply_kind="memory_append", body="x")]
    promotions = [
        RetroPromoteRecord(proposal_id="P-1", decision="needs_rework", decided_by="h", decided_at="t")
        for _ in range(3)
    ]
    partition = partition_proposals_for_review(proposals, promotions, min_evidence=1, rework_alert=3)
    assert "P-1" in partition.stuck_tags


def test_phase_f_regression_and_auto_apply() -> None:
    baseline = {"case_review_gate_pass_rate": 1.0}
    candidate = {"case_review_gate_pass_rate": 0.5}
    suite = {
        "thresholds": [{"metric": "case_review_gate_pass_rate", "gate": "hard", "op": "gte", "value": 0.99}]
    }
    regressed = compare_suite_regression(baseline, candidate, suite)
    assert regressed.regressed is True
    assert should_auto_apply(regressed, suite) is False

    ok = compare_suite_regression(baseline, {"case_review_gate_pass_rate": 1.0}, suite)
    assert ok.regressed is False
    assert should_auto_apply(ok, suite) is True

    missing = compare_suite_regression(baseline, {}, suite)
    assert missing.regressed is True
    assert should_auto_apply(missing, suite) is False

    observe_suite = {"thresholds": [{"metric": "x", "gate": "observe", "op": "gte", "value": 0.0}]}
    ok_observe = compare_suite_regression({}, {}, observe_suite)
    assert should_auto_apply(ok_observe, observe_suite) is False


def test_classify_eval_gate() -> None:
    assert classify_eval_gate({"verdict": "pass"}) == "pass"
    assert classify_eval_gate({"verdict": "fail"}) == "fail"
    assert classify_eval_gate({"verdict": "bogus"}) == "inconclusive"
    assert classify_eval_gate({}) == "inconclusive"
