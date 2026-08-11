"""QuarantineProjection fail-closed model (M3 Task 4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.quarantine import (
    DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
    QuarantineEntry,
    QuarantineProjection,
)
from assurance_agent.artifacts.registry import match_artifact


def _entry(**overrides: object) -> QuarantineEntry:
    payload: dict[str, object] = {
        "subject_kind": "property",
        "subject_key": "entities.dept.constraints.name_unique",
        "status": "active",
        "reason": "seed_replay_rate=0.5",
        "entered_at": "2026-08-05T10:00:00Z",
        "evidence_refs": ("discovery/counterexamples/CE-001/replay/attempt-0.json",),
        "release_requires": DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
        "consecutive_successes": 0,
    }
    payload.update(overrides)
    return QuarantineEntry.model_validate(payload)


def test_active_requires_at_least_one_replay_receipt_ref() -> None:
    with pytest.raises(ValidationError, match="evidence_refs"):
        _entry(evidence_refs=())


def test_released_requires_consecutive_success_policy() -> None:
    with pytest.raises(ValidationError, match="consecutive_successes"):
        _entry(
            status="released",
            consecutive_successes=DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES - 1,
            evidence_refs=("discovery/counterexamples/CE-001/replay/attempt-0.json",),
        )


def test_released_ok_when_policy_met() -> None:
    entry = _entry(
        status="released",
        consecutive_successes=DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
    )
    assert entry.status == "released"
    assert DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES == 2


def test_projection_orders_entries_deterministically() -> None:
    doc = QuarantineProjection(
        schema_version="1",
        change_id="CH-Q-001",
        entries=(
            _entry(subject_kind="journey", subject_key="z_journey"),
            _entry(subject_kind="journey", subject_key="a_journey"),
            _entry(
                subject_kind="property",
                subject_key="entities.dept.constraints.name_unique",
            ),
        ),
    )
    keys = [(e.subject_kind, e.subject_key) for e in doc.entries]
    assert keys == sorted(keys)


def test_registry_matches_inspect_quarantine_projection() -> None:
    spec = match_artifact("inspect/quarantine-projection.json")
    assert spec is not None
    assert spec.artifact_type == "quarantine_projection"
    assert spec.compat == "must_compat"
    assert spec.model is QuarantineProjection
