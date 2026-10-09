from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from graph_engine.persistence.journal import CheckpointAnchor
from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    CheckpointBridgeMarker,
    replace_checkpoint_marker_batch,
)


NoticeSource = Literal["checkpoint", "pending_write"]


@dataclass(frozen=True, slots=True)
class CheckpointAnchorNotice:
    anchor: CheckpointAnchor
    markers: tuple[CheckpointBridgeMarker, ...]
    source: NoticeSource
    # Delivery is authorized by the current caller; the anchor retains its original fence.
    delivery_fencing_token: int


class CheckpointAnchorObserverPort(Protocol):
    async def on_anchored(self, notice: CheckpointAnchorNotice) -> None: ...


def extract_checkpoint_markers(payload: object) -> tuple[CheckpointBridgeMarker, ...]:
    nested = getattr(payload, "value", None)
    if nested is not None and nested is not payload:
        found = extract_checkpoint_markers(nested)
        if found:
            return found
    if isinstance(payload, Mapping):
        raw = payload.get(CHECKPOINT_MARKERS_STATE_KEY)
        if raw is None:
            return ()
        return tuple(replace_checkpoint_marker_batch(None, raw))
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        collected: list[CheckpointBridgeMarker] = []
        for item in payload:
            collected.extend(extract_checkpoint_markers(item))
        if collected:
            return tuple(collected)
        try:
            return tuple(replace_checkpoint_marker_batch(None, payload))
        except (TypeError, ValueError):
            return ()
    return ()


def issuance_markers(markers: Sequence[CheckpointBridgeMarker]) -> tuple[CheckpointBridgeMarker, ...]:
    return tuple(marker for marker in markers if marker.kind == "system_interrupt_issued")


def completion_markers(markers: Sequence[CheckpointBridgeMarker]) -> tuple[CheckpointBridgeMarker, ...]:
    return tuple(marker for marker in markers if marker.kind == "system_interrupt_completed")


__all__ = [
    "CheckpointAnchorNotice",
    "CheckpointAnchorObserverPort",
    "completion_markers",
    "extract_checkpoint_markers",
    "issuance_markers",
]
