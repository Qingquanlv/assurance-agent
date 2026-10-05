from __future__ import annotations

from typing import Any, cast

import pytest

from graph_engine.stategraph.ledger import merge_refs_by_path


def test_merge_refs_by_path_overwrites_and_sorts_by_default() -> None:
    merged = merge_refs_by_path(
        [{"path": "b.json", "digest": "a" * 64}, {"path": "a.json", "digest": "b" * 64}],
        [{"path": "b.json", "digest": "c" * 64}],
    )

    assert merged == [
        {"path": "a.json", "digest": "b" * 64},
        {"path": "b.json", "digest": "c" * 64},
    ]


def test_merge_refs_by_path_rejects_a_different_digest_when_requested() -> None:
    existing = [{"path": "a.json", "digest": "a" * 64}]
    same = [{"path": "a.json", "digest": "a" * 64}, {"path": "b.json", "digest": "b" * 64}]

    assert merge_refs_by_path(existing, same, on_conflict="error") == [
        {"path": "a.json", "digest": "a" * 64},
        {"path": "b.json", "digest": "b" * 64},
    ]
    with pytest.raises(ValueError, match="conflicting artifact ref for a.json"):
        merge_refs_by_path(existing, [{"path": "a.json", "digest": "c" * 64}], on_conflict="error")
    with pytest.raises(ValueError, match="unknown artifact ref conflict policy"):
        merge_refs_by_path(existing, same, on_conflict=cast(Any, "keep"))
