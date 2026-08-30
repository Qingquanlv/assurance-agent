from __future__ import annotations

import pytest

from assurance_healing.effects.apply import HealApplyEffect
from healing_fixtures import (  # pyright: ignore[reportMissingImports]
    FaultingHealStore,
    _drive_apply_then_reconcile,
    heal_intent,
)


@pytest.mark.parametrize("cut", ("before_mutation", "after_mutation", "before_receipt"))
@pytest.mark.asyncio
async def test_heal_apply_crash_cuts_never_duplicate(cut: str) -> None:
    store = FaultingHealStore(cut=cut)
    handler = HealApplyEffect(store=store)
    await _drive_apply_then_reconcile(handler, heal_intent())
    assert store.external_mutation_count <= 1
