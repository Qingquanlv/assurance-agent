from __future__ import annotations

from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.retro.aggregator import build_retro_context, count_signals
from tests.unit.retro.archive_fixtures import make_archived_change


def test_build_context_golden_signals(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    make_archived_change(
        tmp_path,
        "CH-1",
        failures=[
            {"classification": "assertion"},
            {"classification": "assertion"},
            {"classification": "locator"},
        ],
        review_decision="pass",
        gate_pushbacks=2,
    )
    make_archived_change(
        tmp_path,
        "CH-2",
        failures=[{"classification": "environment"}],
        review_decision="reject",
        gate_pushbacks=0,
    )

    context = build_retro_context(tmp_path, changes=["CH-1", "CH-2"], retro_id="retro-test")

    assert context.retro_id == "retro-test"
    assert context.window.change_count == 2
    assert sorted(context.window.change_ids) == ["CH-1", "CH-2"]
    dist = {s.category: s.count for s in context.signals.failure_distribution}
    assert dist == {
        "assertion_failure": 2,
        "locator_failure": 1,
        "environment_failure": 1,
    }
    pushback = {s.gate: s.count for s in context.signals.gate_pushback}
    assert pushback.get("case-review") == 2
    assert context.signal_count == count_signals(context)
    assert context.signal_count > 0

    # Every failure signal must carry citable evidence_ids so aa-retro can file
    # proposals (regression: the Python migration dropped this field, which
    # structurally blocked all proposals).
    by_cat = {s.category: s for s in context.signals.failure_distribution}
    assertion = by_cat["assertion_failure"]
    assert assertion.evidence_ids == ["CH-1#F-1", "CH-1#F-2"]
    assert assertion.changes == ["CH-1"]
    env = by_cat["environment_failure"]
    assert env.evidence_ids == ["CH-2#F-1"]
    assert env.changes == ["CH-2"]


def test_build_context_reclassifications(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    make_archived_change(
        tmp_path,
        "CH-1",
        failures=[{"classification": "assertion"}],
        reclassifications=[
            {"failure": "F-1", "from": "environment_failure", "to": "assertion_failure"},
            {"failure": "F-2", "from": "locator_failure", "to": "assertion_failure"},
        ],
    )
    make_archived_change(
        tmp_path,
        "CH-2",
        failures=[{"classification": "environment"}],
    )

    context = build_retro_context(tmp_path, changes=["CH-1", "CH-2"], retro_id="retro-reclass")

    signals = context.signals.reclassifications
    assert len(signals) == 2
    first = signals[0]
    assert first.change_id == "CH-1"
    assert first.from_category == "environment_failure"
    assert first.to_category == "assertion_failure"
    assert first.evidence_ids == ["CH-1#seq2"]
    assert signals[1].evidence_ids == ["CH-1#seq3"]
    # reclassifications feed the shared signal counter (regression: never wired).
    assert context.signal_count == count_signals(context)


def test_build_context_no_changes_zero_signals(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "archive").mkdir(parents=True)
    context = build_retro_context(tmp_path, changes=[], retro_id="retro-empty")
    assert context.window.change_count == 0
    assert context.signal_count == 0
    assert count_signals(context) == 0


def test_build_context_since_scans_archive(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-A", failures=[{"classification": "assertion"}])
    make_archived_change(tmp_path, "CH-B", failures=[])
    context = build_retro_context(tmp_path, since="2000-01-01T00:00:00Z", retro_id="retro-scan")
    assert context.window.change_count == 2


def test_build_context_gate_pushback_from_gate_verdicts(tmp_path: Path) -> None:
    """Regression: gate_pushback must derive from `gate_verdict` events (the only
    verdict producer), not the never-emitted `gate_pushback` event type."""
    write_aa_config(tmp_path)
    make_archived_change(
        tmp_path,
        "CH-1",
        failures=[{"classification": "assertion"}],
        gate_verdicts=[
            {"gate": "case-review", "verdict": "needs_fix", "reason": "findings unresolved"},
            {"gate": "case-review", "verdict": "needs_fix", "reason": "findings unresolved"},
            {"gate": "case-review", "verdict": "pass"},
            {"gate": "execution-gate", "verdict": "stop", "evidence": {"reason": "invalid json"}},
        ],
    )
    make_archived_change(
        tmp_path,
        "CH-2",
        failures=[],
        gate_verdicts=[{"gate": "case-review", "verdict": "needs_fix"}],
    )

    context = build_retro_context(tmp_path, changes=["CH-1", "CH-2"], retro_id="retro-pushback")

    pushback = {(s.gate, s.verdict): s for s in context.signals.gate_pushback}
    assert set(pushback) == {("case-review", "needs_fix"), ("execution-gate", "stop")}
    case_review = pushback[("case-review", "needs_fix")]
    assert case_review.count == 3
    assert case_review.top_reasons == ["findings unresolved", "(no evidence)"]
    assert case_review.evidence_ids == ["CH-1#seq2", "CH-1#seq3", "CH-2#seq2"]
    stop = pushback[("execution-gate", "stop")]
    assert stop.count == 1
    assert stop.top_reasons == ["invalid json"]
    assert stop.evidence_ids == ["CH-1#seq5"]
    # pass verdicts are not pushback.
    assert all(s.verdict != "pass" for s in context.signals.gate_pushback)
    # sorted by count desc, then gate asc.
    assert [s.gate for s in context.signals.gate_pushback] == ["case-review", "execution-gate"]
    assert context.signal_count == count_signals(context)


def test_build_context_skill_execution_drift(tmp_path: Path) -> None:
    """Regression: skill_execution drift must derive from workflow-state
    `phases.*.skill_loaded == false`, not the never-emitted `skill_executed` event."""
    write_aa_config(tmp_path)
    make_archived_change(
        tmp_path,
        "CH-1",
        failures=[{"classification": "assertion"}],
        phases={
            "explore": {"status": "done", "skill_loaded": False},
            "execution": {"status": "PASS", "skill_loaded": "n/a"},
            "inspect": {"status": "done", "skill_loaded": True},
        },
    )
    make_archived_change(
        tmp_path,
        "CH-2",
        failures=[],
        phases={"explore": {"status": "done", "skill_loaded": False}},
    )

    context = build_retro_context(tmp_path, changes=["CH-1", "CH-2"], retro_id="retro-drift")

    signals = context.signals.skill_execution
    assert len(signals) == 1
    drift = signals[0]
    assert drift.phase == "explore"
    assert drift.count == 2
    assert drift.changes == ["CH-1", "CH-2"]
    assert drift.evidence_ids == ["CH-1#workflow-state:explore", "CH-2#workflow-state:explore"]
    assert context.signal_count == count_signals(context)


def test_build_context_no_pushback_or_drift_evidence(tmp_path: Path) -> None:
    """Without gate_verdict pushback or skill_loaded=false phases, both families
    stay empty — no fabricated signals."""
    write_aa_config(tmp_path)
    make_archived_change(
        tmp_path,
        "CH-1",
        failures=[{"classification": "assertion"}],
        gate_verdicts=[{"gate": "case-review", "verdict": "pass"}],
        phases={"explore": {"status": "done", "skill_loaded": True}},
    )

    context = build_retro_context(tmp_path, changes=["CH-1"], retro_id="retro-clean")

    assert context.signals.gate_pushback == []
    assert context.signals.skill_execution == []


def test_build_context_issue_lifecycle_signals(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    root = make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}])
    from tests.unit.retro.issue_fixtures import write_change_occurrence_detected, write_project_problem_lifecycle

    issue_evidence = write_change_occurrence_detected(root, change_id="CH-1")
    project_evidence = write_project_problem_lifecycle(tmp_path, change_id="CH-1")

    context = build_retro_context(tmp_path, changes=["CH-1"], retro_id="retro-issues")

    trends = context.signals.occurrence_trends
    assert len(trends) == 1
    assert trends[0].classification == "product_bug"
    assert trends[0].count == 1
    assert trends[0].evidence_ids == [issue_evidence]

    assert len(context.signals.issue_regressions) == 1
    assert context.signals.issue_regressions[0].evidence_ids == [project_evidence["regressed"]]

    assert any(
        signal.outcome == "resolved" and signal.evidence_ids == [project_evidence["resolved"]]
        for signal in context.signals.problem_resolutions
    )
    assert context.signals.not_an_issue_patterns[0].classification == "test_bug"
    assert project_evidence["not_an_issue"] in context.signals.not_an_issue_patterns[0].evidence_ids
    assert context.signal_count == count_signals(context)
