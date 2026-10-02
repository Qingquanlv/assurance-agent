"""Construct canonical evidence for a quality-loop round."""

from __future__ import annotations

from assurance_intake.contracts.loop_history import (
    LoopFamily,
    LoopKind,
    LoopRoundHistoryV1,
    history_identity,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def build_loop_round_history(
    *,
    change_id: str,
    coverage_epoch: int,
    loop_kind: LoopKind,
    family: LoopFamily | None,
    round_index: int,
    outcome: str,
    review_input_digest: str,
    source_refs: tuple[EvidenceArtifactRefV1, ...],
) -> LoopRoundHistoryV1:
    ordered = tuple(sorted(source_refs, key=lambda item: (item.path, item.digest)))
    return LoopRoundHistoryV1(
        evidence_id=history_identity(
            change_id=change_id,
            coverage_epoch=coverage_epoch,
            loop_kind=loop_kind,
            family=family,
            round_index=round_index,
            outcome=outcome,
            review_input_digest=review_input_digest,
            source_refs=ordered,
        ),
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        loop_kind=loop_kind,
        family=family,
        round_index=round_index,
        outcome=outcome,
        review_input_digest=review_input_digest,
        source_refs=ordered,
    )
