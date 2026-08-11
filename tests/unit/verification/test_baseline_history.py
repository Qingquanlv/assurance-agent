"""Project-level append-only performance baseline history (§5-A5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.verification.baseline_history import (
    BASELINE_HISTORY_DIR_REL,
    DEFAULT_BASELINE_WINDOW,
    PerformanceBaselineObservation,
    append_baseline_observation,
    recent_baseline_observations,
    receipt_digest,
    receipt_path,
    scenario_identity,
)


def _obs(
    *,
    capability: str = "login",
    endpoint: str = "POST /api/login",
    change_id: str = "CH-1",
    batch_id: str = "b1",
    p95_ms: float = 10.0,
    error_rate: float = 0.0,
    source_digest: str = "a" * 64,
) -> PerformanceBaselineObservation:
    return PerformanceBaselineObservation(
        capability=capability,
        endpoint=endpoint,
        change_id=change_id,
        batch_id=batch_id,
        p95_ms=p95_ms,
        error_rate=error_rate,
        source_digest=source_digest,
    )


def test_history_lives_under_project_aa_and_is_unregistered(tmp_path: Path) -> None:
    written = append_baseline_observation(tmp_path, _obs())
    assert written.is_relative_to(tmp_path / BASELINE_HISTORY_DIR_REL)
    assert BASELINE_HISTORY_DIR_REL == ".aa/baseline/performance"
    rel = written.relative_to(tmp_path).as_posix()
    assert match_artifact(rel) is None
    assert "qa/changes" not in rel


def test_append_is_immutable_and_does_not_overwrite_history(tmp_path: Path) -> None:
    first = append_baseline_observation(tmp_path, _obs(batch_id="b1", p95_ms=10.0))
    original = first.read_bytes()

    # Same identity+batch is idempotent (same payload) — no rewrite of bytes.
    again = append_baseline_observation(tmp_path, _obs(batch_id="b1", p95_ms=10.0))
    assert again == first
    assert first.read_bytes() == original

    # Conflicting payload for the same receipt identity must fail closed.
    with pytest.raises(FileExistsError):
        append_baseline_observation(tmp_path, _obs(batch_id="b1", p95_ms=99.0))
    assert first.read_bytes() == original

    # A new batch appends a second receipt; the first remains untouched.
    second = append_baseline_observation(tmp_path, _obs(batch_id="b2", p95_ms=12.0))
    assert second != first
    assert first.read_bytes() == original
    assert second.is_file()


def test_recent_n_is_per_scenario_identity_newest_first(tmp_path: Path) -> None:
    assert DEFAULT_BASELINE_WINDOW >= 3
    for i in range(1, DEFAULT_BASELINE_WINDOW + 2):
        append_baseline_observation(
            tmp_path,
            _obs(batch_id=f"b{i}", p95_ms=float(i), source_digest=f"{i:064d}"),
        )
    # Different scenario stays isolated.
    append_baseline_observation(
        tmp_path,
        _obs(
            capability="other",
            endpoint="GET /other",
            batch_id="b99",
            p95_ms=999.0,
            source_digest="b" * 64,
        ),
    )

    identity = scenario_identity("login", "POST /api/login")
    recent = recent_baseline_observations(
        tmp_path,
        identity=identity,
        limit=DEFAULT_BASELINE_WINDOW,
    )
    assert len(recent) == DEFAULT_BASELINE_WINDOW
    assert [row.batch_id for row in recent] == [f"b{i}" for i in range(DEFAULT_BASELINE_WINDOW + 1, 1, -1)]
    assert all(row.capability == "login" for row in recent)
    assert recent[0].source_digest == f"{DEFAULT_BASELINE_WINDOW + 1:064d}"

    # history.jsonl is append-only: line count grows, never rewritten in place.
    history = tmp_path / BASELINE_HISTORY_DIR_REL / "history.jsonl"
    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == DEFAULT_BASELINE_WINDOW + 2
    # First line still points at b1.
    assert json.loads(lines[0])["batch_id"] == "b1"


def test_same_payload_heals_orphan_receipt_into_history_without_duplicating(tmp_path: Path) -> None:
    """Crash between receipt write and history append leaves an orphan receipt.

    Same-payload retry must index the receipt into history.jsonl once (no duplicate
    receipt rewrite, no duplicate history lines).
    """
    obs = _obs(batch_id="orphan-1", p95_ms=10.0)
    path = receipt_path(tmp_path, obs)
    payload = {
        "schema_version": "1",
        "capability": obs.capability,
        "endpoint": obs.endpoint,
        "change_id": obs.change_id,
        "batch_id": obs.batch_id,
        "p95_ms": obs.p95_ms,
        "error_rate": obs.error_rate,
        "source_digest": obs.source_digest,
        "scenario_identity": scenario_identity(obs.capability, obs.endpoint),
        "receipt_digest": receipt_digest(obs),
    }
    # Simulate crash-between-write: receipt on disk, history.jsonl missing the line.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(payload))
    history = tmp_path / BASELINE_HISTORY_DIR_REL / "history.jsonl"
    assert not history.is_file()

    again = append_baseline_observation(tmp_path, obs)
    assert again == path
    assert history.is_file()
    lines = [ln for ln in history.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["receipt_digest"] == receipt_digest(obs)

    # Second same-payload call must not duplicate the history line.
    append_baseline_observation(tmp_path, obs)
    lines_again = [ln for ln in history.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines_again) == 1
