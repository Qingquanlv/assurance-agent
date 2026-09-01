from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, TypedDict

from pydantic import Field, ValidationError

from graph_engine.plugin_api import FrozenModel


CHECKPOINT_MARKERS_STATE_KEY = "assurance_checkpoint_markers"
MAX_ACTIVE_GENERATIONS = 8
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MARKER_PHASES = ("system_interrupt_issued", "system_interrupt_completed")


class CheckpointBridgeMarker(FrozenModel):
    kind: Literal["system_interrupt_issued", "system_interrupt_completed"]
    attempt_key: str = Field(pattern=_SHA256_PATTERN)
    generation: int = Field(ge=1)
    ordinal: int = Field(ge=0)
    envelope_digest: str = Field(pattern=_SHA256_PATTERN)


def _coerce_marker(item: CheckpointBridgeMarker | Mapping[str, object]) -> CheckpointBridgeMarker:
    if isinstance(item, CheckpointBridgeMarker):
        return item
    try:
        return CheckpointBridgeMarker.model_validate(item)
    except ValidationError as error:
        raise ValueError("checkpoint bridge marker is not canonical") from error


def _marker_sort_key(marker: CheckpointBridgeMarker) -> tuple[str, int, int]:
    return (marker.attempt_key, marker.generation, marker.ordinal)


def _validate_marker_batch(markers: Sequence[CheckpointBridgeMarker]) -> list[CheckpointBridgeMarker]:
    if len(markers) > MAX_ACTIVE_GENERATIONS:
        raise ValueError("checkpoint marker batch exceeds the active-generation bound")
    seen: set[tuple[str, int]] = set()
    for marker in markers:
        if marker.kind not in _MARKER_PHASES:
            raise ValueError("checkpoint marker phase is not canonical")
        identity = (marker.attempt_key, marker.generation)
        if identity in seen:
            raise ValueError("checkpoint marker generation is not unique")
        seen.add(identity)
    ordered = list(markers)
    if ordered != sorted(ordered, key=_marker_sort_key):
        raise ValueError("checkpoint marker batch is not deterministically ordered")
    return ordered


def replace_checkpoint_marker_batch(
    existing: Sequence[CheckpointBridgeMarker | Mapping[str, object]] | None,
    incoming: Sequence[CheckpointBridgeMarker | Mapping[str, object]] | None,
) -> list[CheckpointBridgeMarker]:
    del existing
    if incoming is None:
        return []
    return _validate_marker_batch([_coerce_marker(item) for item in incoming])


class CheckpointBridgeState(TypedDict, total=False):
    assurance_checkpoint_markers: Annotated[list[CheckpointBridgeMarker], replace_checkpoint_marker_batch]


def omit_checkpoint_bridge_fields(state: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in state.items() if key != CHECKPOINT_MARKERS_STATE_KEY}


__all__ = [
    "CHECKPOINT_MARKERS_STATE_KEY",
    "CheckpointBridgeMarker",
    "CheckpointBridgeState",
    "MAX_ACTIVE_GENERATIONS",
    "omit_checkpoint_bridge_fields",
    "replace_checkpoint_marker_batch",
]
