"""Declarations that copy a committed attempt result into graph state."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.stategraph.ledger import NamedWrite, fill_artifact_ledger


def output_mapping(output: object) -> dict[str, object]:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, Mapping):
        return {str(name): value for name, value in output.items()}
    raise TypeError("attempt output must be a mapping")


def receipt_mapping(receipt: object) -> dict[str, str] | None:
    if receipt is None:
        return None
    if isinstance(receipt, ReceiptRef):
        return receipt.model_dump(mode="json")
    if isinstance(receipt, Mapping):
        return ReceiptRef.model_validate(receipt).model_dump(mode="json")
    return None


def publish_result(
    key: str,
    *,
    receipt: str | None = None,
    identity: tuple[str, ...] = (),
) -> Callable[[Mapping[str, object], object, object], dict[str, object]]:
    """Store a committed output under ``key`` after optional identity checks.

    ``identity`` names fields that must already match ``state`` when ``state`` has
    them (for example ``change_id`` or ``coverage_epoch``). ``receipt`` is the
    state key that receives the commit receipt, when the caller wants one.
    """
    if not key:
        raise ValueError("publish_result key must be nonempty")

    def publish(state: Mapping[str, object], output: object, receipt_value: object) -> dict[str, object]:
        payload = output_mapping(output)
        for field in identity:
            current = state.get(field)
            if current is not None and current != payload.get(field):
                raise ValueError(f"{field} does not match the committed result")
        update: dict[str, object] = {key: payload}
        if receipt is not None:
            update[receipt] = receipt_mapping(receipt_value)
        return update

    return publish


def bind_produced_artifacts(
    publish: Callable[..., object],
    *,
    namespace: str,
    writes: Sequence[NamedWrite],
) -> Callable[..., dict[str, object]]:
    """Fill ``artifact_ledger`` from sealed refs, then keep the rest of ``publish``.

    This wrapper is the only writer of ``artifact_ledger``. A publish function that
    returns that key is ignored. The ledger comes from ``committed``, the kernel's
    sealed write set.
    """
    if not writes:
        raise ValueError("writes must name at least one artifact")

    def wrapped(
        state: Mapping[str, object],
        output: object,
        receipt: object,
        *,
        committed: Sequence[ArtifactRef | Mapping[str, object]] = (),
    ) -> dict[str, object]:
        published = call_publish(publish, state, output, receipt, committed)
        update = {str(name): value for name, value in published.items() if name != "artifact_ledger"}
        produced = fill_artifact_ledger(namespace, writes, committed)
        if produced:
            update["artifact_ledger"] = produced
        return update

    return wrapped


def call_publish(
    publish: Callable[..., Any],
    state: Mapping[str, object],
    output: object,
    receipt: object,
    committed: Sequence[ArtifactRef | Mapping[str, object]],
) -> Mapping[str, object]:
    if _accepts_committed(publish):
        published = publish(state, output, receipt, committed=committed)
    else:
        published = publish(state, output, receipt)
    if not isinstance(published, Mapping):
        raise TypeError("publish must return a mapping")
    return published


def _accepts_committed(publish: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(publish).parameters
    except (TypeError, ValueError):
        return False
    if "committed" in parameters:
        return True
    return any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())


__all__ = [
    "bind_produced_artifacts",
    "call_publish",
    "output_mapping",
    "publish_result",
    "receipt_mapping",
]
