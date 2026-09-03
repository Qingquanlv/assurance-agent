from __future__ import annotations

from typing import Annotated, get_args, get_origin, get_type_hints

import pytest

from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    CheckpointBridgeMarker,
    CheckpointBridgeState,
    MAX_ACTIVE_GENERATIONS,
    omit_checkpoint_bridge_fields,
    replace_checkpoint_marker_batch,
)


def _marker(
    *,
    kind: str = "system_interrupt_completed",
    attempt_key: str = "a" * 64,
    generation: int = 1,
    ordinal: int = 0,
    envelope_digest: str = "b" * 64,
) -> CheckpointBridgeMarker:
    return CheckpointBridgeMarker(
        kind=kind,  # type: ignore[arg-type]
        attempt_key=attempt_key,
        generation=generation,
        ordinal=ordinal,
        envelope_digest=envelope_digest,
    )


def test_checkpoint_bridge_state_reserves_last_write_marker_channel() -> None:
    hints = get_type_hints(CheckpointBridgeState, include_extras=True)
    assert set(hints) == {CHECKPOINT_MARKERS_STATE_KEY}
    assert CHECKPOINT_MARKERS_STATE_KEY == "assurance_checkpoint_markers"
    annotation = hints[CHECKPOINT_MARKERS_STATE_KEY]
    assert get_origin(annotation) is Annotated
    listed, reducer = get_args(annotation)
    assert listed == list[CheckpointBridgeMarker]
    assert reducer is replace_checkpoint_marker_batch


def test_checkpoint_bridge_marker_validates_both_phases_and_canonical_fields() -> None:
    issued = _marker(kind="system_interrupt_issued", generation=1, ordinal=0)
    completed = _marker(kind="system_interrupt_completed", generation=2, ordinal=1)
    assert issued.kind == "system_interrupt_issued"
    assert completed.kind == "system_interrupt_completed"
    assert issued.attempt_key == "a" * 64
    assert issued.generation == 1
    assert issued.ordinal == 0
    assert issued.envelope_digest == "b" * 64

    with pytest.raises((TypeError, ValueError)):
        _marker(kind="human_interrupt")
    with pytest.raises((TypeError, ValueError)):
        _marker(attempt_key="not-a-digest")
    with pytest.raises((TypeError, ValueError)):
        _marker(envelope_digest="NOTHEX")
    with pytest.raises((TypeError, ValueError)):
        _marker(generation=0)
    with pytest.raises((TypeError, ValueError)):
        _marker(ordinal=-1)


def test_replace_checkpoint_marker_batch_replaces_history_and_orders_deterministically() -> None:
    prior = [_marker(generation=1, ordinal=0)]
    incoming = [
        _marker(generation=1, ordinal=0, envelope_digest="c" * 64),
        _marker(generation=2, ordinal=1, envelope_digest="d" * 64),
    ]
    replaced = replace_checkpoint_marker_batch(prior, incoming)
    assert replaced == incoming
    assert replaced is not prior
    assert [marker.generation for marker in replaced] == [1, 2]
    assert replace_checkpoint_marker_batch(prior, []) == []

    with pytest.raises((TypeError, ValueError)):
        replace_checkpoint_marker_batch(
            [],
            [
                _marker(generation=2, ordinal=1),
                _marker(generation=1, ordinal=0),
            ],
        )


def test_marker_batch_enforces_fixed_active_generation_bound() -> None:
    assert MAX_ACTIVE_GENERATIONS >= 2
    allowed = [_marker(generation=index, ordinal=index - 1) for index in range(1, MAX_ACTIVE_GENERATIONS + 1)]
    assert len(replace_checkpoint_marker_batch([], allowed)) == MAX_ACTIVE_GENERATIONS
    overflow = [*allowed, _marker(generation=MAX_ACTIVE_GENERATIONS + 1, ordinal=MAX_ACTIVE_GENERATIONS)]
    with pytest.raises((TypeError, ValueError)):
        replace_checkpoint_marker_batch([], overflow)
    with pytest.raises((TypeError, ValueError)):
        replace_checkpoint_marker_batch(
            [],
            [
                _marker(generation=1, ordinal=0),
                _marker(generation=1, ordinal=1),
            ],
        )


def test_public_adapter_view_never_exposes_reserved_marker_key() -> None:
    markers = [_marker()]
    state = {
        "change_id": "chg-1",
        CHECKPOINT_MARKERS_STATE_KEY: markers,
        "receipts": [{"receipt_id": "r1"}],
    }
    public = omit_checkpoint_bridge_fields(state)
    assert public == {"change_id": "chg-1", "receipts": [{"receipt_id": "r1"}]}
    assert CHECKPOINT_MARKERS_STATE_KEY not in public
    assert CHECKPOINT_MARKERS_STATE_KEY not in omit_checkpoint_bridge_fields(
        markers[0].model_dump(mode="json")
    )
