"""Discovery + coverage_gap Retro history readers — compact archived projection only."""

from __future__ import annotations

from assurance_agent.retro.discovery_history import (
    CompactCoverageGapRecord,
    CompactDiscoveryRecord,
    CoverageGapHistoryReader,
    DiscoveryHistoryReader,
    InMemoryCoverageGapHistoryReader,
    InMemoryDiscoveryHistoryReader,
)
from assurance_agent.retro.window import RetroWindowSelection


def _discovery_record(**overrides: object) -> CompactDiscoveryRecord:
    payload = {
        "change_id": "CH-1",
        "campaign_id": "camp-1",
        "counterexample_ids": ("CE-1", "CE-2"),
        "promotion_receipt_digests": ("sha256:promo-1",),
        "replay_success": 1,
        "replay_attempts": 2,
        "replay_rate": 0.5,
        "problem_escape_refs": ("PROB-ESC-1",),
        "promoted_count": 1,
        "total_counterexamples": 2,
        "sha256": "sha256:discovery-ch1",
    }
    payload.update(overrides)
    return CompactDiscoveryRecord.model_validate(payload)


def _gap_record(**overrides: object) -> CompactCoverageGapRecord:
    payload = {
        "change_id": "CH-1",
        "batch_id": "batch-1",
        "projection_digest": "sha256:proj-1",
        "document_digest": "sha256:gaps-1",
        "gap_identities": (
            {
                "kind": "uncovered_required_case",
                "case_id": "CASE-1",
                "constraint_key": "",
                "cell": "",
                "cluster_key": "",
            },
        ),
        "sha256": "sha256:gap-ch1",
    }
    payload.update(overrides)
    return CompactCoverageGapRecord.model_validate(payload)


def test_in_memory_discovery_reader_filters_by_change_ids() -> None:
    reader: DiscoveryHistoryReader = InMemoryDiscoveryHistoryReader(
        (
            _discovery_record(change_id="CH-1", campaign_id="camp-a"),
            _discovery_record(
                change_id="CH-2",
                campaign_id="camp-b",
                sha256="sha256:discovery-ch2",
                problem_escape_refs=(),
            ),
        )
    )
    selection = RetroWindowSelection(change_ids=("CH-1",), last=None)
    slice_ = reader.read_discovery_slice(selection)
    assert slice_.integrity.status == "complete"
    assert len(slice_.records) == 1
    assert slice_.records[0].campaign_id == "camp-a"
    assert slice_.records[0].counterexample_ids == ("CE-1", "CE-2")
    # Compact projection: IDs/digests/rates only — no CE body fields.
    assert not hasattr(slice_.records[0], "oracle_id")
    assert not hasattr(slice_.records[0], "seed")


def test_corrupt_discovery_record_is_typed_gap_for_discovery_only() -> None:
    reader = InMemoryDiscoveryHistoryReader(
        records=(_discovery_record(),),
        corrupt_change_ids=frozenset({"CH-1"}),
    )
    slice_ = reader.read_discovery_slice(RetroWindowSelection(change_ids=("CH-1",), last=None))
    assert slice_.integrity.status == "incomplete"
    assert any("discovery" in reason and "corrupt" in reason for reason in slice_.integrity.reasons)
    assert slice_.records == ()


def test_coverage_gap_reader_emits_reopened_events_from_diff() -> None:
    previous = _gap_record(
        batch_id="batch-0",
        document_digest="sha256:gaps-0",
        gap_identities=(),
        sha256="sha256:gap-prev",
    )
    current = _gap_record(
        gap_identities=(
            {
                "kind": "uncovered_required_case",
                "case_id": "CASE-1",
                "constraint_key": "",
                "cell": "",
                "cluster_key": "",
            },
        )
    )
    historically_closed = (
        (
            "uncovered_required_case",
            "CASE-1",
            "",
            "",
            "",
        ),
    )
    reader: CoverageGapHistoryReader = InMemoryCoverageGapHistoryReader(
        current_records=(current,),
        previous_by_change_id={"CH-1": previous},
        historically_closed_by_change_id={"CH-1": historically_closed},
    )
    slice_ = reader.read_coverage_gap_slice(RetroWindowSelection(change_ids=("CH-1",), last=None))
    assert slice_.integrity.status == "complete"
    assert len(slice_.records) == 1
    assert any(event.event_kind == "reopened" for event in slice_.events)
    reopened = next(event for event in slice_.events if event.event_kind == "reopened")
    assert reopened.kind == "uncovered_required_case"
    assert reopened.case_id == "CASE-1"


def test_missing_coverage_gap_projection_is_domain_gap_not_fake_rates() -> None:
    reader = InMemoryCoverageGapHistoryReader(current_records=(), missing_change_ids=frozenset({"CH-1"}))
    slice_ = reader.read_coverage_gap_slice(RetroWindowSelection(change_ids=("CH-1",), last=None))
    assert slice_.integrity.status == "incomplete"
    assert any("coverage_gap" in reason and "missing" in reason for reason in slice_.integrity.reasons)
    assert slice_.records == ()
    assert slice_.events == ()
