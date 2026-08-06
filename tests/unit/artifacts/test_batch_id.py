from datetime import UTC, datetime

from assurance_agent.artifacts.batch_id import parse_batch_id


def test_batch_id_parser_accepts_legacy_and_nanosecond_formats() -> None:
    legacy = parse_batch_id("20260806-222614")
    precise = parse_batch_id("20260806-222614-768233000")

    assert legacy is not None
    assert legacy.timestamp == datetime(2026, 8, 6, 22, 26, 14, tzinfo=UTC)
    assert legacy.nanosecond == 0
    assert precise is not None
    assert precise.timestamp == datetime(2026, 8, 6, 22, 26, 14, tzinfo=UTC)
    assert precise.nanosecond == 768_233_000
    assert legacy < precise


def test_batch_id_parser_rejects_malformed_or_impossible_values() -> None:
    assert parse_batch_id("20260806-222614-1") is None
    assert parse_batch_id("20261306-222614-000000001") is None
    assert parse_batch_id("../20260806-222614") is None
