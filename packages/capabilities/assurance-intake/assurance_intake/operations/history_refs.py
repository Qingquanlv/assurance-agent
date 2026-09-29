"""Merge committed quality-loop history refs for graph state reducers."""

from __future__ import annotations

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def merge_history_refs(left: object, right: object) -> list[dict[str, str]]:
    """Retain every committed review round across nested graph and retry updates."""
    by_path: dict[str, EvidenceArtifactRefV1] = {}
    for batch in (left, right):
        if batch is None:
            continue
        if not isinstance(batch, (list, tuple)):
            raise TypeError("history refs must be a list")
        for item in batch:
            ref = EvidenceArtifactRefV1.model_validate(item)
            previous = by_path.get(ref.path)
            if previous is not None and previous.digest != ref.digest:
                raise ValueError(f"conflicting history ref for {ref.path}")
            by_path[ref.path] = ref
    return [by_path[path].model_dump(mode="json") for path in sorted(by_path)]
