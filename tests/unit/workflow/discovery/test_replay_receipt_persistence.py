"""M3 Task 3: campaign confirm path persists immutable replay attempt receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.evidence.replay_telemetry import (
    DEFAULT_API_C3_MIN_RATE,
    api_adversarial_expansion_allowed,
    compute_seed_replay_rate,
    receipt_payload_digest,
)
from assurance_agent.verification.oracle import OracleObservation
from assurance_agent.verification.replay import AttemptResult
from assurance_agent.workflow.discovery import run_deterministic_api_campaign
from assurance_agent.workflow.discovery.replay_receipts import (
    load_replay_attempt_receipts,
    write_replay_attempt_receipts,
)
from tests.integration.test_adversarial_phase1_api_slice import (
    BASE_REVISION,
    CHANGE_ID,
    SEED,
    _campaign_spec,
    _change_dir,
    _oracle_set,
    _hard_denied_500,
    _product_root,
    _runner_hard_violate,
    _strategy_snapshot,
)


def test_campaign_writes_one_receipt_per_replay_attempt(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    replay_n = 3
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        runner=_runner_hard_violate(replay_n=replay_n),
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-receipt",
        observed_at="2026-08-05T00:00:00Z",
        ingest=False,
    )
    assert outcome.counterexample_ids
    ce_id = outcome.counterexample_ids[0]
    receipt_dir = change_dir / "discovery" / "counterexamples" / ce_id / "replay"
    paths = sorted(receipt_dir.glob("attempt-*.json"))
    assert len(paths) == replay_n
    for index, path in enumerate(paths):
        assert path.name == f"attempt-{index}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        receipt = ReplayAttemptReceipt.model_validate(payload)
        assert receipt.counterexample_id == ce_id
        assert receipt.seed == SEED
        assert receipt.base_revision == BASE_REVISION
        assert receipt.attempt_index == index
        assert receipt.outcome == "violate"


def test_same_campaign_inputs_yield_identical_receipt_payload_digests(tmp_path: Path) -> None:
    digests: list[tuple[str, ...]] = []
    for label in ("a", "b"):
        change_dir = tmp_path / label / "qa" / "changes" / CHANGE_ID
        change_dir.mkdir(parents=True)
        product = _product_root(tmp_path / label)
        outcome = run_deterministic_api_campaign(
            campaign_spec=_campaign_spec(),
            oracle_set=_oracle_set(_hard_denied_500()),
            strategy_snapshot=_strategy_snapshot(),
            change_dir=change_dir,
            project_root=product,
            runner=_runner_hard_violate(replay_n=3),
            seed=SEED,
            base_revision=BASE_REVISION,
            model_available=False,
            temp_factory=lambda label=label: tmp_path / label / "ws",
            observed_at="2026-08-05T00:00:00Z",
            ingest=False,
        )
        receipts = load_replay_attempt_receipts(change_dir, outcome.counterexample_ids[0])
        digests.append(tuple(receipt_payload_digest(r) for r in receipts))
    assert digests[0] == digests[1]
    assert len(digests[0]) == 3


def test_loaded_receipts_drive_rate_and_expansion_gate(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        runner=_runner_hard_violate(replay_n=3),
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-gate",
        observed_at="2026-08-05T00:00:00Z",
        ingest=False,
    )
    receipts = load_replay_attempt_receipts(change_dir, outcome.counterexample_ids[0])
    success, attempts, rate = compute_seed_replay_rate(receipts)
    assert (success, attempts, rate) == (3, 3, 1.0)
    assert api_adversarial_expansion_allowed(rate, DEFAULT_API_C3_MIN_RATE) is True


def test_no_receipts_means_not_evaluated_and_blocks_expansion(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    receipts = load_replay_attempt_receipts(change_dir, "CE-missing")
    success, attempts, rate = compute_seed_replay_rate(receipts)
    assert success == 0 and attempts == 0 and rate is None
    assert api_adversarial_expansion_allowed(rate, DEFAULT_API_C3_MIN_RATE) is False


def test_existing_replay_receipt_cannot_be_rewritten_with_different_bytes(
    tmp_path: Path,
) -> None:
    change_dir = _change_dir(tmp_path)
    original = ReplayAttemptReceipt(
        schema_version="1",
        counterexample_id="CE-IMMUTABLE",
        seed=SEED,
        attempt_index=0,
        base_revision=BASE_REVISION,
        oracle_set_digest="sha256:" + ("a" * 64),
        outcome="violate",
        observed_digest="sha256:" + ("b" * 64),
    )
    changed = original.model_copy(update={"outcome": "hold"})
    (path,) = write_replay_attempt_receipts(change_dir, (original,))
    before = path.read_bytes()

    with pytest.raises(ValueError, match="immutable replay receipt"):
        write_replay_attempt_receipts(change_dir, (changed,))

    assert path.read_bytes() == before


def test_corrupt_replay_receipt_fails_closed_instead_of_being_skipped(
    tmp_path: Path,
) -> None:
    change_dir = _change_dir(tmp_path)
    replay_dir = change_dir / "discovery" / "counterexamples" / "CE-CORRUPT" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "attempt-0.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="replay receipt"):
        load_replay_attempt_receipts(change_dir)


def test_partial_reproduce_blocks_expansion(tmp_path: Path) -> None:
    """Below-threshold rate must not allow e2e/fuzz adversarial expansion."""
    from tests.integration.test_adversarial_phase1_api_slice import FakeCampaignRunner, _violate_obs

    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    runner = FakeCampaignRunner(
        selection=[_violate_obs()],
        replay_results=[
            AttemptResult(observation=OracleObservation(status_code=500)),
            AttemptResult(observation=OracleObservation(status_code=200)),
            AttemptResult(observation=OracleObservation(status_code=500)),
        ],
    )
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        runner=runner,
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-partial",
        observed_at="2026-08-05T00:00:00Z",
        ingest=False,
    )
    # Partial reproduce → needs_review CE may still exist; receipts must be written.
    assert outcome.counterexample_ids
    receipts = load_replay_attempt_receipts(change_dir, outcome.counterexample_ids[0])
    success, attempts, rate = compute_seed_replay_rate(receipts)
    assert attempts == 3
    assert success == 2
    assert rate is not None and rate < DEFAULT_API_C3_MIN_RATE
    assert api_adversarial_expansion_allowed(rate, DEFAULT_API_C3_MIN_RATE) is False
