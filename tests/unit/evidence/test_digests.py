"""Pinned projection digest / canonical JSON / TraceSourceRecorder contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes as artifacts_canonical_json_bytes
from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.trace import (
    TraceGap,
    TraceProjection,
    TraceRow,
    TraceSource,
)
from assurance_agent.evidence.digests import (
    TraceSourceConflictError,
    TraceSourceRecorder,
    canonical_json_bytes,
    projection_digest,
)
from assurance_agent.evidence.trace import canonical_json_bytes as trace_canonical_json_bytes

# Compact sorted JSON, ensure_ascii=True (non-ASCII escaped), no trailing newline.
_CANONICAL_GOLDEN = (
    b'{"a":"\\u4e2d\\u6587","targets":{"api":true,"e2e":false,"fuzz":false,'
    b'"performance":true},"ts":"2026-07-29T12:00:00+00:00","z":1}'
)

_PINNED_PROJECTION_DIGEST = "1a7dffe527a81c67aa1f418d01b5afac0a08822be4b33947bfaa31c8bd0a7547"


def _sample_payload() -> dict[str, object]:
    return {
        "z": 1,
        "a": "中文",
        "ts": datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        "targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=True),
    }


def _minimal_projection() -> TraceProjection:
    return TraceProjection(
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="B1",
        sources=(TraceSource(path="cases/a/case.yaml", exists=True, sha256="aa"),),
        rows=(
            TraceRow(
                case_id="API-1",
                module="m",
                case_type="API",
                automation_required=True,
                coverage_state="covered",
                covering_tests=(),
                presence_in_current_batch="executed",
            ),
        ),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )


def test_canonical_json_bytes_pins_legacy_trace_contract() -> None:
    raw = canonical_json_bytes(_sample_payload())
    assert raw == _CANONICAL_GOLDEN
    assert not raw.endswith(b"\n")
    assert b"\\u4e2d\\u6587" in raw
    assert b"\xe4\xb8\xad" not in raw  # not UTF-8 raw for 中


def test_trace_reexports_same_canonical_bytes() -> None:
    assert trace_canonical_json_bytes(_sample_payload()) == _CANONICAL_GOLDEN


def test_artifacts_canonical_differs_from_trace_digest_contract() -> None:
    """Guard against accidental substitution of artifacts.canonical."""
    # artifacts.canonical accepts JSON-ready values and uses ensure_ascii=False + trailing newline.
    json_ready = {
        "a": "中文",
        "targets": {"api": True, "e2e": False, "fuzz": False, "performance": True},
        "ts": "2026-07-29T12:00:00+00:00",
        "z": 1,
    }
    artifacts_bytes = artifacts_canonical_json_bytes(json_ready)
    assert artifacts_bytes != _CANONICAL_GOLDEN
    assert artifacts_bytes.endswith(b"\n")
    assert "中文".encode() in artifacts_bytes


def test_projection_digest_pins_existing_unprefixed_hex() -> None:
    digest = projection_digest(_minimal_projection())
    assert digest == _PINNED_PROJECTION_DIGEST
    assert digest == digest.lower()
    assert not digest.startswith("sha256:")


def test_source_recorder_idempotent_and_sorted() -> None:
    recorder = TraceSourceRecorder()
    first = TraceSource(path="b.yaml", exists=True, sha256="bb")
    second = TraceSource(path="a.yaml", exists=False, sha256=None)
    recorder.add(first)
    recorder.add(second)
    recorder.add(first)  # identical re-add is idempotent
    assert recorder.freeze() == (second, first)


def test_source_recorder_rejects_conflicting_facts() -> None:
    recorder = TraceSourceRecorder()
    recorder.add(TraceSource(path="a.yaml", exists=True, sha256="aa"))
    with pytest.raises(TraceSourceConflictError, match="a.yaml"):
        recorder.add(TraceSource(path="a.yaml", exists=True, sha256="bb"))


def test_projection_digest_stable_under_gap_order_irrelevant_to_payload() -> None:
    """Digest follows model_dump order of the projection as constructed."""
    base = _minimal_projection()
    with_gap = base.model_copy(
        update={
            "gaps": (TraceGap(code="manifest_missing", source="execution/execution-manifest.yaml"),),
            "integrity": "incomplete",
        }
    )
    assert projection_digest(with_gap) != projection_digest(base)
