"""Merge committed quality-loop history refs for graph state reducers."""

from __future__ import annotations

from graph_engine.stategraph.ledger import merge_refs_by_path


def merge_history_refs(left: object, right: object) -> list[dict[str, str]]:
    """Retain every committed review round across nested graph and retry updates."""
    merged: list[dict[str, str]] = []
    for batch in (left, right):
        if batch is None:
            continue
        if not isinstance(batch, (list, tuple)):
            raise TypeError("history refs must be a list")
        merged = merge_refs_by_path(merged, batch, on_conflict="error")
    return merged
