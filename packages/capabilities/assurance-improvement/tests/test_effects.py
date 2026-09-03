from __future__ import annotations

from pathlib import Path

import pytest
from graph_engine import RegistryPorts
from graph_engine.canonical import canonical_digest
from graph_engine.effects.state import EffectCallContext, MemoryEffectState, bind_effect_call
from graph_engine.plugin_api import EffectPolicy
from tests.phase4.conformance import execute_task

from assurance_improvement.effects.archive import ARCHIVE_KIND, ImprovementArchiveEffect
from assurance_improvement.effects.delivery import DELIVERY_KIND, ImprovementDeliveryEffect
from assurance_improvement.effects.promotion import PROMOTION_KIND, ImprovementPromotionEffect
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
    FaultingEffectState,
    _attempt_and_reconcile,
    HEX_A,
    archive_intent,
    as_object,
    delivery_intent,
    improvement_projection,
    json_value,
    promotion_intent,
)


def _context(kind: str, state: MemoryEffectState | FaultingEffectState | None = None) -> EffectCallContext:
    return bind_effect_call(
        state=state or MemoryEffectState(),
        effect_kind=kind,
        settlement_key="c" * 64,
        fencing_token=3,
    )


@pytest.mark.asyncio
async def test_delivery_effect_reconciles_after_mutation_before_receipt() -> None:
    state = FaultingEffectState(cut="after_mutation")
    handler = ImprovementDeliveryEffect()
    await _attempt_and_reconcile(handler, delivery_intent(), _context(DELIVERY_KIND, state))
    assert state.delivery_count == 1


@pytest.mark.asyncio
async def test_delivery_effect_retries_crash_before_mutation() -> None:
    state = FaultingEffectState(cut="before_mutation")
    handler = ImprovementDeliveryEffect()
    result = await _attempt_and_reconcile(handler, delivery_intent(), _context(DELIVERY_KIND, state))
    assert result.status == "applied"
    assert state.delivery_count == 1


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
    applied = await ImprovementDeliveryEffect().apply(
        outcome.effects[0],
        _context(DELIVERY_KIND),
    )
    assert applied.status == "applied"
    stored = as_object(as_object(applied.receipt)["memory_eval"])
    assert stored["outcome"] == "regressed"
    assert stored["eval_run_id"] == "eval-regressed"
    assert stored["outcome"] != "passed"


@pytest.mark.asyncio
async def test_delivery_effect_is_idempotent_and_reconcilable() -> None:
    handler = ImprovementDeliveryEffect()
    intent = delivery_intent()
    context = _context(DELIVERY_KIND)
    first = await handler.apply(intent, context)
    second = await handler.apply(intent, context)
    reconciled = await handler.reconcile(intent, context)
    assert first.status == "applied"
    assert second.status == "applied"
    assert first.receipt == second.receipt
    assert reconciled.status == "applied"
    assert reconciled.receipt == first.receipt


@pytest.mark.asyncio
async def test_delivery_commits_formula_business_key() -> None:
    state = MemoryEffectState()
    context = _context(DELIVERY_KIND, state)
    intent = delivery_intent()
    result = await ImprovementDeliveryEffect().apply(intent, context)
    assert result.status == "applied"
    assert as_object(result.receipt)["settlement_key"] == context.settlement_key


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

    promotion_handler = ImprovementPromotionEffect()
    archive_handler = ImprovementArchiveEffect()
    applied_promotion = await promotion_handler.apply(promotion, _context(PROMOTION_KIND))
    applied_archive = await archive_handler.apply(archive, _context(ARCHIVE_KIND))
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
    promotion_state = FaultingEffectState(cut="after_mutation")
    archive_state = FaultingEffectState(cut="after_mutation")
    await _attempt_and_reconcile(
        ImprovementPromotionEffect(),
        promotion_intent(),
        _context(PROMOTION_KIND, promotion_state),
    )
    await _attempt_and_reconcile(
        ImprovementArchiveEffect(),
        archive_intent(),
        _context(ARCHIVE_KIND, archive_state),
    )
    assert promotion_state.delivery_count == 1
    assert archive_state.delivery_count == 1


@pytest.mark.asyncio
async def test_promotion_commits_formula_business_key() -> None:
    state = MemoryEffectState()
    context = _context(PROMOTION_KIND, state)
    result = await ImprovementPromotionEffect().apply(promotion_intent(), context)
    assert result.status == "applied"
    assert as_object(result.receipt)["settlement_key"] == context.settlement_key


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
