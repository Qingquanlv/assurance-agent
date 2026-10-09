"""Business activations derived from where a step is running."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from graph_engine.attempts.models.keys import BusinessActivation
from graph_engine.flow.control import control_table, read_round

_TRIGGER = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_LIMIT = 64


def activation_value(
    segments: tuple[str, ...],
    node: str,
    rounds: tuple[tuple[str, int], ...],
) -> str:
    """Mount path, node, and enclosing rounds. A digest keeps the trigger bound."""
    parts = [*segments, f"n-{node}"]
    parts.extend(f"l-{key}-{index}" for key, index in rounds)
    text = ".".join(parts)
    if len(text) <= _LIMIT and _TRIGGER.fullmatch(text):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_activation(
    segments: tuple[str, ...],
    node: str,
    loop_keys: tuple[str, ...],
) -> object:
    def activation(state: object) -> BusinessActivation:
        table = control_table(state if isinstance(state, Mapping) else {}, "loops")
        rounds = tuple((key, read_round(table, key)) for key in loop_keys)
        return BusinessActivation.for_trigger(activation_value(segments, node, rounds))

    activation.__name__ = f"activate_{node.replace('-', '_')}"
    return activation


__all__ = ["activation_value", "make_activation"]
