from __future__ import annotations

from typing import Literal

from graph_engine.attempts.events import AttemptEffectState
from graph_engine.plugin_api import EffectIntent


EffectAction = Literal["apply", "reconcile", "done"]


def next_effect_action(state: AttemptEffectState | None) -> EffectAction:
    if state is None:
        return "apply"
    if state.receipt_digest is not None:
        return "done"
    if state.apply_digest is not None:
        return "reconcile"
    return "apply"


def intent_from_state(state: AttemptEffectState) -> EffectIntent:
    return EffectIntent(kind=state.kind, payload=state.payload)


__all__ = [
    "EffectAction",
    "intent_from_state",
    "next_effect_action",
]
