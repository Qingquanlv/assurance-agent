"""M3 Task 3: yield collector attaches honest C3 summary when receipts exist."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.workflow.metrics.adversarial_yield import collect_adversarial_yield
from tests.unit.workflow.metrics.test_collect_adversarial_yield import (
    CHANGE_ID,
    SEED,
    _confirmed_ce,
    _write_campaign,
)


def _write_receipt(change_dir: Path, *, ce_id: str, attempt_index: int, outcome: str) -> None:
    receipt = ReplayAttemptReceipt(
        schema_version="1",
        counterexample_id=ce_id,
        seed=SEED,
        attempt_index=attempt_index,
        base_revision="deadbeef",
        oracle_set_digest="sha256:" + ("a" * 64),
        outcome=outcome,  # type: ignore[arg-type]
        observed_digest="sha256:" + ("b" * 64),
        observed_status=500 if outcome == "violate" else 200,
    )
    path = change_dir / "discovery" / "counterexamples" / ce_id / "replay" / f"attempt-{attempt_index}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.model_dump(mode="json"), sort_keys=True) + "\n", encoding="utf-8")


def test_yield_without_receipts_leaves_replay_summary_unevaluated(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce_dir = change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True)
    (ce_dir / "CE-CONF-1.yaml").write_text(
        yaml.safe_dump(_confirmed_ce(problem_id="PROB-1")),
        encoding="utf-8",
    )
    _write_campaign(change_dir, counterexample_count=1, confirmed_count=1, sample_count=1)
    evidence = collect_adversarial_yield(change_dir=change_dir, change_id=CHANGE_ID)
    assert evidence.status == "evaluated"
    assert evidence.seed_replay_success is None
    assert evidence.seed_replay_attempts is None
    assert evidence.seed_replay_rate is None


def test_yield_with_receipts_attaches_rate_summary(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    ce_dir = change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True)
    (ce_dir / "CE-CONF-1.yaml").write_text(
        yaml.safe_dump(_confirmed_ce(problem_id="PROB-1")),
        encoding="utf-8",
    )
    _write_campaign(change_dir, counterexample_count=1, confirmed_count=1, sample_count=1)
    _write_receipt(change_dir, ce_id="CE-CONF-1", attempt_index=0, outcome="violate")
    _write_receipt(change_dir, ce_id="CE-CONF-1", attempt_index=1, outcome="violate")
    _write_receipt(change_dir, ce_id="CE-CONF-1", attempt_index=2, outcome="hold")
    evidence = collect_adversarial_yield(change_dir=change_dir, change_id=CHANGE_ID)
    assert evidence.status == "evaluated"
    assert evidence.seed_replay_success == 2
    assert evidence.seed_replay_attempts == 3
    assert evidence.seed_replay_rate is not None
    assert abs(evidence.seed_replay_rate - (2 / 3)) < 1e-9
