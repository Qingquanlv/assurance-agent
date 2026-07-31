"""v3 graph event wire assertions（ResumeAnchor / event_schema_version）。"""

from __future__ import annotations

from collections.abc import Sequence


def invocation_ids_along_ns(checkpoint_ns: str) -> list[str]:
    parts = [part for part in checkpoint_ns.split("/") if part]
    return [parts[index] for index in range(0, len(parts), 2)]


def assert_event_schema_version_3(events: Sequence[dict[str, object]]) -> None:
    """Fresh packaged roots emit event schema version 5 with profile snapshots."""
    started = [event for event in events if event.get("type") == "graph_invocation_started"]
    assert started, "expected graph_invocation_started"
    assert started[0].get("event_schema_version") == 5


def assert_no_revision_resume_fields(events: Sequence[dict[str, object]]) -> None:
    """Ordinary non-revision resumes must remain pairless (no revision lineage fields)."""
    for event in events:
        if event.get("type") != "graph_resumed":
            continue
        assert event.get("revision_transition_id") is None
        assert event.get("revision_ordinal") is None
        assert event.get("revision_chain_length") is None


def assert_v3_interrupt_anchors(events: Sequence[dict[str, object]]) -> None:
    interrupted = [event for event in events if event.get("type") == "graph_interrupted"]
    assert interrupted, "expected graph_interrupted"
    for event in interrupted:
        anchor = event.get("anchor")
        assert isinstance(anchor, dict), "graph_interrupted must carry anchor on v3"
        assert anchor.get("invocation_id") == event.get("invocation_id")
        assert anchor.get("checkpoint_ns") == event.get("checkpoint_ns")
        assert anchor.get("interrupt_id") == event.get("interrupt_id")


def assert_v3_resume_anchor_chain(events: Sequence[dict[str, object]], interrupt_checkpoint_ns: str) -> None:
    resumed = [event for event in events if event.get("type") == "graph_resumed"]
    expected_invs = set(invocation_ids_along_ns(interrupt_checkpoint_ns))
    resumed_invs = {event.get("invocation_id") for event in resumed}
    assert expected_invs <= resumed_invs

    by_inv = {str(event["invocation_id"]): event for event in resumed if event.get("invocation_id")}
    ordered_invs = invocation_ids_along_ns(interrupt_checkpoint_ns)

    layer_ns = set()
    for index, inv_id in enumerate(ordered_invs):
        event = by_inv.get(inv_id)
        assert event is not None, f"missing graph_resumed for invocation {inv_id}"
        anchor = event.get("anchor")
        assert isinstance(anchor, dict), f"graph_resumed for {inv_id} must carry anchor on v3"
        assert anchor.get("invocation_id") == inv_id
        assert anchor.get("checkpoint_ns") == event.get("checkpoint_ns")
        assert isinstance(anchor.get("node_id"), str)
        layer_ns.add(event.get("checkpoint_ns"))
        if index == 0:
            assert event.get("parent_anchor_ref") is None
        else:
            assert event.get("parent_anchor_ref") is not None

    if len(ordered_invs) > 1:
        assert len(layer_ns) == len(ordered_invs), "v3 resume must not copy one checkpoint_ns to all layers"
