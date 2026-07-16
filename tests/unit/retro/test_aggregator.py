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
    assert first.evidence_ids == ["CH-1#seq1"]
    assert signals[1].evidence_ids == ["CH-1#seq2"]
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
