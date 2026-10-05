"""Derive a retro run id from the window, source refs, and report receipt."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from graph_engine.canonical import JSONValue, canonical_digest

from assurance_improvement.contracts.retro import RetroSelectionSnapshot, RetroWindow
from assurance_intake.contracts import EvidenceArtifactRefV1


def _evidence_ref(item: object) -> EvidenceArtifactRefV1:
    if isinstance(item, EvidenceArtifactRefV1):
        return item
    if isinstance(item, BaseModel):
        return EvidenceArtifactRefV1.model_validate(item.model_dump(mode="json"))
    return EvidenceArtifactRefV1.model_validate(item)


@dataclass(frozen=True, slots=True)
class RetroIdentity:
    retro_id: str
    window: RetroWindow
    source_refs: tuple[EvidenceArtifactRefV1, ...]


def collect_retro_source_refs(
    *,
    source_refs: Sequence[object] = (),
    runtime_ref: object = None,
    report_refs: Sequence[object] = (),
    history_refs: Sequence[object] = (),
    issue_snapshot_ref: object = None,
    inspection_refs: Sequence[object] = (),
    preparation_refs: Sequence[object] = (),
    reviewed_refs: Sequence[object] = (),
) -> tuple[EvidenceArtifactRefV1, ...]:
    """Dedupe Retro sources by path, using the handwritten tail's conflict rule.

    Preparation refs and reviewed-case refs replace an earlier digest for the same
    path. Every other same-path digest conflict is an error. Invalid candidates
    are skipped, matching the tail.
    """
    by_path: dict[str, EvidenceArtifactRefV1] = {}
    for candidate in (
        *source_refs,
        runtime_ref,
        *report_refs,
        *history_refs,
        issue_snapshot_ref,
        *inspection_refs,
    ):
        _remember_retro_ref(by_path, candidate, replace=False)
    for candidate in (*preparation_refs, *reviewed_refs):
        _remember_retro_ref(by_path, candidate, replace=True)
    return tuple(by_path[path] for path in sorted(by_path))


def _remember_retro_ref(
    by_path: dict[str, EvidenceArtifactRefV1],
    candidate: object,
    *,
    replace: bool,
) -> None:
    if candidate is None:
        return
    try:
        ref = _evidence_ref(candidate)
    except (TypeError, ValueError):
        return
    existing = by_path.get(ref.path)
    if existing is not None and existing.digest != ref.digest and not replace:
        raise ValueError(f"conflicting Retro source digest for {ref.path}")
    by_path[ref.path] = ref


def prepare_retro_identity(
    *,
    change_id: str,
    window: RetroWindow | None,
    source_refs: Sequence[object],
    report_receipt_digest: str | None,
    retro_id: str | None = None,
) -> RetroIdentity:
    """Derive ``retro_id`` from the window, source refs, and report receipt digest.

    An explicit ``retro_id`` is kept. A missing window defaults to the current change.
    The retro flow entry and the handwritten tail both call this, so both paths
    produce the same id. The synthesize prepare hook runs after that id is already
    on the attempt input.
    """
    resolved = window or RetroWindow(
        selection=RetroSelectionSnapshot(
            mode="change_ids",
            requested_change_ids=(change_id,),
        ),
        change_ids=(change_id,),
    )
    refs = tuple(
        sorted(
            (_evidence_ref(item) for item in source_refs),
            key=lambda item: (item.path, item.digest),
        )
    )
    if retro_id is None:
        identity = {
            "window": resolved.model_dump(mode="json"),
            "source_refs": [item.model_dump(mode="json") for item in refs],
            "report_receipt_digest": report_receipt_digest,
        }
        retro_id = f"retro-{canonical_digest(cast(JSONValue, identity))}"
    return RetroIdentity(retro_id=retro_id, window=resolved, source_refs=refs)


__all__ = ["RetroIdentity", "collect_retro_source_refs", "prepare_retro_identity"]
