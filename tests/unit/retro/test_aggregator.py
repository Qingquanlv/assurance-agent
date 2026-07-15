from __future__ import annotations

from pathlib import Path

from assurance_agent.retro.aggregator import build_retro_context, count_signals
from tests.unit.retro.archive_fixtures import make_archived_change


def test_build_context_golden_signals(tmp_path: Path) -> None:
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


def test_build_context_no_changes_zero_signals(tmp_path: Path) -> None:
    (tmp_path / "qa" / "archive").mkdir(parents=True)
    context = build_retro_context(tmp_path, changes=[], retro_id="retro-empty")
    assert context.window.change_count == 0
    assert context.signal_count == 0
    assert count_signals(context) == 0


def test_build_context_since_scans_archive(tmp_path: Path) -> None:
    make_archived_change(tmp_path, "CH-A", failures=[{"classification": "assertion"}])
    make_archived_change(tmp_path, "CH-B", failures=[])
    context = build_retro_context(tmp_path, since="2000-01-01T00:00:00Z", retro_id="retro-scan")
    assert context.window.change_count == 2
