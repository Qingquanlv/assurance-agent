"""M3 Task 3: immutable ReplayAttemptReceipt artifact model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.registry import match_artifact


def _receipt(**overrides: object) -> ReplayAttemptReceipt:
    base: dict[str, object] = {
        "schema_version": "1",
        "counterexample_id": "CE-001",
        "seed": 12345,
        "attempt_index": 0,
        "base_revision": "deadbeef",
        "oracle_set_digest": "sha256:" + ("a" * 64),
        "outcome": "violate",
        "observed_digest": "sha256:" + ("b" * 64),
        "observed_status": 500,
    }
    base.update(overrides)
    return ReplayAttemptReceipt(**base)  # type: ignore[arg-type]


def test_receipt_accepts_closed_outcomes() -> None:
    for outcome in (
        "violate",
        "hold",
        "inconclusive",
        "environment_failure",
        "divergence",
    ):
        receipt = _receipt(outcome=outcome)
        assert receipt.outcome == outcome
        assert receipt.schema_version == "1"


def test_receipt_rejects_unknown_outcome() -> None:
    with pytest.raises(ValidationError):
        _receipt(outcome="ok")


def test_receipt_attempt_index_non_negative() -> None:
    with pytest.raises(ValidationError):
        _receipt(attempt_index=-1)


def test_receipt_recorded_at_optional() -> None:
    bare = _receipt()
    assert bare.recorded_at is None
    stamped = _receipt(recorded_at="2026-08-05T00:00:00Z")
    assert stamped.recorded_at == "2026-08-05T00:00:00Z"


def test_replay_receipt_registry_pattern() -> None:
    path = "discovery/counterexamples/CE-001/replay/attempt-0.json"
    spec = match_artifact(path)
    assert spec is not None
    assert spec.artifact_type == "discovery_replay_attempt_receipt"
    assert spec.compat == "must_compat"
    assert spec.model is ReplayAttemptReceipt
    assert match_artifact("discovery/counterexamples/CE-001.yaml") is not None
    assert match_artifact("discovery/counterexamples/CE-001/replay/attempt-0.yaml") is None
