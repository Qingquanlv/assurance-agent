from __future__ import annotations

import pytest

from assurance_healing.effects.apply import HEAL_APPLY_KIND, HealApplyEffect
from graph_engine.effects.state import bind_effect_call
from healing_fixtures import (  # pyright: ignore[reportMissingImports]
    FaultingEffectState,
    _drive_apply_then_reconcile,
    heal_intent,
)


@pytest.mark.parametrize("cut", ("before_mutation", "after_mutation", "before_receipt"))
@pytest.mark.asyncio
async def test_heal_apply_crash_cuts_never_duplicate(cut: str) -> None:
    state = FaultingEffectState(cut=cut)
    handler = HealApplyEffect()
    context = bind_effect_call(
        state=state,
        effect_kind=HEAL_APPLY_KIND,
        settlement_key="c" * 64,
        fencing_token=3,
    )
    await _drive_apply_then_reconcile(handler, heal_intent(), context)
    assert state.external_mutation_count <= 1
