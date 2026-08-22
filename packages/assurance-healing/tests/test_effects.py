from __future__ import annotations

import pytest
from graph_engine import RegistryPorts
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import EffectPolicy

from assurance_healing.effects.allocation import HealingAllocationEffect
from assurance_healing.plugin import HealingPlugin
from healing_fixtures import (  # pyright: ignore[reportMissingImports]
    ALLOCATION_KEY,
    FakeAllocationStore,
    allocation_intent,
    imported_symbols,
)


def test_healing_plugin_has_no_product_hooks_import() -> None:
    assert "ProductHooks" not in imported_symbols("assurance_healing")


@pytest.mark.asyncio
async def test_allocation_apply_is_idempotent_and_reconcilable() -> None:
    handler = HealingAllocationEffect(store=FakeAllocationStore())
    intent = allocation_intent()
    first = await handler.apply(intent, ALLOCATION_KEY)
    second = await handler.apply(intent, ALLOCATION_KEY)
    reconciled = await handler.reconcile(intent, ALLOCATION_KEY)
    assert first == second
    assert reconciled.status == "applied"
    assert reconciled.receipt == first.receipt


def test_effect_policies_are_frozen_and_identity_bound() -> None:
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api="2.0"))
    policies = {item.kind: item.policy for item in contribution.effects}
    assert policies["assurance.healing.effect.allocation.v2"] == EffectPolicy(
        max_attempts=3,
        timeout_seconds=30.0,
        backoff_seconds=1.0,
    )
    assert policies["assurance.healing.effect.proposal-approved.v1"] == EffectPolicy(
        max_attempts=3,
        timeout_seconds=30.0,
        backoff_seconds=1.0,
    )
    assert policies["assurance.healing.effect.heal-apply.v2"] == EffectPolicy(
        max_attempts=5,
        timeout_seconds=120.0,
        backoff_seconds=2.0,
    )
    baseline = _policy_identity(policies)
    mutated = dict(policies)
    mutated["assurance.healing.effect.allocation.v2"] = EffectPolicy(
        max_attempts=4,
        timeout_seconds=30.0,
        backoff_seconds=1.0,
    )
    assert _policy_identity(mutated) != baseline
    mutated = dict(policies)
    mutated["assurance.healing.effect.proposal-approved.v1"] = EffectPolicy(
        max_attempts=3,
        timeout_seconds=31.0,
        backoff_seconds=1.0,
    )
    assert _policy_identity(mutated) != baseline
    mutated = dict(policies)
    mutated["assurance.healing.effect.heal-apply.v2"] = EffectPolicy(
        max_attempts=5,
        timeout_seconds=120.0,
        backoff_seconds=3.0,
    )
    assert _policy_identity(mutated) != baseline


def _policy_identity(policies: dict[str, EffectPolicy]) -> str:
    return canonical_digest(
        {kind: policy.model_dump(mode="json") for kind, policy in sorted(policies.items())}
    )
