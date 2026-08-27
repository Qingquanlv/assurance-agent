from __future__ import annotations

from pathlib import Path

import pytest
from graph_engine import RegistryPorts
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import EffectPolicy
from tests.phase4.conformance import assert_effect_idempotent, execute_task

from assurance_improvement.effects.archive import ImprovementArchiveEffect
from assurance_improvement.effects.delivery import ImprovementDeliveryEffect
from assurance_improvement.effects.promotion import ImprovementPromotionEffect
from assurance_improvement.effects.store import InMemoryImprovementStore
from assurance_improvement.operations.keys import (
    archive_effect_key,
    delivery_effect_key,
    promotion_effect_key,
)
from assurance_improvement.operations.delivery import EvaluateMemoryImprovementHandler
from assurance_improvement.plugin import ImprovementPlugin
from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    ARCHIVE_KEY,
    DELIVERY_KEY,
    PROMOTION_KEY,
    FaultingDeliveryStore,
    _attempt_and_reconcile,
    HEX_A,
    IMPROVEMENT_ID,
    archive_intent,
    as_object,
    delivery_intent,
    improvement_projection,
    json_value,
    promotion_intent,
)


@pytest.mark.asyncio
async def test_delivery_effect_reconciles_after_mutation_before_receipt() -> None:
    store = FaultingDeliveryStore(cut="after_mutation")
    handler = ImprovementDeliveryEffect(store=store)
    await _attempt_and_reconcile(handler, delivery_intent())
    assert store.delivery_count == 1


@pytest.mark.asyncio
async def test_delivery_effect_retries_crash_before_mutation() -> None:
    store = FaultingDeliveryStore(cut="before_mutation")
    handler = ImprovementDeliveryEffect(store=store)
    result = await _attempt_and_reconcile(handler, delivery_intent())
    assert result.status == "applied"
    assert store.delivery_count == 1


@pytest.mark.asyncio
async def test_regressed_eval_effect_does_not_store_passed(tmp_path: Path) -> None:
    outcome = await execute_task(
        EvaluateMemoryImprovementHandler(),
        json_value(
            {
                "projection": improvement_projection(),
                "eval_run_id": "eval-regressed",
                "outcome": "regressed",
                "report_sha256": "report-regressed",
                "staged_sha256": "staged-regressed",
                "target_digest": HEX_A,
            }
        ),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    assert as_object(outcome.output)["outcome"] == "regressed"
    applied = await ImprovementDeliveryEffect(store=InMemoryImprovementStore()).apply(
        outcome.effects[0],
        f"{IMPROVEMENT_ID}:1:memory_eval:{HEX_A}",
    )
    assert applied.status == "applied"
    stored = as_object(as_object(applied.receipt)["memory_eval"])
    assert stored["outcome"] == "regressed"
    assert stored["eval_run_id"] == "eval-regressed"
    assert stored["outcome"] != "passed"


@pytest.mark.asyncio
async def test_delivery_effect_is_idempotent_and_reconcilable() -> None:
    handler = ImprovementDeliveryEffect(store=InMemoryImprovementStore())
    await assert_effect_idempotent(handler, delivery_intent(), DELIVERY_KEY)


@pytest.mark.asyncio
async def test_delivery_rejects_well_typed_intent_with_non_formula_key() -> None:
    handler = ImprovementDeliveryEffect(store=InMemoryImprovementStore())
    result = await handler.apply(delivery_intent(), "not-a-derived-key")
    assert result.status == "permanent"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert result.failure.retryable is False


@pytest.mark.asyncio
async def test_promotion_and_archive_keys_match_exact_formulas() -> None:
    promotion = promotion_intent()
    archive = archive_intent()
    from assurance_improvement.contracts.effects import ImprovementEffectIntentV1

    assert promotion_effect_key(ImprovementEffectIntentV1.model_validate(promotion.payload)) == PROMOTION_KEY
    assert archive_effect_key(ImprovementEffectIntentV1.model_validate(archive.payload)) == ARCHIVE_KEY
    assert (
        delivery_effect_key(ImprovementEffectIntentV1.model_validate(delivery_intent().payload))
        == DELIVERY_KEY
    )

    promotion_handler = ImprovementPromotionEffect(store=InMemoryImprovementStore())
    archive_handler = ImprovementArchiveEffect(store=InMemoryImprovementStore())
    applied_promotion = await promotion_handler.apply(promotion, PROMOTION_KEY)
    applied_archive = await archive_handler.apply(archive, ARCHIVE_KEY)
    assert applied_promotion.status == "applied"
    assert applied_archive.status == "applied"
    assert as_object(applied_promotion.receipt)["kind"] == "test_promotion"
    promotion_body = as_object(as_object(applied_promotion.receipt)["promotion"])
    assert promotion_body["applied_at"] == "2026-08-21T12:00:00Z"
    assert as_object(promotion_body["write_set"][0])["path"] == "tests/api/test_example.py"
    assert as_object(applied_archive.receipt)["kind"] == "archive"
    assert as_object(applied_archive.receipt)["archive"]["invocation_id"] == "inv-archive-1"


@pytest.mark.asyncio
async def test_promotion_and_archive_reconcile_after_mutation() -> None:
    promotion_store = FaultingDeliveryStore(cut="after_mutation")
    archive_store = FaultingDeliveryStore(cut="after_mutation")
    await _attempt_and_reconcile(ImprovementPromotionEffect(store=promotion_store), promotion_intent())
    await _attempt_and_reconcile(ImprovementArchiveEffect(store=archive_store), archive_intent())
    assert promotion_store.delivery_count == 1
    assert archive_store.delivery_count == 1


@pytest.mark.asyncio
async def test_promotion_rejects_non_formula_key() -> None:
    result = await ImprovementPromotionEffect(store=InMemoryImprovementStore()).apply(
        promotion_intent(),
        "not-a-derived-key",
    )
    assert result.status == "permanent"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"


def test_effect_policies_are_frozen_and_identity_bound() -> None:
    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api="2.0"))
    policies = {item.kind: item.policy for item in contribution.effects}
    assert policies["assurance.improvement.effect.delivery.v1"] == EffectPolicy(
        max_attempts=5,
        timeout_seconds=120.0,
        backoff_seconds=2.0,
    )
    assert policies["assurance.improvement.effect.promotion.v1"] == EffectPolicy(
        max_attempts=3,
        timeout_seconds=60.0,
        backoff_seconds=2.0,
    )
    assert policies["assurance.improvement.effect.archive.v1"] == EffectPolicy(
        max_attempts=3,
        timeout_seconds=60.0,
        backoff_seconds=2.0,
    )
    baseline = _policy_identity(policies)
    mutated = dict(policies)
    mutated["assurance.improvement.effect.delivery.v1"] = EffectPolicy(
        max_attempts=6,
        timeout_seconds=120.0,
        backoff_seconds=2.0,
    )
    assert _policy_identity(mutated) != baseline
    mutated = dict(policies)
    mutated["assurance.improvement.effect.promotion.v1"] = EffectPolicy(
        max_attempts=3,
        timeout_seconds=61.0,
        backoff_seconds=2.0,
    )
    assert _policy_identity(mutated) != baseline
    mutated = dict(policies)
    mutated["assurance.improvement.effect.archive.v1"] = EffectPolicy(
        max_attempts=3,
        timeout_seconds=60.0,
        backoff_seconds=3.0,
    )
    assert _policy_identity(mutated) != baseline


def _policy_identity(policies: dict[str, EffectPolicy]) -> str:
    return canonical_digest(
        {kind: policy.model_dump(mode="json") for kind, policy in sorted(policies.items())}
    )
