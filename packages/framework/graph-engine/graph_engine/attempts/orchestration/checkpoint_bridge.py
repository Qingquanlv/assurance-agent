from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from graph_engine.attempts.orchestration.checkpoint import ActiveSystemInterrupt, AttemptCheckpoint
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.persistence.attempt_checkpoint import AttemptCheckpointStore
from graph_engine.persistence.checkpoint_observer import CheckpointAnchorNotice
from graph_engine.persistence.journal import CheckpointAnchor
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeMarker


class AttemptCheckpointObserver:
    def __init__(self, checkpoints: AttemptCheckpointStore) -> None:
        self._checkpoints = checkpoints

    async def on_anchored(self, notice: CheckpointAnchorNotice) -> None:
        _validate_anchor(notice.anchor)
        if not notice.markers:
            return
        if notice.source == "pending_write":
            await self._anchor_issuance(
                notice,
                tuple(marker for marker in notice.markers if marker.kind == "system_interrupt_issued"),
            )
            return
        await self._anchor_issuance(
            notice,
            tuple(marker for marker in notice.markers if marker.kind == "system_interrupt_issued"),
        )
        await self._anchor_completion(
            notice,
            tuple(marker for marker in notice.markers if marker.kind == "system_interrupt_completed"),
        )

    async def _anchor_issuance(
        self,
        notice: CheckpointAnchorNotice,
        markers: Sequence[CheckpointBridgeMarker],
    ) -> None:
        for marker in markers:
            key, snapshot, issued = await self._require_issued(notice, marker)
            if issued.issuance_anchored:
                continue
            updated = replace(issued, issuance_checkpoint_id=notice.anchor.checkpoint_id)
            await self._replace_interrupt(snapshot, issued, updated, notice.delivery_fencing_token)

    async def _anchor_completion(
        self,
        notice: CheckpointAnchorNotice,
        markers: Sequence[CheckpointBridgeMarker],
    ) -> None:
        grouped: dict[str, list[CheckpointBridgeMarker]] = {}
        for marker in markers:
            grouped.setdefault(marker.attempt_key, []).append(marker)
        for attempt_digest, batch in grouped.items():
            key = AttemptKey(digest=attempt_digest)
            snapshot = await self._checkpoints.load(key)
            if snapshot is None:
                raise ValueError("attempt is missing for checkpoint marker")
            _assert_invocation(snapshot, notice.anchor)
            for marker in batch:
                issued = _find_issued(snapshot, marker)
                if issued is None:
                    raise ValueError("generation does not match issued interrupt")
                _assert_marker_identity(issued, marker)
                if issued.retired:
                    continue
                updated = replace(issued, completion_checkpoint_id=notice.anchor.checkpoint_id)
                snapshot = await self._replace_interrupt(
                    snapshot, issued, updated, notice.delivery_fencing_token
                )

    async def _replace_interrupt(
        self,
        snapshot: AttemptCheckpoint,
        issued: ActiveSystemInterrupt,
        updated: ActiveSystemInterrupt,
        token: int,
    ) -> AttemptCheckpoint:
        retained = tuple(
            updated if item.generation == issued.generation else item for item in snapshot.active_interrupts
        )
        active = tuple(item for item in retained if not item.retired)
        return await self._checkpoints.commit(
            replace(
                snapshot,
                fencing_token=token,
                active_interrupts=retained,
                active_interrupt=active[-1] if active else None,
                retired_generations=tuple(item.generation for item in retained if item.retired),
            ),
            expected_revision=snapshot.revision,
            fencing_token=token,
        )

    async def _require_issued(
        self,
        notice: CheckpointAnchorNotice,
        marker: CheckpointBridgeMarker,
    ) -> tuple[AttemptKey, AttemptCheckpoint, ActiveSystemInterrupt]:
        key = AttemptKey(digest=marker.attempt_key)
        snapshot = await self._checkpoints.load(key)
        if snapshot is None:
            raise ValueError("attempt is missing for checkpoint marker")
        _assert_invocation(snapshot, notice.anchor)
        issued = _find_issued(snapshot, marker)
        if issued is None:
            raise ValueError("generation does not match issued interrupt")
        _assert_marker_identity(issued, marker)
        return key, snapshot, issued


def _validate_anchor(anchor: CheckpointAnchor) -> None:
    if anchor.thread_id != anchor.invocation_id:
        raise ValueError("thread id must equal invocation id")


def _assert_invocation(snapshot: AttemptCheckpoint, anchor: CheckpointAnchor) -> None:
    if snapshot.invocation_id is not None and snapshot.invocation_id != anchor.invocation_id:
        raise ValueError("checkpoint invocation identity drifted")


def _find_issued(snapshot: AttemptCheckpoint, marker: CheckpointBridgeMarker) -> ActiveSystemInterrupt | None:
    for item in snapshot.active_interrupts:
        if item.generation == marker.generation:
            return item
    current = snapshot.active_interrupt
    if current is not None and current.generation == marker.generation:
        return current
    return None


def _assert_marker_identity(issued: ActiveSystemInterrupt, marker: CheckpointBridgeMarker) -> None:
    if issued.envelope_digest != marker.envelope_digest:
        raise ValueError("envelope digest drifted")
    if issued.ordinal != marker.ordinal:
        raise ValueError("ordinal drifted")


__all__ = ["AttemptCheckpointObserver"]
