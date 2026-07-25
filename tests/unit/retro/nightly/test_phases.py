from __future__ import annotations

from pathlib import Path

from tests.helpers_aa import write_aa_config

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
from assurance_agent.retro.types import RetroPromoteRecord
from tests.unit.retro.archive_fixtures import make_archived_change
from tests.unit.retro.proposal_fixtures import issue_proposal, knowledge_proposal, memory_proposal


def test_archived_evidence_includes_issues_dir(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "archive" / "CH-1"
    (change_dir / "issues").mkdir(parents=True)
    (change_dir / "issues" / "snapshot.json").write_text("{}", encoding="utf-8")

    assert has_required_evidence(change_dir, "archive") is True


def test_enumerate_candidates_marks_malformed_issue_jsonl_incomplete(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    root = make_archived_change(tmp_path, "CH-BAD", failures=[])
    (root / "issues").mkdir()
    (root / "issues" / "events.jsonl").write_text("{not json\n", encoding="utf-8")

    candidates, incomplete = enumerate_candidates(tmp_path, {}, is_terminal=lambda _d, _c: True)

    assert candidates == []
    assert incomplete == ["CH-BAD"]


def test_snapshot_unarchived_evidence_copies_issues_tree(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    active = tmp_path / "qa" / "changes" / "CH-1"
    active.mkdir(parents=True)
    issues = active / "issues"
    issues.mkdir()
    (issues / "events.jsonl").write_text("{}\n", encoding="utf-8")

    from assurance_agent.retro.nightly.phase_a import snapshot_unarchived_evidence

    dest = snapshot_unarchived_evidence(tmp_path, "retro-1", "CH-1")
    assert (dest / "issues" / "events.jsonl").is_file()


def test_has_required_evidence(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    root = make_archived_change(tmp_path, "CH-1", failures=[])
    assert has_required_evidence(root) is True
    (root / "events.jsonl").unlink()
    assert has_required_evidence(root) is False


def test_archived_evidence_needs_no_coordinator_files(tmp_path: Path) -> None:
    """`aa-archive` can never copy events.jsonl / workflow-state.yaml — both names are
    excluded from tree capture, so no write-set carries them. Demanding them from an
    archived snapshot rejects every archived change, which is where terminal evidence
    lives once the active change dir is cleaned."""
    change_dir = tmp_path / "qa" / "archive" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True)
    (change_dir / "inspect" / "failure-analysis.json").write_text("{}", encoding="utf-8")

    assert has_required_evidence(change_dir, "archive") is True
    assert has_required_evidence(change_dir, "unarchived") is False


def test_enumerate_candidates_skips_consumed_and_non_terminal(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
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


def test_enumerate_candidates_handles_preserved_change_dir_after_archive(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-ARCHIVED", failures=[])
    stray_active_copy = tmp_path / "qa" / "changes" / "CH-ARCHIVED"
    stray_active_copy.mkdir(parents=True)
    (stray_active_copy / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (stray_active_copy / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")

    candidates, incomplete = enumerate_candidates(tmp_path, {}, is_terminal=lambda root, cid: True)

    assert [c.change_id for c in candidates] == ["CH-ARCHIVED"]
    assert candidates[0].evidence_source == "archive"
    assert incomplete == []


def test_partition_forwards_for_review() -> None:
    proposals = [
        memory_proposal(
            id="P-1",
            evidence_ids=["CH-A#E1", "CH-B#E2"],
        ),
        memory_proposal(
            id="P-2",
            evidence_ids=["CH-A#E1", "CH-C#E3"],
        ),
    ]
    promotions: list[RetroPromoteRecord] = []
    partition = partition_proposals_for_review(proposals, promotions, min_evidence=2, rework_alert=3)
    assert {p.id for p in partition.for_review} == {"P-1", "P-2"}
    md = build_review_queue_markdown("retro-1", partition)
    assert "P-1" in md and "retro-1" in md


def test_partition_flags_stuck_after_rework_alert() -> None:
    proposals = [memory_proposal(id="P-1")]
    promotions = [
        RetroPromoteRecord(proposal_id="P-1", decision="needs_rework", decided_by="h", decided_at="t")
        for _ in range(3)
    ]
    partition = partition_proposals_for_review(proposals, promotions, min_evidence=1, rework_alert=3)
    assert "P-1" in partition.stuck_tags


def test_partition_moves_low_evidence_proposals_to_threshold_bucket() -> None:
    proposals = [
        memory_proposal(
            id="P-LOW",
            evidence_ids=["RET-user-management-20260722#FAIL-001"],
        ),
        memory_proposal(
            id="P-OK",
            evidence_ids=["CH-A#E1", "CH-B#E2"],
        ),
    ]
    partition = partition_proposals_for_review(proposals, [], min_evidence=2, rework_alert=3)
    assert [p.id for p in partition.below_evidence_threshold] == ["P-LOW"]
    assert [p.id for p in partition.for_review] == ["P-OK"]
    md = build_review_queue_markdown("retro-1", partition)
    assert "Below evidence threshold" in md
    assert "P-LOW" in md


def test_partition_routes_export_kinds_to_export_track() -> None:
    proposals = [
        memory_proposal(id="P-MEM"),
        issue_proposal(id="P-ISSUE"),
        knowledge_proposal(id="P-KNOW"),
    ]
    partition = partition_proposals_for_review(proposals, [], min_evidence=1, rework_alert=3)
    assert [p.id for p in partition.for_review] == ["P-MEM"]
    assert {p.id for p in partition.pr_only} == {"P-ISSUE", "P-KNOW"}
    md = build_review_queue_markdown("retro-1", partition)
    assert "Export track (issue/knowledge)" in md


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


def test_retro_contracts_read_issues_but_never_write_them() -> None:
    from pathlib import Path

    from assurance_agent.workflow.graph.contracts import load_execution_contracts

    catalog = load_execution_contracts(Path.cwd())
    collect = catalog.contracts["operation:retro-collect"]
    retro = catalog.contracts["skill:aa-retro"]
    reconcile = catalog.contracts["operation:reconcile-improvements"]
    issue_reads = "project:qa/issues/**"
    issue_writes = "project:qa/issues/events.jsonl"

    assert issue_reads in collect.reads
    # aa-retro is current-run context only; Issue history is collect's job.
    assert issue_reads not in retro.reads
    assert issue_writes not in collect.writes
    assert issue_writes not in collect.authorization_writes
    assert issue_writes not in retro.writes
    assert issue_writes not in retro.authorization_writes
    assert issue_writes not in reconcile.writes
    assert issue_writes not in reconcile.authorization_writes
