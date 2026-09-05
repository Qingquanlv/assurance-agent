"""Canonical evidence written for one local quality-loop round."""

from __future__ import annotations

from typing import Literal, Self, cast

from pydantic import Field, model_validator

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

LoopKind = Literal[
    "coverage",
    "case_review",
    "plan_review",
    "codegen_fix",
    "implementation_repair",
]
LoopFamily = Literal["api", "e2e", "fuzz", "performance"]


class LoopRoundHistoryV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    loop_kind: LoopKind
    family: LoopFamily | None = None
    round_index: int = Field(ge=0)
    outcome: str = Field(min_length=1)
    review_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        family_loop = self.loop_kind in {"plan_review", "codegen_fix"}
        if family_loop != (self.family is not None):
            raise ValueError("family is required only for family-specific generation loops")
        ordered = tuple(sorted(self.source_refs, key=lambda item: (item.path, item.digest)))
        if self.source_refs != ordered or len({item.path for item in self.source_refs}) != len(
            self.source_refs
        ):
            raise ValueError("source_refs must be sorted and unique by path")
        identity = _history_identity(
            change_id=self.change_id,
            coverage_epoch=self.coverage_epoch,
            loop_kind=self.loop_kind,
            family=self.family,
            round_index=self.round_index,
            outcome=self.outcome,
            review_input_digest=self.review_input_digest,
            source_refs=self.source_refs,
        )
        if self.evidence_id != identity:
            raise ValueError("evidence_id does not match loop-round content")
        return self


def _history_identity(
    *,
    change_id: str,
    coverage_epoch: int,
    loop_kind: LoopKind,
    family: LoopFamily | None,
    round_index: int,
    outcome: str,
    review_input_digest: str,
    source_refs: tuple[EvidenceArtifactRefV1, ...],
) -> str:
    payload = {
        "change_id": change_id,
        "coverage_epoch": coverage_epoch,
        "loop_kind": loop_kind,
        "family": family,
        "round_index": round_index,
        "outcome": outcome,
        "review_input_digest": review_input_digest,
        "source_refs": [item.model_dump(mode="json") for item in source_refs],
    }
    return f"loop-{canonical_digest(cast(JSONValue, payload))}"


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
        evidence_id=_history_identity(
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


__all__ = ["LoopFamily", "LoopKind", "LoopRoundHistoryV1", "build_loop_round_history"]
