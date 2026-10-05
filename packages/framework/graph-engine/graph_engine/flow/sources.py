"""Values a flow input binding can name. No callables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True, slots=True)
class Const:
    value: object


@dataclass(frozen=True, slots=True)
class LoopRound:
    """Current round of one loop. Only valid inside that loop."""

    loop: object


@dataclass(frozen=True, slots=True)
class LoopTarget:
    """Advance one loop, then go to ``target`` when the budget allows it."""

    loop: object
    target: str


@dataclass(frozen=True, slots=True)
class GateAction:
    gate: object


@dataclass(frozen=True, slots=True)
class GateField:
    gate: object
    name: str


@dataclass(frozen=True, slots=True)
class LedgerRefs:
    """Refs stored under one declared ledger key."""

    key: str
    many: bool


@dataclass(frozen=True, slots=True)
class LedgerReceipt:
    """Receipt stored beside the refs of one declared ledger key."""

    key: str


InputSource = Union[str, Const, LoopRound, GateAction, GateField, LedgerRefs, LedgerReceipt]
Target = Union[str, LoopTarget]


def const(value: object) -> Const:
    """Bind an input field to a fixed value."""
    return Const(value)


def ledger(handle: object, *, many: bool) -> LedgerRefs:
    """Bind an input to the refs of one ledger handle.

    ``many=True`` passes the whole list. ``many=False`` passes the single ref.
    The key must be written upstream or declared on the flow's ``ledger_inputs``.
    """
    key = getattr(handle, "ledger_key", None)
    if not isinstance(key, str) or not key:
        raise TypeError("ledger handle must have a ledger_key")
    if not isinstance(many, bool):
        raise TypeError("ledger many must be a bool")
    return LedgerRefs(key=key, many=many)


def ledger_receipt(handle: object) -> LedgerReceipt:
    """Bind an input to the receipt stored with one ledger handle.

    An empty key follows ``ledger(handle, many=False)``: None when the target
    field allows None, otherwise an error. The key must be written upstream or
    declared on the flow's ``ledger_inputs``.
    """
    key = getattr(handle, "ledger_key", None)
    if not isinstance(key, str) or not key:
        raise TypeError("ledger handle must have a ledger_key")
    return LedgerReceipt(key=key)


__all__ = [
    "Const",
    "GateAction",
    "GateField",
    "InputSource",
    "LedgerReceipt",
    "LedgerRefs",
    "LoopRound",
    "LoopTarget",
    "Target",
    "const",
    "ledger",
    "ledger_receipt",
]
